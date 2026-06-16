"""Unit tests for the strict barge-in interrupt vocabulary."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from halo.turn import is_barge_in_phrase


def check(text: str, expected: bool) -> int:
    got = is_barge_in_phrase(text)
    ok = got == expected
    marker = "OK" if ok else "FAIL"
    print(f"  [{marker}] {text!r:28} -> {got}")
    return 0 if ok else 1


def main() -> int:
    failed = 0
    failed += check("stop", True)
    failed += check("wait", True)
    failed += check("no", True)
    failed += check("cancel", True)
    failed += check("back to halo", True)
    failed += check("stop the server", False)
    failed += check("make it bigger", False)
    failed += check("what did Claude say", False)
    print(f"\n{8 - failed}/8 passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
