"""Unit test for Halo's short-lived voice conversation brief."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from halo.__main__ import _ConversationBrief


def check(label: str, ok: bool, detail: str = "") -> int:
    marker = "OK" if ok else "FAIL"
    print(f"  [{marker}] {label}")
    if not ok and detail:
        print(f"       {detail}")
    return 0 if ok else 1


def main() -> int:
    failed = 0
    # Default window (30 turns) — the brief stores chitchat too now (it feeds
    # render_history for the chat brain) and filters it at RENDER time, so a
    # tiny window would let chitchat evict real constraints. Production uses
    # the default; this tests the render_for_agent filtering.
    brief = _ConversationBrief()
    brief.add_user("I want a quiet dashboard, not a marketing page")
    brief.add_user("Use compact panels and keep it readable on mobile")
    brief.add_user("Can you hear me?")
    brief.add_user("let's go")
    rendered = brief.render_for_agent("Build the first pass with Claude")

    failed += check(
        "includes useful prior constraint",
        "quiet dashboard" in rendered,
        rendered,
    )
    failed += check(
        "includes second useful prior constraint",
        "compact panels" in rendered,
        rendered,
    )
    failed += check(
        "filters chitchat",
        "Can you hear me" not in rendered,
        rendered,
    )
    failed += check(
        "filters bare launch phrase",
        "let's go" not in rendered.lower(),
        rendered,
    )
    failed += check(
        "includes current request once",
        rendered.count("Build the first pass with Claude") == 1,
        rendered,
    )
    print(f"\n{5 - failed}/5 passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
