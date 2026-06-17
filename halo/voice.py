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

import json
import os
import collections
import random
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

import numpy as np
import sounddevice as sd

from halo import bus
from halo.config import MODELS_DIR
from halo.userconfig import cfg as user_cfg

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
_cloud_voice_ready = False
_load_lock = threading.Lock()
_playback_lock = threading.Lock()

# Track in-flight TTS so the orchestrator can avoid opening the mic
# while Halo is still talking back (would catch echo, fire false
# "no speech detected" turns, and consume idle budget the user needs
# for replying to a question).
_active_say_count = 0
_active_count_lock = threading.Lock()
# After playback ends there's still an acoustic tail in the room + mic/OS
# buffering. Keep is_speaking() True for a short grace window so the recorder
# doesn't transcribe the tail of Halo's own voice (self-dispatch loop).
_SPEAKING_TAIL_SEC = 0.35
_speaking_until = 0.0


def _begin_speaking() -> None:
    global _active_say_count
    with _active_count_lock:
        _active_say_count += 1


def _end_speaking() -> None:
    global _active_say_count, _speaking_until
    import time as _time
    with _active_count_lock:
        _active_say_count = max(0, _active_say_count - 1)
        if _active_say_count == 0:
            _speaking_until = _time.monotonic() + _SPEAKING_TAIL_SEC


def is_speaking() -> bool:
    """True while a say() is mid-flight OR within the post-playback tail."""
    import time as _time
    with _active_count_lock:
        return _active_say_count > 0 or _time.monotonic() < _speaking_until


# Quiet / work mode: when on, agents do NOT narrate their output live and Halo
# skips the "still working" keepalive murmurs — it only speaks when a job
# finishes or needs the user. Direct replies (acks, confirmations, answers to
# the user) still speak. Toggled by voice ("quiet mode" / "narrate") or
# HALO_QUIET_MODE=1. Read by _start_agent_and_ack (live budget) and the
# agents.py keepalive watchdog.
_QUIET_MODE = os.getenv("HALO_QUIET_MODE", "").strip().lower() in ("1", "true", "yes", "on")


def is_quiet() -> bool:
    return _QUIET_MODE


def set_quiet(on: bool) -> None:
    global _QUIET_MODE
    _QUIET_MODE = bool(on)


# Recent spoken text, for the barge-in echo guard. When the mic captures speech
# while Halo is talking, we compare it against what Halo just said — if it
# overlaps heavily it's mic echo of Halo's own voice, not the user.
_recent_spoken: "collections.deque[tuple[float, frozenset]]" = collections.deque(maxlen=16)
_recent_spoken_lock = threading.Lock()


def _sig_words(text: str) -> frozenset:
    """Significant (length >= 3) lowercased word set, for overlap scoring."""
    return frozenset(w for w in re.findall(r"[a-z']+", (text or "").lower()) if len(w) >= 3)


def _note_spoken(text: str) -> None:
    import time as _time
    words = _sig_words(text)
    if not words:
        return
    with _recent_spoken_lock:
        _recent_spoken.append((_time.monotonic(), words))


def recently_spoke(text: str, within_sec: float = 12.0, thresh: float = 0.7) -> bool:
    """True if `text` substantially overlaps something Halo said in the last
    `within_sec` — i.e. it's probably mic echo of Halo's own voice rather than
    the user. The high threshold lets the user echo a word or two ("yes, Claude,
    do it") without being mistaken for feedback."""
    import time as _time
    cand = _sig_words(text)
    if not cand:
        return False
    now = _time.monotonic()
    with _recent_spoken_lock:
        entries = [w for (ts, w) in _recent_spoken if now - ts <= within_sec]
    return any(spoken and len(cand & spoken) / len(cand) >= thresh for spoken in entries)


def wait_until_silent(timeout: float = 15.0, poll_interval: float = 0.05) -> bool:
    """Block until is_speaking() returns False or `timeout` elapses.

    Returns True if Halo went silent cleanly, False on timeout (caller
    can decide whether to proceed anyway).
    """
    import time as _time
    deadline = _time.monotonic() + timeout
    while is_speaking() and _time.monotonic() < deadline:
        _time.sleep(poll_interval)
    return not is_speaking()

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
    """Prepare the selected TTS provider once. Cheap to call from startup."""
    global _cloud_voice_ready, _kokoro
    with _load_lock:
        provider = (user_cfg.voice.provider or "kokoro").strip().lower()
        if provider == "elevenlabs":
            key = os.environ.get(user_cfg.voice.elevenlabs_api_key_env, "").strip()
            voice_id = (user_cfg.voice.elevenlabs_voice_id or "").strip()
            if not key:
                print(
                    "  ElevenLabs API key missing -- voice disabled "
                    f"({user_cfg.voice.elevenlabs_api_key_env})."
                )
                _cloud_voice_ready = False
                return
            if not voice_id:
                print("  ElevenLabs voice id missing -- voice disabled.")
                _cloud_voice_ready = False
                return
            _cloud_voice_ready = True
            print(
                "loading ElevenLabs TTS "
                f"({user_cfg.voice.elevenlabs_model_id}, "
                f"{user_cfg.voice.elevenlabs_output_format})..."
            )
            print("  ElevenLabs configured.")
            return
        if provider != "kokoro":
            print(f"  unknown voice provider {provider!r} -- voice disabled.")
            return
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
    provider = (user_cfg.voice.provider or "kokoro").strip().lower()
    if provider == "elevenlabs":
        return _cloud_voice_ready
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
    provider = (user_cfg.voice.provider or "kokoro").strip().lower()
    if provider == "kokoro" and _kokoro is None:
        return
    if provider == "elevenlabs" and not _cloud_voice_ready:
        return
    if provider not in {"kokoro", "elevenlabs"}:
        return
    spoken = _clean_for_speech(text)
    if not spoken:
        return
    _note_spoken(spoken)  # for the barge-in echo guard
    bus.emit("tts.spoke", text=spoken, who="Halo")
    # Speaking.start drives the Output spectrogram + the central
    # "Speaking" ring on the dashboard. Duration is estimated from
    # word count (~3 wps for natural prosody); the worker thread emits
    # speaking.end when playback actually finishes.
    word_count = max(1, len(spoken.split()))
    bus.emit(
        "speaking.start",
        text=spoken,
        who="Halo",
        words=word_count,
        est_ms=int(word_count * 350),
    )

    # Mark "speaking" synchronously (before the worker thread starts)
    # so is_speaking() is reliable for callers that check immediately
    # after firing a non-blocking say().
    _begin_speaking()

    def _do_synth_and_play() -> None:
        import time as _time
        t0 = _time.monotonic()
        try:
            if provider == "elevenlabs":
                samples, sample_rate = _synth_elevenlabs(spoken)
            else:
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
        finally:
            _end_speaking()
            bus.emit(
                "speaking.end",
                who="Halo",
                ms=int((_time.monotonic() - t0) * 1000),
            )

    if blocking:
        _do_synth_and_play()
    else:
        threading.Thread(target=_do_synth_and_play, daemon=True).start()


def _synth_elevenlabs(text: str) -> tuple[np.ndarray, int]:
    output_format = (user_cfg.voice.elevenlabs_output_format or "pcm_24000").strip()
    sample_rate = _sample_rate_from_output_format(output_format)
    if not output_format.startswith("pcm_"):
        raise RuntimeError(
            "ElevenLabs output_format must be PCM for direct playback "
            f"(got {output_format!r})."
        )
    api_key = os.environ.get(user_cfg.voice.elevenlabs_api_key_env, "").strip()
    voice_id = (user_cfg.voice.elevenlabs_voice_id or "").strip()
    if not api_key or not voice_id:
        raise RuntimeError("ElevenLabs API key or voice id missing")

    query = urllib.parse.urlencode({"output_format": output_format})
    url = (
        "https://api.elevenlabs.io/v1/text-to-speech/"
        f"{urllib.parse.quote(voice_id)}/stream?{query}"
    )
    body = {
        "text": text,
        "model_id": user_cfg.voice.elevenlabs_model_id,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "xi-api-key": api_key,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(
            req, timeout=float(user_cfg.voice.elevenlabs_timeout_sec)
        ) as response:
            audio = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")[:200]
        raise RuntimeError(f"ElevenLabs HTTP {exc.code}: {detail}") from exc
    if not audio:
        raise RuntimeError("ElevenLabs returned empty audio")
    pcm = np.frombuffer(audio, dtype="<i2")
    return (pcm.astype(np.float32) / 32768.0), sample_rate


def _sample_rate_from_output_format(output_format: str) -> int:
    match = re.search(r"_(\d{4,5})(?:_|$)", output_format or "")
    if not match:
        return 24_000
    return int(match.group(1))


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
