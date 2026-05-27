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
)
from halo.discovery import DiscoveryThread, is_available as discovery_available
from halo.followup_gate import passes as followup_gate_passes
from halo.record import preload_models as preload_audio_models
from halo.registry import SessionRegistry
from halo.router import SessionContext, preload_router, understand_and_route
from halo.tools import execute_system_intent
from halo.turn import run_turn
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


def _cwd_for_dispatch(target_session: str) -> Path:
    """Resolve target_session (label / 'active' / 'focused' / '' ) into a Path.

    Falls back to DEFAULT_CWD (Halo's launch dir) when no registry match —
    matches v1.1 single-session behaviour. 'all' is handled separately by
    the orchestrator (multi-dispatch), not here.
    """
    if not target_session or target_session in ("active", "focused"):
        active = REGISTRY.active()
        if active is not None:
            return Path(active.cwd)
        return DEFAULT_CWD
    sess = REGISTRY.by_label(target_session)
    if sess is not None:
        return Path(sess.cwd)
    # Fuzzy fallback — brain may have emitted a slightly off label.
    resolved = REGISTRY.resolve(target_session)
    if resolved.kind == "session" and resolved.label is not None:
        sess = REGISTRY.by_label(resolved.label)
        if sess is not None:
            return Path(sess.cwd)
    return DEFAULT_CWD


# Phrases that close the conversation and send us back to wake-listening.
# Matched anywhere in the cleaned transcript (case-insensitive, word boundaries).
_END_CONVERSATION_RE = re.compile(
    r"\b("
    r"over and out|over n out|"
    r"go to sleep|go back to sleep|"
    r"stop listening|stop conversation|end conversation|end session|"
    r"end of session|session end|that'?s enough|that'?s it|enough|"
    r"good ?bye|bye bye|see you|"
    r"that'?s all|that will be all|i'?m done|done for now|"
    r"thanks that'?s all|"
    r"stand by|standby|"
    r"shut up|leave me alone"
    r")\b",
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


def _wants_back_to_halo(text: str) -> bool:
    return bool(_BACK_TO_HALO_RE.search(text or ""))


def _is_end_phrase(text: str) -> bool:
    return bool(_END_CONVERSATION_RE.search(text or ""))


def _is_new_session_phrase(text: str) -> bool:
    return bool(_NEW_SESSION_RE.search(text or ""))


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


def _drain_completed_jobs() -> None:
    """Print finished agent jobs to stdout, and speak a "done" cap.

    For streaming agents (Claude), the response itself was already
    spoken sentence-by-sentence as it generated; we just say a short
    "Mercury is done." cap so the user knows the agent is no longer
    working. For batch agents (Codex), we speak the full summary now.

    v1.2: spoken_name and label are per-(agent, cwd) so the cap
    correctly says "Mercury in website is done." when relevant.
    """
    for job in completed_unconsumed_jobs():
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
        if job.ok:
            if cfg.streams_text_deltas:
                # Streaming agent already spoke its full response via the
                # on_text_chunk callback during the job. Adding "X is done."
                # on top was just noise — the user can hear the response
                # ended (TTS playback finished). Skip the cap entirely.
                pass
            else:
                short = summarize_for_speech(job.result) or f"{spoken_name} is done."
                say(f"{spoken_name} says, {short}", blocking=True)
        else:
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

    def _speak_chunk(sentence: str) -> None:
        # Non-blocking so a long monologue queues sentence-by-sentence
        # without stalling the agent's stdout reader thread. The
        # sanitizer inside voice.say() strips any markdown the agent
        # leaks despite the voice system prompt.
        print(f"    [{config.key}{where} -> tts] {sentence!r}")
        say(sentence, blocking=False)

    on_chunk = _speak_chunk if config.streams_text_deltas else None

    try:
        job = start_job(agent_key, prompt, cwd=workdir, on_text_chunk=on_chunk)
        print(f"  job #{job.job_id} started in background"
              f"{' (streaming)' if config.streams_text_deltas else ''}")
        if active == "starting":
            if project_tag:
                return f"On it. {spoken} is starting in {project_tag}."
            return f"On it. I'm calling this session {spoken}."
        return f"On it. {spoken}{where} is working."
    except AgentBusy as exc:
        return str(exc)


def _handle_session_action(decision: dict) -> str | None:
    """v1.2: handle brain-emitted session_action / target_session.

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
                return decision.get("confirmation") or f"Switched to {resolved.label}."
        return f"I don't see a session called {target}."

    return None


def _dispatch_to_all(prompt: str, agent_hint: str) -> str:
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
            _start_agent_and_ack(agent_key, prompt, cwd=Path(s.cwd))
            fired += 1
        except Exception as exc:
            print(f"  fanout error for {s.label}: {exc}")
    if fired == 0:
        return "Nothing to dispatch."
    return f"Sent to {fired} session{'s' if fired != 1 else ''}."


def _handle_decision(decision: dict) -> str:
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
        print("  (chitchat — staying silent)")
        return ""

    if status == "cancel":
        return confirmation or "Cancelled."

    if status == "unclear":
        return clarification or "Can you say that again?"

    if status != "ready":
        return ""

    # status == "ready"
    if intent == "system":
        handled, summary = execute_system_intent(cleaned)
        if handled:
            print(f"  executed: {summary}")
            return summary
        # No local tool matched. The Stage 2 LLM was confident this was
        # a "system" intent (probably because the utterance started with
        # an "open ..." verb), but none of our handlers cover it. Two
        # paths lead here:
        #   1. Mixed intent like "open hallo.html and change the colors"
        #      where the open-file split couldn't pull the filename out
        #      of mid-sentence AND the rest is a coding task. Claude can
        #      do both: Read the file, Edit it, and report back.
        #   2. Truly unknown ("open the dishwasher") — Claude politely
        #      explains it can't help.
        # Falling through to Claude beats a flat "I don't know" refusal.
        print(f"  no tool matched {cleaned!r} -> falling through to claude_code")
        cwd = _cwd_for_dispatch(target_session)
        return _start_agent_and_ack("claude_code", cleaned, cwd=cwd)

    # Multi-session fan-out — brain says target_session='all'.
    if target_session == "all":
        return _dispatch_to_all(cleaned, agent_hint=agent if agent in AGENTS else "claude_code")

    # Registry-driven dispatch — non-blocking. Job runs in background;
    # _drain_completed_jobs() between turns will speak the result.
    if agent in AGENTS:
        cwd = _cwd_for_dispatch(target_session)
        return _start_agent_and_ack(agent, cleaned, cwd=cwd)

    # question or other agent=none ready intents
    return confirmation or "Okay."


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
    # Toggled True the first time the user actually says something we
    # process (any non-None decision). The idle budget jumps from 5s
    # to CONVERSATION_IDLE_ENGAGED_SEC (90s) once engaged, so a single
    # spoken command no longer cuts the conversation short the moment
    # Halo finishes its reply.
    engaged = False

    while True:
        # Speak any background-job results that landed since last turn.
        _drain_completed_jobs()

        # Don't open the mic while Halo is still speaking back — it'd
        # pick up echo, fire false "no speech detected" turns, and eat
        # the idle budget the user needs to actually reply to a question.
        # Reset last_activity once Halo goes silent so the idle clock
        # starts from "user could begin talking now", not from when the
        # user last spoke (which may have been before a 10s agent reply).
        if voice_is_speaking():
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
            if active_jobs() or voice_is_speaking() or direct_agent is not None:
                last_activity = time.monotonic()
            else:
                print(f"\nconversation idle for {idle_limit:.0f}s -> sleeping")
                say("Going back to sleep.", blocking=True)
                return

        # Poll faster (2 s) while agents are running so completions land
        # quickly; normal 5 s otherwise.
        first_wait = 2.0 if active_jobs() else None

        # Always skip Stage 2 inside run_turn — every routing decision
        # is made HERE, in priority order, so we only spend the LLM
        # roundtrip on utterances that genuinely need it.
        decision = run_turn(skip_routing=True, max_wait_first_speech_sec=first_wait)
        if decision is None:
            continue

        last_activity = time.monotonic()
        engaged = True
        cleaned = (decision.get("cleaned_text") or "").strip()
        print(f"  heard: {cleaned!r}")

        # 1. End phrase
        if _is_end_phrase(cleaned):
            print("  end-phrase -> closing conversation")
            bus.emit("route.matched", handler="end_phrase")
            say("Goodbye.", blocking=True)
            return

        # 2. New-session phrase
        if _is_new_session_phrase(cleaned):
            print("  reset-session phrase -> dropping agent continuation")
            bus.emit("route.matched", handler="new_session")
            reset_session()
            direct_agent = _set_direct(None)
            bus.emit("session.reset")
            say("Fresh session.", blocking=True)
            continue

        # 3. Vocative dispatch — "Claude, X" / "Codex, X". Goes straight
        #    to the named agent. Saves the 3-5 s Stage 2 LLM round-trip.
        voc_agent, voc_text = _vocative_dispatch(cleaned)
        if voc_agent and voc_text:
            print(f"  vocative dispatch -> {voc_agent}: {voc_text!r}")
            bus.emit("route.matched", handler="vocative", target=voc_agent)
            direct_agent = _set_direct(voc_agent)
            phrase = _start_agent_and_ack(voc_agent, voc_text)
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
            phrase = _start_agent_and_ack(verb_agent, verb_text)
            if phrase:
                say(phrase, blocking=True)
            last_activity = time.monotonic()
            continue

        # 4. Pure mode-switch ("switch to codex" with no instruction after)
        switch_target = _agent_switch_target(cleaned)
        if switch_target is not None:
            direct_agent = _set_direct(switch_target)
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
            bus.emit("route.matched", handler="back_to_halo")
            say("Halo here.", blocking=True)
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

        # 7. Tool fast-path — always wins for "open chrome" style commands,
        #    even in direct-dialogue mode. Lets the user fire quick local
        #    actions without breaking the agent thread.
        from halo.tools import is_pure_tool
        if cleaned and is_pure_tool(cleaned):
            handled, summary = execute_system_intent(cleaned)
            if handled:
                print(f"  tool fast-path: {summary}")
                bus.emit("route.matched", handler="tool", text=summary)
                say(summary, blocking=True)
                continue

        # 8. Direct-dialogue mode — pipe to active agent.
        if direct_agent is not None:
            if not cleaned:
                print("  direct-dialogue: empty after wake-strip, ignoring")
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
            phrase = _start_agent_and_ack(direct_agent, cleaned)
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
        lm_decision = understand_and_route(cleaned, context=session_ctx)
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
                    phrase = _start_agent_and_ack(fallback_agent, cleaned)
                    if phrase:
                        say(phrase, blocking=True)
                    last_activity = time.monotonic()
                    continue
                else:
                    print(f"  fuzzy-matched {fallback_agent} but it isn't "
                          f"connected; falling through to error message")

        _print_decision(lm_decision)

        if lm_decision.get("status") == "cancel":
            phrase = _handle_decision(lm_decision)
            if phrase:
                say(phrase, blocking=True)
            return

        phrase = _handle_decision(lm_decision)
        if phrase:
            print(f"  speaking: {phrase!r}")
            say(phrase, blocking=True)

        # Auto-enter direct-dialogue with whichever agent Stage 2 picked.
        if lm_decision.get("status") == "ready":
            agent_dispatched = lm_decision.get("agent")
            if agent_dispatched in AGENTS:
                direct_agent = _set_direct(agent_dispatched)
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
    else:
        print("discovery: psutil not installed — running single-session mode")

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
            listen_for_wake()
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
    main()
