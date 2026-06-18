"""Unit tests for barge-in: the strict interrupt vocabulary AND the layered
accept/reject policy (`_barge_in_decision`) that keeps Halo from interrupting
itself when the mic hears its own voice (no hardware echo cancellation)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from halo.turn import _barge_in_decision, is_barge_in_phrase

MIN_WORDS = 4
FLOOR = 0.045  # default HALO_BARGE_IN_MIN_RMS


def check(text: str, expected: bool) -> int:
    got = is_barge_in_phrase(text)
    ok = got == expected
    marker = "OK" if ok else "FAIL"
    print(f"  [{marker}] {text!r:28} -> {got}")
    return 0 if ok else 1


def dcheck(label: str, expected, **kw) -> int:
    kw.setdefault("min_words", MIN_WORDS)
    kw.setdefault("min_rms", FLOOR)
    got = _barge_in_decision(**kw)
    ok = got == expected
    marker = "OK" if ok else "FAIL"
    print(f"  [{marker}] {label:42} -> {got!r} (want {expected!r})")
    return 0 if ok else 1


def main() -> int:
    failed = 0

    print("interrupt vocabulary:")
    failed += check("stop", True)
    failed += check("wait", True)
    failed += check("no", True)
    failed += check("cancel", True)
    failed += check("back to halo", True)
    failed += check("stop the server", False)
    failed += check("make it bigger", False)
    failed += check("what did Claude say", False)

    print("\ninterrupt-phrase path (must always be able to stop Halo):")
    # Loud "stop" -> honored.
    failed += dcheck(
        "loud 'stop', not echo", "stop",
        cleaned="stop", peak_rms=0.20, capture_on=True, is_phrase=True, is_echo=False,
    )
    # Quiet "stop" that is NOT an echo -> still honored (user spoke softly).
    failed += dcheck(
        "quiet 'stop', not echo", "stop",
        cleaned="stop", peak_rms=0.010, capture_on=True, is_phrase=True, is_echo=False,
    )
    # Quiet "stop" that overlaps what Halo just said -> Halo hearing itself, drop.
    failed += dcheck(
        "quiet 'stop', echo of Halo", None,
        cleaned="stop", peak_rms=0.010, capture_on=True, is_phrase=True, is_echo=True,
    )
    # Loud "stop" that overlaps Halo's words -> user repeating it, honor (loud).
    failed += dcheck(
        "loud 'stop', echo of Halo", "stop",
        cleaned="stop", peak_rms=0.20, capture_on=True, is_phrase=True, is_echo=True,
    )
    # Interrupt vocab works even when capture is disabled.
    failed += dcheck(
        "'wait' with capture OFF", "stop",
        cleaned="wait", peak_rms=0.20, capture_on=False, is_phrase=True, is_echo=False,
    )

    print("\ncapture path (substantive interruption -> reroute):")
    # Loud, long, novel -> capture.
    failed += dcheck(
        "loud + long + novel", "capture",
        cleaned="switch over to codex now", peak_rms=0.18,
        capture_on=True, is_phrase=False, is_echo=False,
    )
    # Loudness exactly at the floor counts as loud (>=).
    failed += dcheck(
        "peak == floor counts as loud", "capture",
        cleaned="switch over to codex now", peak_rms=FLOOR,
        capture_on=True, is_phrase=False, is_echo=False,
    )
    # Quiet (speaker bleed) -> reject even if novel & long.
    failed += dcheck(
        "quiet bleed rejected", None,
        cleaned="switch over to codex now", peak_rms=0.020,
        capture_on=True, is_phrase=False, is_echo=False,
    )
    # Loud but overlaps Halo's words -> echo, reject.
    failed += dcheck(
        "loud but echo of Halo", None,
        cleaned="switch over to codex now", peak_rms=0.18,
        capture_on=True, is_phrase=False, is_echo=True,
    )
    # Loud, novel, but too short -> reject (blip).
    failed += dcheck(
        "loud but too short (3 words)", None,
        cleaned="do that now", peak_rms=0.18,
        capture_on=True, is_phrase=False, is_echo=False,
    )
    # Capture disabled -> a substantive utterance is never routed.
    failed += dcheck(
        "capture disabled", None,
        cleaned="switch over to codex now", peak_rms=0.18,
        capture_on=False, is_phrase=False, is_echo=False,
    )

    total = 8 + 11
    print(f"\n{total - failed}/{total} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
