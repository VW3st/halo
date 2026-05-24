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

import threading
import time

import numpy as np
import openwakeword
import sounddevice as sd
from openwakeword.model import Model

from halo import bus
from halo.config import MODELS_DIR, SAMPLE_RATE

# Preferred wake: our custom `halo.onnx`. If not on disk (fresh clone,
# user hasn't downloaded models), fall back to the openWakeWord builtin.
WAKE_WORD = "halo"
WAKE_WORD_FALLBACK = "hey_jarvis"
_WAKE_MODEL_PATH = MODELS_DIR / f"{WAKE_WORD}.onnx"

# Single-word "halo" collides acoustically with hello / hollow / hallow /
# Hawaii, so we pre-trained on confusables and bump the activation
# threshold above the openWakeWord default (0.5) to filter the rest.
# Tune in this range:
#   0.55  more sensitive   — more false positives, fewer missed wakes
#   0.65  balanced
#   0.75  conservative default — what shipped after live testing showed
#                                room-noise false fires at 0.65 (Blue
#                                Snowball + no hardware noise suppression
#                                + reverberant room). Raise to 0.80+ if
#                                still false-firing on quiet utterances.
THRESHOLD = 0.75

# silero-VAD gate on top of the wake model. openWakeWord supports an
# auxiliary VAD check that requires the silero model to ALSO score
# above this value before a wake counts. The wake DNN occasionally
# finds "halo"-shaped patterns in pure room noise; gating on silero
# means the fire only counts when there's also actual human speech.
# Dropped false-positive rate to ~zero in our testing. Set to 0.0 to
# disable the gate entirely. Documented in the openWakeWord README
# under "Voice Activity Detection (VAD)".
#
# 0.5 turned out too permissive — silero rates a lot of room noise
# (fans, HVAC, distant typing) above 0.5 because it's looking for
# any human-vocal-ish energy. 0.7 means "I'm pretty sure this is
# speech". 0.85+ is "definitely speech, miss soft whispers".
WAKE_VAD_THRESHOLD = 0.7
CHUNK_SIZE = 1280  # 80 ms at 16 kHz — openWakeWord's expected frame size
COOLDOWN_SEC = 2.0

_model: Model | None = None
_active_wake_key: str = WAKE_WORD  # mutated by _get_model if we fall back

# Print a one-line score update whenever we hear *something* that
# resembles the wake word but doesn't cross THRESHOLD. Helps you tell
# "mic is dead" from "mic works but my pronunciation isn't matching".
LIVE_SCORE_FLOOR = 0.05
LIVE_SCORE_INTERVAL = 0.5

# Last ~PRE_WAKE_BUFFER_SEC of mic audio is kept around so the turn
# orchestrator can seed Moonshine with whatever the user said in the
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


def listen_for_wake() -> None:
    """Block until the wake word is detected.

    While waiting, keeps the last ~PRE_WAKE_BUFFER_SEC of audio in a
    ring buffer so callers can recover whatever the user said in the
    same breath as the wake word (e.g. "Hey Jarvis open calculator").
    """
    model = _get_model()
    model.reset()
    detected = threading.Event()
    last_detection = 0.0
    last_score_print = 0.0

    ring_capacity = int(SAMPLE_RATE * PRE_WAKE_BUFFER_SEC)
    ring = np.zeros(ring_capacity, dtype=np.int16)
    ring_pos = 0
    ring_filled = False
    ring_lock = threading.Lock()

    def callback(indata: np.ndarray, frames: int, time_info, status) -> None:
        # `global` must be inside this nested function — putting it on
        # listen_for_wake only would make this assignment local and
        # silently throw away the captured audio.
        global _pre_wake_audio, _last_audio_status_print
        nonlocal last_detection, last_score_print, ring_pos, ring_filled
        if status:
            now_t = time.monotonic()
            if now_t - _last_audio_status_print > _AUDIO_STATUS_THROTTLE_SEC:
                _last_audio_status_print = now_t
                print(f"audio status: {status}")
        chunk = indata[:, 0]

        # Write chunk into the ring buffer (handles wrap-around).
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

        scores = model.predict(chunk)
        score = float(scores.get(_active_wake_key, 0.0))
        now = time.monotonic()
        if score >= LIVE_SCORE_FLOOR and now - last_score_print > LIVE_SCORE_INTERVAL:
            last_score_print = now
            print(f"  heard something (score={score:.2f})")
            bus.emit("wake.heard", score=score)
        if score >= THRESHOLD and now - last_detection > COOLDOWN_SEC:
            last_detection = now
            # Always print the score that actually fired wake — overrides
            # the rate-limited "heard something" line above so the user
            # sees what crossed threshold and can tune accordingly.
            print(f"  wake fired (score={score:.2f}, threshold={THRESHOLD})")
            bus.emit("wake.fired", score=score, threshold=THRESHOLD)
            # Snapshot the ring buffer in chronological order before
            # we hand control back to the turn orchestrator.
            with ring_lock:
                if ring_filled:
                    snapshot = np.concatenate([ring[ring_pos:], ring[:ring_pos]]).copy()
                else:
                    snapshot = ring[:ring_pos].copy()
            _pre_wake_audio = snapshot
            detected.set()

    with sd.InputStream(
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype="int16",
        blocksize=CHUNK_SIZE,
        callback=callback,
    ):
        print(f"mic: {_describe_input_device()}")
        print(f"listening for wake word {_active_wake_key!r} (threshold={THRESHOLD})...")
        # Poll instead of blocking forever so Ctrl-C propagates on Windows.
        # Event.wait() without a timeout doesn't let SIGINT through cleanly
        # when sounddevice has an active callback.
        while not detected.wait(timeout=0.25):
            pass


if __name__ == "__main__":
    print(f"Listening for wake word '{WAKE_WORD}'... (Ctrl+C to stop)")
    try:
        while True:
            listen_for_wake()
            print("wake word detected")
    except KeyboardInterrupt:
        print("\nstopped")
