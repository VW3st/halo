"""Verify the voice-mode pipeline plumbing: sanitizer, mode switches, names."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from halo import __main__ as m
from halo.agents import reset_session, session_name
from halo.voice import _clean_for_speech


def main() -> None:
    print("=== TTS sanitizer ===")
    ugly = (
        "Created at `D:\\Halo\\index.html` — a single-file page.\n\n"
        "**Design:** parchment cream, *Claude-orange* terracotta.\n\n"
        "- Animated marquee\n"
        "- Hero with **staggered** line-rise\n"
        "- Drop-cap intro\n\n"
        "```python\nprint('hello')\n```\n\n"
        "[Link text](https://example.com) too."
    )
    print("INPUT:")
    print(ugly)
    print("\nOUTPUT:")
    print(_clean_for_speech(ugly))

    print("\n=== Switch-to-agent detection ===")
    for t in [
        "switch to claude", "talk to codex", "use codex",
        "put me on claude", "open chrome",
    ]:
        print(f"  {t!r:30} -> {m._agent_switch_target(t)}")

    print("\n=== Back-to-halo detection ===")
    for t in [
        "back to halo", "halo back", "stop talking to claude",
        "exit codex", "talk to me", "open chrome",
    ]:
        print(f"  {t!r:35} -> {m._wants_back_to_halo(t)}")

    print("\n=== Mythology names ===")
    print(f"claude session 1: {session_name('claude_code')}")
    print(f"codex session 1:  {session_name('codex_cli')}")
    reset_session("claude_code")
    print(f"claude after reset: {session_name('claude_code')}")


if __name__ == "__main__":
    main()
