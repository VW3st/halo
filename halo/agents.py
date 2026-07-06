"""Agent dispatch — registry-driven so adding a new agent is one entry.

Each entry in `AGENTS` is an `AgentConfig`. The single `dispatch(key, prompt)`
function handles subprocess launch, ticker, stdin closure, stderr streaming,
session continuation, and result parsing for every agent.

To add a new agent:

    AGENTS["aider"] = AgentConfig(
        key="aider",
        spoken_name="Aider",
        voice_triggers=("aider",),
        first_call=("aider", "--message", "{PROMPT}", "--yes"),
        continue_call=("aider", "--message", "{PROMPT}", "--yes"),
        parses_json=False,
    )

That's it — `halo.__main__` looks routes up by key, so once the Stage 2 LLM
returns `agent="aider"` (or you wire a voice trigger), it works.

Tokens substituted in command templates:
  {PROMPT} -> the user's spoken instruction
  {CWD}    -> the agent's working directory
"""

from __future__ import annotations

import itertools
import json
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal, Optional

from halo import bus
from halo.userconfig import cfg as user_cfg

# Default to the directory Halo was launched from. Most users will
# `cd <project>` before running Halo; agents then act on that project.
DEFAULT_CWD = Path.cwd()

# Voice-mode preamble injected into every agent call. The user hears the
# response through TTS, so Markdown / long code dumps / em-dashes turn
# into garbage audio. This brief is the single most impactful thing we
# can do to make Claude's responses speakable.
VOICE_SYSTEM_PROMPT = (
    "## YOU ARE ON A VOICE CHANNEL\n"
    "Your output text is read aloud by a text-to-speech engine. The user "
    "cannot see your message. Markdown does not render — every asterisk, "
    "backtick, dash, and bullet is spoken character-by-character and "
    "sounds broken.\n\n"
    "## ABSOLUTE RULES\n"
    "1. NO Markdown. Zero. No `**bold**`, no `*italic*`, no `code`, no "
    "fenced ``` blocks, no `# headings`, no `- bullet lists`, no `> quotes`, "
    "no `[link text](url)`, no em-dashes (—), no en-dashes (–).\n"
    "2. Maximum 2 short sentences. If you need more, you are doing it "
    "wrong — write to a file and reference the filename instead.\n"
    "3. Never paste code, command output, file contents, or stack traces "
    "into the response. Write them to a file. Then say: 'I wrote it to "
    "<basename>.'\n"
    "4. Use file basenames only when speaking. Never full paths like "
    "`D:\\Halo\\src\\foo.py` — say 'foo dot py'.\n"
    "5. For lists, use connecting words: 'I made three changes: A, B, "
    "and C.' Not bullets.\n"
    "6. If you need clarification, ask ONE short question.\n"
    "7. For tasks that will take longer than ~10 seconds (running tests, "
    "scaffolding files, deploys, web fetches), briefly state what you "
    "will do and a rough time estimate BEFORE starting the work — e.g. "
    "'I'll scaffold the routes, about twenty seconds.' This way the user "
    "knows you heard them and isn't left in silence.\n"
    "8. When you finish creating a file the user can preview (HTML page, "
    "image, document, screenshot), state the filename clearly at the end "
    "so the user can ask to open it — e.g. 'I wrote it to landing.html.' "
    "The user can then say 'open landing.html' to view it locally.\n"
    "9. If the user's whole utterance is just a filename ('hello.html', "
    "'auth.py', 'README.md') with no verb, treat it as 'open this file' "
    "or 'show me this file'. Use the Read tool — NEVER create a new file "
    "with that name, NEVER overwrite. If the file doesn't exist, ask "
    "the user (one sentence) whether to create it.\n\n"
    "## YOUR SESSION NAME\n"
    "Your spoken name in this session is {NAME}. You may introduce "
    "yourself by that name once at the start of a session.\n\n"
    "## USER CONTEXT\n"
    "The user's name is {USER_NAME}. Their role/context is {USER_ROLE}. "
    "Halo's configured character is: {HALO_CHARACTER}. Address the user "
    "by name when it sounds natural, but do not force it into every sentence.\n\n"
    "## EXAMPLES\n"
    "Bad:  'Created at `D:\\\\Halo\\\\hello.py` — a one-line script.\\n\\n"
    "**Output:**\\n```python\\nprint(\"hello\")\\n```'\n"
    "Good: 'I wrote it to hello dot py.'\n\n"
    "Bad:  '- Added auth\\n- Wrote tests\\n- Updated README'\n"
    "Good: 'I added auth, wrote tests, and updated the readme.'\n\n"
    "Bad:  (silent for 40 seconds while running a full test suite)\n"
    "Good: 'Running the test suite, this takes about thirty seconds.'"
)

# 5 minutes — coding tasks often need this. Per-call override is supported.
DEFAULT_TIMEOUT_SEC = 300.0

# How often the dispatcher prints "still working" to stdout and (via
# callback) speaks reassurance through Kokoro.
TICKER_STDOUT_SEC = 15.0
TICKER_VOICE_SEC = 45.0


@dataclass(frozen=True)
class AgentConfig:
    """One row in the agent registry.

    Command templates are tuples of argv-style strings. Tokens get
    substituted at call time:
      {PROMPT}        -> user instruction (with voice preamble prepended
                         when system_prompt_arg is empty)
      {CWD}           -> working directory
      {SYSTEM_PROMPT} -> voice-mode instructions (used when
                         system_prompt_arg names how the CLI accepts a
                         system-prompt flag, e.g. Claude's
                         --append-system-prompt)
    """

    key: str
    spoken_name: str
    voice_triggers: tuple[str, ...]
    first_call: tuple[str, ...]
    continue_call: tuple[str, ...]
    parses_json: bool = False
    json_result_field: str = "result"
    sandbox_label: str = ""
    # If non-empty, the dispatcher substitutes {SYSTEM_PROMPT} in the
    # command template. If empty, we prepend voice instructions to the
    # user prompt directly (Codex has no append-system-prompt flag).
    accepts_system_prompt_arg: bool = False
    # If True, stdout is newline-delimited JSON events that include
    # text_delta chunks (Claude's --output-format stream-json). The
    # dispatcher parses each line, accumulates text, and fires
    # on_text_chunk per sentence so the orchestrator can TTS the
    # response live instead of waiting for the agent to finish.
    streams_text_deltas: bool = False
    # Optional CLI invocation used by `check_availability()` to verify
    # the agent's binary is on PATH and responds correctly. Typically
    # `(binary, "--version")`. None disables the responsiveness probe;
    # availability falls back to "binary is on PATH" only.
    check_call: Optional[tuple[str, ...]] = None
    # Free-text install instruction shown by `halo doctor` and announced
    # when the user tries to dispatch to a missing agent. Kept short.
    install_hint: str = ""
    # "persistent": spawn ONE long-lived process per Halo session and
    # ship every turn through its stdin (Claude's --input-format
    # stream-json). "one-shot": fork a fresh subprocess per turn — the
    # legacy model, still used by Codex which has no stream-json input.
    session_kind: Literal["one-shot", "persistent"] = "one-shot"
    # Command template for the persistent-session spawn (no {PROMPT} —
    # turns are sent via stdin JSON envelopes). Only consulted when
    # session_kind == "persistent".
    persistent_call: tuple[str, ...] = ()
    # Per-agent override for the global AGENT_CLI_VISIBLE config flag:
    #   None  -> follow the global setting in halo/config.py
    #   True  -> force-echo this agent's CLI activity to Halo's stdout
    #   False -> force-silence this agent (headless), even if global is on
    # Only meaningful for persistent agents; one-shot dispatches already
    # stream their stderr inline via _drain_stderr.
    cli_visible: Optional[bool] = None
    # Common Whisper mis-transcriptions of the agent's name. Used by the
    # Stage 2 fallback dispatcher in halo.__main__ — when the router LLM
    # is unreachable AND the user's utterance contains any of these
    # variants, we dispatch to this agent instead of giving up. Add
    # whatever Whisper produces in your environment ("Claude" reliably
    # comes out as "Cloud" / "clod" / "clawed").
    fuzzy_triggers: tuple[str, ...] = ()


AGENTS: dict[str, AgentConfig] = {
    "claude_code": AgentConfig(
        key="claude_code",
        spoken_name="Claude Code",
        voice_triggers=("claude", "claude code"),
        # --permission-mode bypassPermissions: the only way to run Claude
        # by voice. acceptEdits still pops a TUI prompt on every Bash
        # call (test runners, git ops, package installs) and there's no
        # mouse/keyboard to approve — Halo would hang silently. The
        # operator (user) IS the supervisor here, just via voice instead
        # of keystroke. Documented in `claude --help` and the official
        # Claude Code CLI reference.
        first_call=(
            "claude", "-p", "{PROMPT}",
            "--output-format", "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--permission-mode", "bypassPermissions",
            "--append-system-prompt", "{SYSTEM_PROMPT}",
        ),
        continue_call=(
            "claude", "-p", "{PROMPT}",
            "--output-format", "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--permission-mode", "bypassPermissions",
            "--continue",
            "--append-system-prompt", "{SYSTEM_PROMPT}",
        ),
        # One long-lived process per Halo session. No --continue: the
        # process IS the session, so we never need to resolve "which
        # session am I continuing". Every turn is a JSON envelope on
        # stdin; replies come back on stdout exactly like the per-turn
        # stream-json path used to parse.
        persistent_call=(
            "claude", "-p",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--permission-mode", "bypassPermissions",
            "--append-system-prompt", "{SYSTEM_PROMPT}",
        ),
        parses_json=True,
        json_result_field="result",
        sandbox_label="bypassPermissions",
        accepts_system_prompt_arg=True,
        streams_text_deltas=True,
        check_call=("claude", "--version"),
        install_hint="npm install -g @anthropic-ai/claude-code  (then: claude login)",
        session_kind="persistent",
        fuzzy_triggers=("claud", "clawed", "clod", "cloud", "clawde", "claus",
                        "clyde", "clode", "clawd"),
    ),
    "codex_cli": AgentConfig(
        key="codex_cli",
        spoken_name="Codex CLI",
        voice_triggers=("codex", "open ai codex", "openai codex"),
        # -c approval_policy="never": OpenAI's documented recommendation
        # for non-interactive runs. Without it Codex pauses for approval
        # on certain commands and Halo has no way to click "yes". We
        # keep --sandbox workspace-write so Codex can only modify files
        # under the project root (network and system paths still
        # blocked). Full "danger" mode would require
        # --dangerously-bypass-approvals-and-sandbox.
        first_call=(
            "codex", "exec",
            "--sandbox", "workspace-write",
            "-c", "approval_policy=\"never\"",
            "-C", "{CWD}",
            "{PROMPT}",
        ),
        continue_call=(
            "codex", "exec", "resume", "--last",
            "--sandbox", "workspace-write",
            "-c", "approval_policy=\"never\"",
            "-C", "{CWD}",
            "{PROMPT}",
        ),
        parses_json=False,
        sandbox_label="workspace-write",
        accepts_system_prompt_arg=False,
        check_call=("codex", "--version"),
        install_hint="npm install -g @openai/codex  (then: codex login)",
        fuzzy_triggers=("codec", "codecs", "kodex", "co dex", "co-dex",
                        "krodex", "kadex", "cadex", "kodaks", "crodex"),
    ),
}


# Per-agent connectivity cache. Filled lazily by `check_availability()`
# and refreshed only on explicit request (so the dashboard's /api/state
# poll doesn't spawn `claude --version` every 750 ms). The user's
# binaries don't appear/disappear during a session.
_availability_cache: dict[str, dict] = {}
_availability_lock = threading.Lock()


def _resolve_executable(binary: str) -> str | None:
    """Return a subprocess-launchable executable path for `binary`.

    On Windows, npm global shims often put both `codex` and `codex.cmd` on
    PATH. PowerShell can run the extensionless shim, but Python CreateProcess
    cannot. Prefer the .cmd/.exe form when present.
    """
    found = shutil.which(binary)
    if sys.platform != "win32":
        return found
    candidates = []
    if found:
        candidates.append(found)
    if not Path(binary).suffix:
        candidates.extend(
            p for p in (
                shutil.which(binary + ".cmd"),
                shutil.which(binary + ".exe"),
                shutil.which(binary + ".bat"),
            )
            if p
        )
    for candidate in candidates:
        if Path(candidate).suffix.lower() in {".exe", ".cmd", ".bat", ".com"}:
            return candidate
    return found


def check_availability(refresh: bool = False) -> dict[str, dict]:
    """Probe every registered agent for installed-ness and responsiveness.

    For each agent, returns a dict with:
      - installed:     binary is on PATH (shutil.which)
      - responsive:    `check_call` (e.g. `claude --version`) exited 0
                       within 10 s. Falls back to `installed` value when
                       `check_call` is None.
      - install_hint:  short text from AgentConfig.install_hint

    Result is cached process-wide; pass `refresh=True` to force a fresh
    probe (e.g. from `halo doctor`).
    """
    with _availability_lock:
        if _availability_cache and not refresh:
            return dict(_availability_cache)
        result: dict[str, dict] = {}
        for key, cfg in AGENTS.items():
            binary = cfg.first_call[0]
            executable = _resolve_executable(binary)
            installed = executable is not None
            responsive = installed
            if installed and cfg.check_call:
                try:
                    check_cmd = list(cfg.check_call)
                    resolved_check = _resolve_executable(check_cmd[0])
                    if resolved_check:
                        check_cmd[0] = resolved_check
                    proc = subprocess.run(
                        check_cmd,
                        capture_output=True,
                        timeout=10.0,
                        text=True,
                        stdin=subprocess.DEVNULL,
                    )
                    responsive = (proc.returncode == 0)
                except Exception:
                    responsive = False
            result[key] = {
                "installed": installed,
                "responsive": responsive,
                "install_hint": cfg.install_hint,
                "binary": binary,
                "executable": executable or binary,
            }
        _availability_cache.clear()
        _availability_cache.update(result)
        return dict(_availability_cache)


def available_agents() -> list[str]:
    """Spoken names of agents that are installed AND responsive — the
    list spoken at startup ('Halo online. Claude and Codex are connected.').
    """
    statuses = check_availability()
    return [
        AGENTS[key].spoken_name
        for key, st in statuses.items()
        if st.get("installed") and st.get("responsive")
    ]


# Session state is keyed by (agent_key, cwd). v1.1 was keyed by
# agent_key only, which forced one Claude session per agent across the
# entire Halo process. v1.2's multi-project mode means we can have
# one persistent Claude per project directory all running at once.
#
# `session_key(agent_key, cwd)` returns a stable composite string used
# everywhere session state is keyed. cwd is normalized via Path.resolve
# so "D:\\Halo" and "D:/Halo/." both hash to the same key.
def session_key(agent_key: str, cwd: Path | str | None) -> str:
    """Stable composite key for per-(agent, cwd) session state.

    cwd=None falls back to DEFAULT_CWD (Halo's launch dir) so v1.1
    single-project callers keep working without changes.
    """
    path = Path(cwd) if cwd is not None else DEFAULT_CWD
    try:
        norm = str(path.resolve())
    except Exception:
        norm = str(path)
    return f"{agent_key}@{norm}"


# Per-(agent, cwd) session-continuation flag.
_sessions_active: dict[str, bool] = {}
# Per-(agent, cwd) spoken session name ("Mars", "Juno", ...).
_session_names: dict[str, str] = {}
# Guards _sessions_active, _session_names, and _last_by_session. These are
# written on the background agent-runner thread and read by both the main
# orchestrator loop AND the web /api/state poll (~750ms). Without this lock,
# iterating one while the runner mutates it raises "dict changed size during
# iteration" and crashes the reader. RLock so nested same-thread calls are safe.
_session_state_lock = threading.RLock()


def _new_session_name(skey: str) -> str:
    """Voice label for a session: model/CLI + project label.

    Replaces the old mythology codenames. Users need to know which real
    session/model they are talking to, e.g. "Claude Code in socialmanager".
    """
    agent_key, _, cwd = skey.partition("@")
    cfg = AGENTS.get(agent_key)
    model = cfg.spoken_name if cfg else agent_key
    project = Path(cwd).name if cwd else ""
    return f"{model} in {project}" if project else model


def session_name(agent_key: str, cwd: Path | str | None = None) -> str:
    """Spoken name for the live (or last) session of `agent_key` in `cwd`."""
    skey = session_key(agent_key, cwd)
    with _session_state_lock:
        if skey not in _session_names:
            _session_names[skey] = _new_session_name(skey)
        return _session_names[skey]


def reset_session(agent: str | None = None, cwd: Path | str | None = None) -> None:
    """Drop the continuation pointer for one (agent, cwd) — or all sessions.

    For persistent-session agents (Claude), also kills the long-lived
    subprocess so the next dispatch spawns a fresh one. Rotates the
    spoken name so the next dispatch introduces itself fresh.

    Behavior:
      reset_session()                     -> reset every session, every cwd
      reset_session("claude_code")        -> reset every cwd for that agent
      reset_session("claude_code", cwd)   -> reset just one (agent, cwd) pair

    Use on explicit "new task" / "start over" / "fresh session" cues —
    NOT between conversations.
    """
    # Local import to dodge a circular import: sessions.py reuses
    # _SentenceBuffer + _extract_* helpers from this module.
    from halo import sessions as _sessions

    with _session_state_lock:
        if agent is None:
            targets = list(_sessions_active.keys())
        elif cwd is not None:
            targets = [session_key(agent, cwd)]
        else:
            prefix = f"{agent}@"
            targets = [k for k in _sessions_active if k.startswith(prefix)]
            # Also catch sessions that were named but never marked active.
            targets += [k for k in _session_names if k.startswith(prefix) and k not in targets]
        for skey in targets:
            if skey in _sessions_active:
                _sessions_active[skey] = False
            if skey in _session_names:
                _session_names[skey] = _new_session_name(skey)

    # Persistent process teardown — only for Claude-style agents. Done OUTSIDE
    # the state lock (sessions.close has its own lock + can block on join).
    for skey in targets:
        agent_key = skey.split("@", 1)[0]
        if AGENTS.get(agent_key) and AGENTS[agent_key].session_kind == "persistent":
            _sessions.close(skey)


def session_status() -> dict[str, bool]:
    """Per-agent session-active aggregate. Keys are agent_keys.

    Back-compat shape from v1.1: `{agent_key: True}` if ANY cwd has a
    live session for that agent. v1.2 callers that need per-cwd detail
    should use `session_status_detail()` instead.
    """
    agg: dict[str, bool] = {key: False for key in AGENTS}
    with _session_state_lock:
        snapshot = list(_sessions_active.items())
    for skey, active in snapshot:
        if not active:
            continue
        agent_key = skey.split("@", 1)[0]
        if agent_key in agg:
            agg[agent_key] = True
    return agg


def session_status_detail() -> dict[str, bool]:
    """Per-(agent, cwd) session-active map. Keys are session_key() strings.

    New in v1.2 — exposes the full per-project state for the dashboard,
    registry, and orchestrator.
    """
    with _session_state_lock:
        return dict(_sessions_active)


def known_agents() -> list[str]:
    return list(AGENTS.keys())


# MCP (Model Context Protocol): when enabled in [mcp] config, Halo passes
# --mcp-config to every Claude session it spawns so the agent gains extra
# tools (desktop control, etc.). Only injected for `claude` invocations
# (Codex has no --mcp-config flag).
def _resolve_uvx() -> str:
    """Absolute path to uvx, preferring the venv we installed it into so the
    MCP server launches even when Halo is run without the venv activated.
    Falls back to bare 'uvx' (PATH lookup) if not found."""
    sub = "Scripts" if sys.platform == "win32" else "bin"
    exe = "uvx.exe" if sys.platform == "win32" else "uvx"
    cand = Path(sys.prefix) / sub / exe
    if cand.is_file():
        return str(cand)
    return shutil.which("uvx") or "uvx"


def _default_mcp_config() -> dict:
    return {
        "mcpServers": {
            # Windows-MCP: native Windows computer-use (click/type/screenshot,
            # app + window control). Runs via uvx in its own isolated env.
            "windows-mcp": {
                "command": _resolve_uvx(),
                "args": ["windows-mcp", "serve"],
            },
        }
    }


def _ensure_default_mcp_config() -> Optional[Path]:
    """Return the bundled mcp.json path, writing the desktop-control default
    on first use. Returns None if it can't be written."""
    path = Path.home() / ".halo" / "mcp.json"
    if path.is_file():
        return path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(_default_mcp_config(), indent=2), encoding="utf-8")
        print(f"  [mcp] wrote default desktop-control config -> {path}")
        return path
    except Exception as exc:
        print(f"  [mcp] could not write default config: {exc}")
        return None


def resolve_mcp_config_path() -> Optional[Path]:
    """The mcp.json path Halo should use: an explicit [mcp] config_path if set,
    else the bundled default (written on first use). None when [mcp] is disabled
    or the path can't be resolved. Shared by the spawned-agent MCP args AND the
    brain's MCP client (mcp_client.py) so both read the same config."""
    mcp = getattr(user_cfg, "mcp", None)
    if mcp is None or not getattr(mcp, "enabled", False):
        return None
    raw = (getattr(mcp, "config_path", "") or "").strip()
    path = Path(raw) if raw else _ensure_default_mcp_config()
    if path is None or not Path(path).is_file():
        if raw:
            print(f"  [mcp] config_path not found: {raw!r} — MCP not loaded")
        return None
    return Path(path)


def _mcp_args() -> list[str]:
    """`--mcp-config <path>` (+ optional --strict-mcp-config) when [mcp] is
    enabled and a config file exists; otherwise empty (MCP off)."""
    path = resolve_mcp_config_path()
    if path is None:
        return []
    args = ["--mcp-config", str(path)]
    if getattr(getattr(user_cfg, "mcp", None), "strict", False):
        args.append("--strict-mcp-config")
    return args


def _build_cmd(
    template: tuple[str, ...],
    *,
    prompt: str,
    cwd: str,
    system_prompt: str,
) -> list[str]:
    out: list[str] = []
    for arg in template:
        if arg == "{PROMPT}":
            out.append(prompt)
        elif arg == "{CWD}":
            out.append(cwd)
        elif arg == "{SYSTEM_PROMPT}":
            out.append(system_prompt)
        else:
            out.append(arg)
    # Claude-only: append MCP server config so spawned sessions get the
    # configured tools. Gated on the unresolved template token, not out[0]
    # (which gets rewritten to a full path just below).
    if template and template[0] == "claude":
        out.extend(_mcp_args())
    if out:
        resolved = _resolve_executable(out[0])
        if resolved:
            out[0] = resolved
    return out


def _system_prompt_for_session(name: str) -> str:
    persona = user_cfg.persona
    user_name = (persona.user_name or "the user").strip()
    user_role = (persona.user_role or "not specified").strip()
    character = (persona.halo_character or "direct, practical voice coding assistant").strip()
    return (
        VOICE_SYSTEM_PROMPT
        .replace("{NAME}", name)
        .replace("{USER_NAME}", user_name)
        .replace("{USER_ROLE}", user_role)
        .replace("{HALO_CHARACTER}", character)
    )


class _SentenceBuffer:
    """Accumulates incremental text and yields complete sentences.

    Used to break Claude's stream-json text_delta chunks into TTS-able
    sentences. Sentence boundary = `.`/`!`/`?` followed by whitespace
    or end of buffer, OR a paragraph break (`\\n\\n`).
    """

    _TERMINATORS = (".", "!", "?")

    def __init__(self, on_sentence: Callable[[str], None]) -> None:
        self._buf = ""
        self._on_sentence = on_sentence
        self._all: list[str] = []

    def feed(self, chunk: str) -> None:
        if not chunk:
            return
        self._buf += chunk
        while True:
            idx = self._find_sentence_end()
            if idx < 0:
                return
            sentence = self._buf[: idx + 1].strip()
            self._buf = self._buf[idx + 1:]
            if sentence:
                self._all.append(sentence)
                try:
                    self._on_sentence(sentence)
                except Exception as exc:
                    print(f"    [stream] on_sentence error: {exc}")

    def flush(self) -> None:
        remaining = self._buf.strip()
        self._buf = ""
        if remaining:
            self._all.append(remaining)
            try:
                self._on_sentence(remaining)
            except Exception as exc:
                print(f"    [stream] on_sentence error (flush): {exc}")

    def full_text(self) -> str:
        return " ".join(self._all).strip()

    def _find_sentence_end(self) -> int:
        # Paragraph break beats sentence-terminator search.
        nn = self._buf.find("\n\n")
        for i, ch in enumerate(self._buf):
            if ch in self._TERMINATORS:
                nxt = self._buf[i + 1] if i + 1 < len(self._buf) else ""
                if not nxt or nxt.isspace():
                    if nn >= 0 and nn < i:
                        return nn  # paragraph break came first
                    return i
        if nn >= 0:
            return nn
        return -1


def _extract_text_delta(event: dict) -> str:
    """Pull the user-facing text out of a Claude stream-json event,
    if it carries one. Returns "" for events that aren't text deltas."""
    if event.get("type") != "stream_event":
        return ""
    inner = event.get("event") or {}
    if inner.get("type") != "content_block_delta":
        return ""
    delta = inner.get("delta") or {}
    if delta.get("type") != "text_delta":
        return ""
    return delta.get("text", "") or ""


def _extract_final_result(event: dict, field: str) -> Optional[str]:
    """If `event` is Claude's terminal `result` event, return its text."""
    if event.get("type") == "result" and isinstance(event.get(field), str):
        return event[field]
    return None


def _run(
    cmd: list[str],
    *,
    cwd: str,
    timeout: float,
    label: str,
    on_voice_tick: Optional[Callable[[float], None]] = None,
    on_text_chunk: Optional[Callable[[str], None]] = None,
    on_output_line: Optional[Callable[[str], None]] = None,
    stream_text_deltas: bool = False,
    json_result_field: str = "result",
) -> tuple[bool, str, str]:
    """Background-thread subprocess runner with status ticker.

    When `stream_text_deltas=True`, stdout is parsed as Claude-style
    newline-delimited JSON events. Text deltas are accumulated, broken
    into sentences, and fired through `on_text_chunk` for live TTS.
    The final `result` event becomes the returned stdout text; if it
    never arrives we fall back to the concatenated deltas.

    Returns (ok, stdout, stderr_tail). Prints "still working (Ns)"
    every TICKER_STDOUT_SEC; calls on_voice_tick(elapsed) every
    TICKER_VOICE_SEC so the orchestrator can speak through Kokoro.

    stdin=DEVNULL closes the input handle — Claude Code can hang on
    Windows when it can't tell if stdin is a TTY waiting for input
    (anthropics/claude-code#9026).
    """
    box: dict = {"proc": None, "stdout": "", "stderr_lines": []}
    done = threading.Event()
    started = time.monotonic()

    def _worker() -> None:
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cwd,
                text=True,
                bufsize=1,
                shell=False,
            )
        except FileNotFoundError as exc:
            box["error"] = f"executable not found: {exc.filename}"
            done.set()
            return
        box["proc"] = proc

        def _drain_stderr() -> None:
            stream = proc.stderr
            if stream is None:
                return
            try:
                for line in stream:
                    line = line.rstrip()
                    if line:
                        box["stderr_lines"].append(line)
                        print(f"    [{label}] {line}")
            except (ValueError, OSError):
                return

        stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
        stderr_thread.start()

        if stream_text_deltas:
            # Parse newline-delimited JSON events as they arrive; fire
            # text_delta chunks into the sentence buffer so TTS can
            # speak Claude's response while it's still generating.
            sentence_cb = on_text_chunk if on_text_chunk is not None else (lambda _s: None)
            buf = _SentenceBuffer(on_sentence=sentence_cb)
            final_text: Optional[str] = None
            try:
                stream = proc.stdout
                assert stream is not None
                for line in stream:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    delta = _extract_text_delta(event)
                    if delta:
                        buf.feed(delta)
                        continue
                    final = _extract_final_result(event, json_result_field)
                    if final is not None:
                        final_text = final
                buf.flush()
                try:
                    proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    box["error"] = f"timed out after {timeout:.0f}s"
                box["stdout"] = final_text if final_text is not None else buf.full_text()
            except (ValueError, OSError):
                buf.flush()
                box["stdout"] = buf.full_text()
            finally:
                stderr_thread.join(timeout=2.0)
                done.set()
        else:
            # Read stdout line-by-line (not communicate()) so a non-streaming
            # agent like Codex is VISIBLE: each line is pushed to the dashboard
            # as it arrives instead of the session going dark until it finishes.
            # The final result is the joined output, same as before.
            lines: list[str] = []
            try:
                stream = proc.stdout
                if stream is not None:
                    for line in stream:
                        line = line.rstrip("\n")
                        lines.append(line)
                        if line.strip():
                            print(f"    [{label}] {line}")
                            if on_output_line is not None:
                                try:
                                    on_output_line(line)
                                except Exception:
                                    pass
                try:
                    proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    box["error"] = f"timed out after {timeout:.0f}s"
                box["stdout"] = "\n".join(lines)
            except (ValueError, OSError):
                box["stdout"] = "\n".join(lines)
            finally:
                stderr_thread.join(timeout=2.0)
                done.set()

    worker = threading.Thread(target=_worker, daemon=True)
    worker.start()

    last_stdout_tick = started
    last_voice_tick = started
    try:
        while not done.wait(timeout=1.0):
            now = time.monotonic()
            if now - last_stdout_tick >= TICKER_STDOUT_SEC:
                print(f"    [{label}] still working ({now - started:.0f}s)...")
                last_stdout_tick = now
            if on_voice_tick and now - last_voice_tick >= TICKER_VOICE_SEC:
                try:
                    on_voice_tick(now - started)
                except Exception as exc:
                    print(f"    [{label}] voice tick error: {exc}")
                last_voice_tick = now
    except KeyboardInterrupt:
        if box.get("proc") is not None:
            box["proc"].kill()
        raise

    if "error" in box:
        return False, box.get("stdout", ""), box["error"]

    proc = box["proc"]
    if proc is None or proc.returncode != 0:
        tail = box["stderr_lines"][-1] if box["stderr_lines"] else (
            f"exit {proc.returncode}" if proc is not None else "unknown error"
        )
        return False, box.get("stdout", ""), tail
    return True, box.get("stdout", ""), ""


# Set to True once we've failed to spawn a persistent claude session
# for an agent. Sticky for the Halo process lifetime so we don't keep
# paying the 0.5s probe cost on a CLI version that doesn't support
# --input-format stream-json — fall back to one-shot for the rest of
# the run.
_persistent_disabled: dict[str, str] = {}


def _dispatch_persistent(
    config: AgentConfig,
    *,
    prompt: str,
    cwd: str,
    system_prompt: str,
    timeout: float,
    on_text_chunk: Optional[Callable[[str], None]],
) -> tuple[bool, str, str]:
    """Send `prompt` to the long-lived process for `(config.key, cwd)`.

    Lazy-spawns the process on first call, reuses it on every
    subsequent call. On a dead-process detection (broken pipe, exit)
    the next call transparently respawns once. Returns the same
    `(ok, stdout, stderr_tail)` shape as `_run`.

    v1.2: keyed by session_key(config.key, cwd) so multiple projects
    each get their own persistent Claude.
    """
    from halo import sessions as _sessions

    if not config.persistent_call:
        return False, "", "no persistent_call template configured"

    argv = _build_cmd(
        config.persistent_call,
        prompt="",  # unused in persistent mode
        cwd=cwd,
        system_prompt=system_prompt,
    )

    # Per-agent override wins over the global flag; otherwise follow it.
    from halo.config import AGENT_CLI_VISIBLE
    verbose = (
        config.cli_visible
        if config.cli_visible is not None
        else AGENT_CLI_VISIBLE
    )

    # Session is per-(agent, cwd). Label = "<agent>@<basename>" so the
    # popup window title and log filename stay readable when several
    # projects are live at once.
    skey = session_key(config.key, cwd)
    label = f"{config.key}-{Path(cwd).name or 'root'}"

    # Try once, retry once on a respawn-after-death; bail on a second
    # consecutive failure so we don't spin.
    last_err = ""
    for attempt in (1, 2):
        try:
            sess, was_new = _sessions.get_or_create(
                skey, argv, cwd=cwd, label=label, verbose=verbose,
            )
        except _sessions.SessionStartupError as exc:
            _persistent_disabled[config.key] = str(exc)
            return False, "", f"persistent spawn failed: {exc}"

        if was_new:
            bus.emit(
                "agent.session_spawned",
                agent=config.key,
                cwd=cwd,
                send_no=sess.send_count + 1,
            )
            print(f"    [{label}] persistent session spawned")
        else:
            print(
                f"    [{label}] reusing persistent session "
                f"(send #{sess.send_count + 1})"
            )

        try:
            ok, text = sess.send(prompt, on_text_chunk=on_text_chunk, timeout=timeout)
        except _sessions.SessionBusy as exc:
            return False, "", str(exc)
        except RuntimeError as exc:
            # Pipe broke / process died. Close the corpse and let the
            # next attempt respawn.
            last_err = str(exc)
            print(f"    [{label}] persistent session died: {exc} — respawning")
            bus.emit("agent.respawn", agent=config.key, cwd=cwd, reason=last_err)
            _sessions.close(skey)
            if attempt == 2:
                break
            continue

        if ok:
            return True, text, ""
        return False, "", text

    return False, "", last_err or "persistent dispatch failed twice"


def dispatch(
    agent_key: str,
    prompt: str,
    *,
    cwd: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT_SEC,
    on_voice_tick: Optional[Callable[[float], None]] = None,
    on_text_chunk: Optional[Callable[[str], None]] = None,
    on_output_line: Optional[Callable[[str], None]] = None,
) -> tuple[bool, str]:
    """Synchronous one-shot dispatch (used by start_job and tests).

    Most callers should use `start_job(...)` instead so the
    conversation loop isn't blocked while the agent runs.

    `cwd` (v1.2) — working directory for this dispatch. Defaults to the
    Halo launch dir (DEFAULT_CWD) when None, matching v1.1 behavior.
    Session state (continuation flag, spoken name, persistent process)
    is keyed by (agent_key, cwd) so multiple projects can each have
    their own live Claude session.

    `on_text_chunk` is invoked per sentence while the agent generates
    its response (only for agents with `streams_text_deltas=True`).
    """
    config = AGENTS.get(agent_key)
    if config is None:
        return False, f"Unknown agent: {agent_key!r}. Known: {known_agents()}"

    workdir = str(cwd) if cwd else str(DEFAULT_CWD)
    skey = session_key(config.key, workdir)

    # Assign / reuse a spoken name for this (agent, cwd) session.
    name = session_name(config.key, workdir)
    system_prompt = _system_prompt_for_session(name)

    # For agents that don't take a system-prompt flag (Codex), prepend
    # the voice brief to the user prompt directly. Use a fenced marker
    # so the agent can recognize where its actual instruction starts.
    if not config.accepts_system_prompt_arg:
        prompt = (
            "[Voice interface preamble — read once, then respond per the "
            "user's actual request below.]\n"
            + system_prompt
            + "\n\n[User request:] "
            + prompt
        )

    persistent_first_choice = (
        config.session_kind == "persistent"
        and config.key not in _persistent_disabled
    )

    ok = False
    stdout = ""
    err = ""

    if persistent_first_choice:
        with _session_state_lock:
            _sessions_active[skey] = True  # process IS the session
        ok, stdout, err = _dispatch_persistent(
            config,
            prompt=prompt,
            cwd=workdir,
            system_prompt=system_prompt,
            timeout=timeout,
            on_text_chunk=on_text_chunk if config.streams_text_deltas else None,
        )
        # If spawn failed because the CLI version lacks --input-format
        # stream-json, _dispatch_persistent populates _persistent_disabled.
        # Fall through to one-shot for THIS call so the user isn't left
        # hanging.
        if not ok and config.key not in _persistent_disabled:
            with _session_state_lock:
                _sessions_active[skey] = False
            return False, f"{config.spoken_name} failed: {err}"
        if not ok:
            with _session_state_lock:
                _sessions_active[skey] = False

    if not ok:
        # One-shot dispatch — either the agent is one-shot by design
        # (Codex) or persistent spawn fell back due to CLI mismatch.
        # See _persistent_disabled note above for the latter case.
        with _session_state_lock:
            was_active = _sessions_active.get(skey, False)
            _sessions_active[skey] = True
        template = config.continue_call if was_active else config.first_call
        cmd = _build_cmd(
            template, prompt=prompt, cwd=workdir, system_prompt=system_prompt,
        )
        ok, stdout, err = _run(
            cmd, cwd=workdir, timeout=timeout,
            label=f"{config.key}-{Path(workdir).name or 'root'}",
            on_voice_tick=on_voice_tick,
            on_text_chunk=on_text_chunk if config.streams_text_deltas else None,
            on_output_line=on_output_line if not config.streams_text_deltas else None,
            stream_text_deltas=config.streams_text_deltas,
            json_result_field=config.json_result_field,
        )
        if not ok:
            _sessions_active[skey] = False
            return False, f"{config.spoken_name} failed: {err}"

    if config.streams_text_deltas:
        # The streaming branch already produced the canonical text
        # (final result event or accumulated deltas).
        text = stdout.strip()
    elif config.parses_json:
        try:
            data = json.loads(stdout)
            text = (data.get(config.json_result_field) or "").strip()
        except json.JSONDecodeError:
            text = stdout.strip()
    else:
        text = stdout.strip()

    return True, text


# Backwards-compatible wrappers so existing callers and tests keep working.
def dispatch_claude_code(prompt: str, **kwargs) -> tuple[bool, str]:
    return dispatch("claude_code", prompt, **kwargs)


def dispatch_codex(prompt: str, **kwargs) -> tuple[bool, str]:
    return dispatch("codex_cli", prompt, **kwargs)


# ---------------------------------------------------------------------------
# Async job registry — concurrent conversation
# ---------------------------------------------------------------------------

@dataclass
class AgentJob:
    """One in-flight or finished agent invocation.

    v1.2 adds `cwd` so the dashboard and registry can distinguish jobs
    that target the same agent across different projects.
    """

    job_id: int
    agent_key: str
    prompt: str
    started_at: float
    cwd: str = ""  # filled at start_job; "" means DEFAULT_CWD
    completed_at: Optional[float] = None
    ok: Optional[bool] = None
    result: str = ""
    _consumed: bool = False
    _thread: Optional[threading.Thread] = field(default=None, repr=False)

    @property
    def is_done(self) -> bool:
        return self.completed_at is not None

    @property
    def elapsed_sec(self) -> float:
        end = self.completed_at if self.completed_at is not None else time.monotonic()
        return end - self.started_at

    @property
    def session_key(self) -> str:
        return session_key(self.agent_key, self.cwd or None)


_jobs: list[AgentJob] = []
_jobs_lock = threading.Lock()
_job_id_seq = itertools.count(1)
# Keyed by session_key (agent_key@cwd) so cross-project lookups work.
_last_by_session: dict[str, AgentJob] = {}

# --- Voice floor (single-speaker guarantee) --------------------------------
# Only ONE job at a time may narrate live or murmur keepalives. Without this,
# a fan-out ("dispatch to all") makes every session's _speak_chunk call say()
# concurrently and the spoken audio interleaves into garbage. The most
# recently dispatched streaming job claims the floor; older jobs keep running
# and are summarized on completion, but stop speaking live.
_voice_floor_job_id: Optional[int] = None
_voice_floor_lock = threading.Lock()


def claim_voice_floor(job_id: int) -> None:
    """Make `job_id` the sole live speaker. Call right after start_job."""
    global _voice_floor_job_id
    with _voice_floor_lock:
        _voice_floor_job_id = job_id


def owns_voice_floor(job_id: Optional[int]) -> bool:
    """True if `job_id` currently owns the floor (or no job_id is set yet —
    the just-dispatched foreground turn, before its id is registered)."""
    if job_id is None:
        return True
    with _voice_floor_lock:
        return _voice_floor_job_id == job_id


def release_voice_floor(job_id: int) -> None:
    """Drop the floor if `job_id` still holds it (job finished)."""
    global _voice_floor_job_id
    with _voice_floor_lock:
        if _voice_floor_job_id == job_id:
            _voice_floor_job_id = None


# Keepalive cadence — how Halo fills dead air during a long, quiet job
# (e.g. Claude running a multi-second tool with no text deltas).
_KEEPALIVE_POLL_SEC = 2.0      # how often the watchdog checks
_KEEPALIVE_QUIET_SEC = 13.0    # silence before the first murmur
_KEEPALIVE_MAX = 4             # cap murmurs per job so it isn't naggy


class AgentBusy(RuntimeError):
    """Raised when start_job is called for an agent that already has an
    active job in the same cwd. v1.2 allows one concurrent job per
    (agent, cwd) pair — different cwds can run in parallel."""


def start_job(
    agent_key: str,
    prompt: str,
    *,
    cwd: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT_SEC,
    on_text_chunk: Optional[Callable[[str], None]] = None,
) -> AgentJob:
    """Spawn an agent in the background. Returns immediately.

    `cwd` (v1.2) — working directory; defaults to DEFAULT_CWD. Per-(agent,
    cwd) busy-check means the same agent can have parallel jobs across
    different projects, but not two jobs in the same project.

    `on_text_chunk(sentence)` fires once per sentence while the agent
    generates its response (streaming-capable agents only). The
    orchestrator passes a TTS-speaker callback so Halo narrates the
    response live instead of going dead-air on long jobs.

    Raises AgentBusy if the agent already has a job running in `cwd`.
    """
    if agent_key not in AGENTS:
        raise ValueError(f"Unknown agent {agent_key!r}. Known: {known_agents()}")

    workdir = str(cwd) if cwd else str(DEFAULT_CWD)
    skey = session_key(agent_key, workdir)

    with _jobs_lock:
        for j in _jobs:
            if j.session_key == skey and not j.is_done:
                raise AgentBusy(
                    f"{AGENTS[agent_key].spoken_name} is already working on "
                    f"job {j.job_id} in {Path(workdir).name or workdir} "
                    f"({int(j.elapsed_sec)}s in)."
                )

        job = AgentJob(
            job_id=next(_job_id_seq),
            agent_key=agent_key,
            prompt=prompt,
            started_at=time.monotonic(),
            cwd=workdir,
        )

        # Wrap the user's on_text_chunk so we can also push streaming
        # sentences onto the bus for the web dashboard. A sentence only
        # reaches the live TTS speaker if THIS job owns the voice floor —
        # otherwise (a concurrent / fan-out job) it's still emitted to the
        # dashboard and accumulated for the end-of-job summary, but stays
        # silent so two agents never talk over each other.
        spoken = session_name(agent_key, workdir)
        _last_chunk_at = time.monotonic()
        _chunk_lock = threading.Lock()

        def _on_chunk(sentence: str) -> None:
            nonlocal _last_chunk_at
            with _chunk_lock:
                _last_chunk_at = time.monotonic()
            bus.emit(
                "agent.streaming",
                agent=agent_key, name=spoken, cwd=workdir, sentence=sentence,
            )
            if on_text_chunk is not None and owns_voice_floor(job.job_id):
                on_text_chunk(sentence)

        def _on_output(line: str) -> None:
            """Non-streaming agents (Codex): push each stdout line to the
            dashboard so the session is VISIBLE live. Shown, never spoken —
            and it resets the keepalive clock so murmurs don't fire while
            Codex is actively producing output."""
            nonlocal _last_chunk_at
            with _chunk_lock:
                _last_chunk_at = time.monotonic()
            bus.emit(
                "agent.streaming",
                agent=agent_key, name=spoken, cwd=workdir, sentence=line,
            )

        def _keepalive() -> None:
            """Fill dead air: if the foreground job goes quiet for a while
            (long tool run, no text deltas), murmur a short 'still working'
            so the user knows Halo didn't freeze. Persistent Claude sessions
            had no such cue before (audit finding)."""
            nonlocal _last_chunk_at  # we read AND reassign it below
            from halo import voice  # lazy — voice doesn't import agents
            murmured = 0
            while not job.is_done and murmured < _KEEPALIVE_MAX:
                time.sleep(_KEEPALIVE_POLL_SEC)
                if job.is_done:
                    break
                # Quiet/work mode: no "still working" murmurs at all.
                if voice.is_quiet():
                    continue
                # Only the active speaker murmurs, and only when nothing is
                # already playing and the stream has actually gone silent.
                if not owns_voice_floor(job.job_id):
                    continue
                if voice.is_speaking():
                    continue
                with _chunk_lock:
                    quiet = time.monotonic() - _last_chunk_at
                if quiet < _KEEPALIVE_QUIET_SEC:
                    continue
                voice.say_one_of(voice.WORKING_PHRASES, blocking=False)
                murmured += 1
                with _chunk_lock:
                    _last_chunk_at = time.monotonic()  # space the next one out

        def _runner() -> None:
            bus.emit(
                "agent.dispatched",
                agent=agent_key, name=spoken, cwd=workdir, prompt=prompt,
                job_id=job.job_id,
            )
            ka = threading.Thread(
                target=_keepalive, daemon=True, name=f"keepalive-{job.job_id}"
            )
            ka.start()
            try:
                ok, text = dispatch(
                    agent_key, prompt,
                    cwd=Path(workdir), timeout=timeout,
                    on_text_chunk=_on_chunk,
                    on_output_line=_on_output,
                )
            except Exception as exc:  # safety net so the thread never crashes silently
                ok, text = False, f"unexpected error: {exc}"
            job.ok = ok
            job.result = text
            job.completed_at = time.monotonic()
            release_voice_floor(job.job_id)
            with _session_state_lock:
                _last_by_session[skey] = job
            bus.emit(
                "agent.done" if ok else "agent.error",
                agent=agent_key, name=spoken, cwd=workdir,
                job_id=job.job_id,
                elapsed_sec=job.elapsed_sec,
                text=text,
            )

        thread = threading.Thread(target=_runner, daemon=True, name=f"agent-{job.job_id}")
        job._thread = thread
        _jobs.append(job)
        # Claim the voice floor BEFORE the runner starts so this dispatch's
        # very first streamed sentence isn't suppressed by the gate in
        # _on_chunk. The latest dispatch always wins the floor.
        claim_voice_floor(job.job_id)

    thread.start()
    return job


def active_jobs() -> list[AgentJob]:
    with _jobs_lock:
        return [j for j in _jobs if not j.is_done]


def completed_unconsumed_jobs() -> list[AgentJob]:
    """Jobs that finished but haven't been spoken back to the user yet."""
    with _jobs_lock:
        return [j for j in _jobs if j.is_done and not j._consumed]


def mark_consumed(job: AgentJob) -> None:
    job._consumed = True


def last_result_for(agent_key: str, cwd: Path | str | None = None) -> Optional[AgentJob]:
    """Most recent completed job for an agent — used by 'what did X say' queries.

    v1.2: cwd=None matches the most recent job for `agent_key` across
    ALL projects (back-compat with v1.1 callers). Pass a cwd to scope
    to one project.
    """
    with _session_state_lock:
        if cwd is not None:
            return _last_by_session.get(session_key(agent_key, cwd))
        values = list(_last_by_session.values())
    # Find the most recent across any cwd.
    best: Optional[AgentJob] = None
    for j in values:
        if j.agent_key != agent_key or j.completed_at is None:
            continue
        if best is None or (j.completed_at or 0) > (best.completed_at or 0):
            best = j
    return best


def status_summary() -> str:
    """One-line voice-friendly status across all (agent, cwd) pairs."""
    actives = active_jobs()
    if not actives:
        # Mention the most recent completed job per session if any.
        with _session_state_lock:
            recent = list(_last_by_session.values())
        if not recent:
            return "Everything is idle."
        parts = []
        for j in recent:
            if j.completed_at is None:
                continue
            cfg = AGENTS[j.agent_key]
            where = Path(j.cwd).name if j.cwd else ""
            tag = f"{cfg.spoken_name} in {where}" if where else cfg.spoken_name
            parts.append(
                f"{tag} finished {int(time.monotonic() - j.completed_at)} seconds ago"
            )
        return "Idle. " + ". ".join(parts) + "." if parts else "Everything is idle."
    parts = []
    for j in actives:
        cfg = AGENTS[j.agent_key]
        snippet = j.prompt if len(j.prompt) <= 60 else j.prompt[:57] + "..."
        where = Path(j.cwd).name if j.cwd else ""
        tag = f"{cfg.spoken_name} in {where}" if where else cfg.spoken_name
        parts.append(f"{tag} is working on '{snippet}', {int(j.elapsed_sec)} seconds in")
    return ". ".join(parts) + "."


def summarize_for_speech(text: str, max_chars: int = 280) -> str:
    """Trim a long agent response to something speakable. Keeps first
    sentence(s) up to `max_chars` chars."""
    text = text.strip()
    if len(text) <= max_chars:
        return text
    head = text[:max_chars]
    for stop in (". ", "! ", "? ", "\n\n"):
        idx = head.rfind(stop)
        if idx > max_chars * 0.5:
            return head[: idx + 1].strip()
    return head.rstrip() + "..."
