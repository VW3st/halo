"""Verify vocative dispatch + the routing-priority pipeline."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from halo import __main__ as m


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'OK' if ok else 'FAIL'}] {label}{(' — ' + detail) if detail else ''}")


def main() -> None:
    print("=== Vocative dispatch ===")
    cases = [
        # ("input", expected_agent, expected_text)
        ("Claude, build me a landing page",
         "claude_code", "build me a landing page"),
        ("Codex, refactor the auth module",
         "codex_cli", "refactor the auth module"),
        ("Hey Claude, write hello world please",
         "claude_code", "write hello world please"),
        ("OK Codex, run the tests",
         "codex_cli", "run the tests"),
        ("Claude code, summarize this file",
         "claude_code", "summarize this file"),
        ("codex: list files",
         "codex_cli", "list files"),

        # Negatives — no comma after name = not vocative
        ("claude is broken", None, ""),
        ("codex refactor this", None, ""),
        ("ask claude to build a thing", None, ""),
        ("open chrome", None, ""),
        ("", None, ""),
        # Trailing-fluff-only after name — not enough to dispatch
        ("Claude,", None, ""),
        ("Codex, please", "codex_cli", "please"),  # debatable; we keep it
    ]
    for text, exp_agent, exp_text in cases:
        agent, inst = m._vocative_dispatch(text)
        ok = (agent == exp_agent) and (inst == exp_text)
        check(f"{text!r:50} -> ({agent}, {inst!r})", ok,
              f"expected ({exp_agent}, {exp_text!r})")


if __name__ == "__main__":
    main()
