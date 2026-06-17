"""Regression tests for local system tool matching."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from halo.tools import (
    _is_open_app_known,
    _open_app_target,
    _try_say,
    execute_system_intent,
    is_pure_tool,
    set_dry_run,
)

# CRITICAL: these tests call execute_system_intent("open calculator and paint"),
# which would otherwise actually launch Calculator/Paint/the browser on the
# developer's desktop. Dry-run makes the launchers no-op while still returning
# their summary strings, so we test routing without spawning anything.
set_dry_run(True)


_ran = 0


def check(label: str, ok: bool) -> int:
    global _ran
    _ran += 1
    marker = "OK" if ok else "FAIL"
    print(f"  [{marker}] {label}")
    return 0 if ok else 1


def main() -> int:
    failed = 0
    failed += check(
        "smart enough date/time is local datetime tool",
        is_pure_tool("Are you smart enough to tell me the date and time?"),
    )
    # Generic app launcher — these must resolve LOCALLY (no Claude dispatch).
    failed += check("open paint is a local tool", is_pure_tool("open paint"))
    failed += check("open spotify is a local tool", is_pure_tool("open spotify"))
    failed += check("open word is a local tool", is_pure_tool("open word"))
    failed += check(
        "open calculator and open paint both local",
        is_pure_tool("open calculator and open paint"),
    )
    failed += check(
        "open a webpage routes to browser tool", is_pure_tool("open a webpage")
    )
    # Article stripping + known-app detection.
    failed += check("target strips article", _open_app_target("open the spotify") == "spotify")
    failed += check("known app detected", _is_open_app_known("open paint"))
    failed += check("unknown app NOT a known tool", not _is_open_app_known("open frobnicator"))
    # A real coding task must NOT be treated as a local tool.
    failed += check(
        "coding task is not a local tool", not is_pure_tool("build me a login page")
    )
    # "say <text>" speaks the remainder verbatim — never goes to Claude.
    failed += check("say is a local tool", is_pure_tool("say hi to the audience"))
    failed += check(
        "say strips the verb", _try_say("say hi everyone") == (True, "hi everyone")
    )
    failed += check(
        "repeat after me works",
        _try_say("repeat after me hello world") == (True, "hello world"),
    )
    failed += check("plain question is not 'say'", _try_say("what time is it")[0] is False)
    failed += check(
        "exec say speaks remainder",
        execute_system_intent("say we are live") == (True, "we are live", ""),
    )
    # Qualified time questions must NOT be answered by the local clock.
    failed += check("plain time is local", is_pure_tool("what time is it"))
    failed += check(
        "time in Brisbane is NOT local", not is_pure_tool("what time is it in Brisbane")
    )
    failed += check(
        "date+time in London is NOT local",
        not is_pure_tool("what is the date and time in London"),
    )
    failed += check(
        "exec doesn't answer Brisbane locally",
        execute_system_intent("what is the date and time in Brisbane")[0] is False,
    )
    # Multitask: a single command naming 2+ apps must open ALL of them.
    failed += check(
        "open A in B opens both (one phrase, two tools)",
        execute_system_intent("open calculator in the browser")
        == (True, "Opened calculator.  Opened browser.", ""),
    )
    failed += check(
        "open A and B opens both (bare list item gets the verb)",
        execute_system_intent("open the calculator and paint")
        == (True, "Opened calculator.  Opened Paint.", ""),
    )
    # Chained command: do the local part, hand the agent-only part BACK as a
    # leftover instead of silently dropping it ("...and search up the score").
    failed += check(
        "chained command hands back the agent-only remainder",
        execute_system_intent("open a browser and search up the score")
        == (True, "Opened browser.", "search up the score"),
    )
    failed += check(
        "pure local multitask leaves no leftover",
        execute_system_intent("open chrome and notepad")[2] == "",
    )
    failed += check(
        "open A and B and C — all three",
        is_pure_tool("open chrome and spotify and notepad"),
    )
    # Image generation — a LOCAL action (real image model), never a coding
    # agent (which only draws SVGs). Subject survives "and" + "open it" trailers.
    from halo.tools import _image_subject
    failed += check(
        "'generate an image of a sunset' is a local tool",
        is_pure_tool("generate an image of a sunset over the ocean"),
    )
    failed += check(
        "image subject keeps 'a cat and a dog', strips 'and open it'",
        _image_subject("create an image of a cat and a dog and open it")
        == "a cat and a dog",
    )
    failed += check(
        "'make three slides' is NOT an image request",
        _image_subject("make three slides") is None,
    )
    failed += check(
        "image generate executes locally (dry-run -> ack, no API call)",
        execute_system_intent("generate an image of a robot")[0] is True,
    )
    # Politeness-wrapped tool commands must still resolve locally (not fall
    # through to the LLM, which mis-routed "open" to a session switch).
    failed += check(
        "'can you open a browser please' is a local tool",
        is_pure_tool("Can you open a browser, please"),
    )
    failed += check(
        "'I want you to open Chrome' is a local tool",
        is_pure_tool("I want you to open Chrome"),
    )
    failed += check(
        "'could you open the calculator please' is local",
        is_pure_tool("could you open the calculator please"),
    )
    failed += check(
        "polite non-tool stays non-tool",
        not is_pure_tool("can you tell me a joke"),
    )
    print(f"\n{_ran - failed}/{_ran} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
