"""Session registry — the brain's view of "what's running and which one is active".

This is the layer that sits between `halo/discovery.py` (raw process scan)
and `halo/__main__.py` (orchestrator). It:

  1. Holds the current snapshot of discovered sessions.
  2. Resolves collisions when two projects share the same basename
     ("D:\\client-a\\halo" and "D:\\client-b\\halo" both label as "halo").
  3. Fuzzy-matches a spoken target ("the website one") against the
     discovered labels.
  4. Tracks which session is currently *active* (the one the next agent
     dispatch goes to unless the brain overrides per-turn).
  5. Provides voice-friendly summaries ("you have 3: halo, website,
     and AIP. you're on halo").

The registry knows nothing about LLMs or audio. It's pure state +
matching logic, easy to unit-test.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from halo.discovery import DiscoveredSession


# Words we strip before matching a spoken target ("the website ONE" ->
# "website"; "switch to the WEBSITE project" -> "website").
_FILLER_WORDS = {
    "the", "a", "an", "this", "that", "my", "your",
    "one", "project", "thing", "folder", "directory", "session",
    "please", "now",
}

# Words that name a pseudo-target rather than a specific session.
_PSEUDO_TARGETS = {
    "all": "all",
    "everyone": "all",
    "everything": "all",
    "every session": "all",
    "every one": "all",
    "focused": "focused",
    "focus": "focused",
    "current": "active",
    "active": "active",
    "here": "active",
}


@dataclass(frozen=True)
class ResolvedTarget:
    """Result of `resolve()` — either a concrete labeled session, a
    pseudo-target ("all" / "focused" / "active"), or nothing."""

    kind: str  # "session" | "pseudo" | "none"
    label: Optional[str] = None  # set when kind=="session"
    pseudo: Optional[str] = None  # set when kind=="pseudo"


class SessionRegistry:
    """Mutable container for discovered sessions + active selection.

    Thread-safe: snapshots and updates use a single lock. All accessors
    return copies so callers can iterate without holding the lock.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, DiscoveredSession] = {}  # label -> session
        self._active_label: Optional[str] = None
        self._lock = threading.Lock()

    # --- snapshot updates --------------------------------------------------

    def update(self, sessions: list[DiscoveredSession]) -> None:
        """Replace the snapshot. Re-keys by label with collision handling.

        If two discovered sessions resolve to the same basename, both get
        a path-disambiguated label (parent\\basename) so each remains
        uniquely addressable. Preserves the current active label if its
        session is still in the new snapshot; clears it otherwise.
        """
        deduped = self._dedupe_labels(sessions)
        with self._lock:
            old_active = self._active_label
            old_session = self._sessions.get(old_active) if old_active else None
            self._sessions = deduped
            if old_active is None:
                self._active_label = None
                return
            if old_active in deduped:
                self._active_label = old_active
                return
            # The active label can change after a rescan when collisions are
            # re-deduped. Preserve the user's selected terminal by stable
            # identity before declaring it gone.
            if old_session is not None:
                for label, session in deduped.items():
                    same_pid = old_session.pid == session.pid
                    same_project = (
                        bool(old_session.cwd)
                        and old_session.cwd == session.cwd
                        and old_session.agent_key == session.agent_key
                    )
                    if same_pid or same_project:
                        self._active_label = label
                        return
            # Active session really disappeared.
            self._active_label = None

    @staticmethod
    def _dedupe_labels(sessions: list[DiscoveredSession]) -> dict[str, DiscoveredSession]:
        """Return a label -> session dict with collisions resolved by
        adding one level of parent-directory disambiguation."""
        by_label: dict[str, list[DiscoveredSession]] = {}
        for s in sessions:
            by_label.setdefault(s.label, []).append(s)

        out: dict[str, DiscoveredSession] = {}
        for label, group in by_label.items():
            if len(group) == 1:
                out[label] = group[0]
                continue
            # Collision: disambiguate by parent dir.
            for s in group:
                p = Path(s.cwd)
                parent_name = p.parent.name or label
                disambiguated = f"{parent_name}/{label}"
                # If even disambiguation collides, append pid.
                if disambiguated in out:
                    disambiguated = f"{disambiguated}#{s.pid}"
                # Replace the label on the stored session so downstream
                # spoken/displayed names match what we keyed by.
                out[disambiguated] = DiscoveredSession(
                    agent_key=s.agent_key,
                    pid=s.pid,
                    cwd=s.cwd,
                    label=disambiguated,
                    parent_pid=s.parent_pid,
                    parent_name=s.parent_name,
                    cmdline=s.cmdline,
                    seen_at=s.seen_at,
                )
        return out

    # --- accessors ---------------------------------------------------------

    def list(self) -> list[DiscoveredSession]:
        with self._lock:
            return list(self._sessions.values())

    def labels(self) -> list[str]:
        with self._lock:
            return list(self._sessions.keys())

    def by_label(self, label: str) -> Optional[DiscoveredSession]:
        with self._lock:
            return self._sessions.get(label)

    def active(self) -> Optional[DiscoveredSession]:
        with self._lock:
            if self._active_label is None:
                return None
            return self._sessions.get(self._active_label)

    def active_label(self) -> Optional[str]:
        with self._lock:
            return self._active_label

    def set_active(self, label: Optional[str]) -> bool:
        """Set the active label. Returns True if the label exists (or is None).
        Returns False if `label` is not in the current snapshot."""
        with self._lock:
            if label is None:
                self._active_label = None
                return True
            if label not in self._sessions:
                return False
            self._active_label = label
            return True

    # --- spoken matching ---------------------------------------------------

    def resolve(self, spoken: str) -> ResolvedTarget:
        """Map a spoken phrase to a concrete session label, a pseudo-target,
        or nothing.

        Matching is deliberately permissive — voice transcripts are noisy,
        people use casual references ("the AI thing", "halo"), and Whisper
        sometimes mangles words. We try in this order:

          1. Pseudo-targets ("all", "focused", "active", "here")
          2. Exact label match (case-insensitive)
          3. Substring match — spoken phrase is a substring of a label
          4. Substring match — label is a substring of the spoken phrase
          5. Token overlap — any word in the spoken phrase matches a token
             of a label (after stripping filler)

        Returns ResolvedTarget(kind="none") if nothing reasonable matched.
        """
        if not spoken:
            return ResolvedTarget(kind="none")

        normalized = self._normalize(spoken)
        if not normalized:
            return ResolvedTarget(kind="none")

        # Pseudo-target check uses the un-stripped string so multi-word
        # pseudos ("every session") work.
        low = spoken.lower().strip()
        for phrase, pseudo in _PSEUDO_TARGETS.items():
            if phrase in low:
                return ResolvedTarget(kind="pseudo", pseudo=pseudo)

        labels = self.labels()
        if not labels:
            return ResolvedTarget(kind="none")

        # Exact (case-insensitive) label match.
        for label in labels:
            if label.lower() == normalized:
                return ResolvedTarget(kind="session", label=label)

        # Substring: normalized in label
        for label in labels:
            if normalized in label.lower():
                return ResolvedTarget(kind="session", label=label)

        # Substring: label in normalized
        for label in labels:
            if label.lower() in normalized:
                return ResolvedTarget(kind="session", label=label)

        # If the user says a PID/suffix-like number, prefer the label that
        # actually contains that number over a shorter shared prefix label.
        digits = re.findall(r"\d+", normalized)
        for digit in digits:
            for label in labels:
                if digit in label:
                    return ResolvedTarget(kind="session", label=label)

        # Compact match: spoken "social manager" should match a project
        # label like "socialmanager". Voice transcripts often insert or
        # remove separators that are meaningful to humans but not paths.
        compact = re.sub(r"[\s/\\\-_.]+", "", normalized)
        if compact:
            for label in labels:
                label_compact = re.sub(r"[\s/\\\-_.]+", "", label.lower())
                if compact in label_compact or label_compact in compact:
                    return ResolvedTarget(kind="session", label=label)

        # Token overlap. Split both by non-word chars and any of /, \, -, _.
        spoken_tokens = set(self._tokens(normalized))
        best: tuple[int, Optional[str]] = (0, None)
        for label in labels:
            label_tokens = set(self._tokens(label.lower()))
            overlap = len(spoken_tokens & label_tokens)
            if overlap > best[0]:
                best = (overlap, label)
        if best[0] > 0 and best[1] is not None:
            return ResolvedTarget(kind="session", label=best[1])

        return ResolvedTarget(kind="none")

    @staticmethod
    def _normalize(spoken: str) -> str:
        """Strip filler words, lowercase, collapse whitespace."""
        words = re.findall(r"[\w'-]+", spoken.lower())
        kept = [w for w in words if w not in _FILLER_WORDS]
        return " ".join(kept)

    @staticmethod
    def _tokens(text: str) -> list[str]:
        return [t for t in re.split(r"[\s/\\\-_.#]+", text.strip()) if t]

    # --- voice-friendly summaries -----------------------------------------

    def speak_list(self) -> str:
        """One-line voice summary of what's running.

        '3 sessions: halo, website, and AIP. You're on website.'
        'Two Claudes: halo and website. No active selection.'
        'No discovered sessions yet.'
        """
        sessions = self.list()
        if not sessions:
            return "I don't see any agent sessions running on this machine."

        labels = [s.label for s in sessions]
        if len(labels) == 1:
            base = f"One session: {labels[0]}."
        elif len(labels) == 2:
            base = f"Two sessions: {labels[0]} and {labels[1]}."
        else:
            tail = ", ".join(labels[:-1]) + f", and {labels[-1]}"
            base = f"{len(labels)} sessions: {tail}."

        active = self.active_label()
        if active is not None:
            base += f" You're on {active}."
        else:
            base += " No active selection."
        return base

    def speak_active(self) -> str:
        """Short answer for 'where am I?' / 'what project am I on?'."""
        a = self.active()
        if a is None:
            return "No session is active."
        return f"{a.label}, at {a.cwd}."
