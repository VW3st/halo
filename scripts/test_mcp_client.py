"""Unit tests for halo.mcp_client — handshake, tool listing, qualified-name
routing, openai_tools shape, and fail-soft, against an in-memory fake server
(no real subprocess)."""

from __future__ import annotations

import json
import queue
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import halo.mcp_client as mc


class _FakeStdin:
    def __init__(self, proc):
        self.proc = proc

    def write(self, s):
        self.proc._handle(s)

    def flush(self):
        pass


class _FakeStdout:
    def __init__(self, proc):
        self.proc = proc

    def __iter__(self):
        return self

    def __next__(self):
        item = self.proc._outq.get()
        if item is None:
            raise StopIteration
        return item


class _FakeProc:
    """Answers initialize / tools/list / tools/call over the JSON-RPC line protocol."""

    def __init__(self, tools, call_result):
        self._outq: queue.Queue = queue.Queue()
        self.stdin = _FakeStdin(self)
        self.stdout = _FakeStdout(self)
        self._tools = tools
        self._call_result = call_result

    def _handle(self, line):
        msg = json.loads(line)
        if "id" not in msg:
            return  # notification
        mid = msg["id"]
        method = msg.get("method")
        if method == "initialize":
            result = {"protocolVersion": "2024-11-05", "capabilities": {}}
        elif method == "tools/list":
            result = {"tools": self._tools}
        elif method == "tools/call":
            result = self._call_result
        else:
            result = {}
        self._outq.put(json.dumps({"jsonrpc": "2.0", "id": mid, "result": result}) + "\n")

    def terminate(self):
        self._outq.put(None)


_TOOLS = [{
    "name": "get_weather",
    "description": "Current weather for a city",
    "inputSchema": {"type": "object", "properties": {"city": {"type": "string"}}},
}]
_CALL_RESULT = {"content": [{"type": "text", "text": "Sunny, 25C"}]}


def _write_cfg(servers: dict) -> Path:
    d = tempfile.mkdtemp()
    p = Path(d) / "mcp.json"
    p.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
    return p


def main() -> int:
    f = 0

    def ok(label, cond):
        nonlocal f
        f += 0 if cond else 1
        print(f"  [{'OK' if cond else 'FAIL'}] {label}")

    # Happy path with one fake server.
    mc.subprocess.Popen = lambda *a, **k: _FakeProc(_TOOLS, _CALL_RESULT)
    cfg = _write_cfg({"weather": {"command": "fake", "args": []}})
    client = mc.McpClient(cfg)
    client.start()

    ok("available after start", client.available)
    tools = client.list_tools()
    ok("discovered one tool", len(tools) == 1)
    ok("qualified name server__tool", tools and tools[0].qualified == "weather__get_weather")

    oai = client.openai_tools()
    ok("openai_tools shape", oai and oai[0]["type"] == "function"
       and oai[0]["function"]["name"] == "weather__get_weather"
       and "parameters" in oai[0]["function"])

    res = client.call("weather__get_weather", {"city": "Brisbane"})
    ok("call routes + returns text", res == "Sunny, 25C")
    ok("unknown tool -> error string", client.call("nope__x", {}).startswith("[tool error]"))
    client.stop()

    # Fail-soft: a server whose Popen raises is skipped, not fatal.
    def _raising_popen(*a, **k):
        raise FileNotFoundError("no such command")
    mc.subprocess.Popen = _raising_popen
    cfg2 = _write_cfg({"bad": {"command": "nonexistent", "args": []}})
    client2 = mc.McpClient(cfg2)
    client2.start()  # must not raise
    ok("bad server -> not available, no crash", client2.available is False)
    client2.stop()

    # isError surfaces as a recoverable string.
    mc.subprocess.Popen = lambda *a, **k: _FakeProc(
        _TOOLS, {"isError": True, "content": [{"type": "text", "text": "bad city"}]}
    )
    cfg3 = _write_cfg({"weather": {"command": "fake", "args": []}})
    client3 = mc.McpClient(cfg3)
    client3.start()
    ok("tool error -> '[tool error]' prefix", client3.call("weather__get_weather", {}).startswith("[tool error]"))
    client3.stop()

    total = 9
    print(f"\n{total - f}/{total} passed")
    return 1 if f else 0


if __name__ == "__main__":
    raise SystemExit(main())
