"""Persistent memory for Halo — short-term, long-term, and important facts.

SQLite-backed (stdlib only), local, and private — nothing leaves the machine.
Every conversation turn, tool action, and agent dispatch is logged; explicit
facts ("remember that ...") and salient statements ("I'm working on ...") are
kept as durable, importance-weighted facts. Each turn a compact MEMORY block
(top facts + recent/relevant turns) is injected into the routing brain and the
chat model, so Halo can answer "what did we just do?" / "what was the last
command?" and recall across sleep/wake AND restarts.

Tiers map to the user's mental model:
  - short term   -> recent turns (this + the last few sessions)
  - long term    -> all turns, pruned after `retention_days`
  - important    -> facts (projects / preferences / routines), never pruned

Designed as a narrow interface (record_turn / record_fact / render_for_brain)
so a vector/graph backend (e.g. Mem0) can replace the keyword retrieval later
without touching callers.
"""

from __future__ import annotations

import re
import sqlite3
import threading
import time
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS turns (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  ts      REAL NOT NULL,
  role    TEXT NOT NULL,
  text    TEXT NOT NULL,
  session TEXT
);
CREATE INDEX IF NOT EXISTS idx_turns_ts ON turns(ts);
CREATE TABLE IF NOT EXISTS facts (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  ts       REAL NOT NULL,
  category TEXT NOT NULL,
  text     TEXT NOT NULL UNIQUE,
  weight   REAL NOT NULL DEFAULT 2.0
);
"""

_WORD_RE = re.compile(r"[a-z0-9']+")
_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "to", "of", "in", "on", "at", "for",
    "is", "are", "was", "were", "be", "do", "did", "does", "you", "i", "it",
    "that", "this", "what", "how", "why", "we", "me", "my", "your", "with",
    "so", "just", "okay", "ok", "yeah", "yes", "no", "halo", "jarvis", "hey",
}


def _tokens(text: str) -> set[str]:
    return {w for w in _WORD_RE.findall((text or "").lower()) if w not in _STOPWORDS and len(w) > 1}


# Salient first-person statements worth keeping as durable facts, with the
# category they map to. Auto-extraction is deliberately conservative — the
# explicit "remember ..." command (see extract_fact) is the primary path.
_SALIENT_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\b(?:i'?m|i am|we'?re|we are)\s+(?:working|building|coding)\s+on\b.+", re.I), "project"),
    (re.compile(r"\bmy\s+(?:project|projects|main project|current project)\s+(?:is|are)\b.+", re.I), "project"),
    (re.compile(r"\bi\s+(?:prefer|like|want|always|usually|hate|don'?t like)\b.+", re.I), "preference"),
    (re.compile(r"\b(?:every ?day|each day|daily)\b.+", re.I), "routine"),
]
# Explicit "remember that X" / "note that X" / "keep in mind X".
_REMEMBER_RE = re.compile(
    r"^\s*(?:please\s+|hey\s+|halo\s+|jarvis\s+)*"
    r"(?:remember|note|keep in mind|don'?t forget|make a note)"
    r"(?:\s+that|\s+this|\s+to)?[\s,:\-]+(.+)$",
    re.IGNORECASE,
)


def extract_fact(text: str) -> tuple[str, str] | None:
    """If `text` is an explicit 'remember X' command, return (fact, category).
    The fact is the X part, recorded verbatim. None otherwise."""
    m = _REMEMBER_RE.match((text or "").strip())
    if not m:
        return None
    fact = m.group(1).strip().rstrip(".!?")
    return (fact, "fact") if fact else None


def auto_facts(user_text: str) -> list[tuple[str, str]]:
    """Conservative auto-extraction of durable facts from a user turn."""
    t = (user_text or "").strip()
    out: list[tuple[str, str]] = []
    for pat, cat in _SALIENT_PATTERNS:
        if pat.search(t):
            out.append((t.rstrip(".!?"), cat))
            break
    return out


class Memory:
    def __init__(self, path: str | Path, retention_days: int = 30) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.retention_days = retention_days
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # --- writes -----------------------------------------------------------
    def record_turn(self, role: str, text: str, session: str | None = None) -> None:
        text = (text or "").strip()
        if not text:
            return
        with self._lock:
            self._conn.execute(
                "INSERT INTO turns(ts, role, text, session) VALUES (?,?,?,?)",
                (time.time(), role, text[:1000], session),
            )
            self._conn.commit()

    def record_fact(self, text: str, category: str = "fact", weight: float = 3.0) -> None:
        text = (text or "").strip()
        if not text:
            return
        with self._lock:
            # Re-stating a fact bumps its weight (and recency).
            self._conn.execute(
                "INSERT INTO facts(ts, category, text, weight) VALUES (?,?,?,?) "
                "ON CONFLICT(text) DO UPDATE SET weight = weight + 0.5, ts = excluded.ts",
                (time.time(), category, text[:500], weight),
            )
            self._conn.commit()

    def forget_fact(self, query: str) -> int:
        """Delete facts matching a keyword (for 'forget that ...'). Returns count."""
        toks = _tokens(query)
        if not toks:
            return 0
        removed = 0
        with self._lock:
            for fid, text in self._conn.execute("SELECT id, text FROM facts").fetchall():
                if toks & _tokens(text):
                    self._conn.execute("DELETE FROM facts WHERE id = ?", (fid,))
                    removed += 1
            self._conn.commit()
        return removed

    def prune(self) -> int:
        """Drop turns older than retention_days (long-term cap). Facts stay."""
        cutoff = time.time() - self.retention_days * 86400
        with self._lock:
            cur = self._conn.execute("DELETE FROM turns WHERE ts < ?", (cutoff,))
            self._conn.commit()
            return cur.rowcount

    # --- reads ------------------------------------------------------------
    def recent_turns(self, limit: int = 14) -> list[tuple[str, str]]:
        cur = self._conn.execute(
            "SELECT role, text FROM turns ORDER BY id DESC LIMIT ?", (limit,)
        )
        return list(reversed(cur.fetchall()))

    def search_turns(self, query: str, limit: int = 4, scan: int = 600) -> list[tuple[str, str]]:
        """Keyword-overlap search over older turns (recency breaks ties)."""
        toks = _tokens(query)
        if not toks:
            return []
        rows = self._conn.execute(
            "SELECT role, text, ts FROM turns ORDER BY id DESC LIMIT ?", (scan,)
        ).fetchall()
        scored = []
        for role, text, ts in rows:
            overlap = len(toks & _tokens(text))
            if overlap:
                scored.append((overlap, ts, role, text))
        scored.sort(reverse=True)
        return [(r, t) for _, _, r, t in scored[:limit]]

    def top_facts(self, limit: int = 8) -> list[tuple[str, str]]:
        cur = self._conn.execute(
            "SELECT category, text FROM facts ORDER BY weight DESC, ts DESC LIMIT ?",
            (limit,),
        )
        return cur.fetchall()

    def render_for_brain(self, current: str = "", max_turns: int = 14, max_facts: int = 8) -> str:
        """Compact MEMORY block: durable facts + recent turns + any older turns
        relevant to `current`. Empty string when there's nothing to show."""
        facts = self.top_facts(max_facts)
        recent = self.recent_turns(max_turns)
        recent_texts = {t for _, t in recent}
        relevant = [
            (r, t) for (r, t) in self.search_turns(current, limit=4)
            if t not in recent_texts
        ] if current else []

        if not facts and not recent and not relevant:
            return ""

        lines: list[str] = ["# MEMORY (what you remember about the user -- use it, don't invent)"]
        if facts:
            lines.append("Known facts:")
            for cat, text in facts:
                lines.append(f"- [{cat}] {text}")
        if relevant:
            lines.append("Earlier, relevant to now:")
            for role, text in relevant:
                lines.append(f"{'You' if role == 'user' else 'Halo'}: {text}")
        if recent:
            lines.append("Recent conversation (earliest to latest):")
            for role, text in recent:
                lines.append(f"{'You' if role == 'user' else 'Halo'}: {text}")
        return "\n".join(lines)

    def stats(self) -> tuple[int, int]:
        t = self._conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
        f = self._conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
        return t, f

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass
