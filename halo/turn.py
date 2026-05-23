"""Turn orchestrator — adaptive turn-taking (step 3.5).

After wake fires we open a recording stream, seed Moonshine with the
~1 s of audio captured BEFORE wake detection (so "Hey Jarvis open
calculator" said in one breath doesn't lose the command), and start
streaming partials.

Silence handling is adaptive based on the transcript shape:

  snappy    -> 0.8 s silence to commit  (short crisp command)
  thinking  -> 2.5 s silence to commit  (long / has connectors)
  composing -> 4.0 s silence to commit  (trailing connector or marker)

End conditions per turn:
  1. Terminator phrase ("go" / "do it" / "send it") at end of transcript
  2. Mode-appropriate silence elapsed AND Stage 1 says COMPLETE
  3. 30 s hard timeout

Hold triggers ("wait", "hold on", "give me a sec") suppress the
turn-complete check for HOLD_SUPPRESS_SEC. A soft backchannel tone
plays once after BACKCHANNEL_AFTER_SEC of silence in thinking/composing
modes so the user knows we're still listening.
"""

from __future__ import annotations

import re
import time

from halo.config import (
    BACKCHANNEL_AFTER_SEC,
    HOLD_SUPPRESS_SEC,
    MODE_SILENCE_SEC,
    SAMPLE_RATE,
    TURN_END_SILENCE_MS,
    TURN_EXTENSION_SEC,
    TURN_MAX_INCOMPLETE_EXTENSIONS,
    TURN_MAX_SEC,
    TURN_MAX_WAIT_FOR_FIRST_SPEECH_SEC,
)
from halo.record import RecorderState, open_stream, play_backchannel, play_chime
from halo.router import check_turn_complete, understand_and_route
from halo.stt import BatchTranscriber
from halo.tools import is_pure_tool
from halo.wake import get_pre_wake_audio

# --- end-of-turn override phrases ---------------------------------------
TERMINATORS_RE = re.compile(r"\b(send it|do it|go)\s*[.!?,]*\s*$", re.IGNORECASE)
HOLD_RE = re.compile(
    r"\b(wait|hold on|hold up|give me (?:a sec|a moment|a minute)|one moment|"
    r"one sec|just a sec|just a moment)\s*[.!?,]*\s*$",
    re.IGNORECASE,
)

# The pre-wake audio seed includes "Hey Jarvis", so every Moonshine
# transcript starts with the wake word. Strip it — otherwise the
# wake-word audio eats Moonshine's first decoding window and the real
# command lands in its weaker tail. Also handles common mishearings
# like "hey jervis" / "hey jarves".
WAKE_PREFIX_RE = re.compile(
    r"^\s*(?:hey|hi|hello|yo|halo|ok)[,.\s]+"
    r"(?:jarvis|jervis|jarves|jarvic|jarviss|jervice|halo|hello)"
    r"[,.\s]*",
    re.IGNORECASE,
)


def _strip_wake_prefix(text: str) -> str:
    return WAKE_PREFIX_RE.sub("", text).strip()

# --- mode-detection vocabulary ------------------------------------------
_TRAILING_CONNECTORS = {
    "and", "or", "but", "with", "also", "plus", "because", "so", "then",
    "the", "a", "my", "to", "for", "in", "on", "at", "of",
}
_THINKING_MARKERS = (
    "um", "uh", "hmm", "wait", "actually", "let me", "give me", "hold on",
)
_LIST_CONNECTORS_IN_TEXT = (" and ", " with ", " also ", " plus ", " then ")
_COMPLEX_WORD_COUNT = 10


def detect_mode(transcript: str) -> tuple[str, float]:
    """Classify the transcript shape and return (mode, total_silence_sec)."""
    text = transcript.strip().lower()
    if not text:
        return "snappy", MODE_SILENCE_SEC["snappy"]

    words = text.split()
    last_word = words[-1].strip(",.!?")

    # Trailing connector / function word -> almost certainly composing.
    if last_word in _TRAILING_CONNECTORS:
        return "composing", MODE_SILENCE_SEC["composing"]

    # Thinking marker in the last 5 words -> bias toward longer wait.
    last_5 = " ".join(words[-5:])
    if any(m in last_5 for m in _THINKING_MARKERS):
        return "thinking", MODE_SILENCE_SEC["thinking"]

    # Long / multi-clause -> still thinking.
    if len(words) > _COMPLEX_WORD_COUNT or any(c in f" {text} " for c in _LIST_CONNECTORS_IN_TEXT):
        return "thinking", MODE_SILENCE_SEC["thinking"]

    return "snappy", MODE_SILENCE_SEC["snappy"]


def _strip_terminator(text: str) -> tuple[str, bool]:
    match = TERMINATORS_RE.search(text)
    if not match:
        return text, False
    return text[: match.start()].rstrip(" ,.!?"), True


def _has_hold(text: str) -> bool:
    return bool(HOLD_RE.search(text))


def _route(text: str) -> dict:
    """Decide what to do with the final transcript.

    Pre-processing:
      - Strip "Hey Jarvis" wake-word prefix so we route the actual command.

    Tool fast-path: if the *entire* cleaned transcript is tool-only
    (every comma/and segment matches a known tool), skip Stage 2 LLM
    and return a synthetic decision. Mixed intent ("open chrome AND
    build me a login page") falls through to Stage 2.

    Everything else falls through to Stage 2 LLM (Ollama).
    """
    cleaned = _strip_wake_prefix(text)
    if cleaned != text:
        print(f"  stripped wake prefix -> {cleaned!r}")

    if cleaned and is_pure_tool(cleaned):
        print("  tool fast-path matched -> skipping stage 2")
        return {
            "status": "ready",
            "cleaned_text": cleaned.rstrip(".!?,"),
            "intent": "system",
            "agent": "none",
            "confirmation": "",
            "clarification": "",
            "_via": "tool_fast_path",
        }
    t0 = time.monotonic()
    decision = understand_and_route(cleaned or text)
    print(f"  stage 2: {(time.monotonic() - t0) * 1000:.0f}ms")
    return decision


def _wait_with_backchannel(
    state: RecorderState,
    total_timeout: float,
    mode: str,
    deadline: float,
) -> bool:
    """Wait up to `total_timeout` for speech, with one backchannel tone
    fired after BACKCHANNEL_AFTER_SEC if we're in thinking/composing.
    Returns True if speech arrived, False on timeout. Caps wait at `deadline`."""
    start = time.monotonic()
    backchannel_played = False
    tick = 0.2
    while True:
        now = time.monotonic()
        elapsed = now - start
        remaining = min(total_timeout - elapsed, deadline - now)
        if remaining <= 0:
            return False
        wait_for = min(tick, remaining)
        if state.wait_for_speech(timeout=wait_for):
            return True
        if (
            not backchannel_played
            and mode in ("thinking", "composing")
            and elapsed >= BACKCHANNEL_AFTER_SEC
        ):
            play_backchannel()
            backchannel_played = True
            print("  (backchannel) still listening...")


def run_turn(
    *,
    skip_routing: bool = False,
    max_wait_first_speech_sec: float | None = None,
) -> dict | None:
    """Run one wake-triggered turn.

    `skip_routing=True` returns the raw transcript without calling Stage 2.
    Use this in direct-dialogue mode where the orchestrator is going to
    pipe the text straight to the active agent anyway — saves ~3-5 s of
    LLM latency per turn.

    Returned dict shape:
      routing on:  full Stage 2 decision (status/intent/agent/confirmation/...)
      routing off: {"status": "raw", "cleaned_text": "<text>", "transcript": "<text>"}

    `max_wait_first_speech_sec` overrides the default 5 s wait for the
    user to start talking. Callers shorten it (e.g. to 2 s) while
    background agent jobs are in flight so the conversation loop polls
    completed jobs sooner.
    """
    first_wait = (
        max_wait_first_speech_sec
        if max_wait_first_speech_sec is not None
        else TURN_MAX_WAIT_FOR_FIRST_SPEECH_SEC
    )
    transcriber = BatchTranscriber()

    # Seed with the audio we captured just before wake fired so commands
    # said in the same breath as the wake word aren't lost.
    pre = get_pre_wake_audio()
    if pre.size > 0:
        transcriber.seed(pre)
        print(f"  seeded with {pre.size / SAMPLE_RATE:.2f}s of pre-wake audio")

    state = RecorderState(
        min_silence_ms=TURN_END_SILENCE_MS,
        on_audio=transcriber.feed,
    )
    turn_start = time.monotonic()
    deadline = turn_start + TURN_MAX_SEC
    incomplete_extensions = 0
    hold_until = 0.0

    def _commit(text: str) -> dict:
        """Pick between Stage 2 routing and a raw passthrough dict."""
        if not skip_routing:
            return _route(text)
        stripped = _strip_wake_prefix(text).rstrip(" .,!?")
        return {
            "status": "raw",
            "cleaned_text": stripped,
            "transcript": text,
            "intent": "raw",
            "agent": "none",
            "confirmation": "",
            "clarification": "",
        }

    try:
        with open_stream(state):
            play_chime()
            print("  [speak now]")
            if not state.wait_for_speech(first_wait):
                print(f"  no speech detected within {first_wait:.1f}s")
                return None
            state.clear_speech_event()

            while True:
                now = time.monotonic()
                if now >= deadline:
                    print("  hard timeout")
                    break

                if not state.wait_for_silence(timeout=deadline - now):
                    print("  hard timeout while user still talking")
                    break
                state.clear_silence_event()

                t_stt = time.monotonic()
                text = transcriber.transcribe()
                print(f"  stt: {(time.monotonic() - t_stt) * 1000:.0f}ms  partial: {text!r}")

                if not text:
                    if not _wait_with_backchannel(state, 2.0, "thinking", deadline):
                        return None
                    state.clear_speech_event()
                    continue

                # Hold: user explicitly asked for a pause. Wait it out.
                if _has_hold(text):
                    hold_until = max(hold_until, time.monotonic() + HOLD_SUPPRESS_SEC)
                    print(f"  hold trigger -> suppressing stage 1 for {HOLD_SUPPRESS_SEC:.0f}s")
                    if not _wait_with_backchannel(state, HOLD_SUPPRESS_SEC, "composing", deadline):
                        # Hold ran out with no continuation; commit.
                        return _commit(text)
                    state.clear_speech_event()
                    continue

                # Mode-adaptive silence extension.
                mode, total_silence_sec = detect_mode(text)
                base_silence_sec = TURN_END_SILENCE_MS / 1000.0
                extra = max(0.0, total_silence_sec - base_silence_sec)
                print(f"  mode={mode}  total_silence={total_silence_sec:.1f}s  extra_wait={extra:.1f}s")

                if extra > 0:
                    if _wait_with_backchannel(state, extra, mode, deadline):
                        state.clear_speech_event()
                        continue  # user resumed -> loop fetches fresh text on next silence
                    # extra wait fully elapsed with no new speech -> proceed with `text` as-is

                # Terminator override.
                cleaned, has_terminator = _strip_terminator(text)
                if has_terminator:
                    print("  terminator phrase -> ending turn")
                    if not cleaned:
                        return None
                    return _commit(cleaned)

                # Now ask Stage 1 / route.
                t_s1 = time.monotonic()
                is_complete = check_turn_complete(text)
                print(f"  stage 1: {(time.monotonic() - t_s1) * 1000:.0f}ms -> "
                      f"{'COMPLETE' if is_complete else 'INCOMPLETE'}")

                if is_complete:
                    return _commit(text)

                incomplete_extensions += 1
                if incomplete_extensions > TURN_MAX_INCOMPLETE_EXTENSIONS:
                    print("  max extensions reached -> committing")
                    return _commit(text)

                print(f"  stage 1 incomplete -> extending {TURN_EXTENSION_SEC}s")
                if not _wait_with_backchannel(state, TURN_EXTENSION_SEC, "thinking", deadline):
                    print("  user did not continue -> committing")
                    return _commit(text)
                state.clear_speech_event()

            # Hard-timeout fall-through.
            final = transcriber.transcribe()
            if not final:
                return None
            cleaned, _ = _strip_terminator(final)
            return _commit(cleaned or final)
    finally:
        try:
            transcriber.stop()
        except Exception:
            pass
