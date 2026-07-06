"""Batch speech-to-text via faster-whisper + large-v3-turbo.

Moonshine streaming was fast but its transcription accuracy on the
user's voice/mic was unworkable ("Chrome" -> "Cut" / "card" / "crack").
faster-whisper batch decoding cuts WER in half at the cost of being
batch-only — but Stage 1 is rules-only now, so we don't actually need
partials during speech. The trade is worth it.

Default model is large-v3-turbo: near-distil speed but multilingual-trained,
which is measurably better on accented English than the English-only
distil-large-v3 we used before ("translation is poor" complaint). Override
with HALO_STT_MODEL; distil-large-v3 remains the automatic fallback.

Flow per turn:
  - BatchTranscriber buffers every audio chunk via .feed()
  - On silence, turn.py calls .transcribe() to get the full text
  - .reset() clears the buffer for the next turn
"""

from __future__ import annotations

import os
import site
import threading
from pathlib import Path
from typing import Optional

import numpy as np

from halo import bus
from halo.config import SAMPLE_RATE, VAD_THRESHOLD


def _add_nvidia_dll_dirs() -> None:
    """Make ctranslate2 find cuBLAS/cuDNN from the pip-installed nvidia wheels.

    Must run BEFORE `import faster_whisper`. We prepend to PATH because
    ctranslate2's native DLL uses Windows' default DLL search path for
    its transitive dependencies, which os.add_dll_directory doesn't reach.
    """
    extra: list[str] = []
    bases = list(site.getsitepackages())
    bases.append(site.getusersitepackages())
    for base in bases:
        for sub in ("nvidia/cublas/bin", "nvidia/cudnn/bin", "nvidia/cuda_nvrtc/bin"):
            path = Path(base) / sub
            if path.is_dir():
                extra.append(str(path))
                try:
                    os.add_dll_directory(str(path))
                except (AttributeError, OSError):
                    pass
    if extra:
        os.environ["PATH"] = os.pathsep.join(extra + [os.environ.get("PATH", "")])


_add_nvidia_dll_dirs()

# Late import so the PATH/DLL fix above takes effect first.
from faster_whisper import WhisperModel  # noqa: E402

# large-v3-turbo: 4-layer decoder like distil (~6x faster than full
# large-v3) but multilingual-trained — noticeably better on accented
# English. int8_float16 on RTX 3060 uses ~1.5 GB VRAM (fits alongside
# Ollama 1.5b + Windows). distil-large-v3 stays as the fallback if the
# requested model can't load. Override with HALO_STT_MODEL.
_MODEL_NAME = os.getenv("HALO_STT_MODEL", "").strip() or "large-v3-turbo"
_FALLBACK_MODEL = "distil-large-v3"

# Parameters for Whisper's INTERNAL silero VAD pass (vad_filter). Must track
# the recorder's threshold (config.VAD_THRESHOLD): we lowered the recorder to
# 0.25-0.35 because silero's stock 0.5 missed this mic's quiet speech entirely —
# but vad_filter's default is that same 0.5, so it silently DELETED quiet
# stretches of already-captured speech before decoding ("words go missing
# mid-sentence"). One threshold, applied in both places.
_VAD_FILTER_PARAMS = dict(
    threshold=VAD_THRESHOLD,
    min_speech_duration_ms=100,   # don't drop a clipped short word (default 250)
    min_silence_duration_ms=500,  # split on real pauses, not the 2s default
    speech_pad_ms=400,
)

# Vocabulary bias for command turns (accent accommodation). Nouns only —
# deliberately NO verbs like "dictate"/"stop" so we don't nudge Whisper into
# false dictation triggers/stops on near-silent turns.
_BIAS_PROMPT = (
    "Halo, Claude, Codex. Chrome, calculator, Paint, Notepad, Spotify, Word, "
    "Excel, Explorer, settings, terminal. GitHub, TypeScript, JavaScript, "
    "Python, React, Next.js."
)
_CUDA_COMPUTE = "int8_float16"
_CPU_COMPUTE = "int8"
_MIN_AUDIO_SEC = 0.2  # any shorter and Whisper will hallucinate

# Confidence gate. faster-whisper exposes per-segment acoustic stats; we
# use them to drop turns that are really mic noise / background TV / a cough
# that the model hallucinated words from (e.g. "no, what do you mean, it
# feels good" out of room tone, which then got dispatched as a task).
# Tuned conservatively — require BOTH a low word-confidence AND a high
# non-speech probability — so genuine quiet speech isn't dropped. Either
# signal alone has too many false positives. The dispatch-side task guard
# is the second line of defence for confidently-mis-transcribed noise.
_MIN_AVG_LOGPROB = -1.0      # below this, the model is unsure of the words
_MAX_NO_SPEECH_PROB = 0.6    # above this, the model thinks it's non-speech

# distil-large-v3 (like the full distil-whisper family) was fine-tuned
# on millions of YouTube auto-captions. Its highest-probability output
# on near-silence is the literal end-of-video credit roll: "thank you",
# "thanks for watching", "subscribe", etc. We see "Thank you." on every
# false-positive wake fire if we don't filter.
# Match is case-insensitive, after stripping punctuation/whitespace.
_HALLUCINATION_PHRASES = {
    "",
    ".", "...", "..", "?", "!", "?!", "??",
    "you", "you you", "you you you",
    "thank you", "thanks", "thanks for watching",
    "thank you for watching", "thanks for watching!",
    "thank you very much", "thank you so much",
    "bye", "bye bye", "goodbye", "see you",
    "okay", "ok", "uh", "um", "hmm", "mm", "mhm",
    "yeah", "yes", "no", "oh",
    "subscribe", "like and subscribe",
    # Greetings — Whisper invents these constantly on near-silence right
    # after a wake fire (the audio is mostly room noise + a faint trail
    # of "halo" which decodes as "hello"). Real commands from a user
    # right after wake are never bare greetings.
    "hello", "hi", "hi there", "hey", "hey there",
    "good morning", "good evening", "good night",
    "ah", "ahh", "huh", "what", "wait",
    "halo", "hi halo",  # echoes of the wake word itself
}


def _is_hallucination(text: str) -> bool:
    """True if `text` is one of distil-whisper's known silence-mode artifacts."""
    cleaned = text.strip().rstrip(".!?,").lower().strip()
    return cleaned in _HALLUCINATION_PHRASES


# Context flag set by the orchestrator: when Halo just asked the user a
# question (pending confirmation / clarification / an agent question), a bare
# "Yes." / "No." / "Okay." is a REAL answer, not a silence artifact — the
# blocklist above must not swallow it. The acoustic-confidence gate still runs.
_expect_answer = False


def set_expect_answer(on: bool) -> None:
    """Orchestrator hint: an answer to a direct question is expected on the
    next turn, so short decision words bypass the hallucination blocklist."""
    global _expect_answer
    _expect_answer = bool(on)


def _acoustic_stats(segments: list) -> tuple[float, float]:
    """(avg_logprob, no_speech_prob) duration-weighted across segments, so one
    short noisy segment can't swing the verdict. Falls back to a plain mean
    when there's no usable per-segment timing (some decode configs zero
    start/end)."""
    total = sum(max(0.0, s.end - s.start) for s in segments)
    if total > 0.0:
        logprob = sum(
            getattr(s, "avg_logprob", 0.0) * max(0.0, s.end - s.start) for s in segments
        ) / total
        nospeech = sum(
            getattr(s, "no_speech_prob", 0.0) * max(0.0, s.end - s.start) for s in segments
        ) / total
    else:
        n = len(segments)
        logprob = sum(getattr(s, "avg_logprob", 0.0) for s in segments) / n
        nospeech = sum(getattr(s, "no_speech_prob", 0.0) for s in segments) / n
    return logprob, nospeech


def _passes_confidence(segments: list, text: str) -> bool:
    """False when the model's own acoustic stats say this was probably
    non-speech. Returns True when there's no usable evidence (don't
    second-guess the model without it)."""
    logprob, nospeech = _acoustic_stats(segments)
    if logprob < _MIN_AVG_LOGPROB and nospeech > _MAX_NO_SPEECH_PROB:
        print(f"  [stt] rejected low-confidence (logprob {logprob:.2f}, "
              f"no_speech {nospeech:.2f}): {text!r}")
        bus.emit(
            "stt.rejected", reason="low_confidence", text=text,
            logprob=round(logprob, 2), no_speech=round(nospeech, 2),
        )
        return False
    return True

def _windowed_peak_rms(f: np.ndarray, window_sec: float = 0.1) -> float:
    """Loudest `window_sec` RMS window in float32 audio (0..1). Windowed (not
    whole-buffer) so a short real word still scores high while steady
    background hiss averages out low."""
    if f.size == 0:
        return 0.0
    win = int(SAMPLE_RATE * window_sec) or 1
    peak = 0.0
    for i in range(0, f.size, win):
        seg = f[i : i + win]
        if seg.size:
            peak = max(peak, float(np.sqrt(np.mean(seg ** 2))))
    return peak


_model: Optional[WhisperModel] = None
_load_lock = threading.Lock()
_backend: str = "(uninitialized)"


def preload_model() -> None:
    """Load distil-large-v3 eagerly + warm CUDA kernels so the first
    real turn doesn't pay the 5-7s cold-start tax we saw on first run."""
    global _model, _backend
    with _load_lock:
        if _model is not None:
            return
        print(f"loading faster-whisper {_MODEL_NAME} (first run downloads ~1.5 GB)...")
        # Try the requested model on GPU, then the fallback model on GPU,
        # then both on CPU — a bad HALO_STT_MODEL or a missing download must
        # degrade accuracy, never kill startup.
        candidates = [_MODEL_NAME]
        if _FALLBACK_MODEL not in candidates:
            candidates.append(_FALLBACK_MODEL)
        last_exc: Exception | None = None
        for device, compute in (("cuda", _CUDA_COMPUTE), ("cpu", _CPU_COMPUTE)):
            for name in candidates:
                try:
                    _model = WhisperModel(name, device=device, compute_type=compute)
                    _backend = f"{device} {compute} ({name})"
                    break
                except Exception as exc:
                    last_exc = exc
                    print(f"  {name} on {device} failed ({exc}); trying next")
            if _model is not None:
                break
        if _model is None:
            raise RuntimeError(f"no whisper model could load: {last_exc}")
        print(f"  faster-whisper loaded: backend={_backend}")

        # Warm CUDA kernels with 0.5 s of silence. The first inference
        # compiles GPU kernels and was costing 5-7 s on the first turn.
        # After warmup, real first turns land in the usual 0.5-1.5 s range.
        print("  warming up faster-whisper kernels...")
        silence = np.zeros(int(SAMPLE_RATE * 0.5), dtype=np.float32)
        try:
            segments, _info = _model.transcribe(
                silence, language="en", beam_size=1, vad_filter=False,
                without_timestamps=True, condition_on_previous_text=False,
            )
            list(segments)  # consume generator to actually run the model
        except Exception as exc:
            print(f"  warmup failed (non-fatal): {exc}")


class BatchTranscriber:
    """Per-turn audio buffer + on-demand transcription.

    Construct once per turn, .feed() chunks as they arrive, .transcribe()
    when the orchestrator wants the current text. Cheap to call repeatedly
    since faster-whisper is fast — but the orchestrator should still avoid
    calling more than once per silence event.
    """

    def __init__(self) -> None:
        preload_model()
        self._chunks: list[np.ndarray] = []
        self._lock = threading.Lock()
        # Decode confidence of the LAST transcribe() call (duration-weighted
        # avg_logprob), None until a decode produced text. Read by turn.py to
        # gate the semantic-repair pass.
        self.last_quality: float | None = None

    def feed(self, audio_int16: np.ndarray) -> None:
        """Append one chunk of int16 PCM audio. Safe to call from the
        sounddevice audio callback — pure list append."""
        with self._lock:
            self._chunks.append(audio_int16)

    def seed(self, audio_int16: np.ndarray) -> None:
        """Prepend a chunk (used for the pre-wake audio capture)."""
        if audio_int16.size == 0:
            return
        with self._lock:
            self._chunks.insert(0, audio_int16)

    def reset(self) -> None:
        """Drop all buffered audio so the next transcribe()/peak_rms() reflect
        only freshly-fed chunks. Used by the barge-in listener between rejected
        candidates so the loudness gate measures THIS utterance, not the running
        sum of everything heard since Halo started speaking."""
        with self._lock:
            self._chunks = []

    def _snapshot(self) -> np.ndarray:
        with self._lock:
            if not self._chunks:
                return np.zeros(0, dtype=np.int16)
            return np.concatenate(self._chunks)

    def transcribe(self, *, raw: bool = False) -> str:
        """Run Whisper on everything buffered so far. Returns "" if too short.

        `raw=True` (dictation mode) skips the hallucination-phrase blocklist
        and the acoustic-confidence gate. Those gates exist to stop a
        command turn from dispatching mic noise / a bare "hello" as a task —
        but in dictation the user is deliberately speaking words to be typed
        verbatim, so dropping ordinary words like "no" / "okay" / "hello" /
        "thanks" would silently swallow legitimate input. The Whisper VAD
        filter still runs (it only strips genuine non-speech segments).
        """
        audio_i16 = self._snapshot()
        if audio_i16.size < int(SAMPLE_RATE * _MIN_AUDIO_SEC):
            return ""
        audio_f32 = audio_i16.astype(np.float32) / 32768.0
        # Gain-normalize quiet mics before Whisper. Devices like NVIDIA
        # Broadcast output a low level, which starves the decoder and produces
        # garbled transcripts ("stop"->"top", "sleep"->"slip"). Keyed to the
        # loudest 100 ms SPEECH window, not the absolute peak — a single click
        # or pop used to defeat the boost and leave the actual words starved.
        # Capped at 12x and only for genuinely-quiet-but-present speech so
        # near-silence isn't blown up into hallucinated words; the absolute-peak
        # cap keeps the boosted audio out of hard clipping.
        rms_peak = _windowed_peak_rms(audio_f32)
        if 0.004 < rms_peak < 0.15:
            abs_peak = float(np.max(np.abs(audio_f32)))
            gain = min(0.15 / rms_peak, 12.0)
            if abs_peak > 0.0:
                gain = min(gain, 0.95 / abs_peak)
            if gain > 1.0:
                audio_f32 = audio_f32 * gain
        assert _model is not None
        segments, _info = _model.transcribe(
            audio_f32,
            language="en",
            beam_size=5,
            # Accent help: prime the decoder with the domain vocabulary so an
            # accented "open Chrome / Claude / calculator" latches onto the
            # intended word instead of a phonetic neighbour. Only for command
            # turns — dictation (raw=True) stays neutral so arbitrary free
            # speech isn't skewed toward these terms.
            initial_prompt=None if raw else _BIAS_PROMPT,
            # Whisper's built-in silero VAD pass strips non-speech segments
            # BEFORE transcription. Massive reduction in "Thank you" /
            # "Thanks for watching" hallucinations on quiet or noisy
            # turns — those artifacts only appear when Whisper gets fed
            # near-silence to decode. With vad_filter on, Whisper sees
            # only the actual speech. Explicit parameters — the defaults
            # (threshold 0.5) deleted quiet speech the recorder captured.
            vad_filter=True,
            vad_parameters=dict(_VAD_FILTER_PARAMS),
            without_timestamps=True,
            condition_on_previous_text=False,  # avoid hallucinated continuations
        )
        seg_list = list(segments)
        text = " ".join(seg.text.strip() for seg in seg_list).strip()
        if not text:
            return ""
        # Expose the decode confidence (duration-weighted avg logprob) so the
        # orchestrator can send LOW-confidence transcripts through the LLM
        # semantic-repair pass. ~-0.1..-0.3 = confident; < -0.5 = shaky.
        self.last_quality = _acoustic_stats(seg_list)[0] if seg_list else None
        if raw:
            # Dictation: type exactly what was said, gates bypassed.
            return text
        # Acoustic-confidence gate — drop turns the model itself flags as
        # probably-non-speech (mic noise / background audio it hallucinated
        # words from). This is what let "no, what do you mean, it feels
        # good" reach the router and spawn a phantom session.
        if seg_list and not _passes_confidence(seg_list, text):
            return ""
        # Filter known distil-whisper silence-mode artifacts ("Thank you.",
        # "Thanks for watching.", etc) so the orchestrator doesn't dispatch
        # them as real commands. SILENT discard — logging each drop spammed
        # the terminal during long silent waits ("discarded hallucination
        # 'Hello.'" x 30). The bus event below still fires for the
        # dashboard, just no stdout noise.
        # BYPASSED while the orchestrator expects an answer to a direct
        # question — a real "Yes." / "No." / "Okay." must reach it (the
        # blocklist was silently swallowing confirmation answers).
        if _is_hallucination(text) and not _expect_answer:
            return ""
        return text

    def peak_rms(self) -> float:
        """Loudest 100 ms window in the buffered audio (RMS, 0..1).

        Used to reject the no-VAD fallback path: if even the loudest window
        is near silence, the mic only captured room tone and any text
        Whisper produced is a hallucination — don't commit it as a turn.
        Windowed (not whole-buffer) so a short real word still scores high
        while steady background hiss averages out low.
        """
        audio_i16 = self._snapshot()
        if audio_i16.size == 0:
            return 0.0
        return _windowed_peak_rms(audio_i16.astype(np.float32) / 32768.0)

    def stop(self) -> str:
        """Symmetric API with the old streaming transcriber. Returns final text."""
        return self.transcribe()
