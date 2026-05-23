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
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

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
    "6. If you need clarification, ask ONE short question.\n\n"
    "## YOUR SESSION NAME\n"
    "Your spoken name in this session is {NAME}. You may introduce "
    "yourself by that name once at the start of a session.\n\n"
    "## EXAMPLES\n"
    "Bad:  'Created at `D:\\\\Halo\\\\hello.py` — a one-line script.\\n\\n"
    "**Output:**\\n```python\\nprint(\"hello\")\\n```'\n"
    "Good: 'I wrote it to hello dot py.'\n\n"
    "Bad:  '- Added auth\\n- Wrote tests\\n- Updated README'\n"
    "Good: 'I added auth, wrote tests, and updated the readme.'"
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


AGENTS: dict[str, AgentConfig] = {
    "claude_code": AgentConfig(
        key="claude_code",
        spoken_name="Claude",
        voice_triggers=("claude", "claude code"),
        first_call=(
            "claude", "-p", "{PROMPT}",
            "--output-format", "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--permission-mode", "acceptEdits",
            "--append-system-prompt", "{SYSTEM_PROMPT}",
        ),
        continue_call=(
            "claude", "-p", "{PROMPT}",
            "--output-format", "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--permission-mode", "acceptEdits",
            "--continue",
            "--append-system-prompt", "{SYSTEM_PROMPT}",
        ),
        parses_json=True,
        json_result_field="result",
        sandbox_label="acceptEdits",
        accepts_system_prompt_arg=True,
        streams_text_deltas=True,
    ),
    "codex_cli": AgentConfig(
        key="codex_cli",
        spoken_name="Codex",
        voice_triggers=("codex", "open ai codex", "openai codex"),
        first_call=(
            "codex", "exec",
            "--sandbox", "workspace-write",
            "-C", "{CWD}",
            "{PROMPT}",
        ),
        continue_call=(
            "codex", "exec", "resume", "--last",
            "--sandbox", "workspace-write",
            "-C", "{CWD}",
            "{PROMPT}",
        ),
        parses_json=False,
        sandbox_label="workspace-write",
        accepts_system_prompt_arg=False,
    ),
}


# Per-agent session-continuation flag. Persists for the Halo process
# lifetime so a follow-up after a wake reuses the same thread.
_sessions_active: dict[str, bool] = {key: False for key in AGENTS}
# Per-agent spoken session name ("Mars", "Juno", ...). Reset when the
# user starts a new session for that agent.
_session_names: dict[str, str] = {}


def _new_session_name(agent_key: str) -> str:
    # Don't repeat the current name for the same agent if we can help it.
    current = _session_names.get(agent_key)
    pool = [n for n in _MYTHOLOGY_NAMES if n != current] or list(_MYTHOLOGY_NAMES)
    return random.choice(pool)


def session_name(agent_key: str) -> str:
    """Spoken name for the live (or last) session of `agent_key`."""
    if agent_key not in _session_names:
        _session_names[agent_key] = _new_session_name(agent_key)
    return _session_names[agent_key]


def reset_session(agent: str | None = None) -> None:
    """Drop the continuation pointer for one agent or all of them.

    Also rotates the spoken name, so the next dispatch introduces itself
    fresh. Use on explicit "new task" / "start over" / "fresh session"
    cues from the user — NOT between conversations.
    """
    targets = list(_sessions_active.keys()) if agent is None else [agent]
    for key in targets:
        if key in _sessions_active:
            _sessions_active[key] = False
            _session_names[key] = _new_session_name(key)


def session_status() -> dict[str, bool]:
    """Which agents have a live session in this Halo process."""
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

    `on_text_chunk` is invoked per sentence while the agent generates
    its response (only for agents with `streams_text_deltas=True`).
    """
    config = AGENTS.get(agent_key)
    if config is None:
        return False, f"Unknown agent: {agent_key!r}. Known: {known_agents()}"

    # Assign / reuse a spoken name for this session.
    name = session_name(config.key)
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

    workdir = str(cwd) if cwd else str(DEFAULT_CWD)
    template = config.continue_call if _sessions_active[config.key] else config.first_call
    cmd = _build_cmd(
        template, prompt=prompt, cwd=workdir, system_prompt=system_prompt,
    )

    ok, stdout, err = _run(
        cmd, cwd=workdir, timeout=timeout,
        label=config.key,
        on_voice_tick=on_voice_tick,
        on_text_chunk=on_text_chunk if config.streams_text_deltas else None,
        stream_text_deltas=config.streams_text_deltas,
        json_result_field=config.json_result_field,
    )
    if not ok:
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

    _sessions_active[config.key] = True
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
    """One in-flight or finished agent invocation."""

    job_id: int
    agent_key: str
    prompt: str
    started_at: float
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


_jobs: list[AgentJob] = []
_jobs_lock = threading.Lock()
_job_id_seq = itertools.count(1)
_last_by_agent: dict[str, AgentJob] = {}


class AgentBusy(RuntimeError):
    """Raised when start_job is called for an agent that already has an
    active job. v0.1 keeps it simple — at most one job per agent."""


def start_job(
    agent_key: str,
    prompt: str,
    *,
    cwd: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT_SEC,
    on_text_chunk: Optional[Callable[[str], None]] = None,
) -> AgentJob:
    """Spawn an agent in the background. Returns immediately.

    `on_text_chunk(sentence)` fires once per sentence while the agent
    generates its response (streaming-capable agents only). The
    orchestrator passes a TTS-speaker callback so Halo narrates the
    response live instead of going dead-air on long jobs.

    Raises AgentBusy if the agent already has a job running. v0.1
    allows concurrent jobs across different agents (Claude + Codex
    can both work at once) but not within the same agent.
    """
    if agent_key not in AGENTS:
        raise ValueError(f"Unknown agent {agent_key!r}. Known: {known_agents()}")

    with _jobs_lock:
        for j in _jobs:
            if j.agent_key == agent_key and not j.is_done:
                raise AgentBusy(
                    f"{AGENTS[agent_key].spoken_name} is already working on "
                    f"job {j.job_id} ({int(j.elapsed_sec)}s in)."
                )

        job = AgentJob(
            job_id=next(_job_id_seq),
            agent_key=agent_key,
            prompt=prompt,
            started_at=time.monotonic(),
        )

        def _runner() -> None:
            try:
                ok, text = dispatch(
                    agent_key, prompt,
                    cwd=cwd, timeout=timeout,
                    on_text_chunk=on_text_chunk,
                )
            except Exception as exc:  # safety net so the thread never crashes silently
                ok, text = False, f"unexpected error: {exc}"
            job.ok = ok
            job.result = text
            job.completed_at = time.monotonic()
            _last_by_agent[agent_key] = job

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


def last_result_for(agent_key: str) -> Optional[AgentJob]:
    """Most recent completed job for an agent — used by 'what did X say' queries."""
    return _last_by_agent.get(agent_key)


def status_summary() -> str:
    """One-line voice-friendly status across all agents."""
    actives = active_jobs()
    if not actives:
        # Mention the most recent completed job per agent if any.
        if not _last_by_agent:
            return "Everything is idle."
        parts = [
            f"{AGENTS[a].spoken_name} finished {int(time.monotonic() - j.completed_at)} seconds ago"
            for a, j in _last_by_agent.items()
            if j.completed_at is not None
        ]
        return "Idle. " + ". ".join(parts) + "." if parts else "Everything is idle."
    parts = []
    for j in actives:
        cfg = AGENTS[j.agent_key]
        snippet = j.prompt if len(j.prompt) <= 60 else j.prompt[:57] + "..."
        parts.append(f"{cfg.spoken_name} is working on '{snippet}', {int(j.elapsed_sec)} seconds in")
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
