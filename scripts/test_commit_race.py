"""Unit test for turn._user_resumed — the commit-race guard.

The race: silence fires, Whisper transcribes (0.5-1.5 s), and the turn used to
commit even if the user had already resumed talking during that window. The
guard checks the recorder's speech event right before committing; a stub state
is enough because _user_resumed only touches wait_for_speech /
clear_speech_event.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from halo.turn import _user_resumed


class _StubState:
    """Minimal RecorderState stand-in: a real threading.Event, so the
    wait(timeout)/clear semantics match production exactly."""

    def __init__(self, speaking: bool) -> None:
        self._ev = threading.Event()
        if speaking:
            self._ev.set()

    def wait_for_speech(self, timeout: float) -> bool:
        return self._ev.wait(timeout=timeout)

    def clear_speech_event(self) -> None:
        self._ev.clear()


def main() -> int:
    f = 0

    def check(label, got, want) -> None:
        nonlocal f
        ok = got == want
        f += 0 if ok else 1
        print(f"  [{'OK' if ok else 'FAIL'}] {label:52} -> {got} (want {want})")

    # User resumed during STT -> abort the commit.
    s = _StubState(speaking=True)
    check("resumed during STT -> True", _user_resumed(s, grace_sec=0.01), True)
    check("speech event cleared after detection", s._ev.is_set(), False)
    # Cleared event -> the same state now reads as silent (no double-fire).
    check("second check after clear -> False", _user_resumed(s, grace_sec=0.01), False)

    # User stayed silent -> commit proceeds.
    s2 = _StubState(speaking=False)
    check("silent user -> False", _user_resumed(s2, grace_sec=0.01), False)

    # Resume that lands DURING the grace window is still caught.
    s3 = _StubState(speaking=False)
    threading.Timer(0.05, s3._ev.set).start()
    check("resume mid-grace -> True", _user_resumed(s3, grace_sec=0.3), True)

    total = 5
    print(f"\n{total - f}/{total} passed")
    return 1 if f else 0


if __name__ == "__main__":
    raise SystemExit(main())
