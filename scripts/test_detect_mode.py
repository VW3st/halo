"""Unit tests for halo.turn.detect_mode().

Mirrors the six adaptive-turn-taking test cases from the step 3.5 spec.
Voice timing has to be tested live, but the classification logic can
be verified deterministically.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from halo.turn import detect_mode

CASES = [
    # (transcript, expected_mode, label)
    ("open Chrome", "snappy", "short command"),
    ("build me a website", "snappy", "no trailing connector, short"),
    ("build me a website with nice colors and", "composing", "trailing 'and'"),
    ("build me a website um let me think with nice colors", "thinking", "thinking marker"),
    ("open Chrome wait actually never mind", "thinking", "'wait' marker"),
    ("make a complicated thing with X and Y and also Z and then open browser",
     "thinking", "long multi-clause"),
    ("", "snappy", "empty transcript"),
    ("cancel", "snappy", "single word"),
    ("I want to refactor the", "composing", "trailing 'the'"),
    ("ok so what we need to do is build a new dashboard and add filters",
     "thinking", "long with connectors"),
]


def main() -> None:
    width = max(len(c[0]) for c in CASES) + 2
    passed = 0
    failed = []
    for text, expected, label in CASES:
        mode, silence = detect_mode(text)
        ok = mode == expected
        passed += int(ok)
        if not ok:
            failed.append((text, expected, mode))
        marker = "OK" if ok else "FAIL"
        print(f"  [{marker}] {text!r:<{width}} -> mode={mode!s:<10} silence={silence:.1f}s  ({label})")
    print(f"\n{passed}/{len(CASES)} cases pass")
    if failed:
        print("\nFailures:")
        for text, expected, got in failed:
            print(f"  {text!r}: expected {expected}, got {got}")
        sys.exit(1)


if __name__ == "__main__":
    main()
