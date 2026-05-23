"""Local system tools — first taste of step 4.

Cross-platform: Windows, macOS, Linux. Each handler picks the right
launcher for the current OS so "open chrome" works the same on a Mac
laptop as on the user's Windows desktop.

Wired in early so we can use "open calculator" / "open browser" as
end-to-end tests of the adaptive turn-taking + routing pipeline.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import webbrowser
from dataclasses import dataclass
from typing import Callable

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
# everything else (Linux, BSD) gets the xdg path

_OPEN = r"\b(open|launch|start|run|fire up|bring up|go to)\b"


def _spawn(cmd: list[str]) -> None:
    """Detached background launch — don't block on the child process."""
    kwargs: dict = {}
    if IS_WIN:
        kwargs["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs)


def _open_url(url: str) -> None:
    """Open `url` in the OS default browser. Falls back across handlers
    because webbrowser.open is flaky on some Windows configs (about:blank
    in particular hits an empty handler chain)."""
    # webbrowser.open is the cross-platform answer when it works.
    try:
        if webbrowser.open(url, new=2):
            return
    except Exception:
        pass
    # Platform-specific fallbacks.
    if IS_WIN:
        os.startfile(url)  # uses HKEY_CLASSES_ROOT\http\shell\open\command
    elif IS_MAC:
        _spawn(["open", url])
    else:
        _spawn(["xdg-open", url])


def _open_app(win_exe: str, mac_app: str, linux_cmd: list[str]) -> None:
    if IS_WIN:
        os.startfile(win_exe)
    elif IS_MAC:
        _spawn(["open", "-a", mac_app])
    else:
        _spawn(linux_cmd)


def _open_browser() -> str:
    # Use a real URL — about:blank can fail to dispatch on Windows when
    # the default browser isn't registered as the about: handler.
    _open_url("https://www.google.com")
    return "Opened browser."


def _open_calculator() -> str:
    _open_app(
        win_exe="calc.exe",
        mac_app="Calculator",
        linux_cmd=["gnome-calculator"],
    )
    return "Opened calculator."


def _open_notepad() -> str:
    _open_app(
        win_exe="notepad.exe",
        mac_app="TextEdit",
        linux_cmd=["gedit"],
    )
    return "Opened notepad."


def _open_explorer() -> str:
    if IS_WIN:
        os.startfile("explorer.exe")
    elif IS_MAC:
        _spawn(["open", os.path.expanduser("~")])
    else:
        _spawn(["xdg-open", os.path.expanduser("~")])
    return "Opened file explorer."


def _open_terminal() -> str:
    if IS_WIN:
        try:
            os.startfile("wt.exe")  # Windows Terminal if installed
            return "Opened Windows Terminal."
        except OSError:
            pass
        try:
            os.startfile("powershell.exe")
            return "Opened PowerShell."
        except OSError:
            os.startfile("cmd.exe")
            return "Opened command prompt."
    if IS_MAC:
        _spawn(["open", "-a", "Terminal"])
        return "Opened Terminal."
    _spawn(["x-terminal-emulator"])
    return "Opened terminal."


@dataclass(frozen=True)
class _Tool:
    pattern: re.Pattern
    handler: Callable[[], str]
    name: str


# Split on conjunctions so "open calculator and open chrome" runs both.
_SPLIT_RE = re.compile(
    r"\s+(?:and then|and also|and|then|after that)\s+|[;,]\s*", re.IGNORECASE
)


# Order matters — first match wins. Keep specific patterns above generic.
# Synonyms are intentionally broad so STT mishearings still hit the right
# handler without an LLM round-trip.
_TOOLS: list[_Tool] = [
    _Tool(
        re.compile(_OPEN + r".*\b(calc|calculator|calculate|math)\b", re.IGNORECASE),
        _open_calculator, "calculator",
    ),
    _Tool(
        re.compile(
            _OPEN + r".*\b(browser|chrome|firefox|edge|web|internet|google|safari)\b",
            re.IGNORECASE,
        ),
        _open_browser, "browser",
    ),
    _Tool(
        re.compile(
            _OPEN + r".*\b(notepad|note ?pad|text editor|textedit)\b", re.IGNORECASE
        ),
        _open_notepad, "notepad",
    ),
    _Tool(
        re.compile(
            _OPEN + r".*\b(file explorer|files|explorer|finder|my documents|home folder)\b",
            re.IGNORECASE,
        ),
        _open_explorer, "explorer",
    ),
    _Tool(
        re.compile(
            _OPEN
            + r".*\b(terminal|powershell|power ?shell|cmd|command prompt|shell|console|iterm)\b",
            re.IGNORECASE,
        ),
        _open_terminal, "terminal",
    ),
]


def is_pure_tool(text: str) -> bool:
    """Strict pre-LLM check: is this transcript ONLY tool commands?

    Returns True iff every comma/and-separated segment matches a tool
    pattern. Mixed intent like "open chrome and build a login page"
    returns False so it falls through to Stage 2 LLM and the code
    request isn't silently dropped.
    """
    cleaned = text.strip().rstrip(".!?,")
    if not cleaned:
        return False
    parts = [p.strip() for p in _SPLIT_RE.split(cleaned) if p and p.strip()] or [cleaned]
    for part in parts:
        if not any(tool.pattern.search(part) for tool in _TOOLS):
            return False
    return True


def try_match(text: str) -> bool:
    """Permissive: did at least one tool fire? Currently unused by the
    orchestrator (which uses is_pure_tool) but kept as a public helper."""
    cleaned = text.strip().rstrip(".!?,")
    parts = [p.strip() for p in _SPLIT_RE.split(cleaned) if p and p.strip()] or [cleaned]
    for part in parts:
        if any(tool.pattern.search(part) for tool in _TOOLS):
            return True
    return False


def execute_system_intent(cleaned_text: str) -> tuple[bool, str]:
    """Run every matching local tool in `cleaned_text`.

    Splits on "and"/"then"/comma so "open calculator and open chrome"
    fires both handlers. Returns (any_handled, joined_summary).
    """
    text = cleaned_text.strip().rstrip(".!?,")
    parts = [p.strip() for p in _SPLIT_RE.split(text) if p and p.strip()] or [text]

    summaries: list[str] = []
    any_handled = False
    fired: set[str] = set()  # don't double-fire the same tool in one turn

    for part in parts:
        for tool in _TOOLS:
            if tool.name in fired:
                continue
            if tool.pattern.search(part):
                try:
                    summaries.append(tool.handler())
                except Exception as exc:
                    summaries.append(f"failed to open {tool.name}: {exc}")
                any_handled = True
                fired.add(tool.name)
                break

    return any_handled, "  ".join(summaries)
