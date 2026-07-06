"""Skill discovery + routing — "what can Claude/Codex do, and which skill fits?"

Claude Code and Codex both ship "skills": a folder with a `SKILL.md` whose YAML
frontmatter has a `name` and a `description`. They live in well-known places:

  Claude:  ~/.claude/skills/<name>/SKILL.md
           ~/.claude/plugins/**/skills/<name>/SKILL.md   (plugin skills)
           <cwd>/.claude/skills/<name>/SKILL.md          (project skills)
  Codex:   ~/.codex/skills/**/SKILL.md                   (incl. .system builtins)

This module discovers them (no shelling out — a fast filesystem scan that also
works offline), and fuzzy-matches a spoken command to a skill so "make a poster
for AIP" can route to a matching design/image skill if one exists.

Discovery touches the filesystem; `parse_frontmatter` and `match_skill` are pure
and unit-tested (scripts/test_skills.py).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

CLAUDE = "claude_code"
CODEX = "codex_cli"


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    agent_key: str          # CLAUDE or CODEX
    path: str               # SKILL.md location
    source: str             # user | project | plugin | system

    @property
    def agent_label(self) -> str:
        return "Claude" if self.agent_key == CLAUDE else "Codex"


_FRONTMATTER_RE = re.compile(r"^﻿?---\s*\n(.*?)\n---\s*", re.DOTALL)
_KV_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(.*)$")


def parse_frontmatter(text: str) -> dict:
    """Extract the leading YAML `key: value` block from a SKILL.md. Pure.

    Handles quoted values and ignores everything after the first `---` fence.
    Multi-line/folded YAML values fall back to the first line (good enough for
    the name/description we need)."""
    m = _FRONTMATTER_RE.match(text or "")
    if not m:
        return {}
    out: dict[str, str] = {}
    for line in m.group(1).splitlines():
        kv = _KV_RE.match(line)
        if not kv:
            continue
        key, val = kv.group(1).strip().lower(), kv.group(2).strip()
        if len(val) >= 2 and val[0] in "\"'" and val[-1] == val[0]:
            val = val[1:-1]
        out[key] = val
    return out


def _skill_from_file(path: Path, agent_key: str, source: str) -> Skill | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    fm = parse_frontmatter(text)
    name = fm.get("name") or path.parent.name
    if not name:
        return None
    return Skill(
        name=name.strip(),
        description=(fm.get("description") or "").strip(),
        agent_key=agent_key,
        path=str(path),
        source=source,
    )


def _scan(glob_root: Path, pattern: str, agent_key: str, source: str) -> list[Skill]:
    out: list[Skill] = []
    if not glob_root.is_dir():
        return out
    try:
        for p in glob_root.glob(pattern):
            if p.name == "SKILL.md":
                s = _skill_from_file(p, agent_key, source)
                if s is not None:
                    out.append(s)
    except OSError:
        pass
    return out


def discover_skills(cwd: Path | None = None, home: Path | None = None) -> list[Skill]:
    """Scan the known skill locations for Claude + Codex. De-duped by
    (agent, name); first occurrence wins (user/project beat plugin/system)."""
    home = home or Path.home()
    cwd = cwd or Path.cwd()
    found: list[Skill] = []

    # Claude — project skills first (most specific), then user, then plugins.
    found += _scan(cwd / ".claude" / "skills", "*/SKILL.md", CLAUDE, "project")
    found += _scan(home / ".claude" / "skills", "*/SKILL.md", CLAUDE, "user")
    plugins = home / ".claude" / "plugins"
    if plugins.is_dir():
        try:
            for p in plugins.rglob("SKILL.md"):
                # only count files whose grandparent dir is literally "skills"
                if p.parent.parent.name == "skills":
                    s = _skill_from_file(p, CLAUDE, "plugin")
                    if s is not None:
                        found.append(s)
        except OSError:
            pass

    # Codex — user skills then the bundled .system builtins.
    codex_root = home / ".codex" / "skills"
    if codex_root.is_dir():
        try:
            for p in codex_root.rglob("SKILL.md"):
                source = "system" if ".system" in p.parts else "user"
                s = _skill_from_file(p, CODEX, source)
                if s is not None:
                    found.append(s)
        except OSError:
            pass

    seen: set[tuple[str, str]] = set()
    deduped: list[Skill] = []
    for s in found:
        key = (s.agent_key, s.name.lower())
        if key in seen:
            continue
        seen.add(key)
        deduped.append(s)
    return deduped


# --- matching ---------------------------------------------------------------

_TOKEN_RE = re.compile(r"[a-z0-9]+")
# Words too generic to count as a skill-match signal.
_STOP = frozenset(
    """the a an of for to and or with on in into make me my our your create
    build do use using please can you could would i want need get new""".split()
)


def _tokens(text: str) -> set[str]:
    return {t for t in _TOKEN_RE.findall((text or "").lower()) if t not in _STOP}


def _name_tokens(name: str) -> set[str]:
    # "yt-shorts-caption" / "ai_captions" -> {yt, shorts, caption, ...}
    return {t for t in _TOKEN_RE.findall((name or "").lower()) if t not in _STOP}


def match_skill(
    text: str,
    skills: list[Skill],
    *,
    min_score: float = 2.0,
) -> tuple[Skill, float] | None:
    """Best skill for a spoken command, or None if nothing clears `min_score`.

    Pure + conservative. Scoring (per skill):
      +3 for each command token that appears in the skill NAME (strong signal),
      +1 for each command token that appears in the DESCRIPTION,
      +2 bonus if the skill's full name appears as a phrase in the command.
    Returns (skill, score). Ties broken by name length (more specific first)."""
    cmd = _tokens(text)
    if not cmd or not skills:
        return None
    text_l = (text or "").lower()
    best: tuple[Skill, float] | None = None
    for s in skills:
        ntoks = _name_tokens(s.name)
        dtoks = _tokens(s.description)
        score = 3.0 * len(cmd & ntoks) + 1.0 * len(cmd & dtoks)
        if s.name.lower() in text_l or s.name.lower().replace("-", " ") in text_l:
            score += 2.0
        if score <= 0:
            continue
        if best is None or score > best[1] or (
            score == best[1] and len(s.name) > len(best[0].name)
        ):
            best = (s, score)
    if best is None or best[1] < min_score:
        return None
    return best


# --- live registry ----------------------------------------------------------

class SkillRegistry:
    """In-memory skill list with a cheap rescan. One instance per Halo run."""

    def __init__(self) -> None:
        self._skills: list[Skill] = []

    def refresh(self, cwd: Path | None = None) -> list[Skill]:
        self._skills = discover_skills(cwd=cwd)
        return self._skills

    def all(self) -> list[Skill]:
        return list(self._skills)

    def for_agent(self, agent_key: str) -> list[Skill]:
        return [s for s in self._skills if s.agent_key == agent_key]

    def match(self, text: str, *, agent_key: str | None = None, min_score: float = 2.0):
        pool = self.for_agent(agent_key) if agent_key else self._skills
        return match_skill(text, pool, min_score=min_score)

    def count(self) -> int:
        return len(self._skills)
