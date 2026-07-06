"""Recording primitives: mic stream, VAD-driven events, WAV writer, chime.

Higher-level turn orchestration lives in halo/turn.py.
"""

from __future__ import annotations

import os
import queue
import tempfile
import threading
import time
import wave
from collections import deque
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterator, Optional

import numpy as np
import sounddevice as sd
import torch
from silero_vad import VADIterator, load_silero_vad

from halo.config import (
    CHIME_DURATION_MS,
    CHIME_FREQ_HZ,
    DEBUG,
    HOT_MIC,
    MIC_NOISE_GATE_RMS,
    RECORDINGS_DIR,
    SAMPLE_RATE,
)
from halo.config import VAD_THRESHOLD as _VAD_THRESHOLD

# silero-vad v5 requires exactly 512 samples per chunk at 16 kHz (~32 ms).
VAD_CHUNK = 512
# Speech threshold now lives in config.VAD_THRESHOLD (shared with stt.py's
# internal VAD filter so recorder and transcriber never disagree). Re-exported
# here for callers that historically imported it from record.
VAD_THRESHOLD = _VAD_THRESHOLD
SPEECH_PAD_MS = 200

# RMS-energy fallback so a quiet mic that under-scores on silero still
# registers speech. If `ENERGY_ONSET_CHUNKS` consecutive chunks clear
# SPEECH_RMS_FLOOR we treat it as a speech onset; after speech, that many
# sub-gate chunks marks the end. ~32 ms per chunk. Both override-able; the
# hot-mic profile lowers the floor and shortens the onset run for snappier
# pickup (HALO_SPEECH_RMS_FLOOR / HALO_ENERGY_ONSET_CHUNKS).
SPEECH_RMS_FLOOR = float(
    os.getenv("HALO_SPEECH_RMS_FLOOR", "0.012" if HOT_MIC else "0.018")
)
ENERGY_ONSET_CHUNKS = int(
    os.getenv("HALO_ENERGY_ONSET_CHUNKS", "3" if HOT_MIC else "6")
)   # ~200 ms (or ~100 ms hot) of sustained energy = onset

# Tell the user "your mic is picking up audio" when RMS rises above this.
# Rate-limited print, purely diagnostic. Bumped from 0.01 to 0.05 so we
# only print on actual speech-level audio, not ambient room noise — the
# old floor was spamming "mic level: 0.013" hundreds of times per
# conversation. Real speech is typically 0.05-0.30.
MIC_RMS_FLOOR = 0.05
MIC_RMS_PRINT_INTERVAL_SEC = 0.4

# Throttle "audio status: input overflow" prints — they used to fire on
# every overflow, sometimes 4-5x per second under load. One per 5s is
# enough to know the mic pipeline is struggling without flooding logs.
_AUDIO_STATUS_THROTTLE_SEC = 5.0
_last_audio_status_print = 0.0

_vad_model = None


def _get_vad_model():
    global _vad_model
    if _vad_model is None:
        print("loading silero-vad model (first run downloads ~2 MB)...")
        _vad_model = load_silero_vad()
    return _vad_model


def preload_models() -> None:
    """Force VAD + Whisper models to load now instead of on the first turn.

    Loading inside the audio callback caused "input overflow" warnings
    and dropped the first ~200 ms of speech.
    """
    _get_vad_model()
    # Imported here to keep the audio module free of an STT hard-dep
    # for callers that only want recording primitives.
    from halo.stt import preload_model as _preload_stt
    _preload_stt()


class RecorderState:
    """Continuous mic recorder with silero-vad-driven speech/silence events.

    The mic stream pushes 32 ms chunks into the buffer and runs each chunk
    through silero-vad. When VAD reports "start", `speech_event` is set;
    when it reports "end" (after `min_silence_ms` of silence), `silence_event`
    is set. Callers wait on those events and `clear_*_event()` between waits.
    """

    def __init__(
        self,
        min_silence_ms: int,
        on_audio: Optional[Callable[[np.ndarray], None]] = None,
        suppress_tts_echo: bool = True,
    ) -> None:
        self.vad = VADIterator(
            _get_vad_model(),
            threshold=VAD_THRESHOLD,
            sampling_rate=SAMPLE_RATE,
            min_silence_duration_ms=min_silence_ms,
            speech_pad_ms=SPEECH_PAD_MS,
        )
        self._on_audio = on_audio
        self._suppress_tts_echo = suppress_tts_echo
        self._chunks: list[np.ndarray] = []
        self._lock = threading.Lock()
        self._speech_event = threading.Event()
        self._silence_event = threading.Event()
        self.had_speech = False
        # Mic-grounded onset provenance (read by turn._commit for the BACKGROUND
        # classifier): did silero VAD ever fire, or only the RMS-energy fallback?
        self.vad_fired = False
        self.energy_only_onset = False
        self.peak_onset_rms = 0.0
        self._last_rms_print = 0.0
        # Realtime audio path: the PortAudio callback ONLY enqueues raw
        # chunks; all inference (silero VAD) runs on a consumer thread.
        # Doing torch work inside the callback overran the 32 ms block
        # budget on a raw USB mic -> "input overflow" -> dropped speech.
        self._q: queue.Queue = queue.Queue(maxsize=512)
        self._stop = threading.Event()
        self._worker: Optional[threading.Thread] = None
        self._energy_run = 0   # consecutive energetic chunks (onset fallback)
        self._silence_run = 0  # consecutive quiet chunks after speech (end fallback)
        self._min_silence_chunks = max(1, int(min_silence_ms / 32))

    def _callback(self, indata: np.ndarray, frames: int, time_info, status) -> None:
        # Keep this as cheap as possible — it runs on the realtime audio
        # thread. Any heavy work here causes PortAudio input overflow.
        if status:
            global _last_audio_status_print
            now = time.monotonic()
            if now - _last_audio_status_print > _AUDIO_STATUS_THROTTLE_SEC:
                _last_audio_status_print = now
                print(f"audio status: {status}")
        try:
            self._q.put_nowait(indata[:, 0].copy())
        except queue.Full:
            pass  # consumer fell behind; drop a frame rather than block audio

    def _consume(self) -> None:
        """Drain the raw-audio queue and run gate + VAD off the realtime thread."""
        from halo.voice import is_speaking as _voice_is_speaking
        while not self._stop.is_set():
            try:
                chunk = self._q.get(timeout=0.2)
            except queue.Empty:
                continue
            if chunk is None:
                break
            # Don't transcribe / VAD Halo's own TTS bleeding into the mic,
            # except in explicit barge-in mode where we are listening only
            # for a tiny interrupt vocabulary while TTS is active.
            if self._suppress_tts_echo and _voice_is_speaking():
                continue

            with self._lock:
                self._chunks.append(chunk)
            if self._on_audio is not None:
                try:
                    self._on_audio(chunk)
                except Exception as exc:
                    print(f"  on_audio error: {exc}")

            chunk_float = chunk.astype(np.float32) / 32768.0
            rms = float(np.sqrt(np.mean(chunk_float ** 2)))
            now = time.monotonic()
            if rms > MIC_RMS_FLOOR and now - self._last_rms_print > MIC_RMS_PRINT_INTERVAL_SEC:
                self._last_rms_print = now
                print(f"  mic level: {rms:.3f}")
            self.peak_onset_rms = max(self.peak_onset_rms, rms)

            # Noise gate: below the floor = silence. Also drives the
            # RMS-energy END fallback once speech has started.
            if rms < MIC_NOISE_GATE_RMS:
                self._energy_run = 0
                if self.had_speech:
                    self._silence_run += 1
                    if self._silence_run >= self._min_silence_chunks:
                        self._speech_event.clear()
                        self._silence_event.set()
                continue

            # RMS-energy ONSET fallback for quiet mics silero under-scores.
            if rms >= SPEECH_RMS_FLOOR:
                self._energy_run += 1
                self._silence_run = 0
                if self._energy_run >= ENERGY_ONSET_CHUNKS and not self.had_speech:
                    self.had_speech = True
                    if not self.vad_fired:
                        self.energy_only_onset = True
                    self._silence_event.clear()
                    self._speech_event.set()
            else:
                self._energy_run = 0

            result = self.vad(torch.from_numpy(chunk_float), return_seconds=False)
            if result is None:
                continue
            if "start" in result:
                self.had_speech = True
                self.vad_fired = True
                self.energy_only_onset = False  # silero agreed -> real speech, not rumble
                self._silence_run = 0
                self._silence_event.clear()
                self._speech_event.set()
            if "end" in result:
                self._speech_event.clear()
                self._silence_event.set()

    def start_worker(self) -> None:
        self._stop.clear()
        self._worker = threading.Thread(target=self._consume, daemon=True, name="halo-rec-consume")
        self._worker.start()

    def stop_worker(self) -> None:
        self._stop.set()
        try:
            self._q.put_nowait(None)
        except queue.Full:
            pass
        if self._worker is not None:
            self._worker.join(timeout=1.5)
            self._worker = None

    def onset_signals(self) -> dict:
        """Snapshot of this turn's mic onset provenance for the classifier."""
        return {
            "vad_fired": self.vad_fired,
            "energy_only_onset": self.energy_only_onset,
            "peak_onset_rms": self.peak_onset_rms,
        }

    def snapshot(self) -> np.ndarray:
        with self._lock:
            if not self._chunks:
                return np.zeros(0, dtype=np.int16)
            return np.concatenate(self._chunks)

    def wait_for_speech(self, timeout: float) -> bool:
        return self._speech_event.wait(timeout=timeout)

    def wait_for_silence(self, timeout: float) -> bool:
        return self._silence_event.wait(timeout=timeout)

    def clear_speech_event(self) -> None:
        self._speech_event.clear()

    def clear_silence_event(self) -> None:
        self._silence_event.clear()


def _windowed_peak_rms_i16(audio: np.ndarray, window_sec: float = 0.1) -> float:
    """Loudest `window_sec` RMS window in int16 PCM audio (0..1). Windowed so
    a short real word scores high while steady room hiss averages out low."""
    if audio.size == 0:
        return 0.0
    f = audio.astype(np.float32) / 32768.0
    win = int(SAMPLE_RATE * window_sec) or 1
    peak = 0.0
    for i in range(0, f.size, win):
        seg = f[i : i + win]
        if seg.size:
            peak = max(peak, float(np.sqrt(np.mean(seg ** 2))))
    return peak


class GapCapture:
    """Rolling mic capture for the gaps BETWEEN turns.

    run_turn owns the mic only while a turn is open; everything said while
    Halo is routing, thinking, or finishing its reply used to land in dead
    air and was lost ("it doesn't capture all I talk"). The orchestrator
    starts a GapCapture when a turn commits and take()s the buffer as the
    seed for the next turn (run_turn(seed_audio=...)).

    - Keeps only the most recent `max_sec` seconds (ring buffer).
    - Drops chunks while Halo's TTS is speaking (echo would poison the seed).
    - take() returns audio ONLY if it contains speech-level energy
      (windowed peak RMS >= SPEECH_RMS_FLOOR) — seeding minutes of room tone
      would slow Whisper and invite hallucinations.
    - Fail-soft: if the input stream can't open (device busy/missing), it
      just captures nothing.

    feed() is separated from the stream callback so the buffering/gating
    logic is unit-testable without audio hardware (scripts/test_gap_capture.py).
    """

    def __init__(self, max_sec: float = 30.0) -> None:
        self._chunks: deque[np.ndarray] = deque(
            maxlen=max(1, int(max_sec * SAMPLE_RATE / VAD_CHUNK))
        )
        self._lock = threading.Lock()
        self._stream = None
        self._is_speaking: Callable[[], bool] | None = None

    def feed(self, chunk: np.ndarray) -> None:
        with self._lock:
            self._chunks.append(chunk)

    def _callback(self, indata: np.ndarray, frames: int, time_info, status) -> None:
        if self._is_speaking is not None and self._is_speaking():
            return  # Halo's own TTS — never seed our voice into the next turn
        self.feed(indata[:, 0].copy())

    def start(self) -> None:
        if self._stream is not None:
            return
        if self._is_speaking is None:
            from halo.voice import is_speaking
            self._is_speaking = is_speaking
        try:
            self._stream = sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=1,
                dtype="int16",
                blocksize=VAD_CHUNK,
                latency="high",
                callback=self._callback,
            )
            self._stream.start()
        except Exception as exc:
            print(f"  gap-capture unavailable ({exc})")
            self._stream = None

    def stop(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass

    def take(self) -> np.ndarray:
        """Return buffered audio if it contains speech-level energy, else an
        empty array. Always clears the buffer."""
        with self._lock:
            chunks = list(self._chunks)
            self._chunks.clear()
        if not chunks:
            return np.zeros(0, dtype=np.int16)
        audio = np.concatenate(chunks)
        if _windowed_peak_rms_i16(audio) < SPEECH_RMS_FLOOR:
            return np.zeros(0, dtype=np.int16)
        return audio


@contextmanager
def open_stream(state: RecorderState) -> Iterator[RecorderState]:
    # latency="high" gives PortAudio a larger host buffer so a raw USB mic
    # has slack and doesn't overflow. The callback is now trivial (enqueue
    # only); the consumer thread does the VAD work.
    state.start_worker()
    try:
        with sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="int16",
            blocksize=VAD_CHUNK,
            latency="high",
            callback=state._callback,
        ):
            yield state
    finally:
        state.stop_worker()


def save_wav(audio: np.ndarray) -> Path:
    """Write int16 PCM audio as a 16 kHz mono WAV.

    Path is timestamped in recordings/ when DEBUG, else a temp file.
    """
    if DEBUG:
        RECORDINGS_DIR.mkdir(exist_ok=True)
        path = RECORDINGS_DIR / f"{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.wav"
    else:
        fd, name = tempfile.mkstemp(suffix=".wav", prefix="halo-")
        os.close(fd)
        path = Path(name)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(audio.tobytes())
    return path


def play_chime(
    freq_hz: int = CHIME_FREQ_HZ,
    duration_ms: int = CHIME_DURATION_MS,
    volume: float = 0.3,
) -> None:
    """Play a short beep so the user knows recording started."""
    n = int(SAMPLE_RATE * duration_ms / 1000)
    t = np.linspace(0, duration_ms / 1000, n, endpoint=False)
    wave_arr = (volume * np.sin(2 * np.pi * freq_hz * t)).astype(np.float32)
    sd.play(wave_arr, samplerate=SAMPLE_RATE, blocking=True)


def play_backchannel() -> None:
    """Soft low tone played during long composing/thinking silences to
    reassure the user we're still listening (think "mm-hmm")."""
    from halo.config import (
        BACKCHANNEL_DURATION_MS,
        BACKCHANNEL_FREQ_HZ,
        BACKCHANNEL_VOLUME,
    )
    n = int(SAMPLE_RATE * BACKCHANNEL_DURATION_MS / 1000)
    t = np.linspace(0, BACKCHANNEL_DURATION_MS / 1000, n, endpoint=False)
    # Cosine fade so the tone doesn't click.
    fade = 0.5 * (1 - np.cos(2 * np.pi * np.arange(n) / n))
    wave_arr = (BACKCHANNEL_VOLUME * fade * np.sin(2 * np.pi * BACKCHANNEL_FREQ_HZ * t)).astype(np.float32)
    sd.play(wave_arr, samplerate=SAMPLE_RATE, blocking=False)
