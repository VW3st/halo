"""Unit tests for the ASK "session reply" helpers: choice parsing, the own-cwd
sentinel, offer resolution, and discovered-only detection with a stub registry."""

from __future__ import annotations

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# router imports ollama (hangs in sandbox) — stub before importing __main__.
sys.modules.setdefault("ollama", types.ModuleType("ollama"))
sys.modules["ollama"].Client = object

import halo.__main__ as m  # noqa: E402
import halo.agents as agents  # noqa: E402


class _Sess:
    def __init__(self, agent_key, label, cwd):
        self.agent_key = agent_key
        self.label = label
        self.cwd = cwd


class _Reg:
    def __init__(self, sessions):
        self._by = {s.label: s for s in sessions}
        self._active = None

    def by_label(self, label):
        return self._by.get(label)

    def set_active(self, label):
        self._active = label

    def active(self):
        return self._by.get(self._active) if self._active else None

    def active_label(self):
        return self._active


def main() -> int:
    f = 0

    def ok(label, cond):
        nonlocal f
        f += 0 if cond else 1
        print(f"  [{'OK' if cond else 'FAIL'}] {label}")

    print("_session_reply_choice:")
    ok("'start your own' -> own", m._session_reply_choice("start your own") == "own")
    ok("'I want to hear the replies' -> own", m._session_reply_choice("I want to hear the replies") == "own")
    ok("'spawn a new one' -> own", m._session_reply_choice("spawn a new one") == "own")
    ok("'use the one already running' -> existing", m._session_reply_choice("use the one already running") == "existing")
    ok("'the existing one' -> existing", m._session_reply_choice("just the existing one") == "existing")
    ok("'send only' -> existing", m._session_reply_choice("send only please") == "existing")
    ok("bare yes -> own", m._session_reply_choice("yes") == "own")
    ok("bare no -> existing", m._session_reply_choice("no") == "existing")
    ok("unrelated -> None", m._session_reply_choice("what's the weather") is None)

    print("\n_own_cwd sentinel:")
    ok("encodes/decodes cwd", m._own_cwd(m._OWN_PREFIX + r"C:\dev\proj") == r"C:\dev\proj")
    ok("plain label -> None", m._own_cwd("socialmanager") is None)
    ok("None -> None", m._own_cwd(None) is None)

    print("\n_resolve_session_reply_offer (stub registry):")
    reg = _Reg([_Sess("claude_code", "socialmanager", r"C:\dev\socialmanager")])
    m.REGISTRY = reg
    offer = m._session_reply_offer(reg.by_label("socialmanager"))
    ok("offer marker", offer.get("_session_reply_offer") is True and offer["cwd"] == r"C:\dev\socialmanager")
    agent, label, phrase = m._resolve_session_reply_offer(offer, want_own=True)
    ok("own -> sentinel label", m._own_cwd(label) == r"C:\dev\socialmanager")
    ok("own -> active cleared", reg.active() is None)
    agent2, label2, phrase2 = m._resolve_session_reply_offer(offer, want_own=False)
    ok("existing -> real label", label2 == "socialmanager")
    ok("existing -> active set", reg.active_label() == "socialmanager")

    print("\n_is_discovered_only (stub registry + status):")
    reg2 = _Reg([_Sess("claude_code", "proj", r"D:\proj")])
    m.REGISTRY = reg2
    _orig = agents.session_status_detail
    agents.session_status_detail = lambda: {}  # no Halo-owned session anywhere
    ok("discovered + no halo-owned -> True", m._is_discovered_only("proj") is True)
    skey = agents.session_key("claude_code", r"D:\proj")
    agents.session_status_detail = lambda: {skey: True}  # halo-owned live in same cwd
    ok("halo-owned exists -> False", m._is_discovered_only("proj") is False)
    ok("unknown label -> False", m._is_discovered_only("nope") is False)
    reg3 = _Reg([_Sess("claude_code", "noaccess", "")])
    m.REGISTRY = reg3
    ok("empty cwd -> False", m._is_discovered_only("noaccess") is False)
    ok("own-sentinel label -> False", m._is_discovered_only(m._OWN_PREFIX + r"D:\x") is False)
    agents.session_status_detail = _orig

    total = 22
    print(f"\n{total - f}/{total} passed")
    return 1 if f else 0


if __name__ == "__main__":
    raise SystemExit(main())
