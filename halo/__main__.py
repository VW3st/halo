"""Halo orchestrator loop.

Top-level shape:

    while True:
        wait for wake word
        say "I'm here"
        run_conversation()           # loops turns until user signs off
        return to wake-listening

A conversation is a sequence of turns. Within a conversation, the user
doesn't need to re-say "Hey Jarvis" — they just speak. The conversation
ends when:

  * the transcript matches an end-phrase ("over and out", "goodbye",
    "go to sleep", "stop listening", "that's all", ...), OR
  * the router returns status=cancel, OR
  * `CONVERSATION_IDLE_SEC` seconds pass with no speech.

Each turn's decision drives one of:
  * intent=system  -> halo.tools.execute_system_intent
  * intent=code    -> halo.agents.dispatch_claude_code (if agent=claude_code)
  * intent=question -> speak the confirmation
  * status=unclear  -> speak the clarification
  * status=chitchat -> stay silent and listen again
"""

from __future__ import annotations

import json
import re
import signal
import threading
import time

from pathlib import Path

from halo import bus
from halo.agents import (
    AGENTS,
    AgentBusy,
    DEFAULT_CWD,
    active_jobs,
    available_agents,
    check_availability,
    completed_unconsumed_jobs,
    last_result_for,
    mark_consumed,
    reset_session,
    session_name,
    session_status,
    start_job,
    status_summary,
    summarize_for_speech,
)
from halo.config import (
    CONVERSATION_IDLE_ENGAGED_SEC,
    CONVERSATION_IDLE_SEC,
    FOLLOWUP_GATE_ENABLED,
    LIVE_STREAM_MAX_SENTENCES,
    REPLY_SUMMARIZE_THRESHOLD_CHARS,
)
from halo.desktop_control import foreground_pid, send_to_session as send_to_desktop_session
from halo.discovery import DiscoveryThread, is_available as discovery_available
from halo.followup_gate import passes as followup_gate_passes
from halo.record import preload_models as preload_audio_models
from halo.registry import SessionRegistry
from halo.router import (
    SessionContext,
    chat_reply,
    chat_reply_stream,
    preload_router,
    summarize_reply,
    understand_and_route,
)
from halo.tools import _try_say, execute_system_intent
from halo.turn import listen_for_barge_in, run_turn
from halo.userconfig import cfg as user_cfg
from halo.voice import is_available as voice_available
from halo.voice import is_speaking as voice_is_speaking
from halo.voice import preload_voice, say
from halo.voice import stop as voice_stop
from halo.voice import wait_until_silent as voice_wait_until_silent
from halo.wake import WAKE_WORD, _get_model, listen_for_wake
from halo.web import start_server as start_web_server

# Module-level session registry. Populated by the discovery thread in
# multi-session mode (v1.2). Stays empty in single-session mode, in which
# case the orchestrator behaves exactly like v1.1.
REGISTRY = SessionRegistry()
_DISCOVERY: DiscoveryThread | None = None
_desktop_last_sent: dict[str, str] = {}
_desktop_last_error: str = ""
# A chained command may finish its local parts but leave a task only an agent
# can do ("open a browser AND search the web for X"). _handle_decision stashes
# that delegatable remainder here; the conversation loop offers to hand it off.
_system_leftover: str = ""
# Navigation MRU: the agent/session frames we've been in, most-recent first,
# so "go back" / "the other one" / "previous session" / "back to Codex" can
# return to where we were. (agent_key, session_label) tuples; persists across
# sleep/wake within one Halo run, cleared on "new session".
_session_mru: list[tuple[str, str | None]] = []


def _user_name() -> str:
    return (user_cfg.persona.user_name or "").strip()


def _address_user(prefix: str = "") -> str:
    name = _user_name()
    if not name:
        return prefix
    return f"{prefix}, {name}" if prefix else name


def _personalize_reply(reply: str) -> str:
    name = _user_name()
    if not name or not reply:
        return reply
    if not reply.lower().startswith(("good", "all good", "hi", "hey", "sure", "okay", "ok")):
        return reply
    # Tuck the name in BEFORE the final terminal punctuation so "What's up?"
    # becomes "What's up, Valentino?" not the broken "What's up?, Valentino."
    m = re.search(r"\s*([.!?]+)\s*$", reply)
    if m:
        return f"{reply[:m.start()].rstrip()}, {name}{m.group(1)}"
    return f"{reply}, {name}."


class _StreamState:
    """Per-job streaming-speech state (v1.2.1 hybrid mode).

    For streaming agents (Claude), the first LIVE_STREAM_MAX_SENTENCES
    chunks are spoken live as they arrive — gives the user immediate
    feedback that the agent is alive. After the budget, we keep
    accumulating text silently and emit one brain-summarized sentence
    in _drain_completed_jobs.

    The state object is owned by _start_agent_and_ack and registered
    in _stream_states[job_id] right after start_job returns. _drain
    pops it on consumption.
    """

    __slots__ = (
        "max_live_sentences",
        "sentences_spoken",
        "chars_spoken",
        "was_truncated",
        "spoken_text",
    )

    def __init__(self, max_live_sentences: int) -> None:
        self.max_live_sentences = max_live_sentences
        self.sentences_spoken = 0
        self.chars_spoken = 0
        self.was_truncated = False
        # Joined live-spoken text — used to subtract from the full
        # reply when summarizing the remainder.
        self.spoken_text = ""


# Stream state keyed by AgentJob.job_id. Populated by _start_agent_and_ack,
# consumed by _drain_completed_jobs.
_stream_states: dict[int, _StreamState] = {}


class _ConversationBrief:
    """Rolling short-term memory of one awakened conversation.

    Records BOTH sides — what the user said and what Halo said/did — with a
    timestamp per turn, windowed by count and age (~last dozen turns / 15 min).
    Two consumers:

      * `render_history()` — a compact "You:/Halo:" transcript fed into the
        Stage 2 router AND the chat brain every turn, so context-dependent
        fragments ("do it again", "the other one") resolve instead of coming
        back "unclear". This is the fix for "it doesn't capture the context".
      * `render_for_agent()` — prior USER task turns only, used to brief a
        coding agent at dispatch (unchanged behaviour).

    Still local and short-lived — not a database, not cross-session memory.
    """

    __slots__ = ("turns", "max_turns", "max_age_sec")

    def __init__(self, max_turns: int = 30, max_age_sec: float = 3600.0) -> None:
        # each turn: (role, text, monotonic_ts); role in {"user", "halo"}
        # Wider window than before (30 turns / 60 min) and PERSISTED across
        # sleep/wake (see run_conversation), so Halo can answer "what did we
        # talk about" / "what was the first message" instead of only knowing
        # the current burst.
        self.turns: list[tuple[str, str, float]] = []
        self.max_turns = max_turns
        self.max_age_sec = max_age_sec

    def _add(self, role: str, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        if len(text) > 400:
            text = text[:400].rstrip() + "…"
        self.turns.append((role, text, time.monotonic()))
        if len(self.turns) > self.max_turns:
            self.turns = self.turns[-self.max_turns:]
        # Write through to the durable store (survives restarts) and auto-keep
        # salient first-person statements as facts.
        if _MEMORY is not None:
            try:
                _MEMORY.record_turn(role, text)
                if role == "user":
                    from halo.memory import auto_facts
                    for fact, cat in auto_facts(text):
                        _MEMORY.record_fact(fact, cat, weight=2.5)
            except Exception:
                pass

    def add_user(self, text: str) -> None:
        self._add("user", text)

    def add_halo(self, text: str) -> None:
        self._add("halo", text)

    def clear(self) -> None:
        self.turns.clear()

    def _recent(self) -> list[tuple[str, str]]:
        now = time.monotonic()
        return [(r, t) for (r, t, ts) in self.turns if now - ts <= self.max_age_sec]

    def render_history(self) -> str:
        """Compact recent transcript for the router / chat brain. "" if empty."""
        recent = self._recent()
        if not recent:
            return ""
        lines = ["# RECENT CONVERSATION"]
        for role, text in recent:
            lines.append(f"{'You' if role == 'user' else 'Halo'}: {text}")
        return "\n".join(lines)

    def render_for_agent(self, current_prompt: str) -> str:
        current = (current_prompt or "").strip()
        # Prior USER task turns only — skip Halo's replies, chit-chat, and
        # control phrases so the agent gets a clean task brief.
        prior: list[str] = []
        for role, text, _ts in self.turns:
            if role != "user" or not text or text == current:
                continue
            if _is_end_phrase(text) or _is_new_session_phrase(text):
                continue
            if _is_bare_go(text) or _chitchat_reply(text) is not None:
                continue
            prior.append(text)
        if not prior:
            return current
        lines = ["Context from the voice conversation before this handoff:"]
        for i, turn in enumerate(prior, 1):
            lines.append(f"{i}. User said: {turn}")
        lines.extend([
            "",
            "Use that context to preserve constraints and decisions, but follow this current request:",
            current,
        ])
        return "\n".join(lines)


# Module-level pointer to the active conversation's brief, so background-job
# completions (_drain_completed_jobs) can fold the agent's reply into the same
# rolling memory the brain reads. Set by run_conversation, cleared on exit.
_active_brief: _ConversationBrief | None = None

# Persistent cross-session memory (halo/memory.py). None when disabled. The
# brief is the live in-RAM short-term window; this is the durable layer (turns
# across restarts + importance-weighted facts) injected into the brain.
_MEMORY = None  # type: ignore[assignment]


def _init_memory() -> None:
    """Create the persistent memory store if [memory] is enabled."""
    global _MEMORY
    if not user_cfg.memory.enabled:
        return
    try:
        from pathlib import Path

        from halo.memory import Memory
        path = (user_cfg.memory.path or "").strip() or str(
            Path.home() / ".halo" / "halo_memory.db"
        )
        _MEMORY = Memory(path, retention_days=user_cfg.memory.retention_days)
        pruned = _MEMORY.prune()
        turns, facts = _MEMORY.stats()
        print(f"memory: {turns} turns, {facts} facts"
              + (f" (pruned {pruned} old)" if pruned else "") + f" -> {path}")
    except Exception as exc:
        print(f"memory: disabled ({exc})")
        _MEMORY = None


def _brain_history(current: str = "") -> str:
    """The memory block fed to the router + chat brain each turn: persistent
    memory (facts + recent/relevant turns) when available, else the live brief."""
    if _MEMORY is not None:
        try:
            block = _MEMORY.render_for_brain(
                current, max_turns=user_cfg.memory.max_turns,
                max_facts=user_cfg.memory.max_facts,
            )
            if block:
                return block
        except Exception:
            pass
    return _active_brief.render_history() if _active_brief is not None else ""


def _on_discovery_change(sessions) -> None:
    """Discovery thread callback — refresh registry + emit bus event so
    the dashboard sees new/closed sessions promptly."""
    REGISTRY.update(sessions)
    bus.emit(
        "discovery.changed",
        count=len(sessions),
        labels=[s.label for s in sessions],
    )


def _build_session_context() -> SessionContext | None:
    """Snapshot of registry state to inject into the Stage 2 LLM call.

    Returns None when there's nothing meaningful to add (empty registry +
    no active label) — keeps the prompt cheap in single-session mode.
    """
    discovered = []
    for s in REGISTRY.list():
        discovered.append({"label": s.label, "cwd": s.cwd, "agent": s.agent_key})
    active_label = REGISTRY.active_label()
    if not discovered and active_label is None:
        return None
    return SessionContext(active_label=active_label, discovered=discovered)


_SESSION_LIST_QUERY_RE = re.compile(
    r"\b("
    r"(?:what|which|show|list|tell me).{0,45}(?:sessions?|session names?|"
    r"cli|agents?|terminals?)|"
    r"what.{0,25}(?:clis?|lies).{0,25}(?:open|running|available)|"
    r"what session are you|"
    r"(?:what|tell me).{0,35}(?:last task|last thing you did)|"
    r"(?:sessions?|agents?|cli).{0,35}(?:online|running|open|active|available|"
    r"connected)|"
    r"do we have.{0,35}(?:sessions?|agents?|cli)|"
    r"who(?:'s| is) active|"
    r"where am i|what project am i on"
    r")\b",
    re.IGNORECASE,
)
_SESSION_SWITCH_RE = re.compile(
    r"\b(?:let me talk to|talk to|speak to|need to speak to|"
    r"i need to speak to|switch to|switch over to|connect me to|"
    r"put me on|transfer me to|go to|go on|go under)\s+(?:the\s+)?(.+?)"
    r"(?:\s+(?:session|project|cli|agent))?\s*[.!?,]*$",
    re.IGNORECASE,
)
_TYPE_LEAD_IN = (
    r"(?:hey\s+|hi\s+|halo\s+|okay\s+|ok\s+|look\s+|"
    r"i\s+want\s+you\s+to\s+|i\s+need\s+you\s+to\s+|"
    r"can\s+you\s+|could\s+you\s+|would\s+you\s+|please\s+)*"
)
_TYPE_VERB = r"(?:type|send|write|paste|(?:i(?:'| a)?m\s+)?typing)"
_TYPE_TARGET = (
    r"active\s+session|selected\s+session|focused\s+session|current\s+session|"
    r"active|selected|focused|current|open|"
    r"social\s+manager|manager|[\w\s/\\#.-]+?"
)
_TYPE_REQUEST_TARGET = (
    r"active\s+session|selected\s+session|focused\s+session|current\s+session|"
    r"active|selected|focused|current|open|"
    r"social\s+manager|manager|"
    r"[\w/\\#-]+(?:\s+[\w/\\#-]+){0,2}"
)
_TYPE_IN_SESSION_RE = re.compile(
    r"^\s*" + _TYPE_LEAD_IN + _TYPE_VERB + r"\s+"
    r"(?:this\s+)?(?:in|into|to|on)\s+"
    r"(?:the\s+)?(?P<target>" + _TYPE_TARGET + r")"
    r"(?:\s+(?:session|project|cli|agent))?"
    r"(?:\s*[:.,-]\s*|\s+)(?P<text>.+)$",
    re.IGNORECASE | re.DOTALL,
)
_TYPE_IN_SESSION_REQUEST_RE = re.compile(
    r"^\s*" + _TYPE_LEAD_IN + _TYPE_VERB + r"\s+"
    r"(?:in|into|to|on)\s+"
    r"(?:the\s+)?(?P<target>" + _TYPE_REQUEST_TARGET + r")"
    r"(?:\s+(?:session|project|cli|agent))?\s*[.!?]*$",
    re.IGNORECASE,
)

# System-wide dictation trigger -> type my speech into whatever field has
# focus. Start phrases are user-configurable ([dictation].start_phrases,
# default "dictate"). Matching is accent-robust / fuzzy (see
# dictation.matches_start) so a mis-transcribed "dictate" ("dictade",
# "dictat") still enters dictation instead of falling through to the LLM.
def _wants_dictation(text: str) -> bool:
    from halo.dictation import matches_start
    return matches_start(text)


def _agent_spoken(agent_key: str) -> str:
    cfg = AGENTS.get(agent_key)
    return cfg.spoken_name if cfg else agent_key


def _session_spoken(session) -> str:
    return f"{_agent_spoken(session.agent_key)} in {session.label}"


def _is_session_query(text: str) -> bool:
    return bool(_SESSION_LIST_QUERY_RE.search(text or ""))


def _session_summary() -> str:
    sessions = REGISTRY.list()
    connected = available_agents()
    parts: list[str] = []
    if connected:
        parts.append(f"Connected CLIs: {_join_names(connected)}.")
    else:
        parts.append("No connected CLIs passed their health check.")
    if not sessions:
        parts.append("I don't see any discovered agent sessions running.")
        return " ".join(parts)
    names = [_session_spoken(s) for s in sessions]
    if len(names) == 1:
        parts.append(f"One discovered session: {names[0]}.")
    else:
        parts.append(f"{len(names)} discovered sessions: {_join_names(names)}.")
    active = REGISTRY.active()
    if active is not None:
        parts.append(f"You're on {_session_spoken(active)}.")
        last = _desktop_last_sent.get(active.label)
        if last:
            parts.append(f"Last thing I sent there: {last}.")
    else:
        parts.append("No session is selected yet.")
    return " ".join(parts)


def _local_session_switch(text: str) -> tuple[str | None, str]:
    m = _SESSION_SWITCH_RE.search(text or "")
    if not m:
        return None, ""
    target = _clean_session_target(m.group(1))
    if not target:
        return None, ""
    if target in {"active", "selected", "active selected", "selected active"}:
        active = REGISTRY.active()
        if active is None:
            return None, "No session is selected yet. Say the session name, like social manager."
        return active.agent_key, f"You're talking to {_session_spoken(active)}."
    # Agent-only switches are handled by _agent_switch_target; this path
    # is for discovered sessions/projects like "social manager".
    if _resolve_agent_word(target) is not None:
        return None, ""
    resolved = REGISTRY.resolve(target)
    if resolved.kind != "session" or resolved.label is None:
        return None, f"I don't see a session called {target}."
    session = REGISTRY.by_label(resolved.label)
    if session is None or not REGISTRY.set_active(resolved.label):
        return None, f"I don't see a session called {target}."
    bus.emit("session.switched", label=resolved.label)
    return session.agent_key, f"You're talking to {_session_spoken(session)}."


def _clean_session_target(target: str) -> str:
    target = (target or "").strip()
    target = re.sub(r"[,.!?]+", " ", target)
    target = re.sub(r"\s+", " ", target).strip()
    target = re.sub(
        r"^(?:selected\s+)?(?:session|project|cli|agent)\s+(?:on|in|for)\s+",
        "",
        target,
        flags=re.IGNORECASE,
    )
    target = re.sub(r"^(?:selected|open|active)\s+", "", target, flags=re.IGNORECASE)
    target = re.sub(
        r"\b(?:cli|agent|session|project|please|thanks|thank you)\b",
        "",
        target,
        flags=re.IGNORECASE,
    )
    target = re.sub(r"\s+", " ", target).strip()
    if target in {"", "selected", "active", "active selected", "selected active"}:
        return "active"
    # In this project "the manager" is how the user naturally refers to the
    # socialmanager session. Prefer an actual manager-labelled session.
    if re.fullmatch(r"(?:social\s+)?manager", target, flags=re.IGNORECASE):
        for session in REGISTRY.list():
            if "manager" in session.label.lower():
                return session.label
    return target


def _direct_desktop_session(agent_key: str, session_label: str | None):
    if session_label:
        session = REGISTRY.by_label(session_label)
        if session is not None and session.agent_key == agent_key:
            return session
    active = REGISTRY.active()
    if active is not None and active.agent_key == agent_key:
        return active
    return None


def _focused_desktop_session():
    pid = foreground_pid()
    if pid is None:
        return None
    for session in REGISTRY.list():
        if session.pid == pid or session.parent_pid == pid:
            return session
    return None


def _session_for_control_target(target: str | None):
    cleaned = _clean_session_target(target or "active")
    if cleaned in {"active", "selected", "current"}:
        return REGISTRY.active() or _focused_desktop_session()
    if cleaned in {"focused", "open"}:
        return _focused_desktop_session() or REGISTRY.active()
    resolved = REGISTRY.resolve(cleaned)
    if resolved.kind == "session" and resolved.label is not None:
        session = REGISTRY.by_label(resolved.label)
        if session is not None:
            REGISTRY.set_active(resolved.label)
            return session
    return None


def _send_to_direct_desktop_session(agent_key: str, text: str, session_label: str | None) -> str | None:
    global _desktop_last_error
    session = _direct_desktop_session(agent_key, session_label)
    if session is None:
        return None
    result = send_to_desktop_session(session, text)
    if result.ok:
        _desktop_last_sent[session.label] = text
        _desktop_last_error = ""
        return f"Sent to {_session_spoken(session)}."
    _desktop_last_error = result.message
    return result.message


def _local_type_into_session(text: str) -> str | None:
    global _desktop_last_error
    # Check the no-payload form first. Otherwise "type in the active
    # session" can be misread as target="active", payload="session".
    m = _TYPE_IN_SESSION_REQUEST_RE.match(text or "")
    request_only = m is not None
    if not m:
        m = _TYPE_IN_SESSION_RE.match(text or "")
    if not m:
        return None
    target = m.group("target") or "active"
    payload = "" if request_only else (m.group("text") or "").strip()
    session = _session_for_control_target(target)
    if session is None:
        return "No desktop session is selected or focused. Say the session name, like social manager."
    if not payload:
        REGISTRY.set_active(session.label)
        return f"What should I type into {_session_spoken(session)}?"
    result = send_to_desktop_session(session, payload)
    if result.ok:
        REGISTRY.set_active(session.label)
        _desktop_last_sent[session.label] = payload
        return f"Sent to {_session_spoken(session)}."
    _desktop_last_error = result.message
    return result.message


def _is_desktop_error_query(text: str) -> bool:
    return bool(re.search(r"\b(why is that|why did (?:that|it) fail|what happened|why)\b", text or "", re.I))


def _cwd_for_dispatch(target_session: str) -> Path:
    """Resolve target_session (label / 'active' / 'focused' / '' ) into a Path.

    Falls back to DEFAULT_CWD (Halo's launch dir) when no registry match —
    matches v1.1 single-session behaviour. 'all' is handled separately by
    the orchestrator (multi-dispatch), not here.
    """
    def _valid(cwd: str | None) -> Path | None:
        # A discovered session whose cwd() was AccessDenied gets cwd="" (or a
        # dead path). Don't dispatch there — it resolves to Halo's own dir and
        # silently runs in the wrong place. Fall back to DEFAULT_CWD instead.
        if not cwd:
            return None
        try:
            p = Path(cwd)
            return p if p.is_dir() else None
        except Exception:
            return None

    if not target_session or target_session in ("active", "focused"):
        active = REGISTRY.active()
        if active is not None:
            return _valid(active.cwd) or DEFAULT_CWD
        return DEFAULT_CWD
    sess = REGISTRY.by_label(target_session)
    if sess is not None:
        return _valid(sess.cwd) or DEFAULT_CWD
    # Fuzzy fallback — brain may have emitted a slightly off label.
    resolved = REGISTRY.resolve(target_session)
    if resolved.kind == "session" and resolved.label is not None:
        sess = REGISTRY.by_label(resolved.label)
        if sess is not None:
            return _valid(sess.cwd) or DEFAULT_CWD
    return DEFAULT_CWD


# Chitchat-to-Halo phrases. These intercept BEFORE Stage 2 LLM fires so
# (a) we save the 4-5 s round-trip and (b) we never accidentally
# dispatch "hello, are you there?" to Claude as a coding task. Pattern
# match → Halo speaks the canned reply and the turn ends.
#
# Order matters: more specific patterns first. Each entry is
# (pattern, [reply variants...]). A random pick keeps it from sounding
# robotic when you hammer Halo with test utterances.
_CHITCHAT_PATTERNS: tuple[tuple[re.Pattern[str], tuple[str, ...]], ...] = (
    (re.compile(r"\b(what(?:'s| is) your name|who are you|what should i call you)\b", re.I),
     ("I'm Halo.", "My name is Halo.")),
    # Existence / presence check
    (re.compile(r"\b(are you (there|here|on|alive|listening|hearing me|with me|awake|around))\b", re.I),
     ("Yes, I'm here.", "Still here.", "Right here.")),
    (re.compile(r"\b(can you hear me|do you hear me|am i coming through|hearing me)\b", re.I),
     ("Loud and clear.", "I hear you.", "Yes, clearly.")),
    # Wake-only greetings (no follow-up content)
    (re.compile(r"^\s*(hi|hey|hello|yo|sup|howdy)\s*(halo|there|you)?[.,!?\s]*$", re.I),
     ("Hi. What can I do for you?", "Hey. Ready when you are.", "Hi there.")),
    (re.compile(r"\b(?:just\s+)?(?:wanted|want)\s+to\s+say\s+hi\b", re.I),
     ("Hi. What can I do for you?", "Hey. Ready when you are.", "Hi there.")),
    # Test pings — bare "test" / "testing" / repeated / mic check
    (re.compile(r"^[\s.,!?]*(test|testing|mic check|audio check)"
                r"([\s.,]+(test|testing|1 2 3|one two three|\d+))*[\s.,!?]*$", re.I),
     ("Test received.", "I'm listening.", "Got it.")),
    # Test pings — numeric / spoken counters ("1 2 3", "one two three")
    (re.compile(r"^[\s.,!?]*(\d+|one|two|three|four|five)"
                r"([\s.,]+(\d+|one|two|three|four|five|test|testing))+[\s.,!?]*$", re.I),
     ("Test received.", "Mic check confirmed.", "Got it.")),
    # Test pings — full sentence
    (re.compile(r"^[\s.,!?]*(this is a |it'?s a |just a |running a |doing a )"
                r"(test|mic check|audio check|system check)[\s.,!?]*$", re.I),
     ("Test received.", "I'm listening.", "Got it.")),
    # Activity check (no specific subject — clearly aimed at Halo)
    (re.compile(r"^\s*(what are you (doing|working on|up to|busy with))[.,!?\s]*$", re.I),
     ("Just listening. What would you like me to do?",
      "Waiting on you. What's the task?",
      "Standing by. What do you need?")),
    # "What's up / what's happening" with no actionable content
    (re.compile(r"^\s*(what'?s (up|happening|going on|new|the deal))[.,!?\s]*$", re.I),
     ("All good. Ready when you are.", "Just here. What's the task?")),
    # "How are you" style
    (re.compile(r"\b(how are you(?: today)?|how'?s it going|how are things|are you good|you good)\b", re.I),
     ("Good. What's up?", "All good. What do you need?")),
    # Compliments / thanks (no follow-up action)
    (re.compile(r"^\s*(thanks|thank you|nice|cool|good job|well done|awesome)[.,!?\s]*$", re.I),
     ("Anytime.", "Glad to help.", "Happy to.")),
)


def _chitchat_reply(text: str) -> str | None:
    """If `text` is a clear conversational ping at Halo, return one of
    the canned replies. Otherwise None — caller proceeds with normal
    routing.

    Pattern match wins over Stage 2 LLM because (a) it's free, (b) it
    never accidentally dispatches a 'hello' to Claude.
    """
    import random
    text = (text or "").strip()
    if not text:
        return None
    for pattern, replies in _CHITCHAT_PATTERNS:
        if pattern.search(text):
            return random.choice(replies)
    return None


def _converse(text: str, brief: "_ConversationBrief | None" = None) -> str:
    """Stream a conversational reply and SPEAK it sentence-by-sentence as it
    generates, so the first words come out ~0.4 s in instead of waiting ~1.3 s
    for the whole reply. Adds the reply to memory and returns "" because it has
    ALREADY been spoken (the caller must not re-speak it). Fails open to a
    short canned line so the user always hears something."""
    text = (text or "").strip()
    if not text:
        return ""
    history = _brain_history(text)
    spoken = {"full": ""}

    def _on_sentence(sentence: str) -> None:
        sentence = (sentence or "").strip()
        if not sentence:
            return
        if not spoken["full"]:
            print(f"  chat (streaming) -> {sentence!r}")
        say(sentence, blocking=False)  # queued; first sentence starts at once
        spoken["full"] = (spoken["full"] + " " + sentence).strip()

    try:
        full = (chat_reply_stream(text, history=history, on_sentence=_on_sentence) or "").strip()
    except Exception:
        full = ""
    full = full or spoken["full"]
    if not full:
        # Brain unreachable and nothing streamed — speak a canned line.
        full = _personalize_reply("I'm here. What's on your mind?")
        say(full, blocking=False)
    if brief is not None:
        brief.add_halo(full)
    return ""  # already spoken via _on_sentence


# Did the user actually NAME a coding agent? Drives the "Confirm before
# Claude" policy — an un-named coding task pauses for a yes/no rather than
# silently spawning a session. Includes common accent/STT mishearings of
# "Claude" ("cloud", "clord", "clawed") so naming it out loud still counts.
_AGENT_NAME_RE = re.compile(
    r"\b(claude(?:\s*code)?|cloud\s*code|clawed|clord|anthropic|"
    r"codex|open\s*ai)\b",
    re.IGNORECASE,
)


def _user_named_agent(text: str) -> bool:
    return bool(_AGENT_NAME_RE.search(text or ""))


# Wake-word confirmation vocabulary — halo-like tokens, including the accent /
# STT variants the model produces ("hello", "hallo", "hey low", "aloha").
_WAKE_CONFIRM_RE = re.compile(
    r"\b(?:h[ae]l+o+w?|hel+o+|hal+o+w?|hull?o|hallow|hey\s*l[oa]w?|hi\s*low|aloha?)\b",
    re.IGNORECASE,
)


def _lev(a: str, b: str) -> int:
    """Levenshtein distance (small inputs — wake-token fuzzy match)."""
    if a == b:
        return 0
    la, lb = len(a), len(b)
    if not la:
        return lb
    if not lb:
        return la
    prev = list(range(lb + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[lb]


def _text_has_wake_word(text: str) -> bool:
    """True if a transcript plausibly contains the configured wake word.

    Word-agnostic: keys off the configured `[wake] word` so switching to a
    different/more-distinct wake word (e.g. "hey_jarvis", "hey halo") keeps
    verification working. Matches the DISTINCTIVE token fuzzily ("jarvis" for
    "hey jarvis", "halo" for "halo"). The halo-family regex is a bonus."""
    t = (text or "").lower()
    if not t.strip():
        return False
    target = (WAKE_WORD or "halo").lower().replace("_", " ").replace("-", " ")
    if "halo" in target and _WAKE_CONFIRM_RE.search(t):
        return True  # halo-family variants only when the word IS halo
    tokens = [w for w in target.split() if w]
    distinctive = tokens[-1] if tokens else "halo"  # skip the "hey" prefix
    for tok in re.findall(r"[a-z']+", t):
        if abs(len(tok) - len(distinctive)) <= 2 and _lev(tok, distinctive) <= 2:
            return True
    return False


# A wake fire at or above this score is trusted even when STT can't cleanly
# read back the wake word, PROVIDED the audio is short (a mis-transcribed
# "halo", not a sentence of other speech). Reduces false REJECTIONS of real
# wakes the recognizer garbled (e.g. "halo" -> "Hey!").
_WAKE_STRONG_ACCEPT = 0.60


def _wake_confirmed_by_stt(score: float = 0.0) -> bool:
    """Second-stage wake gate (issue #1): after the DNN fires, transcribe the
    ~1 s of audio captured before the fire and confirm the wake word was
    actually spoken. `score` is the DNN fire confidence, used as a prior.

    Conservative — biased toward NOT dropping real wakes:
      * verify disabled / no audio / STT error -> accept.
      * STT read back the wake word             -> accept.
      * STT heard nothing but real speech energy-> accept (quiet real "halo").
      * STT heard nothing and near-silence      -> reject (noise spike).
      * STT heard NON-wake text:
          - strong + short fire (mis-heard "halo") -> accept.
          - otherwise (a sentence of other speech) -> REJECT (false-fire win).
    """
    if not user_cfg.wake.verify:
        return True
    try:
        from halo.config import SAMPLE_RATE
        from halo.record import SPEECH_RMS_FLOOR
        from halo.stt import BatchTranscriber
        from halo.wake import get_pre_wake_audio

        audio = get_pre_wake_audio()
        if audio is None or audio.size < int(SAMPLE_RATE * 0.3):
            return True  # nothing to verify -> don't block
        tb = BatchTranscriber()
        tb.seed(audio)
        text = (tb.transcribe(raw=True) or "").strip()
        peak = tb.peak_rms()
        # No tb.stop() — BatchTranscriber holds no stream/thread, and stop()
        # would re-run transcription (wasted ~200 ms on every wake).
    except Exception as exc:
        print(f"  [wake verify] error ({exc}) — accepting")
        return True

    if not text:
        if peak < SPEECH_RMS_FLOOR:
            print(f"  [wake verify] rejected — silent fire (peak {peak:.3f})")
            bus.emit("wake.rejected", reason="silent")
            return False
        return True  # real speech energy, recognizer just fumbled it
    if _text_has_wake_word(text):
        print(f"  [wake verify] confirmed: {text!r}")
        return True
    # Non-wake transcript. Trust a strong, SHORT fire (a garbled "halo"); reject
    # a clear run of other speech.
    word_count = len(re.findall(r"[a-z']+", text.lower()))
    if score >= _WAKE_STRONG_ACCEPT and word_count <= 2:
        print(f"  [wake verify] accepted on strong score {score:.2f} "
              f"(garbled wake heard as {text!r})")
        return True
    print(f"  [wake verify] rejected false fire — heard {text!r} (no wake word)")
    bus.emit("wake.rejected", text=text, score=score)
    return False


# Real-task signal — used by the `intent=system` fall-through to decide
# whether to dispatch an unmatched utterance to Claude vs. politely
# refuse. Reuses the followup_gate vocabulary (we already maintain a
# coding-imperatives + technical-nouns dictionary there).
_TASK_VERB_RE = re.compile(
    r"\b(write|make|build|create|add|remove|delete|fix|update|change|"
    r"rewrite|refactor|rename|test|run|deploy|ship|release|open|close|"
    r"save|commit|push|pull|merge|rebase|clone|checkout|install|"
    r"uninstall|upgrade|edit|modify|set|configure|generate|scaffold|"
    r"format|lint|debug|implement|extract|inline|split|convert|port|"
    r"show me|list|find|search|grep|read|explain|review|audit|"
    r"summarize|undo|revert|continue|finish|complete|switch|toggle|"
    r"enable|disable|hide|reveal|append|prepend|insert|replace|style|"
    r"resize|launch|start|stop|kill|restart|build me|make me|write me)\b",
    re.IGNORECASE,
)
_TASK_NOUN_RE = re.compile(
    r"\b(file|files|function|method|class|variable|module|package|"
    r"library|test|tests|bug|error|exception|component|page|route|"
    r"endpoint|api|schema|model|view|hook|prop|state|css|html|"
    r"database|table|column|query|migration|server|client|branch|"
    r"commit|repo|repository|script|code|browser|chrome|calculator|"
    r"notepad|terminal|explorer|directory|folder|path|filename)\b",
    re.IGNORECASE,
)


def _looks_like_real_task(text: str) -> bool:
    """Heuristic: does this utterance look like an actual task we should
    fall through to Claude for, or is it conversational noise the
    chitchat layer should have caught?

    Returns True iff the cleaned text contains a coding-imperative verb
    OR a technical noun. False otherwise — caller refuses politely
    instead of burning a Claude session.
    """
    if not text:
        return False
    return bool(_TASK_VERB_RE.search(text) or _TASK_NOUN_RE.search(text))


# Whisper frequently prepends a phantom "Hello?" / "Hey" to a captured
# utterance (it mishears the tail of the wake word). That junk prefix makes
# the router mark real commands as chitchat/unclear. Strip leading greeting
# tokens — but keep the last one if nothing substantive follows, so a genuine
# bare "hey" still reaches the chitchat layer.
_LEADING_GREETING_RE = re.compile(
    # ONLY wake-word mishearings — not discourse words. "so/now/okay/oh" are
    # turn-shape signals downstream filters use; stripping them eroded
    # detection (audit finding).
    r"^\s*(?:hello|hallo|hullo|hey|aye|hi|yo|halo)[\s,.!?]+",
    re.IGNORECASE,
)
_LEADING_WAKE_JUNK_RE = re.compile(
    r"^\s*(?:waika|waker|wake up|heyka|hika|ika)[\s,.!?]+",
    re.IGNORECASE,
)


def _strip_leading_greeting(text: str) -> str:
    t = (text or "").strip()
    for _ in range(3):
        m = _LEADING_GREETING_RE.match(t) or _LEADING_WAKE_JUNK_RE.match(t)
        if not m:
            break
        rest = t[m.end():].strip()
        if len(rest) < 2:
            return t  # only a greeting remains — keep it for the chitchat layer
        t = rest
    return t


# Phrases that close the conversation and send us back to wake-listening.
# Split into STRONG (unambiguous — fire even mid-utterance) and WEAK
# (reaction-prone: "that's it?", "that's all", "bye" double as casual replies).
# Weak enders only end when they're the WHOLE utterance AND it isn't a question
# — fixes "That's it?" (= "is that all you've got?") slamming the session shut.
_END_STRONG_RE = re.compile(
    r"\b("
    r"over and out|over n out|"
    r"go to sleep|go back to sleep|back to sleep|sleep now|"
    r"you can sleep|i want you to sleep|go to bed|halo sleep|"
    r"stop listening|stop conversation|end conversation|end session|"
    r"end of session|session end|"
    r"good ?bye|bye bye|see you(?: later)?|"
    r"stand by|standby|"
    r"shut up|leave me alone"
    r")\b",
    re.IGNORECASE,
)
# Reaction-prone enders. Anchored ^...$ so they must be the entire utterance
# (after optional leading filler / trailing "for now"), never a substring.
_END_WEAK_RE = re.compile(
    r"^(?:ok(?:ay)?|alright|all ?right|well|yeah|yep|so|and|no|cool|"
    r"thanks?|thank you)?[,\s]*"
    r"(?:that'?s (?:it|all|enough)|that'?ll be all|that will be all|"
    r"i'?m done|we'?re done|all done|done for (?:now|today)|bye)"
    r"(?:\s+for (?:now|today|the day))?"
    r"[.\s!]*$",
    re.IGNORECASE,
)

# Bare control words that mean "never mind, carry on" when nothing is pending.
# Acknowledged locally so a lone "stop"/"cancel" never reaches Stage 2 (where
# the small router occasionally invents a session switch). NOT an end phrase —
# "go to sleep" / "goodbye" still exit.
_BARE_STOP_RE = re.compile(
    r"^\s*(?:stop|stop it|stop that|stop talking|stop please|please stop|"
    r"cancel|never ?mind|forget it|nothing|no thanks?|"
    r"shut up|be quiet|quiet|hush|enough|that'?s enough|"
    r"that'?s ok(?:ay)?|it'?s ok(?:ay)?)\s*[.!?]*\s*$",
    re.IGNORECASE,
)

# Phrases that drop the agent session-continuation pointer so the
# next dispatch starts a fresh Claude / Codex conversation.
_NEW_SESSION_RE = re.compile(
    r"\b("
    r"new session|new task|start over|fresh session|reset session|"
    r"forget that|forget everything|new conversation|"
    r"clear context|clean slate"
    r")\b",
    re.IGNORECASE,
)

# Status / progress queries — answered locally from the job registry,
# never sent to Claude/Codex.
_STATUS_QUERY_RE = re.compile(
    r"\b("
    r"are you (?:done|finished|ready)|are we done|done yet|finished yet|"
    r"what'?s happening|what'?s going on|how'?s it going|how is it going|"
    r"status|update|progress|"
    r"still working|are you still (?:working|there)|"
    r"any news"
    r")\b",
    re.IGNORECASE,
)

# Replay queries — re-speak the last result from a named agent.
_REPLAY_RE = re.compile(
    r"\b(?:what did|repeat|say again|read me|tell me)\b.*?\b(claude|codex)\b",
    re.IGNORECASE,
)

# Mode-switch phrases: explicitly hand the next utterance to a named
# agent (direct-dialogue mode) or pull back to local Halo routing.
# Two forms accepted:
#   1. "<verb> ... claude/codex"     -> switch
#   2. "claude/codex" mentioned alongside a switch keyword anywhere
_TALK_TO_AGENT_RE = re.compile(
    r"\b(?:"
    r"switch (?:to|over to)|"
    r"transfer (?:me )?(?:to|over to)|"
    r"talk to|let me talk to|"
    r"hand (?:it |this )?to|give (?:it |this )?to|put me on|"
    r"use|try|let'?s (?:try|use|ask)|"
    r"ask|go to|now (?:use|ask|try)|"
    r"i'?ll (?:switch to|use|try)|"
    r"or i switch to"
    r")\s+"
    r"(claude(?: code)?|codex(?: cli)?)\b",
    re.IGNORECASE,
)
# Fallback: agent name as a leading vocative ("Codex, refactor this").
# Comma-required so we don't catch "codex is broken" as a switch.
_AGENT_VOCATIVE_RE = re.compile(
    r"^\s*(claude(?: code)?|codex(?: cli)?)\s*,",
    re.IGNORECASE,
)
_BACK_TO_HALO_RE = re.compile(
    r"\b(?:back to halo|halo (?:back|take over)|stop (?:talking to|using) "
    r"(?:claude|codex)|exit (?:claude|codex)|leave (?:claude|codex)|"
    r"talk to me|hand it back|transfer (?:me )?back to halo)\b",
    re.IGNORECASE,
)


def _resolve_agent_word(word: str) -> str | None:
    w = (word or "").lower()
    if w.startswith("claude"):
        return "claude_code"
    if w.startswith("codex"):
        return "codex_cli"
    return None


# Vocative dispatch: "Claude, build X" / "Codex, refactor Y" - skip the
# router brain entirely, go straight to the named agent with the rest of
# the utterance as the prompt. The comma (or colon) is required so we
# don't grab statements ABOUT the agent ("codex is broken").
_VOCATIVE_DISPATCH_RE = re.compile(
    r"^\s*(?:hey\s+|ok\s+|okay\s+|yo\s+|please\s+)?"
    r"(claude(?:\s+code)?|codex(?:\s+cli)?)\s*[,:]\s*(.+)",
    re.IGNORECASE | re.DOTALL,
)


def _vocative_dispatch(text: str) -> tuple[str | None, str]:
    """If text is 'Claude, X' / 'Codex, X', return (agent_key, X).
    Otherwise (None, '')."""
    m = _VOCATIVE_DISPATCH_RE.match(text or "")
    if not m:
        return None, ""
    agent = _resolve_agent_word(m.group(1))
    if agent is None:
        return None, ""
    instruction = m.group(2).strip()
    if not instruction:
        return None, ""
    return agent, instruction


# Verbal dispatch: "ask Codex to build X" / "tell Claude to refactor Y" /
# "have Codex deploy" / "get Claude to write tests" / "use Codex for the
# bug" / "let Claude handle this". Same effect as vocative dispatch, but
# with an explicit verb instead of a comma. Catches the natural English
# pattern that the Stage 2 router LLM frequently mangles (eg. swapping
# Codex -> Claude or adding hallucinated details).
_VERBAL_DISPATCH_RE = re.compile(
    r"^\s*"
    r"(?:hey\s+|ok\s+|okay\s+|please\s+)?"
    r"(?:can\s+you\s+|could\s+you\s+|would\s+you\s+)?"
    r"(?:ask|tell|have|get|use|let|try)\s+"
    r"(claude(?:\s+code)?|codex(?:\s+cli)?)\s+"
    r"(?:to\s+|please\s+|for\s+|if\s+(?:you|she|he)\s+can\s+)?"
    r"(.+)",
    re.IGNORECASE | re.DOTALL,
)


def _verbal_dispatch(text: str) -> tuple[str | None, str]:
    """If text is 'ask Codex to X' / 'tell Claude to Y' / etc, return
    (agent_key, X). Otherwise (None, '')."""
    m = _VERBAL_DISPATCH_RE.match(text or "")
    if not m:
        return None, ""
    agent = _resolve_agent_word(m.group(1))
    if agent is None:
        return None, ""
    instruction = m.group(2).strip()
    if not instruction:
        return None, ""
    return agent, instruction


def _fuzzy_agent_match(text: str) -> str | None:
    """Permissive agent-name detection used ONLY as a fallback when the
    Stage 2 LLM is unreachable. Matches the canonical voice_triggers
    AND the per-agent fuzzy_triggers (Whisper mis-transcriptions like
    'Cloud' for 'Claude'). Returns the first agent_key whose triggers
    appear as a whole word in `text`, else None.

    The strict vocative / verbal regexes are still preferred for the
    happy path; this only fires when nothing else matched AND the LLM
    couldn't disambiguate either.
    """
    if not text:
        return None
    low = text.lower()
    for key, cfg in AGENTS.items():
        all_triggers = tuple(cfg.voice_triggers) + tuple(cfg.fuzzy_triggers)
        for trigger in all_triggers:
            # Whole-word match so 'cloud' doesn't catch 'cloudy' etc.
            pattern = r"\b" + re.escape(trigger.lower()) + r"\b"
            if re.search(pattern, low):
                return key
    return None


# Trailing fluff that doesn't count as a real instruction after a switch verb.
_SWITCH_TRAILING_FLUFF_RE = re.compile(
    r"^(?:please|now|right now|then|next|thanks|thank you|ok|okay)?\s*[.,!?]?$",
    re.IGNORECASE,
)


def _agent_switch_target(text: str) -> str | None:
    """Detect a PURE switch to a named agent.

    A pure switch is something like "switch to codex" or "talk to claude"
    where there's no remaining instruction. Utterances like
    "ask claude to build a landing page" are NOT switches — they're
    dispatches, and the routing brain (or the active direct agent in
    direct-dialogue mode) handles them as the full instruction.
    """
    text = (text or "").strip()

    m = _TALK_TO_AGENT_RE.search(text)
    if m:
        after = text[m.end():].strip()
        if _SWITCH_TRAILING_FLUFF_RE.match(after):
            return _resolve_agent_word(m.group(1))
        return None

    m = _AGENT_VOCATIVE_RE.search(text)
    if m:
        after = text[m.end():].strip()
        if _SWITCH_TRAILING_FLUFF_RE.match(after):
            return _resolve_agent_word(m.group(1))
        return None

    return None


# Curated, STT-garble-tolerant aliases used ONLY to decide "did the user just
# name the OTHER agent" while in direct mode. Deliberately excludes real English
# words that appear in cfg.fuzzy_triggers ("cloud", "codec", "codecs", "clod") so
# a normal coding instruction ("use the codec library") never yanks the
# conversation to the wrong agent. Non-word mis-transcriptions only.
_AGENT_REDIRECT_ALIASES: dict[str, tuple[str, ...]] = {
    "claude_code": ("claude", "claude code", "claud", "clawde", "clawd", "clyde", "clode"),
    "codex_cli": ("codex", "codex cli", "kodex", "krodex", "kadex", "cadex",
                  "crodex", "kodaks"),
}
# Verbs that mark a deliberate hand-off to a named agent. Required before we
# redirect — a passing mention ("the codex output looks good") must NOT switch.
_AGENT_REDIRECT_VERB_RE = re.compile(
    r"\b(?:ask|tell|have|get|use|let|try|switch|spawn|open|launch|start|"
    r"bring up|fire up|go to|talk to|hand (?:it|this) to|give (?:it|this) to|"
    r"put me on|transfer)\b",
    re.IGNORECASE,
)
# Connectors / fluff peeled off the FRONT of a redirected instruction so
# "spawn codex AS WELL AND generate images" dispatches "generate images".
_REDIRECT_LEAD_RE = re.compile(
    r"^(?:\s*(?:to|and|also|as well|please|for|now|then|that|the|it|them|"
    r"if you can|can you|could you|would you|i want you to|i need you to)\b[\s,]*)+",
    re.IGNORECASE,
)


def _mentioned_agents(text: str) -> list[str]:
    """Agent keys named in `text` (garble-tolerant), ordered by first
    appearance. Uses the curated redirect aliases, not the loose fuzzy set."""
    low = (text or "").lower()
    hits: list[tuple[int, str]] = []
    for key, aliases in _AGENT_REDIRECT_ALIASES.items():
        if key not in AGENTS:
            continue
        best: int | None = None
        for alias in aliases:
            m = re.search(r"\b" + re.escape(alias) + r"\b", low)
            if m and (best is None or m.start() < best):
                best = m.start()
        if best is not None:
            hits.append((best, key))
    hits.sort()
    return [k for _, k in hits]


def _direct_redirect(text: str, current_agent: str) -> tuple[str, str] | None:
    """While in direct dialogue with `current_agent`, detect a command aimed at
    a DIFFERENT agent ("ask Codex to ...", "switch to Codex", "spawn Codex").

    Returns (target_agent, instruction); instruction "" means a pure switch.
    None when the utterance is not cross-agent — forward to current as usual.

    This is the fix for "I asked it to spawn Codex and it kept feeding Claude":
    in direct mode every utterance used to go to the active agent, even an
    explicit hand-off to the other one (made worse by Whisper hearing
    'Codex' as 'Kodex'/'Krodex', which the strict dispatch regexes missed).
    """
    others = [a for a in _mentioned_agents(text) if a != current_agent]
    if not others:
        return None
    if not _AGENT_REDIRECT_VERB_RE.search(text or ""):
        return None
    target = others[0]
    # Instruction = whatever follows the LAST mention of the target's name.
    low = (text or "").lower()
    end: int | None = None
    for alias in _AGENT_REDIRECT_ALIASES[target]:
        for m in re.finditer(r"\b" + re.escape(alias) + r"\b", low):
            if end is None or m.end() > end:
                end = m.end()
    rest = (text[end:] if end is not None else "").strip()
    rest = _REDIRECT_LEAD_RE.sub("", rest).strip().rstrip(".,!?")
    return (target, rest)


def _nav_visit(agent: str | None, label: str | None) -> None:
    """Record the current agent/session at the front of the navigation MRU so
    'go back' / 'the other one' / 'previous session' can return to it. Halo
    mode (agent None) is not a frame — it's the absence of a session."""
    global _session_mru
    if agent is None:
        return
    frame = (agent, label)
    if _session_mru and _session_mru[0] == frame:
        return
    _session_mru = [f for f in _session_mru if f != frame]
    _session_mru.insert(0, frame)
    del _session_mru[8:]


# Standalone "go back" / "the other one" / "previous session" references.
# Anchored ^...$ so an instruction that merely CONTAINS "the last one"
# ("use the last one for the header") is NOT treated as navigation.
_NAV_VERB = r"(?:go|jump|switch|head|come|take me|get me|bring me|put me)"
_NAV_BACK_RE = re.compile(
    r"^\s*(?:(?:ok(?:ay)?|now|so|and|yeah|please|hey|halo|alright)[,\s]+)*"
    r"(?:"
    # "(go) back", optionally "(to) the previous/last/other (one/session)"
    rf"{_NAV_VERB}?\s*back"
    r"(?:\s+(?:to\s+|over to\s+)?(?:the\s+)?(?:previous|last|other|prior)"
    r"(?:\s+(?:one|session|agent|chat|task))?)?|"
    # "go/switch to the previous/last/other (one/session)"
    rf"{_NAV_VERB}\s+(?:to\s+|over to\s+|into\s+|onto\s+|on to\s+)?(?:the\s+)?"
    r"(?:previous|last|other|prior)(?:\s+(?:one|session|agent|chat|task))?|"
    # bare "the previous/last/other one/session"
    r"(?:the\s+|that\s+)?(?:previous|last|other|prior)\s+(?:one|session|agent|chat|task)|"
    r"previous session|last session|other session|"
    r"resume(?:\s+the)?\s+(?:previous|last)(?:\s+session)?|"
    r"where (?:i|we) (?:was|were)(?:\s+before)?"
    r")"
    r"\s*[.!?]*\s*$",
    re.IGNORECASE,
)
# "back to Codex" / "go back to the Claude one" — captures the target phrase so
# we can resolve it (garble-tolerant) to an agent. Also anchored to standalone.
_NAV_BACK_NAMED_RE = re.compile(
    r"^\s*(?:(?:ok(?:ay)?|now|so|and|yeah|please|hey|halo|alright)[,\s]+)*"
    r"(?:go |come |switch |head |jump |take me )?back (?:to|over to|into) "
    r"(?:the |my )?(?P<rest>.+?)"
    r"(?:\s+(?:session|one|chat|agent|please|now|again))?[.!?]*\s*$",
    re.IGNORECASE,
)


def _is_nav_phrase(text: str) -> bool:
    return bool(_NAV_BACK_RE.match((text or "").strip()))


def _resolve_nav(
    text: str, current: tuple[str | None, str | None]
) -> tuple[str, str | None] | None:
    """Resolve a back-navigation utterance to a target (agent, label) frame.

      * named  — "back to Codex" / "go back to the Claude one": the most recent
        MRU frame for that agent that isn't the current one (else (agent, None)).
      * generic — "go back" / "the other one" / "previous session": the most
        recent MRU frame that isn't the current one.

    Returns None when it's not back-navigation (or there's nowhere to go)."""
    t = (text or "").strip()
    mnamed = _NAV_BACK_NAMED_RE.match(t)
    if mnamed:
        others = [a for a in _mentioned_agents(mnamed.group("rest")) if a != current[0]]
        if others:
            target = others[0]
            for frame in _session_mru:
                if frame[0] == target and frame != current:
                    return frame
            return (target, None)
        # "back to the previous one" named nothing concrete — fall to generic.
    if _NAV_BACK_RE.match(t):
        for frame in _session_mru:
            if frame != current:
                return frame
    return None


def _wants_back_to_halo(text: str) -> bool:
    return bool(_BACK_TO_HALO_RE.search(text or ""))


def _is_end_phrase(text: str, raw: str = "") -> bool:
    t = (text or "").strip()
    if _END_STRONG_RE.search(t):
        return True
    # A weak ender phrased as a question is a reaction, not a command. The raw
    # transcript keeps the "?" that cleaned_text strips, so check it when given.
    if (raw or "").strip().endswith("?"):
        return False
    return bool(_END_WEAK_RE.match(t))


def _is_new_session_phrase(text: str) -> bool:
    return bool(_NEW_SESSION_RE.search(text or ""))


# Connective words that glue a new-session phrase to its trailing command
# ("new session, AND THEN ask Claude to..."). Stripped along with the phrase
# so the remaining command routes cleanly.
_NEW_SESSION_GLUE_RE = re.compile(
    r"^\s*(?:[,.;:]+\s*)?(?:and\s+then|and|then|now|so|ok(?:ay)?|please|"
    r"can you|could you|would you|will you|i\s+want\s+to|let'?s)\b[\s,]*",
    re.IGNORECASE,
)


def _strip_new_session_phrase(text: str) -> str:
    """Remove the new-session phrase (and any leading glue) and return the
    trailing command, e.g. 'new session, ask Claude to build X' -> 'ask Claude
    to build X'. Returns '' when nothing substantive follows the phrase."""
    t = (text or "").strip()
    m = _NEW_SESSION_RE.search(t)
    if not m:
        return t
    left = t[: m.start()]
    # Drop a bare article that was attached to the phrase ("a new session").
    left = re.sub(r"(?:^|\s)(?:a|an|the)\s*$", " ", left, flags=re.IGNORECASE)
    remainder = re.sub(r"\s{2,}", " ", (left + " " + t[m.end():])).strip(" ,.;:!?")
    # Peel leading connective filler so the command starts clean.
    for _ in range(3):
        stripped = _NEW_SESSION_GLUE_RE.sub("", remainder).strip(" ,.;:!?")
        if stripped == remainder:
            break
        remainder = stripped
    return remainder if len(remainder) >= 2 else ""


def _is_status_query(text: str) -> bool:
    """Status query only intercepts when there's actually something to
    report. 'What's happening?' with no jobs running/completed is just
    chitchat and should fall through to Stage 2 instead of getting a
    'Everything is idle.' reply that interrupts a conversational flow.
    """
    if not _STATUS_QUERY_RE.search(text or ""):
        return False
    if active_jobs():
        return True
    # Recent completed result also qualifies (user might be asking "what
    # did you just do" right after a dispatch finished).
    for key in AGENTS:
        last = last_result_for(key)
        if last is not None and last.is_done:
            return True
    return False


def _replay_target(text: str) -> str | None:
    """If the user asked us to replay an agent's last result, return the
    agent key (e.g. 'claude_code'). Otherwise None."""
    m = _REPLAY_RE.search(text or "")
    if not m:
        return None
    word = m.group(1).lower()
    if word == "claude":
        return "claude_code"
    if word == "codex":
        return "codex_cli"
    return None


def _print_full_result(job_label: str, ok: bool, text: str, elapsed: float) -> None:
    """Loud terminal block so the user can SEE the full agent response,
    not just hear a summary."""
    bar = "=" * 60
    tag = "DONE" if ok else "FAILED"
    print()
    print(bar)
    print(f"[{job_label}] {tag} ({elapsed:.0f}s)")
    print(bar)
    print(text if text else "(no output)")
    print(bar)
    print()


def _join_names(names: list[str]) -> str:
    """Spoken list join: ['A'] -> 'A', ['A','B'] -> 'A and B',
    ['A','B','C'] -> 'A, B and C'."""
    names = [n for n in names if n]
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    return f"{', '.join(names[:-1])} and {names[-1]}"


def _summarize_with_timeout(reply_text: str, prompt: str, timeout: float = 6.0) -> str:
    """`summarize_reply` with a hard wall-clock cap.

    summarize_reply makes a blocking Ollama call. Run inside the
    conversation loop, a slow or hung model would freeze talk→listen.
    Run it on a daemon thread; if it overruns, fall back to the local
    head-trim summarizer (no network) so the loop always moves on.
    """
    box: dict[str, str | None] = {"text": None}

    def _work() -> None:
        try:
            box["text"] = summarize_reply(reply_text, prompt)
        except Exception:
            box["text"] = None

    t = threading.Thread(target=_work, daemon=True, name="summarize")
    t.start()
    t.join(timeout)
    if t.is_alive() or not box["text"]:
        return summarize_for_speech(reply_text) or ""
    return box["text"] or ""


def _drain_completed_jobs() -> None:
    """Print finished agent jobs to stdout, and speak a "done" cap.

    For streaming agents (Claude), the response itself was already
    spoken sentence-by-sentence as it generated; we just say a short
    "Mercury is done." cap so the user knows the agent is no longer
    working. For batch agents (Codex), we speak the full summary now.

    When several jobs finished since the last turn (e.g. a fan-out),
    we COALESCE: print every result but speak a single combined cap
    ("Mercury and Mars are done.") instead of running an LLM summary +
    TTS serially for each — that pile-up blocked the loop for seconds.
    The detailed per-job summary is reserved for the common single
    completion. The user can always replay a specific session's output.

    v1.2: spoken_name and label are per-(agent, cwd) so the cap
    correctly says "Mercury in website is done." when relevant.
    """
    jobs = list(completed_unconsumed_jobs())
    if not jobs:
        return

    drained = []  # (spoken_name, job, stream_state, cfg)
    for job in jobs:
        cfg = AGENTS[job.agent_key]
        spoken_name = session_name(cfg.key, job.cwd)
        project_tag = Path(job.cwd).name if job.cwd else ""
        full_label = (
            f"{cfg.key} / {spoken_name} ({project_tag})" if project_tag
            else f"{cfg.key} / {spoken_name}"
        )
        _print_full_result(
            full_label, bool(job.ok), job.result, job.elapsed_sec
        )
        # Fold the agent's outcome into the conversation memory so the brain
        # knows what was just built ("open the page you made", "what did it do").
        if _active_brief is not None and job.result:
            _active_brief.add_halo(f"{spoken_name}: {job.result}")
        # Pop and consume the per-job stream state (v1.2.1 hybrid mode).
        stream_state = _stream_states.pop(job.job_id, None)
        drained.append((spoken_name, job, stream_state, cfg))

    # Coalesced path — multiple jobs landed together. One short cap, no
    # serial LLM summaries.
    if len(drained) > 1:
        oks = [n for (n, j, _s, _c) in drained if j.ok]
        errs = [n for (n, j, _s, _c) in drained if not j.ok]
        parts = []
        if oks:
            verb = "are" if len(oks) > 1 else "is"
            parts.append(f"{_join_names(oks)} {verb} done.")
        if errs:
            parts.append(f"{_join_names(errs)} had a problem.")
        say(" ".join(parts), blocking=True)
        for (_n, job, _s, _c) in drained:
            mark_consumed(job)
        return

    # Single completion — full detailed cap.
    spoken_name, job, stream_state, cfg = drained[0]
    if job.ok:
        if cfg.streams_text_deltas:
            # Streaming agent: spoke first N sentences live. If the
            # reply ran past the live budget, summarize the
            # remainder into one cap sentence so the user gets the
            # rest of the story without sitting through a monologue.
            full_reply = (job.result or "").strip()
            if stream_state is not None and stream_state.was_truncated:
                spoken_already = stream_state.spoken_text.strip()
                remainder = full_reply
                if spoken_already and full_reply.startswith(spoken_already):
                    remainder = full_reply[len(spoken_already):].strip()
                if remainder and len(remainder) >= 60:
                    # Only summarize if there's meaningful content
                    # left — a stray trailing word doesn't warrant
                    # an extra TTS round-trip.
                    summary = _summarize_with_timeout(remainder, job.prompt)
                    if summary:
                        say(f"And, {summary}", blocking=True)
            # If was_truncated=False the user already heard the
            # whole reply via the live stream. Skip the cap.
        else:
            # Batch agent (Codex): no live streaming. If short,
            # speak the cleaned head; if long, brain-summarize.
            full_reply = (job.result or "").strip()
            if len(full_reply) > REPLY_SUMMARIZE_THRESHOLD_CHARS:
                summary = _summarize_with_timeout(full_reply, job.prompt)
                if summary:
                    say(f"{spoken_name} says, {summary}", blocking=True)
                else:
                    say(f"{spoken_name} is done.", blocking=True)
            else:
                short = summarize_for_speech(full_reply) or f"{spoken_name} is done."
                say(f"{spoken_name} says, {short}", blocking=True)
    else:
        # Error path: short caps still use the legacy head-trim
        # (errors are usually short stderr tails, not paragraphs).
        say(
            f"{spoken_name} had a problem. {summarize_for_speech(job.result, 120)}",
            blocking=True,
        )
    mark_consumed(job)


def _print_decision(decision: dict) -> None:
    print("\n--- router decision ---")
    print(json.dumps(decision, indent=2))


def _start_agent_and_ack(
    agent_key: str,
    prompt: str,
    *,
    cwd: Path | None = None,
    brief: _ConversationBrief | None = None,
) -> str:
    """Spawn a background job, return the voice-ack to speak.

    For streaming-capable agents (Claude Code), wire a per-sentence TTS
    callback so Halo narrates the response live while the agent works.

    `cwd` (v1.2) — working directory for the session. Defaults to
    DEFAULT_CWD. Session state (continuation, name, persistent process)
    is keyed by (agent, cwd) so multiple projects can run in parallel.
    """
    if not (prompt or "").strip():
        # Belt-and-braces — callers should already guard, but never
        # dispatch a blank prompt to an agent. Claude / Codex both
        # error on empty input.
        return ""
    if brief is not None:
        prompt = brief.render_for_agent(prompt)

    # Pre-flight: refuse to dispatch to an agent whose binary isn't on
    # PATH or didn't respond to its check_call. Otherwise the subprocess
    # errors halfway through and the user hears "had a problem" with no
    # clue what's missing.
    config = AGENTS[agent_key]
    status = check_availability().get(agent_key, {})
    if not (status.get("installed") and status.get("responsive")):
        print(f"  refusing dispatch -> {agent_key}: not connected ({status})")
        return (
            f"{config.spoken_name} isn't connected. "
            f"Run halo doctor for setup help."
        )

    workdir = cwd or DEFAULT_CWD
    spoken = session_name(agent_key, workdir)
    # Per-(agent, cwd) "is this a fresh session" check. session_status()
    # still returns the aggregated agent-level map; we use the detail
    # variant via the per-cwd session_key import only when we care about
    # multi-project state, which is here.
    from halo.agents import session_key as _skey, session_status_detail
    skey = _skey(agent_key, workdir)
    is_active = session_status_detail().get(skey, False)
    if config.session_kind == "persistent":
        active = "continuing" if is_active else "starting"
        suffix = f"persistent session, {active}"
    else:
        active = "continuing" if is_active else "starting"
        suffix = f"{active} session"
    project_tag = Path(workdir).name if workdir != DEFAULT_CWD else ""
    where = f" in {project_tag}" if project_tag else ""
    print(f"  dispatching to {spoken}{where} / {config.spoken_name.lower()} "
          f"({suffix}): {prompt!r}")

    # Hybrid streaming state (v1.2.1). Only used for agents with
    # streams_text_deltas=True. The first N sentences are spoken live;
    # the rest is buffered silently and summarized in _drain.
    stream_state = _StreamState(max_live_sentences=LIVE_STREAM_MAX_SENTENCES)

    def _speak_chunk(sentence: str) -> None:
        # Non-blocking so a long monologue queues sentence-by-sentence
        # without stalling the agent's stdout reader thread. The
        # sanitizer inside voice.say() strips any markdown the agent
        # leaks despite the voice system prompt.
        if stream_state.sentences_spoken < stream_state.max_live_sentences:
            print(f"    [{config.key}{where} -> tts live] {sentence!r}")
            say(sentence, blocking=False)
            stream_state.sentences_spoken += 1
            stream_state.chars_spoken += len(sentence)
            stream_state.spoken_text = (
                stream_state.spoken_text + " " + sentence
                if stream_state.spoken_text
                else sentence
            )
        else:
            # Past the live budget — buffer silently, summarize at end.
            if not stream_state.was_truncated:
                stream_state.was_truncated = True
                print(
                    f"    [{config.key}{where} -> tts] "
                    f"silenced past {stream_state.max_live_sentences} live "
                    f"sentences; will summarize at end"
                )

    on_chunk = _speak_chunk if config.streams_text_deltas else None

    try:
        job = start_job(agent_key, prompt, cwd=workdir, on_text_chunk=on_chunk)
        # (start_job claims the voice floor for this job before its runner
        # thread starts, so the latest dispatch is the sole live speaker.)
        # Register the stream state for _drain to consume on completion.
        # We register even when streams_text_deltas=False so batch agents
        # (Codex) also get the summarize-when-long behaviour — their
        # state will just have was_truncated=False and we'll branch on
        # full-text length instead.
        _stream_states[job.job_id] = stream_state
        # (the original prompt is already stored on the job for the summarizer)
        print(f"  job #{job.job_id} started in background"
              f"{' (streaming)' if config.streams_text_deltas else ''}")
        # Conversational hand-off: the job runs in the background, so invite
        # the next request instead of leaving dead air while the agent works.
        if active == "starting":
            return f"On it — starting {spoken}{where}. Anything else while that runs?"
        return f"On it — {spoken}{where} is on it. Anything else while you wait?"
    except AgentBusy as exc:
        return str(exc)


def _handle_session_action(decision: dict) -> str | None:
    """v1.2: handle brain-emitted session_action / target_session.

    The brain's `understand_and_route()` already applies a regex
    fallback for the small-model misses (forgotten session_action for
    obvious switch/list/where_am_i phrases) before returning the
    decision, so this function just acts on whatever's present.

    Returns:
      str  — a phrase to speak back (action handled here, no agent dispatch)
      None — the brain did NOT request a session action; caller should
             continue with the normal dispatch flow.
    """
    action = (decision.get("session_action") or "").strip()
    target = (decision.get("target_session") or "").strip()

    if action == "list_sessions":
        bus.emit("session.listed")
        return REGISTRY.speak_list()

    if action == "where_am_i":
        bus.emit("session.where")
        return REGISTRY.speak_active()

    if action == "switch":
        if not target:
            return "Switch to which session?"
        resolved = REGISTRY.resolve(target)
        if resolved.kind == "session" and resolved.label is not None:
            if REGISTRY.set_active(resolved.label):
                bus.emit("session.switched", label=resolved.label)
                session = REGISTRY.by_label(resolved.label)
                if session is not None:
                    return f"You're talking to {_session_spoken(session)}."
                return decision.get("confirmation") or f"Switched to {resolved.label}."
        return f"I don't see a session called {target}."

    return None


def _dispatch_to_all(
    prompt: str,
    agent_hint: str,
    *,
    brief: _ConversationBrief | None = None,
) -> str:
    """Fan-out: dispatch the same prompt to every discovered session.

    Used when the brain emits target_session='all'. agent_hint comes
    from the brain (e.g. 'claude_code') and acts as the default for
    sessions that match it; sessions of a different agent get their
    own dispatch via their own agent.
    """
    sessions = REGISTRY.list()
    if not sessions:
        return "There are no discovered sessions to dispatch to."

    fired = 0
    for s in sessions:
        # If the brain picked a specific agent, prefer dispatching only
        # to sessions of that kind; otherwise dispatch to each session
        # with its own agent.
        agent_key = agent_hint if (agent_hint in AGENTS and s.agent_key == agent_hint) else s.agent_key
        if agent_key not in AGENTS:
            continue
        try:
            _start_agent_and_ack(agent_key, prompt, cwd=Path(s.cwd), brief=brief)
            fired += 1
        except Exception as exc:
            print(f"  fanout error for {s.label}: {exc}")
    if fired == 0:
        return "Nothing to dispatch."
    return f"Sent to {fired} session{'s' if fired != 1 else ''}."


def _handle_decision(
    decision: dict,
    *,
    brief: _ConversationBrief | None = None,
) -> str:
    """Act on a decision dict. Returns the short phrase to speak back.

    v1.2: respects target_session for per-turn dispatch routing.
    Session-level actions (switch / list / where_am_i) are handled by
    `_handle_session_action()` before this is called.
    """
    status = decision.get("status", "?")
    intent = decision.get("intent", "?")
    agent = decision.get("agent", "none")
    cleaned = decision.get("cleaned_text", "")
    confirmation = decision.get("confirmation", "")
    clarification = decision.get("clarification", "")
    target_session = (decision.get("target_session") or "").strip()

    if status == "chitchat":
        # Empty cleaned_text means the router heard only noise/laughter —
        # nothing to converse about, stay silent. Otherwise actually chat back.
        if not (cleaned or "").strip():
            print("  (chitchat — nothing to say)")
            return ""
        return _converse(cleaned, brief)  # streams + speaks internally

    if status == "cancel":
        return confirmation or "Cancelled."

    if status == "unclear":
        return clarification or "Can you say that again?"

    if status != "ready":
        return ""

    # status == "ready"
    # Local tools (date/time, open chrome, calculator, ...) can satisfy a
    # ready turn regardless of how the small router labelled the intent.
    # The 1.5B model frequently tags "what's the date / time" as
    # intent=question (or system) inconsistently, then the question branch
    # just speaks a useless "Okay." Try the local tools FIRST for non-code
    # intents so these always resolve locally and instantly.
    if intent != "code":
        handled, summary, leftover = execute_system_intent(cleaned)
        if handled:
            print(f"  executed (local tool): {summary}")
            # A chained command may leave a part no local tool can do
            # ("open a browser AND search the web for X"). Stash it so the
            # conversation loop can offer to delegate instead of dropping it.
            global _system_leftover
            _system_leftover = (
                leftover if (leftover and _looks_like_real_task(leftover)) else ""
            )
            if _system_leftover:
                print(f"  leftover for delegation -> {_system_leftover!r}")
            return summary

    if intent == "question" and agent == "none":
        # Tools didn't answer it — have a real conversation instead of the
        # old robotic "I can help with Halo or your coding agents" line.
        return _converse(cleaned, brief)  # streams + speaks internally

    if intent == "system":
        # Local tools + the generic app launcher already ran above and matched
        # nothing. Per the "Confirm before Claude" policy, NEVER silently spawn
        # an agent here — that's exactly the "open paint went to Claude" leak.
        # Tell the user; they can say "Claude ..." to hand it over explicitly.
        print(f"  system intent, no local match -> not dispatching: {cleaned!r}")
        return (
            "I couldn't do that one myself. Say Claude if you want me to hand "
            "it over."
        )

    # Multi-session fan-out — brain says target_session='all'.
    if target_session == "all":
        return _dispatch_to_all(
            cleaned,
            agent_hint=agent if agent in AGENTS else "claude_code",
            brief=brief,
        )

    # Registry-driven dispatch — non-blocking. Job runs in background;
    # _drain_completed_jobs() between turns will speak the result.
    if agent in AGENTS:
        cwd = _cwd_for_dispatch(target_session)
        return _start_agent_and_ack(agent, cleaned, cwd=cwd, brief=brief)

    # question / other ready intents with no agent. The brain chose
    # agent=none, so this is the ambiguous "is this even for Claude?" case.
    # Only spawn a session when it actually looks like a task — otherwise
    # it's conversational noise / a casual remark that slipped the chitchat
    # filter (e.g. a mis-transcribed "what do you mean, it feels good"),
    # and spawning a fresh Claude session for it is exactly the phantom-
    # session bug the user hit. Speak the brain's confirmation instead.
    # We only reach here for agent=none either (a) after the user confirmed a
    # dispatch ("Send X to Claude?" -> yes), or (b) when it isn't a task. In
    # case (a) the condition below holds and we dispatch; in case (b) we just
    # chat back instead of leaving dead air. The normal (un-confirmed) flow
    # never silently dispatches because _needs_confirmation intercepts first.
    if cleaned and len(cleaned.split()) >= 2 and _looks_like_real_task(cleaned):
        print(f"  ready {intent!r}, no agent -> dispatching to claude_code: {cleaned!r}")
        cwd = _cwd_for_dispatch(target_session)
        return _start_agent_and_ack("claude_code", cleaned, cwd=cwd, brief=brief)
    if cleaned:
        print(f"  ready {intent!r}, no agent, not a task -> conversing")
        return _converse(cleaned, brief)  # streams + speaks internally
    return confirmation or ""


_YES_RE = re.compile(
    r"^\s*(?:yes|yeah|yep|correct|right|affirmative|go ahead|do it|"
    r"send it|start|start it|please do|that'?s right)\s*[.!?,]*\s*$",
    re.IGNORECASE,
)
_NO_RE = re.compile(
    r"^\s*(?:no|nope|cancel|don'?t|do not|stop|never mind|nevermind|"
    r"not now|leave it)\s*[.!?,]*\s*$",
    re.IGNORECASE,
)
_CHANGE_PREFIX_RE = re.compile(
    r"^\s*(?:actually|wait|no actually|change that to|make it|instead|"
    r"rather|use|with)\b",
    re.IGNORECASE,
)
_BARE_GO_RE = re.compile(
    r"^\s*(?:go|let'?s go|let'?s do it|do it|send it|start|start now|"
    r"please go|execute)\s*[.!?,]*\s*$",
    re.IGNORECASE,
)
_RISKY_AGENT_RE = re.compile(
    r"\b("
    r"delete|remove|wipe|erase|drop|truncate|destroy|overwrite|replace all|"
    r"revert|rollback|reset|clean|prune|"
    r"commit|push|pull|merge|rebase|checkout|stash|"
    r"install|uninstall|upgrade|downgrade|deploy|release|publish|ship|"
    r"run|execute|shell|powershell|terminal|cmd|bash|"
    r"token|secret|password|credential|env|environment"
    r")\b",
    re.IGNORECASE,
)


def _is_yes(text: str) -> bool:
    return bool(_YES_RE.match(text or ""))


def _is_no(text: str) -> bool:
    return bool(_NO_RE.match(text or ""))


def _is_change_answer(text: str) -> bool:
    return bool(_CHANGE_PREFIX_RE.match(text or ""))


def _is_bare_go(text: str) -> bool:
    return bool(_BARE_GO_RE.match(text or ""))


def _needs_confirmation(decision: dict) -> bool:
    """True when a ready decision should pause for a spoken yes/no before
    Halo hands the work to a coding agent.

    "Confirm before Claude" policy: the user asked NOT to be auto-routed to
    Claude unless they say so. Explicit "Claude, ..." / "ask Codex to ..."
    already bypass Stage 2 via vocative/verbal dispatch, so by the time a
    decision reaches here the user did NOT name the agent out loud — we
    confirm first. System/question intents are handled locally (tools/chat),
    never a dispatch, so they never need confirmation.
    """
    if decision.get("status") != "ready":
        return False
    cleaned = (decision.get("cleaned_text") or "").strip()
    if not cleaned:
        return False
    intent = decision.get("intent", "")
    if intent == "system":
        return False
    # Questions are ANSWERED (by tools or the chat brain with memory), never
    # dispatched — even when they contain a verb like "open" ("what app did you
    # open first?"). Don't pause to confirm sending a question to Claude.
    if intent == "question":
        return False
    agent = decision.get("agent", "none")

    if agent in AGENTS:
        # The router auto-picked an agent. Confirm unless the user actually
        # named one in the command (rare here — vocative/verbal caught most).
        if not _user_named_agent(cleaned):
            return True
        # Named agent but risky/vague — keep the original guards.
        if _RISKY_AGENT_RE.search(cleaned):
            return True
        if len(cleaned.split()) <= 4 and re.search(
            r"\b(fix|change|update|remove|delete|run|deploy)\b", cleaned, re.I
        ):
            return True
        return False

    # agent == none: would only fall through to a Claude dispatch if it looks
    # like a real task (see _handle_decision's tail). Confirm that too.
    if len(cleaned.split()) >= 2 and _looks_like_real_task(cleaned):
        return True
    return False


def _confirmation_question(decision: dict) -> str:
    agent = decision.get("agent", "none")
    if agent in AGENTS:
        spoken = AGENTS[agent].spoken_name
    else:
        # agent=none real-task confirmations default to Claude (the fall-through
        # target in _handle_decision).
        spoken = AGENTS["claude_code"].spoken_name if "claude_code" in AGENTS else "Claude"
    cleaned = (decision.get("cleaned_text") or "that").strip().rstrip(".")
    if len(cleaned) > 90:
        cleaned = cleaned[:87].rstrip() + "..."
    return f"Send '{cleaned}' to {spoken}?"


def _merge_pending_text(original: str, answer: str) -> str:
    original = (original or "").strip().rstrip(".")
    answer = (answer or "").strip()
    if not original:
        return answer
    if not answer:
        return original
    if _is_change_answer(answer):
        return f"{original}, but {answer}"
    return f"{original}. {answer}"


def _set_direct(direct_agent: str | None) -> str | None:
    """Update direct-dialogue mode + emit a bus event so the dashboard
    pill updates. Returns the new direct_agent value."""
    bus.emit("mode.changed", direct=direct_agent)
    return direct_agent


def run_conversation() -> None:
    """Loop one or more turns without requiring re-wake.

    Routing priority — first match wins, Stage 2 LLM is the LAST resort:

      1.  end-phrase               -> goodbye, return
      2.  new-session phrase       -> reset, continue
      3.  vocative dispatch        -> "Claude, X" / "Codex, X" -> direct
      3b. verbal dispatch          -> "ask Codex to X" / "tell Claude" -> direct
      4.  pure mode-switch         -> "switch to codex" -> direct mode
      5.  back-to-halo             -> exit direct mode
      6.  status query / replay    -> registry, no LLM
      7.  tool fast-path           -> open chrome, calculator, etc.
      8.  direct-dialogue active   -> straight to current agent (no LLM)
      9.  Stage 2 LLM (fallback)   -> only if nothing above matched AND
                                      we're not in direct mode

    Mode auto-enters DIRECT_<agent> the moment a job for that agent
    starts. User exits with "back to halo" or "talk to me".
    """
    last_activity = time.monotonic()
    direct_agent: str | None = None
    direct_session_label: str | None = None
    # Toggled True the first time the user actually says something we
    # process (any non-None decision). The idle budget jumps from 5s
    # to CONVERSATION_IDLE_ENGAGED_SEC (90s) once engaged, so a single
    # spoken command no longer cuts the conversation short the moment
    # Halo finishes its reply.
    engaged = False
    pending_action: dict | None = None
    pending_clarification: str | None = None
    # Persist conversation memory ACROSS sleep/wake within one Halo run, so
    # Halo remembers earlier turns instead of forgetting every time it sleeps.
    # Reset only on an explicit "new session". Also exposed module-level so
    # background-job completions can fold the agent's reply into it.
    global _active_brief, _system_leftover
    if _active_brief is None:
        _active_brief = _ConversationBrief()
    brief = _active_brief
    first_turn_after_wake = True

    while True:
        # Fresh each turn — set by _handle_decision only when a chained command
        # leaves an agent-only remainder, read right after the Stage 2 call.
        _system_leftover = ""
        # Remember where we are (after last turn's switch) so "go back" / "the
        # other one" can return here. Cheap, idempotent on the current frame.
        _nav_visit(direct_agent, direct_session_label)
        # Speak any background-job results that landed since last turn.
        _drain_completed_jobs()

        # Don't open the mic while Halo is still speaking back — it'd
        # pick up echo, fire false "no speech detected" turns, and eat
        # the idle budget the user needs to actually reply to a question.
        # Reset last_activity once Halo goes silent so the idle clock
        # starts from "user could begin talking now", not from when the
        # user last spoke (which may have been before a 10s agent reply).
        barge_in_text: str | None = None
        if voice_is_speaking():
            barge_in_text = listen_for_barge_in(timeout=20.0)
            if barge_in_text:
                print(f"  barge-in accepted -> {barge_in_text!r}")
                bus.emit("barge_in.accepted", text=barge_in_text)
                voice_stop()
            else:
                voice_wait_until_silent(timeout=20.0)
            last_activity = time.monotonic()

        # Idle timeout — extend whenever a job is in flight, Halo is
        # still talking back to the user, OR the user is in direct
        # dialogue with an agent. The last clause means once you've
        # opted in to a session ("Claude, ..." / "ask Codex to ...")
        # Halo only exits the conversation on an explicit phrase
        # ("goodbye" / "back to halo" / "transfer to ..."). No silent
        # cut-off mid-thread.
        idle = time.monotonic() - last_activity
        idle_limit = (
            CONVERSATION_IDLE_ENGAGED_SEC if engaged else CONVERSATION_IDLE_SEC
        )
        if idle > idle_limit:
            # Keep alive while a job runs or Halo is speaking. Direct mode no
            # longer pins the mic open FOREVER — it honors the engaged (90s)
            # budget so a stray/auto-entered direct mode can't trap the user.
            if active_jobs() or voice_is_speaking():
                last_activity = time.monotonic()
            else:
                if direct_agent is not None:
                    print("  direct mode idle -> exiting direct mode")
                    direct_agent = _set_direct(None)
                print(f"\nconversation idle for {idle_limit:.0f}s -> sleeping")
                say("Going back to sleep.", blocking=True)
                return

        # Poll faster (2 s) while agents are running so completions land
        # quickly; normal 5 s otherwise.
        first_wait = 2.0 if active_jobs() else None

        # Always skip Stage 2 inside run_turn — every routing decision
        # is made HERE, in priority order, so we only spend the LLM
        # roundtrip on utterances that genuinely need it.
        if barge_in_text:
            decision = {
                "status": "raw",
                "cleaned_text": barge_in_text,
                "transcript": barge_in_text,
                "intent": "raw",
                "agent": "none",
                "confirmation": "",
                "clarification": "",
            }
        else:
            decision = run_turn(
                skip_routing=True,
                max_wait_first_speech_sec=first_wait,
                seed_pre_wake=first_turn_after_wake,
            )
            first_turn_after_wake = False
        if decision is None:
            continue

        cleaned = (decision.get("cleaned_text") or "").strip()
        cleaned = _strip_leading_greeting(cleaned)
        # Raw transcript keeps trailing punctuation ("That's it?") that
        # cleaned_text strips — the end-phrase guard uses it to tell a casual
        # question apart from a real "we're done" command.
        raw_transcript = (decision.get("transcript") or cleaned)
        print(f"  heard: {cleaned!r}")

        # Empty after wake/greeting strip = Whisper transcribed silence,
        # room noise, or a phantom wake. Do NOT flip `engaged` (that pins
        # the mic open for the 90s engaged budget) and do NOT reset the
        # idle timer — otherwise repeating noise keeps the conversation
        # alive forever. Just loop back to listening.
        if not cleaned:
            continue

        # A genuine, non-empty utterance: we're in an active conversation
        # now, so extend the idle budget to the engaged window.
        last_activity = time.monotonic()
        engaged = True
        brief.add_user(cleaned)

        # Dictation trigger wins over EVERYTHING — including a pending
        # confirmation/clarification — so a mis-heard "dictate" can never
        # spiral into the LLM (it would otherwise get merged into the
        # pending clarification and routed). Clears pending state, then
        # hands the mic to the verbatim dictation loop.
        if _wants_dictation(cleaned):
            print("  dictation trigger -> entering dictation mode")
            bus.emit("route.matched", handler="dictation")
            pending_action = None
            pending_clarification = None
            from halo.dictation import run_dictation
            say(
                "Dictation on. Click where you want the words and start "
                "talking. Say stop when you're done.",
                blocking=True,
            )
            summary = run_dictation()
            if summary:
                say(summary, blocking=True)
            last_activity = time.monotonic()
            continue

        # "Say <text>" — Halo speaks it verbatim. Checked BEFORE the end-phrase
        # guard so quoting an end phrase ("say thanks, see you next time") reads
        # it aloud instead of triggering sleep. A demo/recording puppet aid.
        say_matched, say_text = _try_say(cleaned)
        if say_matched:
            print(f"  say -> {say_text!r}")
            bus.emit("route.matched", handler="say", text=say_text)
            say(say_text, blocking=True)
            brief.add_halo(say_text)
            last_activity = time.monotonic()
            continue

        # "remember that X" -> store a durable fact in persistent memory.
        if _MEMORY is not None:
            from halo.memory import extract_fact
            _fact = extract_fact(cleaned)
            if _fact is not None:
                brief.add_user(cleaned)
                _MEMORY.record_fact(_fact[0], _fact[1], weight=4.0)
                print(f"  remember -> fact: {_fact[0]!r}")
                bus.emit("route.matched", handler="remember", text=_fact[0])
                say("Got it, I'll remember that.", blocking=True)
                last_activity = time.monotonic()
                continue

        # End-phrase wins next — "go to sleep" / "goodbye" must ALWAYS end the
        # conversation, even with a pending confirmation/clarification open.
        # (Previously a pending clarification swallowed it as a "cancel", so
        # the conversation refused to sleep.)
        if _is_end_phrase(cleaned, raw_transcript):
            print("  end-phrase -> closing conversation")
            bus.emit("route.matched", handler="end_phrase")
            say("Goodbye.", blocking=True)
            return

        # Pending yes/no confirmation. This is what makes a spoken
        # "ready to start?" a real dialogue turn instead of dispatching
        # immediately on a small-router guess.
        if pending_action is not None:
            if _is_yes(cleaned):
                lm_decision = pending_action
                pending_action = None
                print("  pending action confirmed")
                bus.emit("route.matched", handler="confirmed_action")
                phrase = _handle_decision(lm_decision, brief=brief)
                if phrase:
                    say(phrase, blocking=True)
                if lm_decision.get("status") == "ready":
                    agent_dispatched = lm_decision.get("agent")
                    if agent_dispatched in AGENTS:
                        direct_agent = _set_direct(agent_dispatched)
                        direct_session_label = None
                        print(f"  entering direct-dialogue mode with {direct_agent}")
                last_activity = time.monotonic()
                continue
            if _is_no(cleaned) or _is_end_phrase(cleaned):
                pending_action = None
                bus.emit("route.matched", handler="cancel_pending_action")
                say("Cancelled.", blocking=True)
                last_activity = time.monotonic()
                continue
            original = pending_action.get("cleaned_text", "")
            cleaned = _merge_pending_text(original, cleaned)
            pending_action = None
            print(f"  pending action revised -> {cleaned!r}")

        # Pending clarification. The user's short answer is not a fresh
        # command; attach it to the original unclear request and let the
        # normal routing stack interpret the completed instruction.
        if pending_clarification is not None:
            if _is_no(cleaned) or _is_end_phrase(cleaned):
                pending_clarification = None
                bus.emit("route.matched", handler="cancel_pending_clarification")
                say("Cancelled.", blocking=True)
                last_activity = time.monotonic()
                continue
            cleaned = _merge_pending_text(pending_clarification, cleaned)
            pending_clarification = None
            print(f"  clarification merged -> {cleaned!r}")

        # 1b. Bare control word with nothing pending — acknowledge locally
        #     instead of burning a Stage 2 round-trip. A lone "stop" used to
        #     reach the LLM and get mis-routed to a session switch; now it just
        #     gets a quick "Okay" and we keep listening ("go to sleep" exits).
        if _BARE_STOP_RE.match(cleaned):
            print(f"  bare control word -> acknowledging: {cleaned!r}")
            bus.emit("route.matched", handler="bare_control")
            say("Okay.", blocking=True)
            last_activity = time.monotonic()
            continue

        # 2. New-session phrase
        if _is_new_session_phrase(cleaned):
            remainder = _strip_new_session_phrase(cleaned)
            print(f"  reset-session phrase -> fresh session"
                  + (f"; carrying command {remainder!r}" if remainder else ""))
            bus.emit("route.matched", handler="new_session")
            reset_session()
            direct_agent = _set_direct(None)
            direct_session_label = None
            pending_action = None
            pending_clarification = None
            brief.clear()
            _session_mru.clear()  # nothing to "go back" to after a fresh start
            try:
                REGISTRY.set_active(None)  # else next dispatch reuses old cwd
            except Exception:
                pass
            bus.emit("session.reset")
            # If the user said the reset AND a command in one breath ("new
            # session, ask Claude to build X"), don't drop the command — keep
            # routing it on a clean slate. Otherwise just acknowledge.
            if remainder:
                cleaned = remainder
                brief.add_user(cleaned)
                # fall through to vocative / verbal / Stage 2 routing below
            else:
                say("Fresh session.", blocking=True)
                continue

        # 3. Vocative dispatch — "Claude, X" / "Codex, X". Goes straight
        #    to the named agent. Saves the 3-5 s Stage 2 LLM round-trip.
        voc_agent, voc_text = _vocative_dispatch(cleaned)
        if voc_agent and voc_text:
            print(f"  vocative dispatch -> {voc_agent}: {voc_text!r}")
            bus.emit("route.matched", handler="vocative", target=voc_agent)
            direct_agent = _set_direct(voc_agent)
            direct_session_label = None
            phrase = _start_agent_and_ack(voc_agent, voc_text, cwd=_cwd_for_dispatch(""), brief=brief)
            if phrase:
                say(phrase, blocking=True)
            last_activity = time.monotonic()
            continue

        # 3b. Verbal dispatch — "ask Codex to X" / "tell Claude to Y" /
        #     "have Codex deploy". Same effect as vocative but with a
        #     verb instead of a comma. The small Stage 2 LLM frequently
        #     mis-routes these (swapping the agent name, hallucinating
        #     extras), so we intercept here.
        verb_agent, verb_text = _verbal_dispatch(cleaned)
        if verb_agent and verb_text:
            print(f"  verbal dispatch -> {verb_agent}: {verb_text!r}")
            bus.emit("route.matched", handler="verbal", target=verb_agent)
            direct_agent = _set_direct(verb_agent)
            direct_session_label = None
            phrase = _start_agent_and_ack(verb_agent, verb_text, cwd=_cwd_for_dispatch(""), brief=brief)
            if phrase:
                say(phrase, blocking=True)
            last_activity = time.monotonic()
            continue

        # 4. Pure mode-switch ("switch to codex" with no instruction after)
        switch_target = _agent_switch_target(cleaned)
        if switch_target is not None:
            direct_agent = _set_direct(switch_target)
            direct_session_label = None
            spoken = session_name(switch_target)
            print(f"  pure switch -> {switch_target} ({spoken})")
            bus.emit("route.matched", handler="mode_switch", target=switch_target)
            say(f"Switching to {spoken}.", blocking=True)
            continue

        # 5. Back to Halo
        if _wants_back_to_halo(cleaned):
            if direct_agent:
                print(f"  leaving direct dialogue with {direct_agent}")
            direct_agent = _set_direct(None)
            direct_session_label = None
            bus.emit("route.matched", handler="back_to_halo")
            say("Halo here.", blocking=True)
            continue

        # 5b. Back-navigation — "go back" / "the other one" / "previous
        #     session" / "back to Codex". Returns to a session from the
        #     navigation MRU instead of forwarding to the current agent. Runs
        #     before status/chitchat/direct so the reference is honored.
        nav_target = _resolve_nav(cleaned, (direct_agent, direct_session_label))
        if nav_target is not None:
            target_agent, target_label = nav_target
            direct_agent = _set_direct(target_agent)
            direct_session_label = target_label
            if target_label:
                REGISTRY.set_active(target_label)
            spoken = session_name(target_agent)
            print(f"  nav back -> {target_agent} ({spoken}), label={target_label!r}")
            bus.emit("route.matched", handler="nav_back", target=target_agent)
            say(f"Back with {spoken}.", blocking=True)
            last_activity = time.monotonic()
            continue
        if _is_nav_phrase(cleaned):
            print("  nav back -> nothing to return to")
            bus.emit("route.matched", handler="nav_back_empty")
            say("There's nothing to go back to yet.", blocking=True)
            last_activity = time.monotonic()
            continue

        # 6. Status / replay
        if _is_status_query(cleaned):
            summary = status_summary()
            print(f"  status: {summary}")
            bus.emit("route.matched", handler="status_query")
            say(summary, blocking=True)
            continue

        replay_agent = _replay_target(cleaned)
        if replay_agent is not None:
            last = last_result_for(replay_agent)
            cfg = AGENTS[replay_agent]
            spoken = session_name(replay_agent)
            if last is None or not last.is_done:
                say(f"{spoken} hasn't finished anything yet.", blocking=True)
            else:
                _print_full_result(
                    f"{cfg.key} / {spoken}", bool(last.ok),
                    last.result, last.elapsed_sec,
                )
                say(summarize_for_speech(last.result) or "Nothing to repeat.",
                    blocking=True)
            continue

        # 6b. Chitchat-to-Halo intercept — pattern-match conversational
        #     pings at Halo herself ("hello", "are you there?", "can you
        #     hear me?", "what are you working on?") and speak a canned
        #     reply WITHOUT touching Claude. Saves the 4-5s Stage 2
        #     round-trip AND prevents the v1.2.2-era bug where Halo
        #     would dispatch "Hello can you hear me?" as a coding task
        #     to a fresh Claude session.
        chitchat = _chitchat_reply(cleaned)
        if chitchat is not None:
            chitchat = _personalize_reply(chitchat)
            print(f"  chitchat-to-halo -> {chitchat!r}")
            bus.emit("route.matched", handler="chitchat_halo", text=chitchat)
            say(chitchat, blocking=True)
            brief.add_halo(chitchat)
            last_activity = time.monotonic()
            continue

        type_phrase = _local_type_into_session(cleaned)
        if type_phrase is not None:
            print(f"  local desktop type -> {type_phrase!r}")
            bus.emit("route.matched", handler="desktop_type")
            say(type_phrase, blocking=True)
            active_session = REGISTRY.active()
            if active_session is not None:
                direct_agent = _set_direct(active_session.agent_key)
                direct_session_label = REGISTRY.active_label()
            last_activity = time.monotonic()
            continue

        switch_agent, switch_phrase = _local_session_switch(cleaned)
        if switch_phrase:
            print(f"  local session switch -> {switch_phrase!r}")
            bus.emit("route.matched", handler="session_switch")
            say(switch_phrase, blocking=True)
            if switch_agent:
                direct_agent = _set_direct(switch_agent)
                direct_session_label = REGISTRY.active_label()
                print(f"  entering direct-dialogue mode with {direct_agent}")
            last_activity = time.monotonic()
            continue

        if _is_session_query(cleaned):
            summary = _session_summary()
            print(f"  local session query -> {summary}")
            bus.emit("route.matched", handler="session_query")
            say(summary, blocking=True)
            last_activity = time.monotonic()
            continue

        if _desktop_last_error and _is_desktop_error_query(cleaned):
            print(f"  desktop error query -> {_desktop_last_error}")
            bus.emit("route.matched", handler="desktop_error_query")
            say(_desktop_last_error, blocking=True)
            last_activity = time.monotonic()
            continue

        # 7. Tool fast-path — always wins for "open chrome" style commands,
        #    even in direct-dialogue mode. Lets the user fire quick local
        #    actions without breaking the agent thread.
        if _is_bare_go(cleaned):
            print("  bare go phrase with no pending action -> asking for task")
            bus.emit("route.matched", handler="bare_go_no_action")
            say("What should I start?", blocking=True)
            last_activity = time.monotonic()
            continue

        from halo.tools import is_pure_tool
        if cleaned and is_pure_tool(cleaned):
            handled, summary, _ = execute_system_intent(cleaned)
            if handled:
                print(f"  tool fast-path: {summary}")
                bus.emit("route.matched", handler="tool", text=summary)
                say(summary, blocking=True)
                brief.add_halo(summary)  # remember what was opened ("what did you open first?")
                last_activity = time.monotonic()
                continue

        # 8. Direct-dialogue mode — pipe to active agent.
        if direct_agent is not None:
            if not cleaned:
                print("  direct-dialogue: empty after wake-strip, ignoring")
                continue

            # Cross-agent hand-off FIRST: if the user named the OTHER agent
            # with a targeting verb ("switch to Codex", "spawn Codex", "ask
            # Codex to ..."), switch/dispatch THERE instead of feeding the
            # current agent. Without this, "spawn Codex" got piped to Claude.
            redirect = _direct_redirect(cleaned, direct_agent)
            if redirect is not None:
                target, instruction = redirect
                direct_agent = _set_direct(target)
                direct_session_label = None
                if instruction:
                    print(f"  direct-mode redirect -> {target}: {instruction!r}")
                    bus.emit("route.matched", handler="direct_redirect", target=target)
                    phrase = _start_agent_and_ack(
                        target, instruction, cwd=_cwd_for_dispatch(""), brief=brief
                    )
                    if phrase:
                        say(phrase, blocking=True)
                else:
                    spoken = session_name(target)
                    print(f"  direct-mode switch -> {target} ({spoken})")
                    bus.emit("route.matched", handler="direct_switch", target=target)
                    say(f"Switching to {spoken}.", blocking=True)
                last_activity = time.monotonic()
                continue

            # Follow-up gate: every direct-mode utterance has to look
            # like it's addressed to the agent before we forward it.
            # Without this, anything the mic captures while you're
            # mid-session — phone calls, side conversations, the
            # colleague who just walked over — gets dispatched to
            # Claude as if it were a command. See halo/followup_gate.py
            # for the 4-rule decision logic.
            if FOLLOWUP_GATE_ENABLED:
                allow, reason = followup_gate_passes(cleaned, direct_agent)
                if not allow:
                    print(f"  [side-talk dropped: {reason}] {cleaned!r}")
                    bus.emit(
                        "side_convo.ignored",
                        text=cleaned,
                        reason=reason,
                        agent=direct_agent,
                    )
                    # Stay in direct mode. Don't reset last_activity —
                    # we don't want a phone call to keep the engaged
                    # window alive indefinitely; the existing 90s
                    # idle clock should still apply.
                    continue
                print(f"  [direct-dialogue gate: {reason}]")

            print(f"  direct-dialogue -> {direct_agent}")
            bus.emit("route.matched", handler="direct", target=direct_agent)
            desktop_phrase = _send_to_direct_desktop_session(
                direct_agent, cleaned, direct_session_label
            )
            if desktop_phrase is not None:
                print(f"  desktop-session send -> {desktop_phrase!r}")
                say(desktop_phrase, blocking=True)
                last_activity = time.monotonic()
                continue
            phrase = _start_agent_and_ack(direct_agent, cleaned, cwd=_cwd_for_dispatch(""), brief=brief)
            if phrase:
                say(phrase, blocking=True)
            last_activity = time.monotonic()
            continue

        # 9. Stage 2 LLM as last resort. Only fires for utterances that:
        #    - didn't name an agent (no vocative),
        #    - aren't a pure switch / status / tool / end phrase,
        #    - and aren't in direct mode.
        if not cleaned:
            continue
        print(f"  no local handler matched -> Stage 2 LLM")
        bus.emit("route.matched", handler="stage2_llm")
        t0 = time.monotonic()
        # v1.2 — inject discovered-sessions context so the brain can
        # emit target_session / session_action for multi-project
        # routing. None in single-session mode (empty registry).
        session_ctx = _build_session_context()
        # v1.3 — also inject the rolling conversation history so the brain can
        # resolve references and stop marking context-dependent fragments
        # "unclear".
        lm_decision = understand_and_route(
            cleaned, context=session_ctx, history=_brain_history(cleaned)
        )
        print(f"  stage 2: {(time.monotonic() - t0) * 1000:.0f}ms")

        # 9a. v1.2 — handle session_action BEFORE dispatch interpretation.
        #     The brain may have decided this turn is a meta-operation
        #     (switch / list / where_am_i) rather than a command for an
        #     agent. Speak the result, skip dispatch.
        if not lm_decision.get("_error"):
            session_phrase = _handle_session_action(lm_decision)
            if session_phrase is not None:
                print(f"  session action -> {lm_decision.get('session_action')!r}")
                say(session_phrase, blocking=True)
                if lm_decision.get("session_action") == "switch":
                    active_session = REGISTRY.active()
                    if active_session is not None:
                        direct_agent = _set_direct(active_session.agent_key)
                        direct_session_label = REGISTRY.active_label()
                        print(
                            "  entering direct desktop session "
                            f"{direct_session_label} with {direct_agent}"
                        )
                last_activity = time.monotonic()
                continue

        # 9b. Fallback: if Stage 2 failed (Ollama down / network error /
        #     model crash) AND the user mentioned an agent name — even
        #     a Whisper-mangled one like 'Cloud' for 'Claude' — dispatch
        #     to that agent directly instead of giving up with "Sorry,
        #     the router brain didn't respond." Saves the turn whenever
        #     the user intent was clear even though Ollama wasn't.
        if lm_decision.get("_error"):
            fallback_agent = _fuzzy_agent_match(cleaned)
            if fallback_agent:
                avail = check_availability().get(fallback_agent, {})
                if avail.get("installed") and avail.get("responsive"):
                    spoken = AGENTS[fallback_agent].spoken_name
                    print(f"  stage 2 unreachable -> fuzzy fallback to "
                          f"{fallback_agent} ({spoken})")
                    bus.emit(
                        "route.matched",
                        handler="stage2_fallback",
                        target=fallback_agent,
                    )
                    direct_agent = _set_direct(fallback_agent)
                    phrase = _start_agent_and_ack(fallback_agent, cleaned, brief=brief)
                    if phrase:
                        say(phrase, blocking=True)
                    last_activity = time.monotonic()
                    continue
                else:
                    print(f"  fuzzy-matched {fallback_agent} but it isn't "
                          f"connected; falling through to error message")

        _print_decision(lm_decision)

        if lm_decision.get("status") == "cancel":
            pending_action = None
            pending_clarification = None
            phrase = _handle_decision(lm_decision, brief=brief)
            if phrase:
                say(phrase, blocking=True)
            return

        if lm_decision.get("status") == "unclear":
            # If the brain can't make sense of it, it almost certainly wasn't a
            # real command — STAY SILENT and keep listening instead of nagging
            # with a verbose "I didn't understand" AND merging the fragment into
            # the next turn (the blob that misrouted "open Spotify"). The user
            # just restates the whole thing. Only nudge for a clear-but-vague
            # TASK (coding verb), with one short canned line.
            pending_action = None
            pending_clarification = None  # never merge fragments into a blob
            if _looks_like_real_task(cleaned) and len(cleaned.split()) >= 3:
                say("What would you like me to do?", blocking=True)
            else:
                print(f"  unclear / not a command -> ignoring: {cleaned!r}")
            last_activity = time.monotonic()
            continue

        if _needs_confirmation(lm_decision):
            pending_action = lm_decision
            pending_clarification = None
            question = _confirmation_question(lm_decision)
            print(f"  confirmation required -> {question!r}")
            bus.emit("route.matched", handler="confirm_action")
            say(question, blocking=True)
            last_activity = time.monotonic()
            continue

        phrase = _handle_decision(lm_decision, brief=brief)
        if phrase:
            print(f"  speaking: {phrase!r}")
            say(phrase, blocking=True)
            brief.add_halo(phrase)

        # A chained command did its local parts but left a task only an agent
        # can do ("open a browser AND search the web for the score"). Per the
        # "confirm before Claude" policy, offer to delegate the remainder
        # instead of silently spawning a session — a "yes" hands it off.
        if _system_leftover:
            leftover = _system_leftover
            _system_leftover = ""
            pending_action = {
                "status": "ready",
                "intent": "code",
                "agent": "claude_code" if "claude_code" in AGENTS else "none",
                "cleaned_text": leftover,
                "confirmation": "",
                "clarification": "",
                "target_session": lm_decision.get("target_session", ""),
            }
            offer = "I can't do that part myself — want me to put Claude on it?"
            print(f"  offering to delegate leftover -> {leftover!r}")
            bus.emit("route.matched", handler="offer_delegate", text=leftover)
            say(offer, blocking=True)
            brief.add_halo(offer)
            last_activity = time.monotonic()
            continue

        # Auto-enter direct-dialogue with whichever agent Stage 2 picked.
        if lm_decision.get("status") == "ready":
            agent_dispatched = lm_decision.get("agent")
            if agent_dispatched in AGENTS:
                direct_agent = _set_direct(agent_dispatched)
                direct_session_label = None
                print(f"  entering direct-dialogue mode with {direct_agent}")

        last_activity = time.monotonic()


def main() -> None:
    # On Windows, Python's default SIGINT handling can stall behind
    # sounddevice's C callbacks; install the default handler explicitly.
    signal.signal(signal.SIGINT, signal.default_int_handler)

    # Web dashboard — daemon thread, dies with the process. Start it
    # first so it's already serving by the time the heavy models load
    # and the user opens the URL.
    try:
        url = start_web_server()
        print(f"Dashboard:  {url}")
    except Exception as exc:
        print(f"Dashboard failed to start: {exc}")

    # v1.2 multi-session discovery — start the background scanner so
    # the brain sees other running Claude/Codex sessions on the machine.
    # Silently skipped when psutil isn't installed (single-session
    # fallback identical to v1.1).
    global _DISCOVERY
    if discovery_available():
        _DISCOVERY = DiscoveryThread(on_change=_on_discovery_change)
        _DISCOVERY.start()
        initial = _DISCOVERY.snapshot()
        REGISTRY.update(initial)
        print(
            f"discovery: found {len(initial)} agent session"
            f"{'s' if len(initial) != 1 else ''} on this machine"
        )
        for s in initial:
            print(f"  - {s}")
        if not initial:
            # Auto-diagnose so the user can see WHY nothing was found without
            # needing to set HALO_DISCOVERY_DEBUG.
            from halo.discovery import diagnose
            diagnose()
    else:
        print("discovery: psutil not installed — running single-session mode")

    # Persistent memory (cross-session). Loads/prunes the SQLite store.
    _init_memory()

    # Pre-load every heavy thing so the first turn doesn't pay cold-start.
    _get_model()
    preload_audio_models()
    preload_router()
    preload_voice()
    print(f"Halo ready. Say the wake word ('{WAKE_WORD}') to start. (Ctrl+C to stop)\n")

    # Probe which coding agents are actually connected and announce them
    # — the user gets immediate audible confirmation that Claude/Codex
    # are wired (or that they aren't). Cached so /api/state polls don't
    # re-fork `--version` every 750 ms.
    if voice_available():
        avail = available_agents()
        sess_count = len(REGISTRY.list())
        sess_tail = (
            f" {sess_count} session{'s' if sess_count != 1 else ''} discovered."
            if sess_count else ""
        )
        if not avail:
            say(
                f"Halo online. No coding agents connected. Run halo doctor.{sess_tail}",
                blocking=False,
            )
        elif len(avail) == 1:
            say(f"Halo online. {avail[0]} is connected.{sess_tail}", blocking=False)
        elif len(avail) == 2:
            say(f"Halo online. {avail[0]} and {avail[1]} are connected.{sess_tail}",
                blocking=False)
        else:
            joined = ", ".join(avail[:-1]) + f", and {avail[-1]}"
            say(f"Halo online. Connected: {joined}.{sess_tail}", blocking=False)
    print(f"agents: {', '.join(available_agents()) or '(none connected)'}")

    try:
        while True:
            wake_score = listen_for_wake()
            # Second-stage gate: confirm the wake word with STT before opening
            # a conversation, so a DNN false-fire on ordinary speech doesn't
            # wake Halo (issue #1). The fire score is a prior — a strong fire is
            # trusted even if STT garbles it. Rejected -> back to listening.
            if not _wake_confirmed_by_stt(wake_score):
                print("  (false wake rejected -- back to listening)\n")
                continue
            print("\nwake word detected -- entering conversation\n")
            bus.emit("convo.entered")
            say("I'm here.", blocking=True)
            run_conversation()
            bus.emit("convo.exited")
            print("conversation ended -- back to wake-listening\n")
    except KeyboardInterrupt:
        print("\nstopped")
        voice_stop()
    finally:
        if _DISCOVERY is not None:
            _DISCOVERY.stop()


if __name__ == "__main__":
    # `python -m halo` -> voice loop (unchanged). `python -m halo <cmd>` ->
    # route through the CLI so subcommands (calibrate, doctor, ...) work the
    # same as the installed `halo` console script.
    import sys as _sys
    if len(_sys.argv) > 1:
        from halo.cli import main as _cli_main
        _sys.exit(_cli_main(_sys.argv[1:]))
    main()
