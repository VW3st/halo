"""Text-to-speech via Kokoro-82M ONNX.

Halo speaks back. Used for:
  - acknowledgement after wake fires ("Yes?")
  - tool summaries ("Opened calculator.")
  - Stage 2 confirmations ("Building a login page, ready to start?")
  - agent responses ("Mars created the landing page.")

Playback is non-blocking — we synthesize on a worker thread and start
audio playback as soon as samples are ready. We don't wait for it to
finish before returning to the wake-listen loop, so a fast follow-up
command from the user isn't blocked behind Halo's voice.

All text passes through `_clean_for_speech()` first because agent
responses come in Markdown and TTS reads characters literally —
asterisks, backticks, and em-dashes turn into garbage audio.
"""

from __future__ import annotations

import random
import re
import threading
from pathlib import Path
from typing import Optional

import numpy as np
import sounddevice as sd

from halo import bus
from halo.config import MODELS_DIR

# af_heart is the only A/A graded voice in the Kokoro lineup
# (per hexgrad/Kokoro-82M/VOICES.md). Natural-sounding, conversational,
# no uncanny-valley artifacts that B-grade voices like am_michael had.
# Speed 1.0 = natural cadence; faster sounds rushed/creepy.
DEFAULT_VOICE = "af_heart"
DEFAULT_SPEED = 1.0

# Model files (downloaded manually — see README).
_MODEL_PATH = MODELS_DIR / "kokoro-v1.0.fp16.onnx"
_VOICES_PATH = MODELS_DIR / "voices-v1.0.bin"

_kokoro = None
_load_lock = threading.Lock()
_playback_lock = threading.Lock()

# Tiny per-state phrase banks so Halo doesn't sound robotic.
ACK_PHRASES = (
    "Yes?",
    "I'm here.",
    "Listening.",
    "Ready.",
    "Go ahead.",
)
WORKING_PHRASES = (
    "On it.",
    "Working on it.",
    "Hang on.",
    "Got it.",
)


def preload_voice() -> None:
    """Load Kokoro ONNX once. Cheap to call from startup."""
    global _kokoro
    with _load_lock:
        if _kokoro is not None:
            return
        if not _MODEL_PATH.exists() or not _VOICES_PATH.exists():
            print(f"  Kokoro model files missing -- voice disabled.")
            print(f"  Download into {MODELS_DIR}:")
            print(f"    https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.fp16.onnx")
            print(f"    https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin")
            return
        print("loading Kokoro TTS (fp16 ONNX)...")
        try:
            from kokoro_onnx import Kokoro  # noqa: WPS433 — late import for optional dep
            _kokoro = Kokoro(str(_MODEL_PATH), str(_VOICES_PATH))
            print(f"  Kokoro loaded.")
        except Exception as exc:
            print(f"  Kokoro load failed: {exc} -- voice disabled.")


def is_available() -> bool:
    return _kokoro is not None


_MARKDOWN_RE = re.compile(r"```[\s\S]*?```|`([^`]*)`|\*{1,3}([^\*]+)\*{1,3}|_{1,2}([^_]+)_{1,2}")
_HEADING_RE = re.compile(r"^#{1,6}\s*", flags=re.MULTILINE)
_LINK_RE = re.compile(r"\[([^\]]+)\]\([^\)]+\)")
_BULLET_RE = re.compile(r"^[\s]*[-*+•]\s+", flags=re.MULTILINE)
_PATHISH_RE = re.compile(r"[A-Za-z]:[\\/][^\s\)`]+|[/~][^\s\)`]+")
_WHITESPACE_RE = re.compile(r"\s+")


def _clean_for_speech(text: str) -> str:
    """Strip Markdown / control chars / weird unicode that read as garbage
    when sent through TTS. Agents like Claude love to respond in Markdown;
    Kokoro pronounces every asterisk and backtick literally otherwise."""
    if not text:
        return ""
    # Collapse fenced code blocks first (their content is rarely speakable).
    out = re.sub(r"```[\s\S]*?```", " I wrote a code block. ", text)
    # Inline `code` -> just the inner text
    out = re.sub(r"`([^`]*)`", r"\1", out)
    # **bold** / *italic* / _underline_ markers
    out = re.sub(r"\*{1,3}([^\*]+)\*{1,3}", r"\1", out)
    out = re.sub(r"_{1,2}([^_]+)_{1,2}", r"\1", out)
    # Headings, bullets, blockquotes
    out = _HEADING_RE.sub("", out)
    out = _BULLET_RE.sub("", out)
    out = re.sub(r"^>\s+", "", out, flags=re.MULTILINE)
    # Markdown links -> link text only
    out = _LINK_RE.sub(r"\1", out)
    # Long file paths read aloud as "C colon back slash..." — shorten to basename
    out = _PATHISH_RE.sub(lambda m: Path(m.group(0)).name or m.group(0), out)
    # Em-dash / en-dash / bullets / curly punctuation
    out = (out
           .replace("—", ", ")
           .replace("–", ", ")
           .replace("•", "")
           .replace("§", "section")
           .replace("·", ",")
           .replace("…", "...")
           .replace("‘", "'").replace("’", "'")
           .replace("“", '"').replace("”", '"')
           .replace("·", ","))
    # Mojibake from latin-1/cp1252 round-trips (â€" etc) — drop them.
    out = re.sub(r"â€.|â€", "", out)
    # Collapse newlines into sentence boundaries, then squash whitespace.
    out = re.sub(r"\n+", ". ", out)
    out = _WHITESPACE_RE.sub(" ", out).strip()
    # Avoid double periods like ". ."
    out = re.sub(r"\.\s*\.", ".", out)
    return out


def say(
    text: str,
    *,
    voice: str = DEFAULT_VOICE,
    speed: float = DEFAULT_SPEED,
    blocking: bool = False,
) -> None:
    """Synthesize `text` and play it on the default output device.

    Non-blocking by default — synthesis + playback happen on a worker
    thread so the wake-listen loop can resume immediately. Pass
    blocking=True if the caller needs to wait (e.g. shutdown).
    """
    if _kokoro is None:
        return  # voice disabled
    spoken = _clean_for_speech(text)
    if not spoken:
        return
    bus.emit("tts.spoke", text=spoken, who="Halo")

    def _do_synth_and_play() -> None:
        try:
            samples, sample_rate = _kokoro.create(
                spoken, voice=voice, speed=speed, lang="en-us"
            )
            if samples.dtype != np.float32:
                samples = samples.astype(np.float32)
            with _playback_lock:
                sd.play(samples, samplerate=sample_rate, blocking=True)
                sd.wait()
        except Exception as exc:
            print(f"  voice synth/playback error: {exc}")

    if blocking:
        _do_synth_and_play()
    else:
        threading.Thread(target=_do_synth_and_play, daemon=True).start()


def say_one_of(phrases: tuple[str, ...], **kwargs) -> str:
    """Pick a random phrase from the bank and speak it. Returns what was said."""
    phrase = random.choice(phrases)
    say(phrase, **kwargs)
    return phrase


def stop() -> None:
    """Interrupt any current playback (e.g. when user starts a new turn)."""
    try:
        sd.stop()
    except Exception:
        pass
