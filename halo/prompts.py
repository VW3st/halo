"""User-adjustable prompts.

The brain's wording (routing instructions, the summarizer, the dictation
autocorrect) used to be baked into Python string literals — un-editable without
touching code. This module makes them overridable: the literals in `router.py`
are the DEFAULTS, but if the user drops a file at `~/.halo/prompts/<name>.txt`
(or `.md`), it replaces the default. Routing SCHEMA + validation stay in code —
only the wording is tunable.

`halo prompts --init` scaffolds the files; `halo prompts` lists them. Overrides
are read once per process (prompts are edited between runs, not mid-run).
"""

from __future__ import annotations

import os
from pathlib import Path

# Known prompt slots + one-line descriptions (drives `halo prompts`).
KNOWN: dict[str, str] = {
    "stage2_router": "Stage-2 routing system prompt (intent / agent classification)",
    "stage2_session": "Multi-session routing block (appended when sessions exist)",
    "summarize": "Reply summarization (long agent replies -> one spoken sentence)",
    "stt_correct": "Dictation autocorrect prompt",
}

_cache: dict[str, str] = {}


def prompts_dir() -> Path:
    override = os.environ.get("HALO_PROMPTS_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".halo" / "prompts"


def get(name: str, default: str) -> str:
    """Return the user override for `name` if a non-empty file exists, else the
    baked `default`. Cached per process."""
    if name in _cache:
        return _cache[name]
    d = prompts_dir()
    for ext in (".txt", ".md"):
        p = d / f"{name}{ext}"
        try:
            if p.is_file():
                text = p.read_text(encoding="utf-8").strip()
                if text:
                    _cache[name] = text
                    return text
        except OSError:
            pass
    _cache[name] = default
    return default


def write_defaults(defaults: dict[str, str], *, overwrite: bool = False) -> list[Path]:
    """Write baked defaults to editable files so the user can tweak them.
    Skips existing files unless `overwrite`. Returns the paths written."""
    d = prompts_dir()
    d.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name, text in defaults.items():
        p = d / f"{name}.txt"
        if p.exists() and not overwrite:
            continue
        p.write_text(text, encoding="utf-8")
        written.append(p)
    return written
