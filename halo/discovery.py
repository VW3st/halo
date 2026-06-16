"""Discovery — find running coding-agent CLI processes on the machine.

On a developer's box at any moment there may be N terminals open, each
running a `claude` / `codex` / etc. session against a different project.
Halo's old "spawn-my-own-Claude in the launch cwd" model ignored all of
that and gave the user a parallel, deaf-to-existing-work Claude. This
module is the first half of the fix: a process scanner that reports
every coding-agent session it can see, with its cwd and parent terminal.

The orchestrator feeds this list (and the user's currently-spoken target)
into the Stage 2 LLM so the brain can route to the right session by name
("work on website") instead of forcing the user to manually register
projects.

Design notes:
- Pure psutil. No win32 hacks. Cross-platform-friendly (Mac/Linux mostly
  work — child cmdlines are POSIX-portable; cwd lookup is universal).
- Cheap. One scan = O(n_processes) with two cheap calls per match. We
  run it on a daemon thread every DISCOVERY_INTERVAL_SEC.
- Identification is by cmdline substring, not binary name — `claude` on
  Windows is a `.cmd` shim that spawns `node.exe <path-to-cli-js>`. We
  match the JS path or the node argv, not the binary, so both Windows
  shims and POSIX direct execs are caught.
- Excludes ourselves: Halo spawns its own Claude subprocesses via
  halo.sessions; those have well-known argv patterns and are filtered
  out so they don't show up as "discovered" sessions to the user.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional

try:
    import psutil
except ImportError as _exc:  # pragma: no cover
    psutil = None
    _PSUTIL_IMPORT_ERROR = _exc
else:
    _PSUTIL_IMPORT_ERROR = None


# How often the background thread re-scans process state. 2 s is short
# enough that the brain sees new terminals/sessions within one turn; long
# enough that we don't burn CPU on a busy laptop.
DISCOVERY_INTERVAL_SEC = 2.0


# Marker substrings we look for in process cmdlines to identify each
# kind of agent. Cmdlines are slash-normalized (\ -> /) before matching
# so the same fingerprint catches both POSIX and Windows paths.
#
# Three install paths to cover:
#   1. npm-installed: cmdline = `node .../@anthropic-ai/claude-code/cli.js`
#   2. WinGet-installed: cmdline = `.../WinGet/Links/claude.exe --continue`
#   3. POSIX binary: cmdline = `/usr/local/bin/claude` (just basename match)
_AGENT_FINGERPRINTS: dict[str, tuple[str, ...]] = {
    "claude_code": (
        # npm/node shim — works for POSIX and Windows after slash normalize
        "@anthropic-ai/claude-code",
        "claude-code/cli",
        "claude.js",
        # WinGet-installed binary on Windows
        "/WinGet/Links/claude.exe",
        # Generic standalone binary launches — interactive Claude Code
        # CLI typically runs as just `claude` / `claude.exe` with no flags
        # or `--continue` / `--resume`. The exclusion list below filters
        # the Electron desktop app + Halo's own spawn.
        "/claude.exe",  # any path ending in claude.exe (slash-normalized)
    ),
    "codex_cli": (
        "@openai/codex",
        "codex/cli",
        "codex-cli",
        "/WinGet/Links/codex.exe",
        "/codex.exe",
    ),
}

# Cmdline substrings that DISQUALIFY a match. Cmdlines are slash-
# normalized before checking. Covers:
#   - Halo's own subprocesses (we manage them through halo.sessions,
#     so they're already addressable as the "active" session without
#     going through discovery)
#   - the Claude / Codex Desktop apps (Electron — different product,
#     not a CLI session)
#   - Electron child processes (renderer / gpu-process / utility)
_HALO_SPAWNED_MARKERS: tuple[str, ...] = (
    "--input-format",       # only Halo's persistent Claude uses this
    # Halo's one-shot Claude fallback (first_call / continue_call). Neither
    # flag is something an interactive user types — they're automation/SDK
    # flags Halo uses to stream and parse the reply. Without these, a
    # fallback spawn self-registers as a discoverable session and Halo can
    # try to dispatch to its own child (audit finding).
    "--include-partial-messages",
    "--output-format stream-json",
    "codex exec",            # only Halo's one-shot Codex
    "/WindowsApps/Claude_",   # Claude desktop app (Electron)
    "/WindowsApps/Codex_",    # Codex desktop app (if installed)
    "/Claude.app/Contents",   # macOS Claude desktop app bundle
    "--type=renderer",       # Electron renderer child
    "--type=gpu-process",    # Electron GPU child
    "--type=utility",        # Electron utility child
    "--type=crashpad-handler",
    # Claude Code's INTERNAL persistent bash shell (one per session) sources
    # a snapshot under ~/.claude/shell-snapshots. It is a child of a real
    # claude session, NOT a session itself — counting it double-reports.
    "shell-snapshots",
    "shell-snapshot",
)

# Shell / terminal process names — used to walk back from an agent
# process to identify the parent terminal window the user is interacting
# with. Helps the focus tracker (Phase 2) and improves the spoken label
# when two projects share a basename.
_TERMINAL_NAMES = {
    "pwsh.exe", "powershell.exe", "cmd.exe",
    "WindowsTerminal.exe", "wt.exe", "conhost.exe",
    "bash", "zsh", "fish", "sh",
    "alacritty", "kitty", "wezterm", "iterm2", "Terminal",
}


@dataclass(frozen=True)
class DiscoveredSession:
    """One detected agent process on the machine.

    Fields:
      agent_key:       "claude_code" | "codex_cli" | future agents
      pid:             process id of the agent itself
      cwd:             working directory (absolute path)
      label:           short spoken/displayed name (basename of cwd by default;
                       see registry.py for collision handling)
      parent_pid:      parent process pid (usually a shell)
      parent_name:     parent process name (powershell.exe, bash, ...)
      cmdline:         the agent's full argv as a string (truncated for display)
      seen_at:         monotonic timestamp of last scan that confirmed it
    """

    agent_key: str
    pid: int
    cwd: str
    label: str
    parent_pid: Optional[int]
    parent_name: Optional[str]
    cmdline: str
    seen_at: float

    def __str__(self) -> str:
        # ASCII-only — Windows console default codec (cp1252) chokes on
        # unicode arrows when stdout isn't UTF-8.
        parent = f" <- {self.parent_name}" if self.parent_name else ""
        return f"[{self.agent_key} #{self.pid}] {self.label}  ({self.cwd}){parent}"


def is_available() -> bool:
    """True if process discovery is supported on this machine."""
    return psutil is not None


# Set HALO_DISCOVERY_DEBUG=1 to print every node/claude/codex-ish process the
# scanner sees and why it was kept or skipped — the way to debug "found 0".
_DISCOVERY_DEBUG = bool(os.environ.get("HALO_DISCOVERY_DEBUG"))


def _classify_by_name(name: Optional[str]) -> Optional[str]:
    """Fallback classification by executable name alone. Catches the
    standalone `claude.exe` / `codex.exe` even when psutil can't read the
    process cmdline (AccessDenied across integrity levels on Windows)."""
    n = (name or "").lower()
    if n in ("claude.exe", "claude"):
        return "claude_code"
    if n in ("codex.exe", "codex"):
        return "codex_cli"
    return None


def _is_excluded_cmdline(cmdline_parts: Iterable[str]) -> bool:
    """True if the cmdline is a Halo-spawned process or a desktop app —
    something we must NOT report as a discoverable session. Used both by
    `_classify_cmdline` and (critically) by the scan loop so the
    executable-name fallback can't resurrect a process the cmdline
    explicitly disqualified (e.g. a WinGet `claude.exe` Halo spawned with
    `--output-format stream-json`)."""
    joined = " ".join(cmdline_parts).replace("\\", "/")
    return any(marker in joined for marker in _HALO_SPAWNED_MARKERS)


def _classify_cmdline(cmdline_parts: Iterable[str]) -> Optional[str]:
    """Return the agent_key whose fingerprint matches `cmdline_parts`, or None.

    Cmdline is slash-normalized (backslash -> forward slash) before
    matching so a single fingerprint catches both POSIX and Windows
    paths. Halo-spawned processes and desktop-app variants are
    filtered first.
    """
    # Filter Halo-spawned processes + Electron desktop apps first.
    if _is_excluded_cmdline(cmdline_parts):
        return None
    joined = " ".join(cmdline_parts).replace("\\", "/")
    for agent_key, fingerprints in _AGENT_FINGERPRINTS.items():
        for fp in fingerprints:
            if fp in joined:
                return agent_key
    # Broad fallback for variant install layouts. Require the PACKAGE name
    # ("claude-code" / "@openai/codex"), not a bare "claude"/"codex" — the
    # latter false-matches plugin caches like ~/.claude/plugins/... and
    # project dirs that merely contain the word.
    low = joined.lower()
    if "claude-code" in low:
        return "claude_code"
    if "@openai/codex" in low or "codex-cli" in low or "codex/cli" in low:
        return "codex_cli"
    return None


def _safe_cwd(proc: "psutil.Process") -> Optional[str]:
    try:
        return proc.cwd()
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return None


def _safe_cmdline(proc: "psutil.Process") -> Optional[list[str]]:
    try:
        return proc.cmdline()
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return None


def _safe_parent(proc: "psutil.Process") -> Optional["psutil.Process"]:
    try:
        return proc.parent()
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return None


def _label_for(cwd: str) -> str:
    """Default human label = basename of cwd. Falls back to drive root.

    Collisions (two projects with the same basename) are resolved one
    level up by the registry; this function stays simple.
    """
    try:
        path = Path(cwd)
        base = path.name
        if base:
            return base
        # Drive roots: "D:\\" -> "D:"
        return str(path).rstrip("\\/").rstrip(":") + ":"
    except Exception:
        return cwd


def scan_once() -> list[DiscoveredSession]:
    """Walk all processes once. Returns the current detected agent sessions.

    Errors on individual processes are swallowed (race vs. process exit
    is common during a sweep; the next scan will catch up).
    """
    if psutil is None:
        return []

    now = time.monotonic()
    out: list[DiscoveredSession] = []
    self_pid = None
    try:
        self_pid = psutil.Process().pid
    except Exception:
        pass

    for proc in psutil.process_iter(attrs=["pid", "name"]):
        try:
            if self_pid is not None and proc.pid == self_pid:
                continue
            name = ""
            try:
                name = proc.info.get("name") or ""
            except Exception:
                pass
            cmdline = _safe_cmdline(proc)
            # Classify by cmdline first (precise), then fall back to the
            # executable name so a claude.exe whose cmdline is access-denied
            # is still discovered instead of silently dropped.
            agent_key = _classify_cmdline(cmdline) if cmdline else None
            if agent_key is None:
                # Don't let the executable-name fallback resurrect a process
                # the cmdline explicitly disqualified (Halo's own spawn /
                # desktop app). Only fall back to name when the cmdline was
                # unreadable or simply didn't match a positive fingerprint.
                if cmdline and _is_excluded_cmdline(cmdline):
                    if _DISCOVERY_DEBUG:
                        print(f"  [discovery] skip (halo-spawned) pid={proc.pid} "
                              f"name={name!r}")
                    continue
                agent_key = _classify_by_name(name)
            if agent_key is None:
                if _DISCOVERY_DEBUG:
                    low = name.lower()
                    if "claude" in low or "codex" in low or "node" in low:
                        cl = "<denied>" if cmdline is None else " ".join(cmdline)[:90]
                        print(f"  [discovery] skip pid={proc.pid} name={name!r} cmdline={cl}")
                continue
            if _DISCOVERY_DEBUG:
                print(f"  [discovery] MATCH pid={proc.pid} name={name!r} -> {agent_key}")
            parent = _safe_parent(proc)
            parent_pid: Optional[int] = None
            parent_name: Optional[str] = None
            if parent is not None:
                try:
                    parent_pid = parent.pid
                    parent_name = parent.name()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            # cwd() raises AccessDenied for many same-user/elevated processes
            # on Windows. DON'T drop the session for that — it's the #1 reason
            # discovery reported "0 sessions". Fall back to the parent shell's
            # cwd, then to an empty cwd with a pid-derived label so the
            # session is still discoverable and targetable by voice.
            cwd = _safe_cwd(proc)
            if not cwd and parent is not None:
                cwd = _safe_cwd(parent)
            if cwd:
                label = _label_for(cwd)
            else:
                cwd = ""
                short = agent_key.split("_")[0]
                label = f"{short}-{proc.pid}"
            joined = " ".join(cmdline)
            if len(joined) > 200:
                joined = joined[:197] + "..."
            out.append(
                DiscoveredSession(
                    agent_key=agent_key,
                    pid=proc.pid,
                    cwd=cwd,
                    label=label,
                    parent_pid=parent_pid,
                    parent_name=parent_name,
                    cmdline=joined,
                    seen_at=now,
                )
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
        except Exception:
            # Don't let a single bad row kill the whole scan.
            continue
    return out


def diagnose() -> None:
    """Print every node/claude/codex-ish process and whether it classified.

    Called automatically when discovery finds 0 sessions so the user can see
    WHY — without setting any env var. The usual culprits: (a) the real
    Claude CLI is a `node.exe ...cli.js` whose cmdline psutil can't read
    across Windows integrity levels, or (b) there simply is no local Claude
    CLI running (claude.ai in a browser is not a local process Halo can see).
    """
    if psutil is None:
        print("  [discovery] psutil not installed — cannot diagnose")
        return
    print("  [discovery] diagnostic scan (node / claude / codex processes):")
    n = 0
    for proc in psutil.process_iter(attrs=["pid", "name"]):
        try:
            name = proc.info.get("name") or ""
            low = name.lower()
            cmdline = _safe_cmdline(proc)
            cl = " ".join(cmdline) if cmdline else None
            interesting = (
                "node" in low or "claude" in low or "codex" in low or "bun" in low
                or (cl and ("claude" in cl.lower() or "codex" in cl.lower()))
            )
            if not interesting:
                continue
            n += 1
            cls = (_classify_cmdline(cmdline) if cmdline else None) or _classify_by_name(name)
            shown = "<cmdline ACCESS DENIED>" if cmdline is None else (cl[:130] if cl else "<empty>")
            print(f"    pid={proc.pid:<6} name={name!r:<16} class={cls or '—'}  {shown}")
        except Exception:
            continue
    if n == 0:
        print("    none found — no local Claude/Codex CLI is running in another")
        print("    terminal. (A claude.ai browser tab is NOT a local process.)")
    else:
        print("    ^ if your session shows class=— with ACCESS DENIED, run this")
        print("      terminal as the same user/elevation as that session.")


class DiscoveryThread:
    """Background scanner. Owns the current snapshot; callers read it
    via `snapshot()` whenever they need fresh data.

    Cheap enough that we just always run it when Halo is in multi-session
    mode. Stop it explicitly on shutdown to avoid the daemon thread
    leaking past the process exit during pytest.
    """

    def __init__(
        self,
        interval_sec: float = DISCOVERY_INTERVAL_SEC,
        on_change: Optional[Callable[[list[DiscoveredSession]], None]] = None,
    ) -> None:
        self._interval = interval_sec
        self._on_change = on_change
        self._snapshot: list[DiscoveredSession] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is not None:
            return
        # Do the FIRST scan synchronously in the caller's thread, BEFORE
        # spawning the background loop. Previously the first scan lived in
        # _loop() (the spawned thread), so a caller that start()s and then
        # immediately snapshot()s raced the thread and got an empty list —
        # the real cause of intermittent "discovery: found 0 sessions" at
        # startup even when sessions were clearly present.
        try:
            self.refresh_now()
        except Exception as exc:
            print(f"  [discovery] initial scan error: {exc}")
        self._thread = threading.Thread(
            target=self._loop, name="halo-discovery", daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def snapshot(self) -> list[DiscoveredSession]:
        with self._lock:
            return list(self._snapshot)

    def refresh_now(self) -> list[DiscoveredSession]:
        """Force a synchronous scan and update the snapshot. Useful for
        `halo sessions` CLI and tests."""
        sessions = scan_once()
        self._set_snapshot(sessions)
        return sessions

    def _loop(self) -> None:
        # start() already performed the first (synchronous) scan; here we
        # just re-scan on the interval to pick up new/closed sessions.
        while not self._stop.wait(self._interval):
            try:
                sessions = scan_once()
            except Exception as exc:
                print(f"  [discovery] scan error: {exc}")
                continue
            self._set_snapshot(sessions)

    def _set_snapshot(self, sessions: list[DiscoveredSession]) -> None:
        changed = False
        with self._lock:
            old_keys = {(s.pid, s.cwd) for s in self._snapshot}
            new_keys = {(s.pid, s.cwd) for s in sessions}
            if old_keys != new_keys:
                changed = True
            self._snapshot = sessions
        if changed and self._on_change is not None:
            try:
                self._on_change(list(sessions))
            except Exception as exc:
                print(f"  [discovery] on_change error: {exc}")
