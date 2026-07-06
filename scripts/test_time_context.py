"""Unit tests for time-awareness: _relative_time phrasing (pure) and Memory's
first_seen / last_seen / time_context against an in-memory DB."""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from halo.memory import Memory, _relative_time

DAY = 86400.0


def main() -> int:
    f = 0

    def ok(label, cond):
        nonlocal f
        f += 0 if cond else 1
        print(f"  [{'OK' if cond else 'FAIL'}] {label}")

    now = 1_000_000.0
    print("relative phrasing:")
    ok("just now", _relative_time(now - 10, now) == "just now")
    ok("a minute ago", _relative_time(now - 60, now) == "a minute ago")
    ok("5 minutes ago", _relative_time(now - 300, now) == "5 minutes ago")
    ok("about an hour ago", _relative_time(now - 3600, now) == "about an hour ago")
    ok("3 hours ago", _relative_time(now - 3 * 3600, now) == "3 hours ago")
    ok("yesterday", _relative_time(now - DAY, now) == "yesterday")
    ok("3 days ago", _relative_time(now - 3 * DAY, now) == "3 days ago")
    ok("last week", _relative_time(now - 9 * DAY, now) == "last week")
    ok("3 weeks ago", _relative_time(now - 21 * DAY, now) == "3 weeks ago")
    ok("3 months ago", _relative_time(now - 90 * DAY, now) == "3 months ago")

    print("\nMemory first/last/time_context:")
    with tempfile.TemporaryDirectory() as d:
        m = Memory(Path(d) / "mem.db", retention_days=30)
        base = time.time()
        m._conn.execute("INSERT INTO turns(ts,role,text) VALUES(?,?,?)", (base - 100000, "user", "first"))
        m._conn.execute("INSERT INTO turns(ts,role,text) VALUES(?,?,?)", (base - 50, "user", "recent"))
        m._conn.commit()
        ok("first_seen = earliest", abs((m.first_seen() or 0) - (base - 100000)) < 1)
        ok("last_seen = latest", abs((m.last_seen() or 0) - (base - 50)) < 1)
        tc = m.time_context()
        ok("time_context has current date", "Current real date and time:" in tc)
        ok("time_context has first-started", "first started using Halo" in tc)
        # empty DB -> no first-started line, no crash
        m2 = Memory(Path(d) / "empty.db", retention_days=30)
        tc2 = m2.time_context()
        ok("empty DB: still has date, no crash", "Current real date and time:" in tc2)
        ok("empty DB: no first-started line", "first started" not in tc2)
        m._conn.close()   # close before the temp dir is removed (Windows lock)
        m2._conn.close()

    total = 16
    print(f"\n{total - f}/{total} passed")
    return 1 if f else 0


if __name__ == "__main__":
    raise SystemExit(main())
