"""Wake word listener for Halo.

Blocks until the configured wake word fires, then returns. The openWakeWord
model is loaded once per process and reused across calls.

Wake word: custom-trained `halo` ONNX classifier (trained via bbarrick's
wakeword_trainer with ElevenLabs TTS samples of "Halo" + "Hey low" — the
latter shares the acoustic fingerprint of someone saying "halo" naturally
as one word, so the model fires reliably on a plain "halo" utterance).
Falls back to the openWakeWord-builtin `hey_jarvis` if the custom file
isn't on disk (so a fresh checkout without the model still boots).
"""

from __future__ import annotations

import queue
import threading
import time

import numpy as np
import openwakeword
import sounddevice as sd
from openwakeword.model import Model

from halo import bus
from halo.config import MODELS_DIR, SAMPLE_RATE
from halo.userconfig import cfg as _user_cfg

# Wake word + all sensitivity knobs come from [wake] config (env / TOML) so
# they can be tuned per-machine WITHOUT editing source. Defaults live in
# halo/userconfig.py WakeConfig. Lower threshold / vad_gate = more sensitive
# (fires more easily; more false positives). Preferred wake model is the
# custom `<word>.onnx`; if it's not on disk we fall back to a builtin.
WAKE_WORD = (_user_cfg.wake.word or "halo").strip() or "halo"
WAKE_WORD_FALLBACK = "hey_jarvis"
_WAKE_MODEL_PATH = MODELS_DIR / f"{WAKE_WORD}.onnx"

# Activation threshold (0..1): the wake DNN must score at least this.
THRESHOLD = float(_user_cfg.wake.threshold)
# Sustained-activation debounce (see listen_for_wake). A wake fires when the
# score stays >= THRESHOLD for WAKE_MIN_CONSEC consecutive 80 ms frames.
#   1 = fire on a single qualifying frame — needed for mics/voices whose
#       "halo" peaks for just one frame (e.g. a Blue Snowball topping out
#       ~0.52). Safe when the mic's noise floor stays well under THRESHOLD.
#   2 = require ~160 ms of sustained activation — use on a hotter mic where a
#       lone transient frame false-fires (e.g. a BRIO spiking to 0.70 on
#       random speech).
# There is deliberately NO separate "strong peak" gate any more: that bar was
# derived from one mic's levels and silently broke quieter mics. Tune the pair
# THRESHOLD + min_consecutive to the mic instead (see `halo` config).
WAKE_MIN_CONSEC = max(1, int(_user_cfg.wake.min_consecutive))

# silero-VAD gate baked into openWakeWord: silero must ALSO score above this
# before a wake counts (filters wake-DNN hallucinations on room noise). Too
# high and a quiet mic's real speech is suppressed and the wake never fires
# — the usual cause of "I say halo and nothing happens". Lower it (or 0.0 to
# disable) if wakes are MISSED; raise it if room noise false-fires.
WAKE_VAD_THRESHOLD = float(_user_cfg.wake.vad_gate)
CHUNK_SIZE = 1280  # 80 ms at 16 kHz — openWakeWord's expected frame size
COOLDOWN_SEC = float(_user_cfg.wake.cooldown_sec)

_model: Model | None = None
_active_wake_key: str = WAKE_WORD  # mutated by _get_model if we fall back

# Print a one-line score update whenever we hear *something* that
# resembles the wake word but doesn't cross THRESHOLD. Helps you tell
# "mic is dead" from "mic works but my pronunciation isn't matching".
LIVE_SCORE_FLOOR = 0.03
LIVE_SCORE_INTERVAL = 0.3

# Last ~PRE_WAKE_BUFFER_SEC of mic audio is kept around so the turn
# orchestrator can seed the transcriber with whatever the user said in the
# same breath as the wake word ("Hey Jarvis open calculator").
PRE_WAKE_BUFFER_SEC = 1.0
_pre_wake_audio: np.ndarray | None = None

# Throttle audio-status overflow prints — same reason as record.py.
_AUDIO_STATUS_THROTTLE_SEC = 5.0
_last_audio_status_print = 0.0


def _get_model() -> Model:
    global _model, _active_wake_key
    if _model is None:
        if _WAKE_MODEL_PATH.exists():
            print(f"loading wake word model: {_WAKE_MODEL_PATH.name} "
                  f"(threshold={THRESHOLD}, vad_gate={WAKE_VAD_THRESHOLD})")
            _model = Model(
                wakeword_models=[str(_WAKE_MODEL_PATH)],
                inference_framework="onnx",
                vad_threshold=WAKE_VAD_THRESHOLD,
            )
            _active_wake_key = WAKE_WORD
        else:
            print(f"  no custom {_WAKE_MODEL_PATH.name} found, falling back "
                  f"to builtin {WAKE_WORD_FALLBACK!r}")
            openwakeword.utils.download_models()
            _model = Model(
                wakeword_models=[WAKE_WORD_FALLBACK],
                inference_framework="onnx",
                vad_threshold=WAKE_VAD_THRESHOLD,
            )
            _active_wake_key = WAKE_WORD_FALLBACK
    return _model


def get_pre_wake_audio() -> np.ndarray:
    """Audio captured in the ~1 s before the most recent wake detection.

    Returns an empty int16 array if no wake has fired yet this process.
    """
    if _pre_wake_audio is None:
        return np.zeros(0, dtype=np.int16)
    return _pre_wake_audio


def _describe_input_device() -> str:
    try:
        info = sd.query_devices(kind="input")
        return f"{info['name']}"
    except Exception as exc:
        return f"<unknown: {exc}>"


def listen_for_wake(interrupt: "threading.Event | None" = None) -> float:
    """Block until the wake word is detected. Returns the DNN score at the
    moment it fired (used by the caller's STT verification as a confidence
    prior — a strong fire is trusted more readily).

    While waiting, keeps the last ~PRE_WAKE_BUFFER_SEC of audio in a
    ring buffer so callers can recover whatever the user said in the
    same breath as the wake word (e.g. "Hey Jarvis open calculator").

    If `interrupt` (a threading.Event) is set while waiting, returns the sentinel
    -1.0 (not a real fire) so the caller can release the mic for another action
    (e.g. hotkey-triggered dictation).
    """
    model = _get_model()
    model.reset()
    detected = threading.Event()
    last_detection = 0.0
    last_score_print = 0.0
    consec = 0     # consecutive frames at/above THRESHOLD (sustain counter)
    run_peak = 0.0  # peak score within the current consecutive run
    fired_score = [0.0]  # mutable holder so the consumer can hand back the score

    ring_capacity = int(SAMPLE_RATE * PRE_WAKE_BUFFER_SEC)
    ring = np.zeros(ring_capacity, dtype=np.int16)
    ring_pos = 0
    ring_filled = False
    ring_lock = threading.Lock()

    # Realtime callback only writes the ring + enqueues the chunk. The wake
    # DNN (model.predict, which also runs silero VAD) is far too heavy for
    # the audio thread — running it inline overran the block budget and
    # caused PortAudio "input overflow" that dropped frames and degraded
    # wake scoring. It now runs on the consumer thread below.
    pred_q: queue.Queue = queue.Queue(maxsize=256)
    stop = threading.Event()

    def callback(indata: np.ndarray, frames: int, time_info, status) -> None:
        global _last_audio_status_print
        nonlocal ring_pos, ring_filled
        if status:
            now_t = time.monotonic()
            if now_t - _last_audio_status_print > _AUDIO_STATUS_THROTTLE_SEC:
                _last_audio_status_print = now_t
                print(f"audio status: {status}")
        chunk = indata[:, 0].copy()
        n = len(chunk)
        with ring_lock:
            if n >= ring_capacity:
                ring[:] = chunk[-ring_capacity:]
                ring_pos = 0
                ring_filled = True
            else:
                end = ring_pos + n
                if end <= ring_capacity:
                    ring[ring_pos:end] = chunk
                else:
                    split = ring_capacity - ring_pos
                    ring[ring_pos:] = chunk[:split]
                    ring[: end - ring_capacity] = chunk[split:]
                if end >= ring_capacity:
                    ring_filled = True
                ring_pos = end % ring_capacity
        try:
            pred_q.put_nowait(chunk)
        except queue.Full:
            pass

    def consumer() -> None:
        global _pre_wake_audio
        nonlocal last_detection, last_score_print, consec, run_peak
        while not stop.is_set():
            try:
                chunk = pred_q.get(timeout=0.2)
            except queue.Empty:
                continue
            if chunk is None:
                break
            scores = model.predict(chunk)
            score = float(scores.get(_active_wake_key, 0.0))
            now = time.monotonic()
            # Sustain counter: how many consecutive frames have stayed
            # >= THRESHOLD. Fire on a run of >= WAKE_MIN_CONSEC frames. No
            # separate peak gate — threshold + min_consecutive are the only
            # knobs, tuned per mic (a brittle fixed peak silently broke quiet
            # mics). run_peak is tracked for display only.
            if score >= THRESHOLD:
                consec += 1
                run_peak = max(run_peak, score)
            else:
                consec = 0
                run_peak = 0.0
            cooled = now - last_detection > COOLDOWN_SEC
            fired = cooled and consec >= WAKE_MIN_CONSEC
            # Visibility: show what the wake model hears AND why a near-miss
            # didn't fire, so threshold / min_consecutive can be tuned by ear.
            if score >= LIVE_SCORE_FLOOR and now - last_score_print > LIVE_SCORE_INTERVAL:
                last_score_print = now
                if fired:
                    note = ""
                elif score >= THRESHOLD:
                    note = (f"  [>= {THRESHOLD:.2f} but not sustained: "
                            f"frame {consec}/{WAKE_MIN_CONSEC}]")
                else:
                    note = f"  [below threshold {THRESHOLD:.2f}]"
                print(f"  heard {_active_wake_key!r} score={score:.2f}{note}")
                bus.emit("wake.heard", score=score)
            if fired:
                last_detection = now
                fired_score[0] = score
                print(f"  wake fired (score={score:.2f}, threshold={THRESHOLD})")
                bus.emit("wake.fired", score=score, threshold=THRESHOLD)
                with ring_lock:
                    if ring_filled:
                        snapshot = np.concatenate([ring[ring_pos:], ring[:ring_pos]]).copy()
                    else:
                        snapshot = ring[:ring_pos].copy()
                _pre_wake_audio = snapshot
                detected.set()
                return

    worker = threading.Thread(target=consumer, daemon=True, name="halo-wake-predict")
    worker.start()
    try:
        with sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="int16",
            blocksize=CHUNK_SIZE,
            latency="high",
            callback=callback,
        ):
            print(f"mic: {_describe_input_device()}")
            print(f"listening for wake word {_active_wake_key!r} (threshold={THRESHOLD})...")
            while not detected.wait(timeout=0.25):
                if interrupt is not None and interrupt.is_set():
                    return -1.0  # interrupted (e.g. dictation hotkey) — not a real fire
    finally:
        stop.set()
        try:
            pred_q.put_nowait(None)
        except queue.Full:
            pass
        worker.join(timeout=1.0)
    return fired_score[0]


if __name__ == "__main__":
    print(f"Listening for wake word '{WAKE_WORD}'... (Ctrl+C to stop)")
    try:
        while True:
            listen_for_wake()
            print("wake word detected")
    except KeyboardInterrupt:
        print("\nstopped")
