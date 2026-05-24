"""Persistent CLI sessions — one long-lived agent process per Halo session.

`ClaudeSession` wraps a single `claude -p --input-format stream-json
--output-format stream-json` subprocess that stays alive for the whole
Halo session. Every voice turn is shipped to it as one newline-delimited
JSON user message on stdin; the response streams back as JSON events on
stdout (the same `stream_event` / `result` shape `halo.agents._run`
already parses).

Why: the previous "spawn-claude-per-turn + --continue" model raced with
itself — a follow-up dispatch arriving while the previous process was
still streaming would either pick up the wrong session or close the
pipe and exit with `None`. A single long-lived process per logical
session removes the race entirely, eliminates per-turn cold-start cost,
and is the model the official `--input-format stream-json` flag exists
to support.

Codex CLI doesn't expose a stream-json input mode, so it stays one-shot
in `halo.agents._run`. This module is Claude-only by design.
"""

from __future__ import annotations

import atexit
import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import Future
from pathlib import Path
from typing import Callable, Optional

from halo import bus
from halo.agents import (
    _SentenceBuffer,
    _extract_final_result,
    _extract_text_delta,
)

# Windows: stop the conhost flash when claude spawns from Halo's daemon.
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

# Per-session log files for the popup-console monitor. Created on first
# verbose session start. Cleaned up at process exit by close_all().
_RUNTIME_DIR = Path.home() / ".halo" / "runtime"


def _redact_argv(argv: list[str]) -> str:
    """Render the spawn argv for the popup header. The full VOICE_SYSTEM_PROMPT
    that follows `--append-system-prompt` is ~3 KB of voice rules and dumps
    a wall of text into the popup. Replace it with a `<voice-prompt>`
    placeholder so the argv stays one readable line."""
    out: list[str] = []
    skip_next = False
    for a in argv:
        if skip_next:
            out.append("<voice-prompt>")
            skip_next = False
            continue
        out.append(a)
        if a == "--append-system-prompt":
            skip_next = True
    return " ".join(out)


def _spawn_monitor_console(log_path: Path, label: str) -> Optional[subprocess.Popen]:
    """When the persistent agent is verbose, open a SEPARATE console window
    that live-tails the session's event log. This is what most users mean
    by "see the CLI" — a dedicated window where they can watch Claude
    work without scrolling through Halo's own output.

    Returns the monitor's Popen handle (so we can terminate it on close)
    or None on platforms without a clean way to do this (Mac/Linux).
    """
    if sys.platform != "win32":
        return None
    cmd = [
        "powershell",
        "-NoExit",
        "-NoLogo",
        "-NoProfile",
        "-Command",
        f"$Host.UI.RawUI.WindowTitle = 'Halo: {label}'; "
        f"Write-Host 'Live feed of Claude session ({label}) — close when done.' -ForegroundColor Cyan; "
        f"Write-Host ''; "
        f"Get-Content -Wait -Path '{log_path}'",
    ]
    try:
        # CRITICAL: do NOT redirect stdout/stderr here. CREATE_NEW_CONSOLE
        # gives the child its own console window; if we also pipe stdout
        # to DEVNULL, PowerShell writes its `Get-Content -Wait` output to
        # the null device instead of the new console — popup opens but
        # stays empty. Letting Popen inherit the parent's stdio is also
        # wrong (would dump Claude output into Halo's terminal); the
        # right move is to omit the redirects entirely so the child
        # binds its OWN stdio handles to the new console.
        return subprocess.Popen(
            cmd,
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
    except Exception as exc:
        print(f"  [{label}] could not spawn monitor console: {exc}")
        return None


class SessionStartupError(RuntimeError):
    """Spawn failed (binary missing, flag unsupported, immediate exit)."""


class SessionBusy(RuntimeError):
    """A `send()` is already in flight on this session — wait for it."""


class ClaudeSession:
    """One long-lived `claude` process. Reused across every turn in a
    Halo session; respawned only on explicit reset or hard failure.

    Concurrency model: one in-flight `send()` at a time. The orchestrator
    already serialises voice turns (the conversation loop waits for TTS
    to drain before the next mic open), so the lock is mostly belt-and-
    braces — `SessionBusy` surfaces if a second send slips through.
    """

    _STDOUT_READ_BLOCK_SEC = 0.5

    def __init__(
        self,
        agent_key: str,
        argv: list[str],
        *,
        cwd: str,
        label: str,
        verbose: bool = False,
    ) -> None:
        self.agent_key = agent_key
        self._argv = list(argv)
        self._cwd = cwd
        self._label = label
        # When True, _read_stdout prints a per-event trace so the user
        # can watch Claude work (text deltas streaming, tool calls
        # firing, the result event landing). Wired from
        # config.AGENT_CLI_VISIBLE (with per-agent override). Also opens
        # a separate console window tailing the per-session log file.
        self._verbose = verbose
        self._proc: Optional[subprocess.Popen] = None
        self._stdin_lock = threading.Lock()
        self._send_lock = threading.Lock()  # one in-flight send at a time
        self._reader_thread: Optional[threading.Thread] = None
        self._stderr_thread: Optional[threading.Thread] = None
        self._send_count = 0
        self._pending: Optional[dict] = None  # {"future", "buf", "final"}
        self._pending_lock = threading.Lock()
        self._closed = False
        self._log_path: Optional[Path] = None
        self._log_file = None  # text file handle
        self._monitor: Optional[subprocess.Popen] = None
        # Buffer for streaming text deltas — flushed to log at sentence
        # boundaries so the popup shows whole phrases instead of single
        # characters per line.
        self._delta_buf: str = ""

    # --- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Spawn the underlying process. Raises SessionStartupError on
        immediate exit (bad flag, missing binary, auth failure)."""
        try:
            proc = subprocess.Popen(
                self._argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=self._cwd,
                text=True,
                bufsize=1,
                encoding="utf-8",
                errors="replace",
                creationflags=_NO_WINDOW,
            )
        except FileNotFoundError as exc:
            raise SessionStartupError(f"executable not found: {exc.filename}") from exc

        # Detect a process that dies before it even reads its first
        # stdin line — usually means the CLI version doesn't accept
        # `--input-format stream-json`. Give it 0.5s; that's enough for
        # an arg-parse rejection but not enough to wait on the API.
        time.sleep(0.5)
        if proc.poll() is not None:
            tail = ""
            try:
                if proc.stderr is not None:
                    tail = proc.stderr.read() or ""
            except Exception:
                pass
            raise SessionStartupError(
                f"claude exited immediately (rc={proc.returncode}): {tail.strip()[:200]}"
            )

        self._proc = proc

        # In verbose mode, open the per-session log + spawn a separate
        # console that tails it. The reader thread writes a human-
        # friendly trace to the log; the popup console renders it live.
        if self._verbose:
            try:
                _RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
                self._log_path = _RUNTIME_DIR / f"{self._label}.log"
                self._log_file = open(self._log_path, "w", encoding="utf-8", buffering=1)
                self._log_file.write(
                    f"=== Halo persistent session: {self._label} ===\n"
                    f"argv: {_redact_argv(self._argv)}\n"
                    f"cwd:  {self._cwd}\n\n"
                )
                self._monitor = _spawn_monitor_console(self._log_path, self._label)
            except Exception as exc:
                print(f"    [{self._label}] verbose-log setup failed (non-fatal): {exc}")
                self._log_file = None
                self._monitor = None

        self._reader_thread = threading.Thread(
            target=self._read_stdout, name=f"{self._label}-stdout", daemon=True,
        )
        self._stderr_thread = threading.Thread(
            target=self._read_stderr, name=f"{self._label}-stderr", daemon=True,
        )
        self._reader_thread.start()
        self._stderr_thread.start()

    def is_alive(self) -> bool:
        if self._closed or self._proc is None:
            return False
        if self._proc.poll() is not None:
            return False
        return self._proc.stdin is not None and not self._proc.stdin.closed

    def close(self, timeout: float = 5.0) -> None:
        """Graceful stdin close, then terminate, then kill. Idempotent."""
        self._closed = True
        proc = self._proc
        if proc is None:
            return
        try:
            if proc.stdin is not None and not proc.stdin.closed:
                with self._stdin_lock:
                    try:
                        proc.stdin.close()
                    except Exception:
                        pass
        finally:
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                try:
                    proc.terminate()
                    proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
                except Exception:
                    pass
            except Exception:
                pass

        # Close monitor console + log file. The popup will keep running
        # until the user closes it (so they can review the final output);
        # we just stop writing.
        if self._log_file is not None:
            try:
                self._log_file.write(f"\n=== session closed ===\n")
                self._log_file.close()
            except Exception:
                pass
            self._log_file = None
        # NOTE: we deliberately do NOT kill self._monitor — the user
        # may want to review the final log. They close the console
        # window themselves when done.

        # If a send was pending when we closed, fail it cleanly so the
        # caller's Future.result() returns instead of hanging forever.
        with self._pending_lock:
            pending = self._pending
            self._pending = None
        if pending is not None:
            fut: Future = pending["future"]
            if not fut.done():
                fut.set_exception(RuntimeError("session closed"))

    # --- send --------------------------------------------------------------

    def send(
        self,
        prompt: str,
        *,
        on_text_chunk: Optional[Callable[[str], None]] = None,
        timeout: float = 300.0,
    ) -> tuple[bool, str]:
        """Send one user turn. Blocks until the matching `result` event
        arrives (or the process dies). Returns (ok, text)."""
        if not self._send_lock.acquire(blocking=False):
            raise SessionBusy(f"{self._label} is already processing a turn")

        try:
            if not self.is_alive():
                raise RuntimeError("session is not alive — caller should respawn")

            future: Future = Future()
            sentence_cb = on_text_chunk if on_text_chunk is not None else (lambda _s: None)
            buf = _SentenceBuffer(on_sentence=sentence_cb)
            with self._pending_lock:
                self._pending = {"future": future, "buf": buf, "final": None}

            envelope = {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": prompt}],
                },
            }
            line = json.dumps(envelope, ensure_ascii=False) + "\n"

            try:
                with self._stdin_lock:
                    assert self._proc is not None and self._proc.stdin is not None
                    self._proc.stdin.write(line)
                    self._proc.stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as exc:
                with self._pending_lock:
                    self._pending = None
                raise RuntimeError(f"stdin write failed: {exc}") from exc

            self._send_count += 1

            try:
                text = future.result(timeout=timeout)
                return True, text
            except Exception as exc:
                # Don't leave a stale pending entry around if the future
                # raised; reader thread already cleared it on the result
                # path, this guards the timeout / error path.
                with self._pending_lock:
                    self._pending = None
                return False, str(exc)
        finally:
            self._send_lock.release()

    @property
    def send_count(self) -> int:
        return self._send_count

    # --- background readers ------------------------------------------------

    def _trace_event(self, event: dict) -> None:
        """When verbose=True, write a per-event line to the session log
        file. The monitor popup console (spawned in start()) live-tails
        that file, so the user sees Claude's activity in a dedicated
        window — separate from Halo's own terminal noise.

        Text-delta chunks are accumulated in `self._delta_buf` and flushed
        only at sentence/clause boundaries (`. ! ? \\n`) — without buffering,
        a 1-character delta + newline per chunk turned the popup into a
        choppy `I\\n'll\\nbuild\\n...` mess. Tool calls and the result
        event force a flush so the popup stays roughly linear.
        """
        if self._log_file is None:
            return
        etype = event.get("type", "?")
        try:
            if etype == "stream_event":
                inner = event.get("event", {})
                inner_type = inner.get("type", "")
                if inner_type == "content_block_delta":
                    delta = inner.get("delta", {})
                    if delta.get("type") == "text_delta":
                        self._delta_buf += delta.get("text", "")
                        # Flush on sentence terminator or newline — keeps
                        # words intact while still updating live.
                        if any(self._delta_buf.endswith(c) for c in (". ", "! ", "? ", ".\n", "!\n", "?\n", "\n")):
                            self._flush_delta_buf()
                        # Long buffer (>200 chars) without a terminator
                        # also flushes so the popup doesn't go quiet during
                        # a single long token stream.
                        elif len(self._delta_buf) > 200:
                            self._flush_delta_buf()
                        return
                elif inner_type == "content_block_start":
                    block = inner.get("content_block", {})
                    if block.get("type") == "tool_use":
                        name = block.get("name", "?")
                        self._flush_delta_buf()
                        self._log_file.write(f"\n[tool] {name}\n")
                        self._log_file.flush()
                        return
                elif inner_type == "message_stop":
                    self._flush_delta_buf()
                    self._log_file.write("\n")
                    self._log_file.flush()
                    return
            elif etype == "result":
                self._flush_delta_buf()
                self._log_file.write(f"\n[done] turn complete\n\n")
                self._log_file.flush()
                return
        except (ValueError, OSError):
            # Log file got closed under us (likely process tearing down).
            self._log_file = None

    def _flush_delta_buf(self) -> None:
        """Write the accumulated text-delta buffer to the log + clear it.
        Adds a trailing newline so `Get-Content -Wait` emits the block
        promptly in the popup window."""
        if not self._delta_buf or self._log_file is None:
            return
        try:
            text = self._delta_buf.rstrip()
            if text:
                self._log_file.write(text + "\n")
                self._log_file.flush()
        except (ValueError, OSError):
            self._log_file = None
        finally:
            self._delta_buf = ""

    def _read_stdout(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if self._verbose:
                    self._trace_event(event)
                with self._pending_lock:
                    pending = self._pending
                if pending is None:
                    # Event arrived between turns (e.g. rate_limit_event
                    # right after spawn). Nothing to dispatch to — drop.
                    continue
                delta = _extract_text_delta(event)
                if delta:
                    pending["buf"].feed(delta)
                    continue
                final = _extract_final_result(event, "result")
                if final is not None:
                    pending["final"] = final
                # `result` event terminates the turn. Claude emits exactly
                # one per send; treat it as the boundary regardless of
                # whether it carried a `result` field.
                if event.get("type") == "result":
                    pending["buf"].flush()
                    text = pending["final"] if pending["final"] is not None else pending["buf"].full_text()
                    fut: Future = pending["future"]
                    with self._pending_lock:
                        self._pending = None
                    if not fut.done():
                        fut.set_result(text)
        except (ValueError, OSError):
            pass
        finally:
            # If the process died mid-turn, fail the pending future so
            # the caller doesn't hang.
            with self._pending_lock:
                pending = self._pending
                self._pending = None
            if pending is not None:
                pending["buf"].flush()
                fut = pending["future"]
                if not fut.done():
                    partial = pending["buf"].full_text()
                    if partial:
                        fut.set_result(partial)
                    else:
                        fut.set_exception(RuntimeError("stdout closed before result"))

    def _read_stderr(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stderr is not None
        try:
            for line in proc.stderr:
                line = line.rstrip()
                if line:
                    print(f"    [{self._label}] {line}")
        except (ValueError, OSError):
            return


# Module-level registry of live sessions, keyed by agent_key.
_sessions: dict[str, ClaudeSession] = {}
_registry_lock = threading.Lock()


def get_or_create(
    agent_key: str,
    argv: list[str],
    *,
    cwd: str,
    label: str,
    verbose: bool = False,
) -> tuple[ClaudeSession, bool]:
    """Return (session, was_new). Respawn if the existing one died."""
    with _registry_lock:
        existing = _sessions.get(agent_key)
        if existing is not None and existing.is_alive():
            return existing, False
        if existing is not None:
            # Dead — drop the corpse before respawning.
            try:
                existing.close(timeout=1.0)
            except Exception:
                pass
        sess = ClaudeSession(agent_key, argv, cwd=cwd, label=label, verbose=verbose)
        sess.start()
        _sessions[agent_key] = sess
        return sess, True


def close(agent_key: str) -> None:
    """Close one named session. Used by `reset_session()`."""
    with _registry_lock:
        sess = _sessions.pop(agent_key, None)
    if sess is not None:
        try:
            sess.close()
        except Exception as exc:
            print(f"    [{agent_key}] close error: {exc}")


def close_all() -> None:
    with _registry_lock:
        items = list(_sessions.items())
        _sessions.clear()
    for key, sess in items:
        try:
            sess.close()
        except Exception as exc:
            print(f"    [{key}] close error: {exc}")
        bus.emit("agent.session_closed", agent=key)


atexit.register(close_all)
