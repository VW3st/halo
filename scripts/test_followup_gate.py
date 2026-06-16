"""Unit tests for direct-dialogue follow-up gating.

This intentionally avoids importing halo.__main__ / audio modules so it can
run without VAD, STT, TTS, or a microphone.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from halo.followup_gate import passes


def check(text: str, expected: bool, reason: str | None = None) -> int:
    got, got_reason = passes(text, "claude_code")
    ok = got == expected and (reason is None or got_reason == reason)
    marker = "OK" if ok else "FAIL"
    suffix = f" expected ({expected}, {reason}), got ({got}, {got_reason})"
    print(f"  [{marker}] {text!r:34} -> ({got}, {got_reason})")
    if not ok:
        print(f"       {suffix}")
    return 0 if ok else 1


def main() -> int:
    failed = 0
    failed += check("now also add tests", True, "continuation")
    failed += check("make it bigger", True, "coding_intent")
    failed += check("move that to the top", True, "coding_intent")
    failed += check("yes do that", True, "continuation")
    failed += check("the other one", True, "continuation")
    failed += check("let's add tests", True, "coding_intent")
    failed += check("let's go", False, "no_signal")
    failed += check("can you hear me?", False, "side_conversation")
    failed += check("want to grab lunch", False, "side_conversation")
    failed += check("the weather is nice", False, "no_signal")
    print(f"\n{10 - failed}/10 passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
