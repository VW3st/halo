"""Verify the five-bug round."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from halo import __main__ as m
from halo.agents import reset_session


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'OK' if ok else 'FAIL'}] {label}{(' — ' + detail) if detail else ''}")


def main() -> None:
    print("=== Pure mode-switch detection (bug fix) ===")
    cases = [
        # Pure switches — should match
        ("switch to codex", "codex_cli"),
        ("use claude", "claude_code"),
        ("try codex please", "codex_cli"),
        ("let me talk to claude", "claude_code"),
        ("Or I switch to codex", "codex_cli"),
        ("codex,", "codex_cli"),

        # NOT switches (have instruction after) — should be None so Stage 2 routes them
        ("ask claude code to build a landing page", None),
        ("ask codex what time it is", None),
        ("codex, refactor the auth", None),
        ("claude, write hello world", None),
        ("give it to codex and tell it to test the auth flow", None),

        # Negatives
        ("open chrome", None),
        ("codex is broken", None),
    ]
    for text, expected in cases:
        got = m._agent_switch_target(text)
        check(f"{text!r:50} -> {got}", got == expected, f"expected {expected}")

    print("\n=== Status query: should NOT fire with no jobs ===")
    reset_session()
    # No jobs / no completions
    check("'whats happening' with nothing running -> falls through",
          m._is_status_query("whats happening") is False)
    check("'are you done' with nothing running -> falls through",
          m._is_status_query("are you done") is False)

    print("\n=== Empty-prompt guard ===")
    phrase = m._start_agent_and_ack("claude_code", "")
    check("_start_agent_and_ack with empty prompt returns '' (no dispatch)",
          phrase == "", f"got {phrase!r}")
    phrase = m._start_agent_and_ack("claude_code", "   ")
    check("_start_agent_and_ack with whitespace-only returns ''",
          phrase == "", f"got {phrase!r}")

    print("\n=== Wake threshold ===")
    from halo.wake import THRESHOLD
    check(f"threshold lowered to 0.5 (was 0.62) — actual={THRESHOLD}", THRESHOLD == 0.5)


if __name__ == "__main__":
    main()
