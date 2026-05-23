"""Process-wide event bus for the web dashboard.

Every interesting moment in Halo's lifecycle (wake fired, transcript
emitted, routing decision made, agent job started/streamed/finished,
TTS spoke a sentence) gets `bus.emit("kind", ...)`-ed into a thread-safe
ring buffer. The web layer polls `events_since(seq)` and ships them to
the browser.

Zero-cost when nobody is subscribed — the ring just rotates.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any

# Hold the last N events. 2000 is enough for a 30-minute conversation.
_MAX_EVENTS = 2000

_lock = threading.Lock()
_events: deque[dict[str, Any]] = deque(maxlen=_MAX_EVENTS)
_seq = 0


def emit(kind: str, **data: Any) -> None:
    """Publish an event. `kind` is a short slug like 'wake.fired'."""
    global _seq
    with _lock:
        _seq += 1
        evt = {"seq": _seq, "ts": time.time(), "kind": kind}
        evt.update(data)
        _events.append(evt)


def events_since(since: int = 0, limit: int | None = None) -> list[dict[str, Any]]:
    """All events with seq > since, oldest first."""
    with _lock:
        out = [e for e in _events if e["seq"] > since]
    if limit is not None and len(out) > limit:
        out = out[-limit:]
    return out


def current_seq() -> int:
    with _lock:
        return _seq


def reset() -> None:
    """Test helper — clear the buffer."""
    global _seq
    with _lock:
        _events.clear()
        _seq = 0
