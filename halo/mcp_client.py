"""Minimal stdio JSON-RPC MCP client — lets Halo's BRAIN call MCP tools directly.

Spawns the MCP servers declared in ~/.halo/mcp.json, does the MCP `initialize`
handshake, enumerates their tools, and executes `tools/call` — all over
newline-delimited JSON-RPC 2.0 on each server's stdin/stdout. No `mcp` pip
package: we implement only the four methods we use (initialize,
notifications/initialized, tools/list, tools/call), mirroring how sessions.py
hand-rolls stream-json over a subprocess.

Fail-soft throughout: a server that won't start is logged and skipped; if no
tools are discovered, `available` is False and the brain silently uses its
no-tool path. Tools are exposed to the model in OpenAI/Ollama function-calling
shape, qualified as ``server__tool`` so names stay unique and routable.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

from halo import bus

_NAME_BAD = re.compile(r"[^A-Za-z0-9_-]")
_RESULT_CAP = 4000  # truncate tool output to bound prompt growth
_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def _sanitize(name: str) -> str:
    return _NAME_BAD.sub("_", name)


@dataclass
class McpTool:
    server: str
    name: str          # bare tool name (what the server expects)
    qualified: str     # f"{server}__{name}" — what the model sees
    description: str
    input_schema: dict


class _Server:
    """One MCP server subprocess + its JSON-RPC plumbing (reader thread + id map)."""

    def __init__(self, name: str, command: str, args: list[str], timeout_sec: float) -> None:
        self.name = name
        self.command = command
        self.args = args
        self.timeout_sec = timeout_sec
        self.proc: subprocess.Popen | None = None
        self.tools: list[McpTool] = []
        self._lock = threading.Lock()
        self._id = 0
        self._pending: dict[int, dict] = {}
        self._reader: threading.Thread | None = None

    def start(self) -> bool:
        try:
            self.proc = subprocess.Popen(
                [self.command, *self.args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=_CREATE_NO_WINDOW,
            )
        except Exception as exc:
            bus.emit("mcp.server_failed", server=self.name, error=str(exc))
            print(f"  mcp: server {self.name!r} failed to start ({exc})")
            return False
        self._reader = threading.Thread(
            target=self._read_loop, daemon=True, name=f"mcp-{self.name}"
        )
        self._reader.start()
        try:
            self._handshake()
            self._load_tools()
        except Exception as exc:
            bus.emit("mcp.server_failed", server=self.name, error=str(exc))
            print(f"  mcp: server {self.name!r} handshake failed ({exc})")
            self.stop()
            return False
        return True

    def _read_loop(self) -> None:
        proc = self.proc
        if proc is None or proc.stdout is None:
            return
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except Exception:
                continue
            mid = msg.get("id")
            if mid is None:
                continue  # server-originated notification — ignore
            slot = self._pending.get(mid)
            if slot is not None:
                slot["result"] = msg.get("result")
                slot["error"] = msg.get("error")
                slot["event"].set()

    def _rpc(self, method: str, params: dict | None, timeout: float | None = None):
        if not self.proc or not self.proc.stdin:
            raise RuntimeError("server not running")
        with self._lock:
            self._id += 1
            mid = self._id
            ev = threading.Event()
            self._pending[mid] = {"event": ev, "result": None, "error": None}
            payload = {"jsonrpc": "2.0", "id": mid, "method": method}
            if params is not None:
                payload["params"] = params
            self.proc.stdin.write(json.dumps(payload) + "\n")
            self.proc.stdin.flush()
        if not ev.wait(timeout or self.timeout_sec):
            self._pending.pop(mid, None)
            raise TimeoutError(f"{method} timed out")
        slot = self._pending.pop(mid, None)
        if slot and slot["error"]:
            raise RuntimeError(f"{method} error: {slot['error']}")
        return slot["result"] if slot else None

    def _notify(self, method: str, params: dict | None = None) -> None:
        if not self.proc or not self.proc.stdin:
            return
        payload = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        with self._lock:
            self.proc.stdin.write(json.dumps(payload) + "\n")
            self.proc.stdin.flush()

    def _handshake(self) -> None:
        self._rpc("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "halo", "version": "1.6.0"},
        })
        self._notify("notifications/initialized")

    def _load_tools(self) -> None:
        result = self._rpc("tools/list", {})
        raw = (result or {}).get("tools", []) if isinstance(result, dict) else []
        tools: list[McpTool] = []
        for t in raw:
            bare = (t or {}).get("name", "")
            if not bare:
                continue
            tools.append(McpTool(
                server=self.name,
                name=bare,
                qualified=_sanitize(f"{self.name}__{bare}"),
                description=(t.get("description") or "")[:1024],
                input_schema=t.get("inputSchema") or {"type": "object", "properties": {}},
            ))
        self.tools = tools

    def call(self, bare_name: str, arguments: dict) -> str:
        result = self._rpc("tools/call", {"name": bare_name, "arguments": arguments or {}})
        if not isinstance(result, dict):
            return str(result)[:_RESULT_CAP]
        parts = [
            b.get("text", "")
            for b in result.get("content", [])
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        text = "\n".join(p for p in parts if p).strip() or json.dumps(result)
        if result.get("isError"):
            text = f"[tool error] {text}"
        return text[:_RESULT_CAP]

    def stop(self) -> None:
        if self.proc is None:
            return
        try:
            self.proc.terminate()
        except Exception:
            pass
        self.proc = None


class McpClient:
    """Owns the MCP server subprocesses for one consumer (the brain)."""

    def __init__(self, config_path, *, allow_servers=None, timeout_sec: float = 20.0) -> None:
        self.config_path = Path(config_path)
        self.allow_servers = set(allow_servers or [])
        self.timeout_sec = timeout_sec
        self._servers: list[_Server] = []
        self._started = False
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
            try:
                cfg = json.loads(self.config_path.read_text(encoding="utf-8"))
            except Exception as exc:
                print(f"  mcp: can't read {self.config_path} ({exc})")
                return
            for name, spec in (cfg.get("mcpServers") or {}).items():
                if self.allow_servers and name not in self.allow_servers:
                    continue
                command = (spec or {}).get("command")
                if not command:
                    continue
                srv = _Server(name, command, list((spec or {}).get("args") or []), self.timeout_sec)
                if srv.start():
                    self._servers.append(srv)
                    print(f"  mcp: {name} -> {len(srv.tools)} tool(s)")

    def list_tools(self) -> list[McpTool]:
        out: list[McpTool] = []
        for s in self._servers:
            out.extend(s.tools)
        return out

    def openai_tools(self) -> list[dict]:
        """tools[] in OpenAI/Ollama function-calling shape."""
        out = []
        for t in self.list_tools():
            out.append({
                "type": "function",
                "function": {
                    "name": t.qualified,
                    "description": (f"[{t.server}] {t.description}")[:1024],
                    "parameters": t.input_schema or {"type": "object", "properties": {}},
                },
            })
        return out

    def call(self, qualified_name: str, arguments: dict) -> str:
        for s in self._servers:
            for t in s.tools:
                if t.qualified == qualified_name:
                    return s.call(t.name, arguments)
        return f"[tool error] unknown tool {qualified_name!r}"

    @property
    def available(self) -> bool:
        return any(s.tools for s in self._servers)

    def stop(self) -> None:
        for s in self._servers:
            s.stop()


_BRAIN_CLIENT: McpClient | None = None
_BRAIN_CLIENT_TRIED = False


def get_brain_client() -> McpClient | None:
    """Process-wide singleton built from cfg.mcp. Returns None when mcp.enabled or
    mcp.brain_tools is off, the config is missing, or no servers start. Cached;
    safe to call every turn."""
    global _BRAIN_CLIENT, _BRAIN_CLIENT_TRIED
    if _BRAIN_CLIENT_TRIED:
        return _BRAIN_CLIENT if (_BRAIN_CLIENT and _BRAIN_CLIENT.available) else None
    _BRAIN_CLIENT_TRIED = True
    try:
        from halo.userconfig import cfg
        mcp = getattr(cfg, "mcp", None)
        if not mcp or not getattr(mcp, "enabled", False) or not getattr(mcp, "brain_tools", False):
            return None
        from halo.agents import resolve_mcp_config_path
        path = resolve_mcp_config_path()
        if path is None:
            return None
        client = McpClient(
            path,
            allow_servers=getattr(mcp, "brain_tool_servers", None) or None,
            timeout_sec=float(getattr(mcp, "brain_tool_timeout_sec", 20.0)),
        )
        client.start()
        if not client.available:
            client.stop()
            return None
        _BRAIN_CLIENT = client
        return client
    except Exception as exc:
        print(f"  mcp: brain client init failed ({exc})")
        return None
