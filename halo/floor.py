"""Conversational floor — who's holding it, and when Halo reclaims it.

Halo's loop is otherwise half-duplex and user-led: it waits for you to speak, then
answers. That breaks the moment Halo PROMISES a beat — "give me ten seconds",
"hold on", "let me check". It owes you a follow-up, but nothing schedules one, so
after you wait (or count to ten) Halo just sits there and never takes its turn.

This module is the missing primitive:
  - `note_halo_said(text)` watches Halo's own speech for a promise-of-delay and
    ARMS a reclaim timer for the promised duration;
  - the orchestrator calls `reclaim_due()` each loop and, when the time has
    elapsed, takes the floor back and continues instead of waiting on you.

Pure logic over a MONOTONIC clock that is injectable (`now=`) so the policy is
unit-testable without ever sleeping (see scripts/test_floor.py).
"""

from __future__ import annotations

import re
import threading
import time

# Floor owner states.
USER = "user"
AI = "ai"
CONTESTED = "contested"

_DEFAULT_DELAY = 8.0     # promise with no explicit number ("hold on")
_MIN_DELAY = 2.0
_MAX_DELAY = 60.0

_lock = threading.RLock()
_state = USER
_deadline: float | None = None   # monotonic seconds when Halo should reclaim
_reason = ""

# Spelled-out small numbers, for "give me ten seconds".
_WORD_NUM = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "twenty": 20, "thirty": 30,
    "forty": 40, "sixty": 60, "ninety": 90,
}

# "<quantity> <unit>" — give me 10 seconds / a few moments / two minutes.
_DURATION_RE = re.compile(
    r"\b(?:give me|in|wait|for|just)\s+(?:about\s+)?"
    r"(a few|a couple|a|an|\d+|"
    + "|".join(_WORD_NUM) + r")?\s*"
    r"(seconds?|secs?|minutes?|mins?|moments?)\b",
    re.IGNORECASE,
)

# Bare hold phrases with no explicit duration.
_HOLD_RE = re.compile(
    r"\b("
    r"hold on|hang on|one moment|one second|one sec|just a moment|just a sec|"
    r"just a minute|let me think|let me see|let me check|bear with me|give me a sec"
    r")\b",
    re.IGNORECASE,
)


def _clamp(seconds: float) -> float:
    return max(_MIN_DELAY, min(_MAX_DELAY, seconds))


def parse_promise(text: str) -> float | None:
    """If `text` is a promise-of-delay, return the delay in seconds; else None.

    Pure — no clock, no state. Exposed for unit tests.
    """
    if not text:
        return None
    m = _DURATION_RE.search(text)
    # "...minutes ago" is a PAST reference, not a promise of delay. Halo's own
    # time-aware replies say things like "a few minutes ago" — never arm on those.
    if m and re.match(r"\s*ago\b", text[m.end():], re.IGNORECASE):
        m = None
    if m:
        qty_raw, unit = m.group(1), m.group(2).lower()
        if unit.startswith("moment"):
            return _DEFAULT_DELAY
        if qty_raw is None:
            qty = 1.0
        else:
            q = qty_raw.lower()
            if q in ("a", "an"):
                qty = 1.0
            elif q == "a few":
                qty = 3.0
            elif q == "a couple":
                qty = 2.0
            elif q.isdigit():
                qty = float(q)
            else:
                qty = float(_WORD_NUM.get(q, 1))
        unit_sec = 60.0 if unit.startswith("min") else 1.0
        # "a second"/"a minute" alone reads as a short beat, not literally 1s.
        seconds = qty * unit_sec
        if unit_sec == 1.0 and qty <= 1.0:
            seconds = _DEFAULT_DELAY
        return _clamp(seconds)
    if _HOLD_RE.search(text):
        return _DEFAULT_DELAY
    return None


def note_halo_said(text: str, *, now: float | None = None) -> bool:
    """Called for every line Halo speaks. If it's a promise-of-delay, arm a
    reclaim timer. Returns True if a reclaim was armed."""
    delay = parse_promise(text)
    if delay is None:
        return False
    arm(delay, reason=(text or "").strip(), now=now)
    return True


def arm(seconds: float, *, reason: str = "", now: float | None = None) -> None:
    """Arm a reclaim `seconds` from now and mark the floor as Halo's."""
    global _state, _deadline, _reason
    t = time.monotonic() if now is None else now
    with _lock:
        _state = AI
        _deadline = t + _clamp(seconds)
        _reason = reason


def owes_turn() -> bool:
    """True while Halo has promised a turn it hasn't taken yet."""
    with _lock:
        return _deadline is not None


def reclaim_due(*, now: float | None = None) -> bool:
    """True when an armed reclaim's time has elapsed."""
    t = time.monotonic() if now is None else now
    with _lock:
        return _deadline is not None and t >= _deadline


def seconds_remaining(*, now: float | None = None) -> float:
    t = time.monotonic() if now is None else now
    with _lock:
        if _deadline is None:
            return 0.0
        return max(0.0, _deadline - t)


def consume() -> str:
    """Take the floor back: clear the owe, return the promise text, reset to USER."""
    global _state, _deadline, _reason
    with _lock:
        reason = _reason
        _deadline = None
        _reason = ""
        _state = USER
        return reason


def clear() -> None:
    """Drop any pending owe (e.g. on new session / sleep)."""
    global _state, _deadline, _reason
    with _lock:
        _deadline = None
        _reason = ""
        _state = USER


def set_state(state: str) -> None:
    global _state
    with _lock:
        _state = state


def state() -> str:
    with _lock:
        return _state
