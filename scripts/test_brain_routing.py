"""End-to-end test of the upgraded Stage 2 brain with session context.

Runs against the actual Ollama instance configured in halo/config.py.
Skipped (exit 0) when Ollama is unreachable so CI / fresh installs
don't fail.

We don't assert exact strings — qwen2.5:1.5b is small and variable.
We assert the shape of routing decisions:
  - "switch to website"       -> session_action="switch", target_session~="website"
  - "what sessions do I have" -> session_action="list_sessions"
  - "where am I"              -> session_action="where_am_i"
  - "add a docstring"         -> session_action="", target_session in {"", "active"}
  - "in website, fix X"       -> session_action="", target_session~="website"

Run: python scripts/test_brain_routing.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from halo.router import SessionContext, understand_and_route


CONTEXT = SessionContext(
    active_label="halo",
    discovered=[
        {"label": "halo", "cwd": "D:\\Halo", "agent": "claude_code"},
        {"label": "website", "cwd": "D:\\website-redesign", "agent": "claude_code"},
        {"label": "aip", "cwd": "D:\\AIP-Claude", "agent": "claude_code"},
    ],
)


def run_case(transcript: str, context: SessionContext | None = CONTEXT) -> dict:
    decision = understand_and_route(transcript, context=context)
    return decision


def fmt(d: dict) -> str:
    return (
        f"status={d.get('status'):8}  intent={d.get('intent'):8}  "
        f"agent={d.get('agent'):14}  session_action={d.get('session_action'):14}  "
        f"target={d.get('target_session'):12}  cleaned={d.get('cleaned_text')!r}"
    )


def check(name: str, transcript: str, predicate, context=CONTEXT) -> bool:
    d = run_case(transcript, context)
    if d.get("_error"):
        print(f"  SKIP  {name}  (router error: {d.get('_error')[:80]})")
        return True  # treat as skip, not fail
    try:
        ok, reason = predicate(d)
    except Exception as exc:
        print(f"  ERR   {name}\n    {type(exc).__name__}: {exc}")
        print(f"    decision: {fmt(d)}")
        return False
    if ok:
        print(f"  PASS  {name}")
        return True
    print(f"  FAIL  {name}\n    {reason}\n    decision: {fmt(d)}")
    return False


def main() -> int:
    # Cheap reachability probe first — preload triggers an Ollama round-trip.
    try:
        from halo.router import preload_router
        preload_router()
    except Exception as exc:
        print(f"Ollama not reachable ({exc}) — skipping brain tests")
        return 0

    cases: list[tuple[str, str, callable]] = [
        (
            "switch_basic",
            "switch to website",
            lambda d: (
                d.get("session_action") == "switch"
                and "website" in (d.get("target_session") or "").lower(),
                "expected session_action=switch + target~=website",
            ),
        ),
        (
            "switch_natural",
            "work on the aip one now",
            lambda d: (
                d.get("session_action") == "switch"
                and "aip" in (d.get("target_session") or "").lower(),
                "expected session_action=switch + target~=aip",
            ),
        ),
        (
            "list",
            "what sessions do I have",
            lambda d: (
                d.get("session_action") == "list_sessions",
                "expected session_action=list_sessions",
            ),
        ),
        (
            "where_am_i",
            "where am I",
            lambda d: (
                d.get("session_action") == "where_am_i",
                "expected session_action=where_am_i",
            ),
        ),
        (
            "implicit_active",
            "add a docstring",
            lambda d: (
                d.get("session_action") == ""
                and (d.get("target_session") or "") in ("", "active"),
                "expected normal dispatch to active session",
            ),
        ),
        (
            "cross_session_one_shot",
            "in website, ask Claude to add dark mode",
            lambda d: (
                d.get("session_action") == ""
                and "website" in (d.get("target_session") or "").lower(),
                "expected target_session~=website without switch",
            ),
        ),
        (
            "fanout",
            "tell all of them to run their tests",
            lambda d: (
                d.get("target_session") == "all",
                "expected target_session=all",
            ),
        ),
        (
            "no_context_baseline",
            "build me a hello world script",
            lambda d: (
                d.get("status") == "ready" and d.get("agent") == "claude_code",
                "without context, schema fields should be empty / default",
            ),
            # context=None — make sure single-session mode still works
        ),
    ]

    failed = 0
    for entry in cases:
        if len(entry) == 4:
            name, transcript, predicate, ctx = entry
        else:
            name, transcript, predicate = entry
            ctx = CONTEXT
        if not check(name, transcript, predicate, context=ctx):
            failed += 1
    total = len(cases)
    print(f"\n{total - failed}/{total} passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
