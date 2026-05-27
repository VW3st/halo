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
import random
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal, Optional

from halo import bus

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
    "## EXAMPLES\n"
    "Bad:  'Created at `D:\\\\Halo\\\\hello.py` — a one-line script.\\n\\n"
    "**Output:**\\n```python\\nprint(\"hello\")\\n```'\n"
    "Good: 'I wrote it to hello dot py.'\n\n"
    "Bad:  '- Added auth\\n- Wrote tests\\n- Updated README'\n"
    "Good: 'I added auth, wrote tests, and updated the readme.'\n\n"
    "Bad:  (silent for 40 seconds while running a full test suite)\n"
    "Good: 'Running the test suite, this takes about thirty seconds.'"
)

# Pool of Roman mythology names; one is assigned per agent session and
# spoken back to the user ("Mars is on it", "Juno wrote it to login.html").
_MYTHOLOGY_NAMES = (
    "Mars", "Mercury", "Neptune", "Jupiter", "Apollo", "Diana", "Minerva",
    "Vulcan", "Juno", "Saturn", "Bacchus", "Ceres", "Pluto", "Vesta",
    "Janus", "Aurora", "Flora", "Fortuna",
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
        spoken_name="Claude",
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
        fuzzy_triggers=("claud", "clawed", "clod", "cloud", "clawde", "claus"),
    ),
    "codex_cli": AgentConfig(
        key="codex_cli",
        spoken_name="Codex",
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
        fuzzy_triggers=("codec", "codecs", "kodex", "co dex", "co-dex"),
    ),
}


# Per-agent connectivity cache. Filled lazily by `check_availability()`
# and refreshed only on explicit request (so the dashboard's /api/state
# poll doesn't spawn `claude --version` every 750 ms). The user's
# binaries don't appear/disappear during a session.
_availability_cache: dict[str, dict] = {}
_availability_lock = threading.Lock()


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
            installed = shutil.which(binary) is not None
            responsive = installed
            if installed and cfg.check_call:
                try:
                    proc = subprocess.run(
                        list(cfg.check_call),
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


def _new_session_name(skey: str) -> str:
    # Don't repeat the current name for the same session if we can help it.
    current = _session_names.get(skey)
    pool = [n for n in _MYTHOLOGY_NAMES if n != current] or list(_MYTHOLOGY_NAMES)
    return random.choice(pool)


def session_name(agent_key: str, cwd: Path | str | None = None) -> str:
    """Spoken name for the live (or last) session of `agent_key` in `cwd`."""
    skey = session_key(agent_key, cwd)
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
        # Persistent process teardown — only for Claude-style agents.
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
    for skey, active in _sessions_active.items():
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
    return dict(_sessions_active)


def known_agents() -> list[str]:
    return list(AGENTS.keys())


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
    return out


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
            try:
                stdout, _ = proc.communicate(timeout=timeout)
                box["stdout"] = stdout or ""
            except subprocess.TimeoutExpired:
                proc.kill()
                box["error"] = f"timed out after {timeout:.0f}s"
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
    system_prompt = VOICE_SYSTEM_PROMPT.replace("{NAME}", name)

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
            _sessions_active[skey] = False
            return False, f"{config.spoken_name} failed: {err}"
        if not ok:
            _sessions_active[skey] = False

    if not ok:
        # One-shot dispatch — either the agent is one-shot by design
        # (Codex) or persistent spawn fell back due to CLI mismatch.
        # See _persistent_disabled note above for the latter case.
        was_active = _sessions_active.get(skey, False)
        template = config.continue_call if was_active else config.first_call
        _sessions_active[skey] = True
        cmd = _build_cmd(
            template, prompt=prompt, cwd=workdir, system_prompt=system_prompt,
        )
        ok, stdout, err = _run(
            cmd, cwd=workdir, timeout=timeout,
            label=f"{config.key}-{Path(workdir).name or 'root'}",
            on_voice_tick=on_voice_tick,
            on_text_chunk=on_text_chunk if config.streams_text_deltas else None,
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
        # sentences onto the bus for the web dashboard.
        spoken = session_name(agent_key, workdir)

        def _on_chunk(sentence: str) -> None:
            bus.emit(
                "agent.streaming",
                agent=agent_key, name=spoken, cwd=workdir, sentence=sentence,
            )
            if on_text_chunk is not None:
                on_text_chunk(sentence)

        def _runner() -> None:
            bus.emit(
                "agent.dispatched",
                agent=agent_key, name=spoken, cwd=workdir, prompt=prompt,
                job_id=job.job_id,
            )
            try:
                ok, text = dispatch(
                    agent_key, prompt,
                    cwd=Path(workdir), timeout=timeout,
                    on_text_chunk=_on_chunk,
                )
            except Exception as exc:  # safety net so the thread never crashes silently
                ok, text = False, f"unexpected error: {exc}"
            job.ok = ok
            job.result = text
            job.completed_at = time.monotonic()
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
    if cwd is not None:
        return _last_by_session.get(session_key(agent_key, cwd))
    # Find the most recent across any cwd.
    best: Optional[AgentJob] = None
    for j in _last_by_session.values():
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
        if not _last_by_session:
            return "Everything is idle."
        parts = []
        for j in _last_by_session.values():
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
