"""Regression tests for the persistent memory layer (halo/memory.py)."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from halo.memory import Memory, auto_facts, extract_fact


def check(label: str, ok: bool) -> int:
    print(f"  [{'OK' if ok else 'FAIL'}] {label}")
    return 0 if ok else 1


def main() -> int:
    failed = 0

    # --- pure helpers (no DB) ---
    failed += check(
        "extract_fact pulls the X out of 'remember that ...'",
        extract_fact("remember that I use vim") == ("I use vim", "fact"),
    )
    failed += check(
        "extract_fact ignores non-remember text",
        extract_fact("open the browser") is None,
    )
    failed += check(
        "auto_facts catches 'I'm working on ...'",
        auto_facts("I'm working on the Halo project")[0][1] == "project",
    )
    failed += check(
        "auto_facts catches a preference",
        auto_facts("I prefer short replies")[0][1] == "preference",
    )
    failed += check(
        "auto_facts ignores ordinary chatter",
        auto_facts("what's the weather today") == [],
    )

    # --- DB round-trip (temp file) ---
    db = os.path.join(tempfile.gettempdir(), "halo_mem_pytest.db")
    if os.path.exists(db):
        os.remove(db)
    m = Memory(db, retention_days=30)
    m.record_turn("user", "open calculator")
    m.record_turn("halo", "Opened calculator.")
    m.record_turn("user", "open the browser")
    m.record_turn("halo", "Opened browser.")
    m.record_fact("works on socialmanager", "project", 3.0)

    failed += check("stats counts turns + facts", m.stats() == (4, 1))
    recent = m.recent_turns(10)
    failed += check(
        "recent_turns is chronological (calculator before browser)",
        recent[0] == ("user", "open calculator")
        and recent[2] == ("user", "open the browser"),
    )
    block = m.render_for_brain("what did you open first")
    failed += check("render_for_brain includes the fact", "socialmanager" in block)
    failed += check(
        "render_for_brain orders calculator before browser",
        block.index("open calculator") < block.index("open the browser"),
    )
    # re-stating a fact bumps weight, doesn't duplicate
    m.record_fact("works on socialmanager", "project", 3.0)
    failed += check("duplicate fact not re-inserted", m.stats()[1] == 1)

    m.close()
    os.remove(db)

    total = 10
    print(f"\n{total - failed}/{total} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
