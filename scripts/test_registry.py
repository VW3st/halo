"""Unit tests for halo/registry.py — session registry, fuzzy matching,
collision handling. Pure logic, no I/O. Run with: python scripts/test_registry.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make the repo root importable when running the script directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import time

from halo.discovery import DiscoveredSession
from halo.registry import SessionRegistry, ResolvedTarget


def make_session(label: str, cwd: str, agent: str = "claude_code", pid: int = 1000) -> DiscoveredSession:
    return DiscoveredSession(
        agent_key=agent,
        pid=pid,
        cwd=cwd,
        label=label,
        parent_pid=None,
        parent_name=None,
        cmdline="claude",
        seen_at=time.monotonic(),
    )


def assert_eq(actual, expected, msg=""):
    if actual != expected:
        raise AssertionError(f"{msg}\n  expected: {expected!r}\n  actual:   {actual!r}")


def test_empty_registry():
    r = SessionRegistry()
    assert_eq(r.list(), [], "empty list")
    assert_eq(r.labels(), [], "empty labels")
    assert_eq(r.active(), None, "no active")
    assert_eq(r.resolve("anything").kind, "none", "resolve on empty")
    assert_eq(r.speak_list(), "I don't see any agent sessions running on this machine.")


def test_basic_update_and_list():
    r = SessionRegistry()
    r.update([
        make_session("halo", "D:\\Halo"),
        make_session("website", "D:\\website"),
        make_session("aip", "D:\\AIP-Claude"),
    ])
    assert_eq(sorted(r.labels()), ["aip", "halo", "website"])
    assert_eq(r.by_label("halo").cwd, "D:\\Halo")


def test_collision_disambiguation():
    """Two cwds whose basenames collide should get parent-disambiguated labels."""
    r = SessionRegistry()
    r.update([
        make_session("halo", "D:\\client-a\\halo", pid=100),
        make_session("halo", "D:\\client-b\\halo", pid=101),
    ])
    labels = sorted(r.labels())
    assert len(labels) == 2, f"expected 2 disambiguated labels, got {labels}"
    assert "client-a/halo" in labels, f"missing client-a/halo: {labels}"
    assert "client-b/halo" in labels, f"missing client-b/halo: {labels}"


def test_resolve_exact():
    r = SessionRegistry()
    r.update([make_session("halo", "D:\\Halo"), make_session("website", "D:\\web")])
    res = r.resolve("halo")
    assert_eq(res.kind, "session", "exact match")
    assert_eq(res.label, "halo")


def test_resolve_case_insensitive():
    r = SessionRegistry()
    r.update([make_session("Halo", "D:\\Halo")])
    res = r.resolve("HALO")
    assert_eq(res.kind, "session")
    assert_eq(res.label, "Halo")


def test_resolve_with_filler():
    r = SessionRegistry()
    r.update([make_session("website", "D:\\web")])
    # "the website project" -> after stripping "the", "project" -> "website"
    res = r.resolve("the website project")
    assert_eq(res.kind, "session")
    assert_eq(res.label, "website")


def test_resolve_substring():
    r = SessionRegistry()
    r.update([make_session("client-a-project", "D:\\foo")])
    res = r.resolve("client-a")
    assert_eq(res.kind, "session")
    assert_eq(res.label, "client-a-project")


def test_resolve_token_overlap():
    r = SessionRegistry()
    r.update([make_session("AIP-Claude", "D:\\AIP-Claude")])
    res = r.resolve("the AIP one")
    assert_eq(res.kind, "session")
    assert_eq(res.label, "AIP-Claude")


def test_resolve_pseudo_targets():
    r = SessionRegistry()
    r.update([make_session("halo", "D:\\Halo")])
    for phrase, expected in [
        ("all of them", "all"),
        ("send to everyone", "all"),
        ("the focused one", "focused"),
        ("the active session", "active"),
        ("right here", "active"),
    ]:
        res = r.resolve(phrase)
        assert_eq(res.kind, "pseudo", f"phrase {phrase!r}")
        assert_eq(res.pseudo, expected, f"phrase {phrase!r}")


def test_resolve_no_match():
    r = SessionRegistry()
    r.update([make_session("halo", "D:\\Halo")])
    res = r.resolve("xyzzy")
    assert_eq(res.kind, "none")


def test_set_active():
    r = SessionRegistry()
    r.update([make_session("halo", "D:\\Halo"), make_session("website", "D:\\web")])
    assert r.set_active("website")
    assert_eq(r.active_label(), "website")
    assert_eq(r.active().cwd, "D:\\web")
    # Invalid label refused.
    assert not r.set_active("nonexistent")
    assert_eq(r.active_label(), "website")
    # Clear.
    assert r.set_active(None)
    assert_eq(r.active_label(), None)


def test_active_disappears_on_update():
    """If the active session is no longer in the new snapshot, active clears."""
    r = SessionRegistry()
    r.update([make_session("halo", "D:\\Halo")])
    r.set_active("halo")
    r.update([make_session("website", "D:\\web")])
    assert_eq(r.active_label(), None, "active should clear when session vanishes")


def test_speak_list():
    r = SessionRegistry()
    r.update([
        make_session("halo", "D:\\Halo"),
        make_session("website", "D:\\web"),
        make_session("aip", "D:\\AIP"),
    ])
    r.set_active("halo")
    spoken = r.speak_list()
    # Don't pin exact phrasing — just check key bits.
    assert "3 sessions" in spoken or "Three" in spoken or "3" in spoken
    assert "halo" in spoken
    assert "website" in spoken
    assert "aip" in spoken
    assert "halo" in spoken.lower()  # active mention


def test_speak_active():
    r = SessionRegistry()
    r.update([make_session("halo", "D:\\Halo")])
    assert_eq(r.speak_active(), "No session is active.")
    r.set_active("halo")
    assert "halo" in r.speak_active()
    assert "D:\\Halo" in r.speak_active() or "halo" in r.speak_active().lower()


# ---------------------------------------------------------------------------

def main() -> int:
    tests = [
        test_empty_registry,
        test_basic_update_and_list,
        test_collision_disambiguation,
        test_resolve_exact,
        test_resolve_case_insensitive,
        test_resolve_with_filler,
        test_resolve_substring,
        test_resolve_token_overlap,
        test_resolve_pseudo_targets,
        test_resolve_no_match,
        test_set_active,
        test_active_disappears_on_update,
        test_speak_list,
        test_speak_active,
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
