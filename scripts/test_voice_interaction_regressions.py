"""Regression tests for awkward live voice interactions."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from halo import __main__ as m


def check(label: str, ok: bool, detail: str = "") -> int:
    marker = "OK" if ok else "FAIL"
    print(f"  [{marker}] {label}")
    if not ok and detail:
        print(f"       {detail}")
    return 0 if ok else 1


def main() -> int:
    failed = 0
    failed += check(
        "strips hello plus wake-tail mishearing",
        m._strip_leading_greeting("Hello, Waika. What is your name?") == "What is your name?",
    )
    failed += check(
        "answers identity locally",
        m._chitchat_reply("what is your name") in {"I'm Halo.", "My name is Halo."},
    )
    failed += check(
        "answers how-are-you locally",
        m._chitchat_reply("how are you today") in {"Good. What's up?", "All good. What do you need?"},
    )
    failed += check(
        "answers prefixed how-are-you locally",
        m._chitchat_reply("I'm just asking, how are you today")
        in {"Good. What's up?", "All good. What do you need?"},
    )
    failed += check(
        "answers wanted-to-say-hi locally",
        m._chitchat_reply("I just wanted to say hi")
        in {"Hi. What can I do for you?", "Hey. Ready when you are.", "Hi there."},
    )
    failed += check(
        "strips aye greeting",
        m._strip_leading_greeting("Aye, are you good?") == "are you good?",
    )
    failed += check(
        "answers are-you-good locally",
        m._chitchat_reply("are you good")
        in {"Good. What's up?", "All good. What do you need?"},
    )
    failed += check(
        "smart enough date question is not end phrase",
        not m._is_end_phrase("Are you smart enough to tell me the date and time"),
    )
    failed += check(
        "bare bye ends conversation",
        m._is_end_phrase("Bye"),
    )
    failed += check(
        "that's enough remains end phrase",
        m._is_end_phrase("that's enough"),
    )
    total = 10
    print(f"\n{total - failed}/{total} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
