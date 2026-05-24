"""Follow-up gate for direct-dialogue mode.

After you say "Claude, do X" Halo enters direct-dialogue mode and the
mic stays hot — every subsequent utterance is piped straight to the
active agent so follow-ups ("now also add tests") don't need to
re-state the agent name.

That's convenient — until you take a phone call, talk to a colleague,
or mutter at your own screen. Without a gate, ALL of that audio gets
transcribed and dispatched to Claude as if it were a command.

This module is the gate. It runs against every direct-dialogue
transcript BEFORE dispatch and answers one question:

    Is this utterance plausibly addressed to the active agent?

Four rules, first match wins → SEND. If none match → DROP silently
(the orchestrator stays in direct mode, the user can just talk again).

  Rule 1 — agent name mentioned (claude / codex / fuzzy_triggers)
  Rule 2 — continuation marker on a short utterance ("now also add X")
  Rule 3 — coding imperative + technical/follow-up signal
  Rule 4 — none of the above   → drop

The gate is INTENTIONALLY regex-only: no Ollama round-trip, no model
load. Latency budget for direct-mode follow-ups stays at ~0 ms.

Public API:
    passes(text: str, direct_agent: str | None) -> tuple[bool, str]
        Returns (allow, reason). `reason` is a short tag for logging
        ("agent_name", "continuation", "coding_intent", "side_conversation").
"""

from __future__ import annotations

import re

from halo.agents import AGENTS

# ---------------------------------------------------------------------------
# Keyword sets
# ---------------------------------------------------------------------------

# Continuation / discourse markers people use mid-session to address the
# agent without re-naming them. "now also add tests", "and then make it
# async", "instead use a class".
_CONTINUATION_MARKERS = {
    "now", "also", "then", "next", "instead", "additionally",
    "actually", "wait", "hold on", "scratch that", "ignore that",
    "no actually", "no wait", "on second thought",
    "and now", "now also", "now do", "now make", "now add",
    "and then", "then also", "after that",
    "one more", "one more thing", "another thing",
    # Question/request openers clearly addressed to the agent.
    "what about", "how about", "what if",
    "can you", "could you", "would you", "will you",
    "please", "let's", "lets",
}

# Imperatives that almost always indicate a command to the coding agent.
# Single verbs — the gate looks for any whole-word match in the
# transcript.
_CODING_IMPERATIVES = {
    "write", "make", "build", "create", "add", "remove", "delete",
    "fix", "update", "change", "rewrite", "refactor", "rename",
    "test", "run", "deploy", "ship", "release",
    "open", "close", "save", "commit", "push", "pull", "merge",
    "rebase", "clone", "checkout", "stash",
    "install", "uninstall", "upgrade", "downgrade",
    "edit", "modify", "set", "configure", "generate", "scaffold",
    "format", "lint", "typecheck", "debug",
    "implement", "extract", "inline", "split", "combine",
    "convert", "translate", "port",
    "show", "list", "find", "search", "grep", "read", "explain",
    "review", "audit", "summarize",
    "undo", "revert", "rollback",
    "continue", "finish", "complete",
    # UI / toggle vocabulary — common follow-up shapes for frontend work.
    "switch", "toggle", "enable", "disable", "hide", "reveal",
    "append", "prepend", "insert", "replace", "swap",
    "style", "color", "resize", "shrink", "grow", "expand", "collapse",
}

# Technical nouns / code-context vocabulary. The presence of ANY of
# these in the transcript suggests the user is talking about code,
# not their lunch plans.
_CODING_NOUNS = {
    # Code structure
    "file", "files", "function", "functions", "method", "methods",
    "class", "classes", "variable", "variables", "var", "let", "const",
    "module", "modules", "package", "packages", "library", "libraries",
    "import", "imports", "dependency", "dependencies",
    "interface", "interfaces", "type", "types", "enum", "enums",
    "struct", "structs", "trait", "traits",
    "constant", "constants", "parameter", "parameters", "param", "params",
    "argument", "arguments", "arg", "args",
    "return", "returns", "yield", "throw", "throws", "raise", "raises",
    # Code artifacts
    "code", "script", "program", "binary", "executable",
    "snippet", "block", "statement", "expression",
    # Testing / quality
    "test", "tests", "spec", "specs", "fixture", "fixtures",
    "mock", "mocks", "stub", "stubs", "assertion", "assertions",
    "bug", "bugs", "error", "errors", "exception", "exceptions",
    "warning", "warnings", "issue", "issues",
    # Web / frontend
    "component", "components", "page", "pages", "route", "routes",
    "endpoint", "endpoints", "api", "apis", "schema", "schemas",
    "model", "models", "view", "views", "controller", "controllers",
    "hook", "hooks", "prop", "props", "state", "store",
    "style", "styles", "css", "html", "dom",
    # UI elements (most common follow-up nouns for frontend changes)
    "button", "buttons", "menu", "menus", "modal", "modals",
    "form", "forms", "input", "inputs", "field", "fields",
    "label", "labels", "icon", "icons", "image", "images",
    "header", "headers", "footer", "footers", "sidebar", "sidebars",
    "navbar", "dropdown", "tooltip", "popover", "dialog", "dialogs",
    "color", "colors", "background", "foreground", "theme", "themes",
    "layout", "grid", "flex", "padding", "margin", "border", "borders",
    "shadow", "shadows", "animation", "animations", "transition",
    "font", "fonts", "size", "spacing",
    "mode",  # "dark mode", "light mode"
    # Backend / data
    "database", "db", "table", "tables", "column", "columns", "row", "rows",
    "query", "queries", "migration", "migrations",
    "server", "servers", "client", "clients", "request", "requests",
    "response", "responses", "header", "headers", "payload",
    # Git / workflow
    "branch", "branches", "commit", "commits", "pr", "mr",
    "pull request", "merge request", "repo", "repos", "repository",
    "remote", "origin", "upstream", "head", "main", "master",
    # Data types
    "string", "strings", "int", "integer", "integers", "float", "floats",
    "bool", "boolean", "booleans", "list", "lists", "array", "arrays",
    "dict", "dictionary", "dictionaries", "map", "maps", "set", "sets",
    "tuple", "tuples", "json", "yaml", "toml", "xml", "csv",
    # Concepts
    "feature", "features", "bugfix", "patch",
    "config", "configuration", "setting", "settings", "env",
    "log", "logs", "logging", "trace", "traces",
    "thread", "threads", "process", "processes", "async", "sync",
    "loop", "iteration", "recursion",
    "input", "output", "stdin", "stdout", "stderr",
    "directory", "folder", "path", "filename",
}

# Named tech / frameworks / languages. Same role as _CODING_NOUNS but
# the proper-noun vocab kept separate so it's easier to extend.
_CODING_TECH = {
    "python", "javascript", "typescript", "js", "ts", "rust", "go", "golang",
    "java", "kotlin", "swift", "ruby", "php", "perl", "lua",
    "c", "cpp", "csharp",
    "react", "vue", "svelte", "angular", "next", "nextjs", "nuxt", "remix",
    "tailwind", "bootstrap",
    "node", "nodejs", "deno", "bun",
    "django", "flask", "fastapi", "rails", "express", "nestjs",
    "postgres", "postgresql", "mysql", "sqlite", "mongodb", "redis",
    "docker", "kubernetes", "k8s", "terraform", "ansible",
    "aws", "gcp", "azure", "vercel", "netlify", "cloudflare", "supabase",
    "github", "gitlab", "bitbucket",
    "linux", "windows", "macos", "ubuntu", "debian",
    "vscode", "vim", "neovim", "emacs", "intellij",
    "pytest", "jest", "vitest", "playwright", "cypress",
    "ollama", "anthropic", "openai",
}

# Things that strongly suggest a real-world side conversation — phone
# greetings, scheduling chatter, asides to other humans. If ANY of these
# fire we drop early even if a weak imperative also matched.
#
# Conservative list — kept short on purpose; the default DROP behaviour
# does the heavy lifting. These exist for the cases where a transcript
# accidentally contains a coding-imperative-shaped word ("fix the AC",
# "make plans") but is clearly a human-to-human exchange.
_SIDE_CONVERSATION_MARKERS = (
    # Phone openers / closers
    r"\bhello\?+",
    r"\bcan you hear me\b",
    r"\blet me call you back\b",
    r"\bi'?ll call you back\b",
    r"\bcall me back\b",
    r"\bcan you hold\b",
    r"\bbear with me\b",
    r"\bbe right back\b",
    # Lunch / social
    r"\bgrab (?:lunch|coffee|dinner|a beer|a drink)\b",
    r"\bwant (?:to grab|to get) (?:lunch|coffee|dinner)\b",
    r"\bsee you (?:at|in|later|tomorrow|tonight)\b",
    # Greetings to a named person
    r"\bhey (?!claude|codex|halo)\w+,",
    r"\bhi (?!claude|codex|halo)\w+,",
)
_SIDE_CONVERSATION_RE = re.compile(
    "|".join(_SIDE_CONVERSATION_MARKERS),
    re.IGNORECASE,
)

# Short-utterance threshold for the continuation-marker rule. Long
# utterances with "now" sprinkled in could be anything; short ones
# starting with a continuation marker are almost always follow-ups.
_SHORT_UTTERANCE_WORDS = 12

# Pattern compiled once per agent at module load. Built from the
# AGENT registry so adding a new agent in halo/agents.py automatically
# extends the gate without code changes here.
_AGENT_NAME_PATTERNS: dict[str, re.Pattern[str]] = {}


def _build_agent_patterns() -> None:
    """Compile a whole-word regex for every agent's trigger list."""
    for key, cfg in AGENTS.items():
        triggers = tuple(cfg.voice_triggers) + tuple(cfg.fuzzy_triggers)
        if not triggers:
            continue
        # Escape each trigger and join with | inside a (?: ... ) group.
        # Whole-word match (\b) so "claud" doesn't fire on "claudia".
        joined = "|".join(re.escape(t.lower()) for t in triggers)
        _AGENT_NAME_PATTERNS[key] = re.compile(rf"\b(?:{joined})\b", re.IGNORECASE)


_build_agent_patterns()


def _words(text: str) -> set[str]:
    """Lowercase word-set for fast set-membership checks."""
    return set(re.findall(r"\b[\w']+\b", text.lower()))


def _mentions_any_agent(text: str) -> str | None:
    """Return the agent_key whose trigger fired, or None."""
    for key, pattern in _AGENT_NAME_PATTERNS.items():
        if pattern.search(text):
            return key
    return None


# Strong openers that pass Rule 2 regardless of utterance length.
# These are unambiguous "I am addressing you, agent" phrases — even a
# long sentence beginning with them should be sent.
_STRONG_OPENERS = (
    "what about", "how about", "what if",
    "can you", "could you", "would you", "will you",
    "please ",  # trailing space so "pleasant" doesn't match
)


def _starts_with_continuation(text: str) -> bool:
    """True if the utterance opens with a continuation marker
    (case-insensitive, allows light leading filler)."""
    cleaned = text.strip().lower()
    if not cleaned:
        return False
    # Strip leading filler ("uh", "um", "ok", "okay") so "um now also..."
    # still reads as a continuation.
    cleaned = re.sub(r"^(?:uh|um|er|ah|ok|okay|so|well|alright)[\s,]+", "", cleaned)
    for marker in _CONTINUATION_MARKERS:
        if cleaned.startswith(marker):
            # Marker must be followed by space, comma, or end-of-string —
            # not extra letters ("nowhere" shouldn't match "now").
            end = len(marker)
            if end == len(cleaned) or not cleaned[end].isalpha():
                return True
    return False


def _starts_with_strong_opener(text: str) -> bool:
    """Strong openers bypass the short-utterance cap on Rule 2."""
    cleaned = text.strip().lower()
    cleaned = re.sub(r"^(?:uh|um|er|ah|ok|okay|so|well|alright)[\s,]+", "", cleaned)
    return any(cleaned.startswith(o) for o in _STRONG_OPENERS)


def _contains_continuation_marker(text: str) -> bool:
    """True if a continuation marker appears anywhere as a whole word."""
    low = " " + text.lower() + " "
    for marker in _CONTINUATION_MARKERS:
        # Multi-word markers ("hold on") need a different match
        # strategy than single-word ones.
        if " " in marker:
            if f" {marker} " in low or low.startswith(f"{marker} ") or low.endswith(f" {marker}"):
                return True
        else:
            if re.search(rf"\b{re.escape(marker)}\b", low):
                return True
    return False


def _has_coding_imperative(words: set[str]) -> bool:
    return bool(words & _CODING_IMPERATIVES)


def _has_coding_signal(words: set[str], text: str) -> bool:
    """A coding 'signal' = anything that anchors the utterance in the
    code domain. Coding noun, named tech, or a continuation marker."""
    if words & _CODING_NOUNS:
        return True
    if words & _CODING_TECH:
        return True
    if _contains_continuation_marker(text):
        return True
    return False


def _looks_like_side_conversation(text: str) -> bool:
    return bool(_SIDE_CONVERSATION_RE.search(text))


def passes(text: str, direct_agent: str | None) -> tuple[bool, str]:
    """Decide whether a direct-mode transcript should reach the agent.

    Returns (allow, reason). `reason` is a short tag suitable for logs
    and bus events:

      allow = True:
        "agent_name"     — Rule 1 fired (claude/codex/fuzzy mentioned)
        "continuation"   — Rule 2 fired (continuation marker on short utterance)
        "coding_intent"  — Rule 3 fired (imperative + technical signal)
      allow = False:
        "empty"          — transcript was blank after cleanup
        "side_conversation" — explicit side-talk pattern fired
        "no_signal"      — no rule matched
    """
    text = (text or "").strip()
    if not text:
        return False, "empty"

    # Hard-drop: explicit phone/lunch chatter. These patterns are
    # specific enough that even a weak coding-keyword overlap
    # ("fix the AC", "make lunch plans") should still be dropped.
    if _looks_like_side_conversation(text):
        return False, "side_conversation"

    # Rule 1 — agent name mentioned. Most reliable signal.
    if _mentions_any_agent(text):
        return True, "agent_name"

    words = _words(text)

    # Rule 2 — opens with a continuation marker. Strong openers
    # ("what about", "can you", ...) pass regardless of length; weaker
    # ones ("now", "also") only pass when short, because a long
    # utterance with "now" buried at the front could be ambient
    # ("now I was telling John that we need to...").
    if _starts_with_strong_opener(text):
        return True, "continuation"
    if _starts_with_continuation(text) and len(words) <= _SHORT_UTTERANCE_WORDS:
        return True, "continuation"

    # Rule 3 — coding imperative + at least one signal anchor.
    if _has_coding_imperative(words) and _has_coding_signal(words, text):
        return True, "coding_intent"

    return False, "no_signal"
