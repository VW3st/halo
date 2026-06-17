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
    BARGE_IN_CAPTURE_MIN_WORDS,
    HOLD_SUPPRESS_SEC,
    MODE_SILENCE_SEC,
    SAMPLE_RATE,
    TURN_CONTINUATION,
    TURN_CONTINUATION_GRACE_SEC,
    TURN_END_SILENCE_MS,
    TURN_EXTENSION_SEC,
    TURN_MAX_HARD_SEC,
    TURN_MAX_INCOMPLETE_EXTENSIONS,
    TURN_MAX_SEC,
    TURN_MAX_WAIT_FOR_FIRST_SPEECH_SEC,
)
from halo import bus
from halo.record import (
    RecorderState,
    SPEECH_RMS_FLOOR,
    open_stream,
    play_backchannel,
    play_chime,
)
from halo.router import check_turn_complete, understand_and_route
from halo.stt import BatchTranscriber
from halo.tools import is_pure_tool
from halo.wake import get_pre_wake_audio

# --- end-of-turn override phrases ---------------------------------------
# Anchored to end-of-utterance so "send the email" / "go to the gym"
# don't accidentally fire mid-thought. The user can naturally trail off
# and let silence commit the turn (mode-adaptive 0.8-4 s), or explicitly
# punctuate with any of these phrases.
TERMINATORS_RE = re.compile(
    r"\b("
    r"send it|do it|go|"
    r"start now|please go|let'?s go|let'?s do it|"
    r"fire it off|ship it|run it|execute"
    r")\s*[.!?,]*\s*$",
    re.IGNORECASE,
)
HOLD_RE = re.compile(
    r"\b(wait|hold on|hold up|give me (?:a sec|a moment|a minute)|one moment|"
    r"one sec|just a sec|just a moment)\s*[.!?,]*\s*$",
    re.IGNORECASE,
)

BARGE_IN_RE = re.compile(
    r"^\s*(?:"
    r"stop|stop talking|stop speaking|quiet|be quiet|shut up|"
    r"wait|hold on|pause|hang on|"
    r"no|nope|cancel|never mind|nevermind|"
    r"back to halo|halo stop|halo wait"
    r")\s*[.!?,]*\s*$",
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


# Dangling words to peel off a transcript the user ABANDONED mid-sentence
# ("...build a landing page and", "...refactor the"). Deliberately
# conjunctions / articles / possessives ONLY — NOT prepositions like
# for/to/in/of, because those legitimately end real questions
# ("what is it for", "where are you from"). Applied only on the
# incomplete-commit paths, never to a transcript Stage 1 judged complete.
_TRAILING_DANGLING_RE = re.compile(
    r"[\s,]+\b(?:and|or|but|so|then|also|plus|because|the|a|an|my|your)\b"
    r"[\s,.!?]*$",
    re.IGNORECASE,
)


def _strip_trailing_dangling(text: str) -> str:
    t = (text or "").rstrip()
    for _ in range(2):  # "...the landing page and the" -> peel up to two
        new = _TRAILING_DANGLING_RE.sub("", t).rstrip(" ,.!?")
        if not new or new == t:
            break
        t = new
    return t or text


def _has_hold(text: str) -> bool:
    return bool(HOLD_RE.search(text))


def is_barge_in_phrase(text: str) -> bool:
    """True for the deliberately tiny interrupt vocabulary accepted while
    Halo is speaking. Keep this strict; during TTS the mic can hear Halo's
    own output, so arbitrary speech must not be routed here."""
    return bool(BARGE_IN_RE.match((text or "").strip()))


def listen_for_barge_in(timeout: float = 20.0) -> str | None:
    """Listen for a short interrupt phrase while TTS is active.

    Returns the transcribed interrupt phrase, or None if Halo finishes
    speaking / timeout elapses without a trusted interrupt. This does NOT
    route arbitrary user speech; it only accepts `is_barge_in_phrase()`.
    """
    from halo.voice import is_speaking as _voice_is_speaking

    transcriber = BatchTranscriber()
    state = RecorderState(
        min_silence_ms=300,
        on_audio=transcriber.feed,
        suppress_tts_echo=False,
    )
    deadline = time.monotonic() + timeout
    try:
        with open_stream(state):
            while _voice_is_speaking() and time.monotonic() < deadline:
                if not state.wait_for_speech(timeout=0.08):
                    continue
                state.clear_speech_event()
                # Interrupt words are short; if there is no silence quickly,
                # treat it as non-interrupt audio and keep waiting.
                if not state.wait_for_silence(timeout=1.2):
                    state.clear_silence_event()
                    continue
                state.clear_silence_event()
                text = transcriber.transcribe().strip()
                if not text:
                    continue
                cleaned = _strip_wake_prefix(text).rstrip(" .,!?")
                print(f"  barge-in candidate: {cleaned!r}")
                if is_barge_in_phrase(cleaned):
                    return cleaned
                # Capture: a real, substantive interruption (not one of Halo's
                # own echoed words) stops the reply and gets routed — "capture
                # what I say even while you're talking". Live flag so an
                # echo-cancelling mic can enable it at startup.
                from halo.voice import is_barge_capture as _barge_capture_on
                if (
                    _barge_capture_on()
                    and len(cleaned.split()) >= BARGE_IN_CAPTURE_MIN_WORDS
                ):
                    from halo.voice import recently_spoke as _recently_spoke
                    if _recently_spoke(cleaned):
                        print(f"  barge-in ignored (echo of Halo): {cleaned!r}")
                    else:
                        print(f"  barge-in capture -> {cleaned!r}")
                        return cleaned
    finally:
        try:
            transcriber.stop()
        except Exception:
            pass
    return None


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
    seed_pre_wake: bool = True,
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

    # Seed only the first turn after wake with the audio captured just before
    # wake fired, so "halo open calculator" said in one breath is not lost.
    # Follow-up turns must not reuse that same buffer or the original wake
    # utterance gets re-transcribed and re-routed on every loop.
    if seed_pre_wake:
        pre = get_pre_wake_audio()
        if pre.size > 0:
            transcriber.seed(pre)

    # "Polling" mode: orchestrator passes a short first_wait (<3s) when
    # we're just checking for a follow-up between agent-job ticks. We
    # suppress the per-iteration `[speak now]` / `no speech detected`
    # chatter in that case — those messages flood the terminal during
    # direct dialogue while waiting for the user's next turn.
    polling = first_wait < 3.0

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
        # Ensure Halo has finished speaking BEFORE the mic opens, so the
        # chime / "I'm here" / a prior reply isn't captured and the
        # first-speech clock starts when the user can actually talk. (Only
        # for real turns; polling follow-ups must stay snappy.)
        if not polling:
            try:
                from halo.voice import wait_until_silent as _wait_until_silent
                _wait_until_silent(timeout=20.0)
            except Exception:
                pass
            # Chime before opening the stream so the beep isn't recorded.
            play_chime()
        with open_stream(state):
            if not polling:
                print("  [speak now]")
            bus.emit("record.opened")
            if not state.wait_for_speech(first_wait):
                if not polling:
                    # VAD can under-score a quiet/AGC-less mic. If the
                    # transcriber captured words anyway, use them instead of
                    # dropping the whole turn.
                    fallback = transcriber.transcribe()
                    # Only trust the no-VAD fallback if the buffer actually
                    # had speech-level energy. Otherwise Whisper hallucinated
                    # words from room tone and we'd dispatch a phantom turn.
                    peak = transcriber.peak_rms()
                    if fallback and fallback.strip() and peak >= SPEECH_RMS_FLOOR:
                        print(f"  [no VAD start, captured anyway] {fallback!r} "
                              f"(peak rms {peak:.3f})")
                        cleaned, _ = _strip_terminator(fallback)
                        return _commit(cleaned or fallback)
                    if fallback and fallback.strip():
                        print(f"  [no VAD start] discarded low-energy "
                              f"{fallback!r} (peak rms {peak:.3f} < "
                              f"{SPEECH_RMS_FLOOR})")
                    print(f"  no speech detected within {first_wait:.1f}s")
                return None
            state.clear_speech_event()

            while True:
                now = time.monotonic()
                if now >= deadline:
                    print("  hard timeout")
                    break

                if not state.wait_for_silence(timeout=deadline - now):
                    # User is STILL talking at the soft cap. Don't chop them off
                    # mid-sentence — extend the window in grace chunks up to the
                    # hard ceiling so a long thought finishes ("never lose the
                    # mic"). Only the continuously-speaking path extends; a
                    # paused turn already committed above, so the run-on pile-up
                    # the 14s cap guards against can't come back.
                    if (
                        TURN_CONTINUATION
                        and (time.monotonic() - turn_start) < TURN_MAX_HARD_SEC
                    ):
                        deadline = min(
                            turn_start + TURN_MAX_HARD_SEC,
                            time.monotonic() + TURN_CONTINUATION_GRACE_SEC,
                        )
                        print("  still talking at soft cap -> extending the turn")
                        bus.emit("turn.continuation")
                        continue
                    print("  hard timeout while user still talking")
                    break
                state.clear_silence_event()
                bus.emit("record.silence")

                t_stt = time.monotonic()
                text = transcriber.transcribe()
                ms = int((time.monotonic() - t_stt) * 1000)
                print(f"  stt: {ms}ms  partial: {text!r}")
                bus.emit("stt.done", ms=ms, text=text)

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
                    # Abandoned mid-sentence — peel a dangling conjunction/
                    # article so we don't route "...build a landing page and".
                    return _commit(_strip_trailing_dangling(text))
                state.clear_speech_event()

            # Hard-timeout fall-through.
            final = transcriber.transcribe()
            if not final:
                return None
            cleaned, _ = _strip_terminator(final)
            return _commit(_strip_trailing_dangling(cleaned or final))
    finally:
        try:
            transcriber.stop()
        except Exception:
            pass
