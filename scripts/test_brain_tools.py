"""Unit tests for the router brain tool-use loop (_chat_with_tools): a two-hop
tool call, the hop cap, and no-tool single response. Provider callers are
monkeypatched to scripted assistant messages; the MCP execute is a stub."""

from __future__ import annotations

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# router imports `ollama`, whose import hangs in this sandbox — stub it.
sys.modules.setdefault("ollama", types.ModuleType("ollama"))
sys.modules["ollama"].Client = object

import halo.router as r


def main() -> int:
    f = 0

    def ok(label, cond):
        nonlocal f
        f += 0 if cond else 1
        print(f"  [{'OK' if cond else 'FAIL'}] {label}")

    # --- two-hop: model asks for a tool, then answers ---
    responses = [
        {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "weather__get", "arguments": '{"city":"BNE"}'}}
        ]},
        {"content": "It's sunny and 25 in Brisbane.", "tool_calls": []},
    ]
    state = {"i": 0}

    def fake_call(messages, tools, max_tokens):
        m = responses[state["i"]]
        state["i"] += 1
        return dict(m)

    r._chat_tools_ollama = fake_call
    r._chat_tools_openrouter = fake_call

    executed = []

    def execute(name, args):
        executed.append((name, args))
        return "sunny 25C"

    sentences = []
    full = r._chat_with_tools(
        [{"role": "user", "content": "weather in brisbane?"}],
        tools=[], execute=execute, on_sentence=sentences.append, max_hops=4,
    )
    ok("final answer returned", full == "It's sunny and 25 in Brisbane.")
    ok("tool executed once with parsed args", executed == [("weather__get", {"city": "BNE"})])
    ok("final answer streamed via on_sentence", any("Brisbane" in s for s in sentences))

    # --- no-tool: single plain response, identical behavior ---
    state2 = {"i": 0}
    one = [{"content": "Hey, all good here.", "tool_calls": []}]

    def fake_one(messages, tools, max_tokens):
        m = one[state2["i"]]
        state2["i"] += 1
        return dict(m)

    r._chat_tools_ollama = fake_one
    r._chat_tools_openrouter = fake_one
    got = r._chat_with_tools(
        [{"role": "user", "content": "hi"}], tools=[], execute=lambda n, a: "x",
        on_sentence=None, max_hops=4,
    )
    ok("no-tool returns plain text", got == "Hey, all good here.")

    # --- hop cap: a model that always asks for a tool stops, doesn't hang ---
    def fake_always(messages, tools, max_tokens):
        return {"content": "", "tool_calls": [
            {"id": "c", "function": {"name": "loop__x", "arguments": "{}"}}
        ]}

    r._chat_tools_ollama = fake_always
    r._chat_tools_openrouter = fake_always
    raised = False
    try:
        r._chat_with_tools(
            [{"role": "user", "content": "go"}], tools=[], execute=lambda n, a: "ok",
            on_sentence=None, max_hops=2,
        )
    except RuntimeError:
        raised = True
    ok("hop cap raises (caller falls open)", raised)

    total = 5
    print(f"\n{total - f}/{total} passed")
    return 1 if f else 0


if __name__ == "__main__":
    raise SystemExit(main())
