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
import threading
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from halo import bus

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
# everything else (Linux, BSD) gets the xdg path

# Non-capturing so the file-open regex's group(1) is the filename, not
# the verb. None of the existing tool patterns use .group() either —
# they just .search() to test membership — so making this non-capturing
# changes no behavior.
_OPEN = r"\b(?:open|launch|start|run|fire up|bring up|go to)\b"

# "open landing.html" / "open the file landing.html" / "open my notes.md".
# Captures the bare filename; relative to CWD at call time.
# Extension required so we don't false-match things like "open chrome".
_OPEN_FILE_RE = re.compile(
    _OPEN + r"\s+(?:the\s+|file\s+|my\s+)*([A-Za-z0-9_\-./\\]+\.[A-Za-z0-9]{1,5})\b",
    re.IGNORECASE,
)

# Generic "open <app>" — captures everything after the verb so we can launch
# apps we don't have a dedicated handler for (Paint, Spotify, Word, ...).
# Runs AFTER the specific _TOOLS so "open chrome" still uses the browser
# handler. Greedy .+ is fine because callers split on conjunctions first.
_OPEN_APP_RE = re.compile(
    _OPEN + r"\s+(?:the\s+|my\s+|app\s+|application\s+|program\s+)*(.+)$",
    re.IGNORECASE,
)
_OPEN_ARTICLE_RE = re.compile(r"^(?:a|an|some|the)\s+", re.IGNORECASE)

# "say <text>" / "repeat after me <text>" / "tell the audience <text>" — Halo
# speaks the remainder out loud. Lets you puppet Halo's voice (demos, greeting
# an audience) without it trying to route the phrase to Claude. Anchored at the
# start (after an optional politeness prefix) so it doesn't fire on a "say" in
# the middle of a sentence.
_SAY_RE = re.compile(
    r"^\s*(?:please\s+|hey\s+|halo\s+)*"
    r"(?:can|could|would|will)?\s*(?:you\s+)?(?:please\s+)?"
    r"(?:say|speak|repeat(?:\s+after\s+me)?|"
    r"tell\s+(?:them|everyone|us|the\s+(?:audience|crowd|viewers|people)))"
    r"[\s,:\-]+(.+)$",
    re.IGNORECASE,
)

# A location/timezone qualifier on a date/time question ("...in Brisbane",
# "...in UTC", "...London time"). When present, the LOCAL datetime tool must
# NOT answer — it would read out THIS machine's clock (the "5:47 PM for
# Brisbane" bug). Such questions fall through to the brain, which gets the
# current local time as context and can convert. The "in/for/at <Capitalized>"
# arm keys off the proper-noun capitalization Whisper/the router produce, so
# "in the morning" / "in a minute" (lowercase) do NOT trip it.
_TZ_QUALIFIER_RE = re.compile(
    r"\b(?:in|for|at)\s+[A-Z][a-z]+"
    r"|\b(?:utc|gmt|est|edt|pst|pdt|cst|cdt|mst|mdt|bst|cet|cest|ist|jst|aest|aedt)\b"
    r"|\btime\s*zone\b"
    r"|\b(?:eastern|western|central|mountain|pacific)\s+time\b",
)

# Friendly app name -> per-OS launch token. The token is resolved by the OS
# (App Paths registry / PATH on Windows, LaunchServices on macOS), so we
# avoid hardcoding install paths. Keys are matched against the lowercased
# remainder of "open <X>". Add aliases for the obvious mishearings.
_APP_ALIASES: dict[str, dict] = {
    "paint":          {"win": "mspaint",      "mac": "Preview",                 "linux": ["kolourpaint"]},
    "ms paint":       {"win": "mspaint",      "mac": "Preview",                 "linux": ["kolourpaint"]},
    "wordpad":        {"win": "write",        "mac": "TextEdit",                "linux": ["gedit"]},
    "word pad":       {"win": "write",        "mac": "TextEdit",                "linux": ["gedit"]},
    "word":           {"win": "winword",      "mac": "Microsoft Word",          "linux": ["libreoffice", "--writer"]},
    "excel":          {"win": "excel",        "mac": "Microsoft Excel",         "linux": ["libreoffice", "--calc"]},
    "powerpoint":     {"win": "powerpnt",     "mac": "Microsoft PowerPoint",    "linux": ["libreoffice", "--impress"]},
    "outlook":        {"win": "outlook",      "mac": "Microsoft Outlook",       "linux": ["thunderbird"]},
    "spotify":        {"win": "spotify",      "mac": "Spotify",                 "linux": ["spotify"]},
    "discord":        {"win": "discord",      "mac": "Discord",                 "linux": ["discord"]},
    "slack":          {"win": "slack",        "mac": "Slack",                   "linux": ["slack"]},
    "vs code":        {"win": "code",         "mac": "Visual Studio Code",      "linux": ["code"]},
    "vscode":         {"win": "code",         "mac": "Visual Studio Code",      "linux": ["code"]},
    "code":           {"win": "code",         "mac": "Visual Studio Code",      "linux": ["code"]},
    "settings":       {"win": "ms-settings:", "mac": "System Settings",         "linux": ["gnome-control-center"]},
    "control panel":  {"win": "control",      "mac": "System Settings",         "linux": ["gnome-control-center"]},
    "task manager":   {"win": "taskmgr",      "mac": "Activity Monitor",        "linux": ["gnome-system-monitor"]},
    "snipping tool":  {"win": "snippingtool", "mac": "Screenshot",              "linux": ["gnome-screenshot"]},
    "camera":         {"win": "microsoft.windows.camera:", "mac": "Photo Booth", "linux": ["cheese"]},
    "calculator":     {"win": "calc",         "mac": "Calculator",              "linux": ["gnome-calculator"]},
}


# Dry-run: when True, every launcher returns its normal summary string but does
# NOT actually start a process / open a window. This exists so the regression
# tests (which call execute_system_intent("open calculator and paint")) verify
# the routing LOGIC without spawning real apps on the developer's desktop.
# Enable via HALO_TOOLS_DRY_RUN=1 or tools.set_dry_run(True).
_DRY_RUN = os.getenv("HALO_TOOLS_DRY_RUN", "").strip().lower() in ("1", "true", "yes", "on")


def set_dry_run(on: bool) -> None:
    """Toggle dry-run mode (no real launches). Used by the test suite."""
    global _DRY_RUN
    _DRY_RUN = bool(on)


def _startfile(target: str) -> None:
    """os.startfile, but a no-op in dry-run. Single chokepoint for Windows
    ShellExecute launches so tests never actually open anything."""
    if _DRY_RUN:
        return
    os.startfile(target)


def _spawn(cmd: list[str]) -> None:
    """Detached background launch — don't block on the child process."""
    if _DRY_RUN:
        return
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
    if _DRY_RUN:
        return
    # webbrowser.open is the cross-platform answer when it works.
    try:
        if webbrowser.open(url, new=2):
            return
    except Exception:
        pass
    # Platform-specific fallbacks.
    if IS_WIN:
        _startfile(url)  # uses HKEY_CLASSES_ROOT\http\shell\open\command
    elif IS_MAC:
        _spawn(["open", url])
    else:
        _spawn(["xdg-open", url])


def _open_app(win_exe: str, mac_app: str, linux_cmd: list[str]) -> None:
    if IS_WIN:
        _startfile(win_exe)
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
        _startfile("explorer.exe")
    elif IS_MAC:
        _spawn(["open", os.path.expanduser("~")])
    else:
        _spawn(["xdg-open", os.path.expanduser("~")])
    return "Opened file explorer."


def _try_open_file(part: str) -> tuple[bool, str]:
    """Open `landing.html`-style filenames in the OS default app.

    Returns (matched, spoken_summary). matched=True even on failure (e.g.
    file not in CWD) so the orchestrator doesn't fall through to the
    LLM router for what's clearly a local-tool intent.
    """
    m = _OPEN_FILE_RE.search(part)
    if not m:
        return False, ""
    target = m.group(1)
    path = Path(target)
    if not path.is_absolute():
        path = Path.cwd() / path
    if not path.exists():
        return True, f"I don't see {path.name} in this folder."
    try:
        if IS_WIN:
            _startfile(str(path))
        elif IS_MAC:
            _spawn(["open", str(path)])
        else:
            _spawn(["xdg-open", str(path)])
        return True, f"Opened {path.name}."
    except Exception as exc:
        return True, f"Couldn't open {path.name}: {exc}"


def _try_say(text: str) -> tuple[bool, str]:
    """'say <text>' -> speak <text> verbatim. Returns (matched, spoken_text).

    The spoken summary IS the text to read aloud, so the orchestrator speaks
    it like any other tool result. Checked against the WHOLE utterance (before
    conjunction splitting) so 'say hi, everyone, and welcome' is read in full.
    """
    m = _SAY_RE.search((text or "").strip())
    if not m:
        return False, ""
    spoken = m.group(1).strip().strip("\"'").strip()
    if not spoken:
        return False, ""
    return True, spoken


# --- image generation -------------------------------------------------------
# "generate an image of X" is a LOCAL action: Halo calls a real image model
# (halo/image_gen.py) and opens the PNG, instead of dispatching to a coding
# agent (which only ever hand-draws SVGs). Whole-utterance match (before the
# conjunction split) because image prompts naturally contain "and"
# ("a cat AND a dog").
_GEN_VERB = (
    r"(?:generate|create|make|draw|paint|render|design|whip up|cook up|"
    r"produce|give me|get me)"
)
_IMG_NOUN = (
    r"(?:image|images|picture|pictures|photo|photos|photograph|drawing|"
    r"illustration|graphic|graphics|artwork|painting|logo|icon|wallpaper|"
    r"banner|poster|portrait|sketch|mockup|render)"
)
_IMAGE_REQUEST_RE = re.compile(
    rf"\b{_GEN_VERB}\s+(?:me\s+)?(?:a|an|some|the|another|new)?\s*"
    rf"(?:\w+\s+){{0,3}}?{_IMG_NOUN}\b",
    re.IGNORECASE,
)
_IMAGE_OF_RE = re.compile(
    rf"{_IMG_NOUN}\b\s*(?:of|showing|with|that shows?|depicting|about|for|:|,)?\s*"
    r"(?P<subject>.*)$",
    re.IGNORECASE,
)
_IMAGE_TRAIL_OPEN_RE = re.compile(
    r"\s*(?:,|\.)?\s*(?:and|then|,)?\s*(?:and\s+)?open\s+"
    r"(?:it|that|them|the (?:image|picture|file))\b.*$",
    re.IGNORECASE,
)
_IMAGE_TRAIL_FLUFF_RE = re.compile(
    r"\s*(?:please|thanks|thank you|for me|"
    r"when (?:it'?s|they'?re|he'?s) ready|now)\s*$",
    re.IGNORECASE,
)


def _image_subject(text: str) -> str | None:
    """If `text` is an image-generation request, return the cleaned subject
    (possibly "" when none was given). None when it's not an image request."""
    if not _IMAGE_REQUEST_RE.search(text or ""):
        return None
    m = _IMAGE_OF_RE.search(text)
    subject = (m.group("subject") if m else "").strip()
    subject = _IMAGE_TRAIL_OPEN_RE.sub("", subject).strip()
    for _ in range(2):
        subject = _IMAGE_TRAIL_FLUFF_RE.sub("", subject).strip()
    return subject.strip(" .,!?")


def _image_worker(subject: str) -> None:
    """Background: generate the image, save it, open it, speak when ready."""
    from halo import image_gen, voice
    try:
        bus.emit("image.start", subject=subject)
        path = image_gen.generate_image(subject)
        print(f"  [image] saved: {path}")
        bus.emit("image.done", subject=subject, path=path)
        try:
            _startfile(path)
        except Exception:
            pass
        voice.say(f"Here's your image of {subject}.", blocking=False)
    except Exception as exc:
        print(f"  [image] error: {exc}")
        bus.emit("image.error", subject=subject, error=str(exc))
        voice.say("Sorry, I couldn't generate that image.", blocking=False)


def _try_generate_image(text: str) -> tuple[bool, str]:
    """'generate an image of X' -> kick off real image generation in the
    background and return the ack to speak. (matched, ack)."""
    subject = _image_subject(text)
    if subject is None:
        return False, ""
    if not subject:
        return True, "Sure — what should the image be of?"
    bus.emit("tools.invoke", name="image", text=subject)
    if _DRY_RUN:  # tests: confirm routing without a real API call
        return True, f"Generating an image of {subject}."
    threading.Thread(
        target=_image_worker, args=(subject,), daemon=True,
        name="halo-image-gen",
    ).start()
    return True, f"Generating an image of {subject}. I'll open it when it's ready."


# Politeness wrappers that shroud a tool command ("can you open a browser
# please" / "I want you to open Chrome"). Stripped ONLY inside the tool
# matchers so "open a browser" reaches the local launcher instead of falling
# through to the LLM (which, with sessions running, mis-read "open" as a
# session switch). The caller's original text is untouched for other routing.
_POLITE_LEAD_RE = re.compile(
    r"^\s*(?:"
    r"please|hey|halo|jarvis|ok|okay|"
    r"(?:can|could|would|will)\s+you(?:\s+please)?|"
    r"(?:can|could)\s+(?:i|we)|"
    r"i\s+(?:want|need|would\s+like)(?:\s+you)?\s+to|"
    r"i'?d\s+like(?:\s+you)?\s+to|"
    r"let'?s|go\s+ahead\s+and"
    r")\b[\s,]+",
    re.IGNORECASE,
)
_POLITE_TRAIL_RE = re.compile(
    r"[\s,]+(?:please|thanks|thank\s+you|for\s+me)\s*[.!?]*\s*$",
    re.IGNORECASE,
)


def _strip_politeness(text: str) -> str:
    t = (text or "").strip()
    for _ in range(3):  # peel stacked leads: "hey can you please ..."
        new = _POLITE_LEAD_RE.sub("", t)
        if new == t:
            break
        t = new
    t = _POLITE_TRAIL_RE.sub("", t).strip()
    return t or (text or "").strip()


def _open_app_target(part: str) -> str | None:
    """Normalized app name from an 'open <X>' part, or None if not an
    open-app phrase. Strips a leading article so 'open the spotify' works."""
    m = _OPEN_APP_RE.search(part)
    if not m:
        return None
    raw = m.group(1).strip().strip(" .!?,").lower()
    raw = _OPEN_ARTICLE_RE.sub("", raw).strip()
    return raw or None


def _launch_spec(spec: dict) -> bool:
    try:
        if IS_WIN:
            _startfile(spec["win"])  # ShellExecute: resolves App Paths/PATH/protocol
        elif IS_MAC:
            _spawn(["open", "-a", spec["mac"]])
        else:
            _spawn(list(spec["linux"]))
        return True
    except Exception:
        return False


def _launch_generic(name: str) -> bool:
    """Best-effort launch of an app we have no alias for. os.startfile raises
    synchronously (caught here) when the name can't be resolved, so an unknown
    app fails cleanly instead of popping a Windows 'cannot find' dialog."""
    try:
        if IS_WIN:
            _startfile(name)
        elif IS_MAC:
            _spawn(["open", "-a", name])
        else:
            _spawn([name])
        return True
    except Exception:
        return False


def _is_open_app_known(part: str) -> bool:
    """True iff this is 'open <X>' for an X in our alias map. Used by the
    pre-LLM tool check — only KNOWN apps qualify for the local fast-path, so
    an un-resolvable 'open <gibberish>' still routes through Stage 2 (where a
    failed launch produces a graceful spoken message instead of a dead turn)."""
    target = _open_app_target(part)
    return target in _APP_ALIASES if target else False


def _try_open_app(part: str) -> tuple[bool, str]:
    """Launch 'open <app>' for apps without a dedicated handler.

    Returns (matched, spoken_summary). A recognized open-app phrase counts as
    matched even when the launch fails, so the user hears "I couldn't open X"
    instead of the request silently falling through to a Claude dispatch.
    """
    target = _open_app_target(part)
    if not target:
        return False, ""
    # Filenames ("open notes.md") belong to _try_open_file.
    if _OPEN_FILE_RE.search(part):
        return False, ""
    nice = target.title()
    spec = _APP_ALIASES.get(target)
    if spec:
        ok = _launch_spec(spec)
        return True, (f"Opened {nice}." if ok else f"I couldn't open {nice}.")
    # Generic: only attempt short tokens; a long phrase isn't an app name.
    if len(target.split()) <= 3:
        ok = _launch_generic(target)
        return True, (f"Opened {nice}." if ok else f"I couldn't find {nice} to open.")
    return False, ""


def _open_terminal() -> str:
    if IS_WIN:
        if _DRY_RUN:
            return "Opened Windows Terminal."
        try:
            _startfile("wt.exe")  # Windows Terminal if installed
            return "Opened Windows Terminal."
        except OSError:
            pass
        try:
            _startfile("powershell.exe")
            return "Opened PowerShell."
        except OSError:
            _startfile("cmd.exe")
            return "Opened command prompt."
    if IS_MAC:
        _spawn(["open", "-a", "Terminal"])
        return "Opened Terminal."
    _spawn(["x-terminal-emulator"])
    return "Opened terminal."


def _tell_datetime() -> str:
    """Answer 'what day/date/time is it' locally — no agent, no LLM.

    Returns a speech-friendly string with leading zeros stripped from the
    hour so TTS reads it naturally ('two fifteen PM', not 'zero two...')."""
    import datetime

    now = datetime.datetime.now()
    hour = now.strftime("%I").lstrip("0") or "12"
    return (
        f"It's {now.strftime('%A, %B')} {now.day}, {now.year}. "
        f"The time is {hour}:{now.strftime('%M %p')}."
    )


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
            _OPEN
            + r".*\b(browser|chrome|firefox|edge|web|internet|google|safari"
            + r"|web ?page|web ?site)\b",
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
    # Date / time / day — answered locally, instantly, no LLM round-trip.
    # Broad enough to catch "what day is it", "what's the date", "what time
    # is it now", "tell me the time", "today's date", "current time".
    _Tool(
        re.compile(
            r"\b(?:what(?:'?s| is)?\s+(?:the\s+|today'?s\s+)?(?:date|day|time)"
            r"|what\s+day\s+is\s+(?:it|today)|what\s+time\s+is\s+it"
            r"|today'?s\s+date|current\s+(?:date|time)"
            r"|tell\s+me\s+(?:the\s+)?(?:date|day|time)"
            r"|are\s+you\s+smart\s+enough\s+to\s+tell\s+me\s+(?:the\s+)?(?:date|day|time)"
            r"|do\s+you\s+know\s+(?:the\s+|what\s+)?(?:date|day|time))"
            # don't fire on coding phrases that embed a date/time word, e.g.
            # "time complexity", "date format/column/picker", "time zone/stamp"
            r"(?!\s*(?:complexity|format|column|field|picker|zone|stamp|"
            r"range|parsing|parser|out|delta|series|index|object))\b",
            re.IGNORECASE,
        ),
        _tell_datetime, "datetime",
    ),
]


def is_pure_tool(text: str) -> bool:
    """Strict pre-LLM check: is this transcript ONLY tool commands?

    Returns True iff every comma/and-separated segment matches a tool
    pattern OR the `open <filename>` pattern. Mixed intent like
    "open chrome and build a login page" returns False so it falls
    through to Stage 2 LLM and the code request isn't silently dropped.
    """
    cleaned = _strip_politeness(text).rstrip(".!?,")
    if not cleaned:
        return False
    # "say <text>" is a whole-utterance command — Halo just speaks it. Check
    # before splitting so the entire phrase counts as one local action.
    if _SAY_RE.search(cleaned):
        return True
    # "generate an image of X" — whole-utterance local action (image prompts
    # contain "and"), so it never gets split or dispatched to a coding agent.
    if _image_subject(cleaned) is not None:
        return True
    # Date/time phrases commonly contain "and" ("date and time"). Check the
    # datetime tool against the whole utterance before conjunction splitting.
    # A timezone/location qualifier disqualifies the LOCAL tool (see
    # _TZ_QUALIFIER_RE) so "what time is it in Brisbane" reaches the brain.
    for tool in _TOOLS:
        if tool.name == "datetime" and tool.pattern.search(cleaned):
            return not _TZ_QUALIFIER_RE.search(cleaned)
    parts = [p.strip() for p in _SPLIT_RE.split(cleaned) if p and p.strip()] or [cleaned]
    open_mode = False
    for part in parts:
        if re.search(_OPEN, part):
            open_mode = True
        # A bare list item after an "open ..." command ("...and paint") gets
        # the verb prepended so it's recognized as "open paint".
        probe = part if re.search(_OPEN, part) else (f"open {part}" if open_mode else part)
        if _OPEN_FILE_RE.search(probe):
            continue
        if _is_open_app_known(probe):
            continue
        if not any(tool.pattern.search(probe) for tool in _TOOLS):
            return False
    return True


def try_match(text: str) -> bool:
    """Permissive: did at least one tool fire? Currently unused by the
    orchestrator (which uses is_pure_tool) but kept as a public helper."""
    cleaned = _strip_politeness(text).rstrip(".!?,")
    parts = [p.strip() for p in _SPLIT_RE.split(cleaned) if p and p.strip()] or [cleaned]
    for part in parts:
        if any(tool.pattern.search(part) for tool in _TOOLS):
            return True
        if _is_open_app_known(part):
            return True
    return False


def execute_system_intent(cleaned_text: str) -> tuple[bool, str, str]:
    """Run every matching local tool in `cleaned_text`.

    Splits on "and"/"then"/comma so "open calculator and open chrome"
    fires both handlers. Returns (any_handled, joined_summary, leftover)
    where `leftover` joins the phrases NO local tool could satisfy — e.g.
    "open a browser AND search the web for X" opens the browser and hands
    back "search the web for X" so the caller can offer to delegate it to a
    coding agent instead of silently dropping it.
    """
    text = _strip_politeness(cleaned_text).rstrip(".!?,")

    # "say <text>" — speak the remainder verbatim. Whole-utterance, so it runs
    # before conjunction splitting and short-circuits the rest.
    shandled, sspoken = _try_say(text)
    if shandled:
        bus.emit("tools.invoke", name="say", text=sspoken)
        return True, sspoken, ""

    # "generate an image of X" — whole-utterance, runs before the split so the
    # subject ("a cat AND a dog") isn't broken up. Generates in the background.
    ihandled, iack = _try_generate_image(text)
    if ihandled:
        return True, iack, ""

    parts = [p.strip() for p in _SPLIT_RE.split(text) if p and p.strip()] or [text]

    summaries: list[str] = []
    leftovers: list[str] = []  # phrases no local tool could handle
    any_handled = False
    fired: set[str] = set()  # don't double-fire the same tool in one turn
    # "open A and B" splits to ["open A", "B"]; the bare "B" lost the verb.
    # Once we've seen an open command, treat a later bare app-name segment as
    # another thing to open, so "open the calculator and paint" opens BOTH.
    open_mode = False

    for part in parts:
        if re.search(_OPEN, part):
            open_mode = True
        # File-open path runs first — "open landing.html" otherwise hits
        # the generic _OPEN regex from the next tool and false-fires.
        fhandled, fsummary = _try_open_file(part)
        if fhandled:
            summaries.append(fsummary)
            any_handled = True
            bus.emit("tools.invoke", name="open_file", text=fsummary)
            continue
        matched = False
        for tool in _TOOLS:
            if tool.name in fired:
                continue
            if tool.pattern.search(part):
                # Location/timezone date-time queries are not local — hand
                # them to the brain instead of reading this machine's clock.
                if tool.name == "datetime" and _TZ_QUALIFIER_RE.search(text):
                    continue
                try:
                    result = tool.handler()
                    summaries.append(result)
                    bus.emit("tools.invoke", name=tool.name, text=result)
                except Exception as exc:
                    err = f"failed to open {tool.name}: {exc}"
                    summaries.append(err)
                    bus.emit("tools.invoke", name=tool.name, text=err, error=True)
                any_handled = True
                fired.add(tool.name)
                matched = True
                # NO break — a single phrase can name MORE THAN ONE tool
                # ("open calculator and the browser" / "open calculator in the
                # browser"); fire every matching tool, not just the first.
        if matched:
            continue

        # Generic app launcher — runs only when no dedicated tool matched, so
        # "open chrome" still uses the browser handler but "open paint",
        # "open spotify", "open word" launch locally instead of going to Claude.
        # A bare list item ("...and paint") gets the verb prepended so it's
        # recognized as "open paint".
        probe = part if re.search(_OPEN, part) else (f"open {part}" if open_mode else part)
        # If the bare item names a dedicated tool ("...and the browser"), fire it.
        if probe is not part:
            for tool in _TOOLS:
                if tool.name not in fired and tool.name != "datetime" and tool.pattern.search(probe):
                    try:
                        result = tool.handler()
                        summaries.append(result)
                        bus.emit("tools.invoke", name=tool.name, text=result)
                    except Exception as exc:
                        summaries.append(f"failed to open {tool.name}: {exc}")
                    any_handled = True
                    fired.add(tool.name)
                    matched = True
            if matched:
                continue
        ahandled, asummary = _try_open_app(probe)
        if ahandled:
            summaries.append(asummary)
            any_handled = True
            bus.emit("tools.invoke", name="open_app", text=asummary)
        else:
            # Nothing local matched this segment — keep it as a leftover the
            # caller may delegate ("...and search the web for the score").
            leftovers.append(part)

    return any_handled, "  ".join(summaries), "  ".join(leftovers)
