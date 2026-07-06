"""Unit tests for halo.skills — frontmatter parsing, fuzzy skill matching, and
filesystem discovery against a temp fixture tree."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from halo.skills import (
    CLAUDE,
    CODEX,
    Skill,
    discover_skills,
    match_skill,
    parse_frontmatter,
)

SKILLS = [
    Skill("ai-captions", "Write Instagram captions for the AI Playgrounds brand", CLAUDE, "p1", "user"),
    Skill("yt-shorts-caption", "Write a YouTube Shorts title description and hashtags", CLAUDE, "p2", "user"),
    Skill("imagegen", "Generate or edit raster images photos illustrations mockups", CODEX, "p3", "system"),
    Skill("deep-research", "Fan out web searches fetch sources synthesize a cited report", CLAUDE, "p4", "user"),
]


def main() -> int:
    f = 0

    def ok(label, cond):
        nonlocal f
        f += 0 if cond else 1
        print(f"  [{'OK' if cond else 'FAIL'}] {label}")

    print("parse_frontmatter:")
    fm = parse_frontmatter('---\nname: "imagegen"\ndescription: "make images"\n---\n# body')
    ok("quoted name", fm.get("name") == "imagegen")
    ok("quoted description", fm.get("description") == "make images")
    fm2 = parse_frontmatter("---\nname: deep-research\n---\nbody")
    ok("unquoted name", fm2.get("name") == "deep-research")
    ok("no frontmatter -> empty", parse_frontmatter("# just a heading") == {})

    print("\nmatch_skill (should match):")

    def expect(label, text, name):
        nonlocal f
        m = match_skill(text, SKILLS)
        got = m[0].name if m else None
        cond = got == name
        f += 0 if cond else 1
        score = f"{m[1]:.0f}" if m else "-"
        print(f"  [{'OK' if cond else 'FAIL'}] {label:38} {text!r:42} -> {got} (score {score}, want {name})")

    expect("instagram captions", "make instagram captions for my brand", "ai-captions")
    expect("youtube shorts", "write a youtube shorts title", "yt-shorts-caption")
    expect("explicit name reference", "use the imagegen skill to make art", "imagegen")
    expect("deep research phrase", "do some deep research on this", "deep-research")

    print("\nmatch_skill (should NOT match — route normally):")
    expect("coding task", "build me a login page", None)
    expect("plain question", "what time is it", None)
    expect("greeting", "hey how are you", None)

    print("\ndiscover_skills (temp fixture tree):")
    with tempfile.TemporaryDirectory() as d:
        home = Path(d) / "home"
        cwd = Path(d) / "proj"
        # ~/.claude/skills/foo/SKILL.md
        cs = home / ".claude" / "skills" / "foo"
        cs.mkdir(parents=True)
        (cs / "SKILL.md").write_text('---\nname: "foo"\ndescription: "does foo"\n---\n', encoding="utf-8")
        # ~/.codex/skills/.system/bar/SKILL.md
        bs = home / ".codex" / "skills" / ".system" / "bar"
        bs.mkdir(parents=True)
        (bs / "SKILL.md").write_text('---\nname: "bar"\ndescription: "does bar"\n---\n', encoding="utf-8")
        # project .claude/skills/proj-skill/SKILL.md
        ps = cwd / ".claude" / "skills" / "proj-skill"
        ps.mkdir(parents=True)
        (ps / "SKILL.md").write_text('---\nname: "proj-skill"\ndescription: "project one"\n---\n', encoding="utf-8")

        found = discover_skills(cwd=cwd, home=home)
        names = {s.name for s in found}
        ok("found claude user skill 'foo'", "foo" in names)
        ok("found codex system skill 'bar'", "bar" in names)
        ok("found project skill 'proj-skill'", "proj-skill" in names)
        ok("codex 'bar' tagged system", any(s.name == "bar" and s.source == "system" for s in found))
        ok("claude 'foo' agent is claude", any(s.name == "foo" and s.agent_key == CLAUDE for s in found))
        ok("codex 'bar' agent is codex", any(s.name == "bar" and s.agent_key == CODEX for s in found))

    total = 17
    print(f"\n{total - f}/{total} passed")
    return 1 if f else 0


if __name__ == "__main__":
    raise SystemExit(main())
