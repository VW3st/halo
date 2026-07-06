"""Unit tests for halo.floor — promise parsing + the reclaim timer, using an
injected clock so nothing sleeps."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from halo import floor


def approx(a, b, tol=0.01) -> bool:
    return a is not None and b is not None and abs(a - b) <= tol


def main() -> int:
    f = 0

    def check(label, got, want) -> None:
        nonlocal f
        ok = (approx(got, want) if isinstance(want, float) else got == want)
        f += 0 if ok else 1
        print(f"  [{'OK' if ok else 'FAIL'}] {label:38} -> {got!r} (want {want!r})")

    print("parse_promise (pure):")
    check("give me ten seconds", floor.parse_promise("give me ten seconds"), 10.0)
    check("give me 10 seconds", floor.parse_promise("give me 10 seconds"), 10.0)
    check("give me a few seconds", floor.parse_promise("give me a few seconds"), 3.0)
    check("give me thirty seconds", floor.parse_promise("give me thirty seconds"), 30.0)
    check("give me a second -> default", floor.parse_promise("give me a second"), 8.0)
    check("two minutes -> capped 60", floor.parse_promise("give me two minutes"), 60.0)
    check("in 5 seconds", floor.parse_promise("in 5 seconds"), 5.0)
    check("hold on -> default", floor.parse_promise("hold on"), 8.0)
    check("let me check -> default", floor.parse_promise("let me check"), 8.0)
    check("one moment -> default", floor.parse_promise("one moment"), 8.0)
    check("not a promise (command)", floor.parse_promise("build me a login page"), None)
    check("not a promise (question)", floor.parse_promise("what time is it"), None)
    check("PAST 'a few minutes ago' -> None", floor.parse_promise("we started just a few minutes ago"), None)
    check("PAST 'ten minutes ago' -> None", floor.parse_promise("that was ten minutes ago"), None)

    print("\narm / reclaim timer (injected clock):")
    floor.clear()
    floor.arm(10.0, reason="give me ten seconds", now=100.0)
    check("owes_turn after arm", floor.owes_turn(), True)
    check("state is AI", floor.state(), floor.AI)
    check("not due at t=105", floor.reclaim_due(now=105.0), False)
    check("remaining at t=105", floor.seconds_remaining(now=105.0), 5.0)
    check("due at t=110", floor.reclaim_due(now=110.0), True)
    reason = floor.consume()
    check("consume returns reason", reason, "give me ten seconds")
    check("owes cleared after consume", floor.owes_turn(), False)
    check("state back to USER", floor.state(), floor.USER)

    print("\nnote_halo_said wiring:")
    floor.clear()
    check("promise arms", floor.note_halo_said("give me ten seconds", now=0.0), True)
    check("owes after note", floor.owes_turn(), True)
    check("not due at t=9", floor.reclaim_due(now=9.0), False)
    check("due at t=10", floor.reclaim_due(now=10.0), True)
    floor.clear()
    check("non-promise does not arm", floor.note_halo_said("nice work, that's done", now=0.0), False)
    check("owes false", floor.owes_turn(), False)

    total = 27
    print(f"\n{total - f}/{total} passed")
    return 1 if f else 0


if __name__ == "__main__":
    raise SystemExit(main())
