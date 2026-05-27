"""Tests that halo/agents.py keys session state by (agent, cwd) so
multiple projects can each run their own Claude/Codex in parallel.

Doesn't actually spawn agents (subprocesses are not faked); just checks
the in-process state-keying logic — session names, session_status,
session_key, reset behaviour.

Run: python scripts/test_agents_multicwd.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from halo import agents


def assert_eq(actual, expected, msg=""):
    if actual != expected:
        raise AssertionError(f"{msg}\n  expected: {expected!r}\n  actual:   {actual!r}")


def assert_ne(actual, expected, msg=""):
    if actual == expected:
        raise AssertionError(f"{msg}\n  expected NOT: {expected!r}\n  actual: {actual!r}")


def _reset_module_state():
    agents._sessions_active.clear()
    agents._session_names.clear()
    agents._last_by_session.clear()


def test_session_key_normalizes():
    a = agents.session_key("claude_code", "D:\\Halo")
    b = agents.session_key("claude_code", Path("D:\\Halo"))
    assert_eq(a, b, "Path and str must hash the same")


def test_session_name_per_cwd():
    _reset_module_state()
    n1 = agents.session_name("claude_code", "D:\\Halo")
    n2 = agents.session_name("claude_code", "D:\\website")
    # Should be different (likely — name pool is random; check inequality of keys)
    assert n1 in agents._MYTHOLOGY_NAMES
    assert n2 in agents._MYTHOLOGY_NAMES
    # Same cwd returns same name twice.
    assert_eq(agents.session_name("claude_code", "D:\\Halo"), n1)
    # Two separate state entries:
    assert len([k for k in agents._session_names if k.startswith("claude_code@")]) == 2


def test_session_status_aggregates():
    _reset_module_state()
    agents._sessions_active[agents.session_key("claude_code", "D:\\Halo")] = True
    agents._sessions_active[agents.session_key("claude_code", "D:\\web")] = False
    agents._sessions_active[agents.session_key("codex_cli", "D:\\Halo")] = True
    aggregate = agents.session_status()
    assert_eq(aggregate.get("claude_code"), True, "claude active in at least one cwd")
    assert_eq(aggregate.get("codex_cli"), True, "codex active")
    detail = agents.session_status_detail()
    assert len(detail) == 3, f"expected 3 detail entries, got {detail}"


def test_reset_session_all():
    _reset_module_state()
    agents._sessions_active[agents.session_key("claude_code", "D:\\Halo")] = True
    agents._sessions_active[agents.session_key("claude_code", "D:\\web")] = True
    # Patch sessions.close so it's a no-op (no real processes to kill).
    from halo import sessions
    original = sessions.close
    sessions.close = lambda *a, **kw: None
    try:
        agents.reset_session()
    finally:
        sessions.close = original
    for v in agents._sessions_active.values():
        assert v is False, "all sessions should be marked inactive"


def test_reset_session_by_agent():
    _reset_module_state()
    agents._sessions_active[agents.session_key("claude_code", "D:\\Halo")] = True
    agents._sessions_active[agents.session_key("claude_code", "D:\\web")] = True
    agents._sessions_active[agents.session_key("codex_cli", "D:\\Halo")] = True
    from halo import sessions
    original = sessions.close
    sessions.close = lambda *a, **kw: None
    try:
        agents.reset_session("claude_code")
    finally:
        sessions.close = original
    # All claude_code @ * should be False; codex should still be True.
    for k, v in agents._sessions_active.items():
        if k.startswith("claude_code@"):
            assert v is False, f"claude {k} should be reset"
        elif k.startswith("codex_cli@"):
            assert v is True, f"codex {k} should be untouched"


def test_reset_session_by_agent_and_cwd():
    _reset_module_state()
    agents._sessions_active[agents.session_key("claude_code", "D:\\Halo")] = True
    agents._sessions_active[agents.session_key("claude_code", "D:\\web")] = True
    from halo import sessions
    original = sessions.close
    sessions.close = lambda *a, **kw: None
    try:
        agents.reset_session("claude_code", "D:\\Halo")
    finally:
        sessions.close = original
    assert agents._sessions_active[agents.session_key("claude_code", "D:\\Halo")] is False
    assert agents._sessions_active[agents.session_key("claude_code", "D:\\web")] is True


def test_last_result_for_finds_latest_across_cwds():
    _reset_module_state()
    job1 = agents.AgentJob(
        job_id=1, agent_key="claude_code", prompt="hi",
        started_at=100.0, cwd="D:\\Halo",
        completed_at=110.0, ok=True, result="halo done",
    )
    job2 = agents.AgentJob(
        job_id=2, agent_key="claude_code", prompt="hi",
        started_at=200.0, cwd="D:\\web",
        completed_at=210.0, ok=True, result="web done",
    )
    agents._last_by_session[job1.session_key] = job1
    agents._last_by_session[job2.session_key] = job2

    # No cwd arg → most recent across all cwds (job2).
    assert_eq(agents.last_result_for("claude_code").job_id, 2)
    # cwd arg → scoped.
    assert_eq(agents.last_result_for("claude_code", "D:\\Halo").job_id, 1)
    assert_eq(agents.last_result_for("claude_code", "D:\\web").job_id, 2)


# ---------------------------------------------------------------------------

def main() -> int:
    tests = [
        test_session_key_normalizes,
        test_session_name_per_cwd,
        test_session_status_aggregates,
        test_reset_session_all,
        test_reset_session_by_agent,
        test_reset_session_by_agent_and_cwd,
        test_last_result_for_finds_latest_across_cwds,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as exc:
            print(f"  FAIL  {t.__name__}\n    {exc}")
            failed += 1
        except Exception as exc:
            print(f"  ERR   {t.__name__}\n    {type(exc).__name__}: {exc}")
            failed += 1
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
