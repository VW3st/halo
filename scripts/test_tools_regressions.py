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
)


def check(label: str, ok: bool) -> int:
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
        execute_system_intent("say we are live") == (True, "we are live"),
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
        execute_system_intent("what is the date and time in Brisbane") == (False, ""),
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
    total = 23
    print(f"\n{total - failed}/{total} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
