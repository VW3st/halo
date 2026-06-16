"""Regression tests for local voice session routing.

Run with: python scripts/test_session_routing.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from halo.discovery import DiscoveredSession
from halo.registry import SessionRegistry
import halo.__main__ as main


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


def test_session_query_phrases():
    assert main._is_session_query("Do we have any good session online?")
    assert main._is_session_query("Can you tell me which CLI is active and what are the session names?")


def test_switch_to_spoken_project_label():
    old_registry = main.REGISTRY
    try:
        registry = SessionRegistry()
        registry.update([make_session("socialmanager", "D:\\socialmanager")])
        main.REGISTRY = registry

        agent, phrase = main._local_session_switch("Okay, let me talk to the social manager.")

        assert_eq(agent, "claude_code")
        assert_eq(registry.active_label(), "socialmanager")
        assert "Claude Code in socialmanager" in phrase
    finally:
        main.REGISTRY = old_registry


def test_switch_to_social_manager_cli_please():
    old_registry = main.REGISTRY
    try:
        registry = SessionRegistry()
        registry.update([make_session("socialmanager", "D:\\socialmanager")])
        main.REGISTRY = registry

        agent, phrase = main._local_session_switch("Okay, let's go talk to the social manager, CLI, please.")

        assert_eq(agent, "claude_code")
        assert_eq(registry.active_label(), "socialmanager")
        assert "Claude Code in socialmanager" in phrase
    finally:
        main.REGISTRY = old_registry


def test_active_selected_without_active_does_not_pick_random_session():
    old_registry = main.REGISTRY
    try:
        registry = SessionRegistry()
        registry.update([
            make_session("Halo/Halo", "D:\\Halo", agent="codex_cli"),
            make_session("socialmanager", "D:\\socialmanager"),
        ])
        main.REGISTRY = registry

        agent, phrase = main._local_session_switch("Okay, go under active selected.")

        assert_eq(agent, None)
        assert "No session is selected" in phrase
        assert_eq(registry.active_label(), None)
    finally:
        main.REGISTRY = old_registry


def test_active_selected_with_active_reuses_active_session():
    old_registry = main.REGISTRY
    try:
        registry = SessionRegistry()
        registry.update([make_session("socialmanager", "D:\\socialmanager")])
        registry.set_active("socialmanager")
        main.REGISTRY = registry

        agent, phrase = main._local_session_switch("Okay, go under active selected.")

        assert_eq(agent, "claude_code")
        assert "Claude Code in socialmanager" in phrase
    finally:
        main.REGISTRY = old_registry


def test_switch_to_selected_session_on_project_phrase():
    old_registry = main.REGISTRY
    try:
        registry = SessionRegistry()
        registry.update([make_session("socialmanager", "D:\\socialmanager")])
        main.REGISTRY = registry

        agent, phrase = main._local_session_switch("Okay, go on the selected session on social manager.")

        assert_eq(agent, "claude_code")
        assert_eq(registry.active_label(), "socialmanager")
        assert "Claude Code in socialmanager" in phrase
    finally:
        main.REGISTRY = old_registry


def test_speak_to_manager_maps_to_manager_session():
    old_registry = main.REGISTRY
    try:
        registry = SessionRegistry()
        registry.update([make_session("socialmanager", "D:\\socialmanager")])
        main.REGISTRY = registry

        agent, phrase = main._local_session_switch("I need to speak to the manager.")

        assert_eq(agent, "claude_code")
        assert_eq(registry.active_label(), "socialmanager")
        assert "Claude Code in socialmanager" in phrase
    finally:
        main.REGISTRY = old_registry


def test_last_task_is_session_query():
    assert main._is_session_query("What was the last task you did?")
    assert main._is_session_query("Tell me the last task you did please.")
    assert main._is_session_query("What here lies do you see open?")


def test_tool_open_phrase_is_not_session_switch():
    old_registry = main.REGISTRY
    try:
        registry = SessionRegistry()
        registry.update([make_session("chrome", "D:\\chrome")])
        main.REGISTRY = registry

        agent, phrase = main._local_session_switch("open chrome")

        assert_eq(agent, None)
        assert_eq(phrase, "")
        assert_eq(registry.active_label(), None)
    finally:
        main.REGISTRY = old_registry


def test_direct_desktop_send_uses_selected_discovered_session():
    old_registry = main.REGISTRY
    old_sender = main.send_to_desktop_session
    old_last_sent = dict(main._desktop_last_sent)
    try:
        registry = SessionRegistry()
        registry.update([make_session("socialmanager", "D:\\socialmanager", pid=31328)])
        registry.set_active("socialmanager")
        main.REGISTRY = registry
        calls = []

        def fake_sender(session, text):
            calls.append((session.label, text))
            return type("Result", (), {"ok": True, "message": "sent"})()

        main.send_to_desktop_session = fake_sender
        phrase = main._send_to_direct_desktop_session(
            "claude_code", "run the test", "socialmanager"
        )

        assert_eq(calls, [("socialmanager", "run the test")])
        assert_eq(main._desktop_last_sent.get("socialmanager"), "run the test")
        assert "Claude Code in socialmanager" in phrase
    finally:
        main._desktop_last_sent.clear()
        main._desktop_last_sent.update(old_last_sent)
        main.send_to_desktop_session = old_sender
        main.REGISTRY = old_registry


def test_type_in_active_session_sends_payload_not_router_task():
    old_registry = main.REGISTRY
    old_sender = main.send_to_desktop_session
    old_last_sent = dict(main._desktop_last_sent)
    try:
        registry = SessionRegistry()
        registry.update([make_session("socialmanager", "D:\\socialmanager")])
        registry.set_active("socialmanager")
        main.REGISTRY = registry
        calls = []

        def fake_sender(session, text):
            calls.append((session.label, text))
            return type("Result", (), {"ok": True, "message": "sent"})()

        main.send_to_desktop_session = fake_sender
        cleaned = main._strip_leading_greeting(
            "Halo. Type in the active session. This is a test. Do nothing."
        )
        phrase = main._local_type_into_session(cleaned)

        assert_eq(calls, [("socialmanager", "This is a test. Do nothing.")])
        assert "Claude Code in socialmanager" in phrase
    finally:
        main._desktop_last_sent.clear()
        main._desktop_last_sent.update(old_last_sent)
        main.send_to_desktop_session = old_sender
        main.REGISTRY = old_registry


def test_type_in_active_session_without_target_does_not_spawn():
    old_registry = main.REGISTRY
    old_foreground = main.foreground_pid
    try:
        registry = SessionRegistry()
        registry.update([make_session("socialmanager", "D:\\socialmanager")])
        main.REGISTRY = registry
        main.foreground_pid = lambda: None

        phrase = main._local_type_into_session(
            "Type in the active session. This is a test. Do nothing."
        )

        assert "No desktop session is selected or focused" in phrase
        assert_eq(registry.active_label(), None)
    finally:
        main.foreground_pid = old_foreground
        main.REGISTRY = old_registry


def test_type_in_active_session_request_without_payload_stays_local():
    old_registry = main.REGISTRY
    try:
        registry = SessionRegistry()
        registry.update([make_session("socialmanager", "D:\\socialmanager")])
        registry.set_active("socialmanager")
        main.REGISTRY = registry

        phrase = main._local_type_into_session(
            "I want you to type in the active session."
        )

        assert "What should I type into Claude Code in socialmanager" in phrase
    finally:
        main.REGISTRY = old_registry


def test_typing_active_session_stt_variant_sends_payload():
    old_registry = main.REGISTRY
    old_sender = main.send_to_desktop_session
    old_last_sent = dict(main._desktop_last_sent)
    try:
        registry = SessionRegistry()
        registry.update([make_session("socialmanager", "D:\\socialmanager")])
        registry.set_active("socialmanager")
        main.REGISTRY = registry
        calls = []

        def fake_sender(session, text):
            calls.append((session.label, text))
            return type("Result", (), {"ok": True, "message": "sent"})()

        main.send_to_desktop_session = fake_sender
        phrase = main._local_type_into_session(
            "look I'm typing in the active session. This is a test."
        )

        assert_eq(calls, [("socialmanager", "This is a test.")])
        assert "Claude Code in socialmanager" in phrase
    finally:
        main._desktop_last_sent.clear()
        main._desktop_last_sent.update(old_last_sent)
        main.send_to_desktop_session = old_sender
        main.REGISTRY = old_registry


def main_test() -> int:
    tests = [
        test_session_query_phrases,
        test_switch_to_spoken_project_label,
        test_switch_to_social_manager_cli_please,
        test_active_selected_without_active_does_not_pick_random_session,
        test_active_selected_with_active_reuses_active_session,
        test_switch_to_selected_session_on_project_phrase,
        test_speak_to_manager_maps_to_manager_session,
        test_last_task_is_session_query,
        test_tool_open_phrase_is_not_session_switch,
        test_direct_desktop_send_uses_selected_discovered_session,
        test_type_in_active_session_sends_payload_not_router_task,
        test_type_in_active_session_without_target_does_not_spawn,
        test_type_in_active_session_request_without_payload_stays_local,
        test_typing_active_session_stt_variant_sends_payload,
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
    sys.exit(main_test())
