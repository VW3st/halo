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

from halo.agents import (
    AGENTS,
    AgentBusy,
    active_jobs,
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
from halo.config import CONVERSATION_IDLE_SEC
from halo.record import preload_models as preload_audio_models
from halo.router import preload_router, understand_and_route
from halo.tools import execute_system_intent
from halo.turn import run_turn
from halo.voice import is_available as voice_available
from halo.voice import preload_voice, say
from halo.voice import stop as voice_stop
from halo.wake import WAKE_WORD, _get_model, listen_for_wake

# Phrases that close the conversation and send us back to wake-listening.
# Matched anywhere in the cleaned transcript (case-insensitive, word boundaries).
_END_CONVERSATION_RE = re.compile(
    r"\b("
    r"over and out|over n out|"
    r"go to sleep|go back to sleep|"
    r"stop listening|stop conversation|end conversation|"
    r"good ?bye|bye bye|see you|"
    r"that'?s all|that will be all|i'?m done|done for now|"
    r"thanks that'?s all|"
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
    r"talk to me|hand it back)\b",
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
    """Speak + print any finished agent jobs. Call between turns so we
    don't talk over the user."""
    for job in completed_unconsumed_jobs():
        cfg = AGENTS[job.agent_key]
        spoken_name = session_name(cfg.key)
        _print_full_result(
            f"{cfg.key} / {spoken_name}", bool(job.ok), job.result, job.elapsed_sec
        )
        if job.ok:
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


def _start_agent_and_ack(agent_key: str, prompt: str) -> str:
    """Spawn a background job, return the voice-ack to speak."""
    if not (prompt or "").strip():
        # Belt-and-braces — callers should already guard, but never
        # dispatch a blank prompt to an agent. Claude / Codex both
        # error on empty input.
        return ""
    config = AGENTS[agent_key]
    spoken = session_name(agent_key)
    active = "continuing" if session_status()[agent_key] else "starting"
    print(f"  dispatching to {spoken} / {config.spoken_name.lower()} "
          f"({active} session): {prompt!r}")
    try:
        job = start_job(agent_key, prompt)
        print(f"  job #{job.job_id} started in background")
        if active == "starting":
            return f"On it. I'm calling this session {spoken}. I'll let you know."
        return f"On it. {spoken} is working. I'll let you know."
    except AgentBusy as exc:
        return str(exc)


def _handle_decision(decision: dict) -> str:
    """Act on a decision dict. Returns the short phrase to speak back."""
    status = decision.get("status", "?")
    intent = decision.get("intent", "?")
    agent = decision.get("agent", "none")
    cleaned = decision.get("cleaned_text", "")
    confirmation = decision.get("confirmation", "")
    clarification = decision.get("clarification", "")

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
        print(f"  no tool matched {cleaned!r}")
        return "I don't know how to do that yet."

    # Registry-driven dispatch — non-blocking. Job runs in background;
    # _drain_completed_jobs() between turns will speak the result.
    if agent in AGENTS:
        return _start_agent_and_ack(agent, cleaned)

    # question or other agent=none ready intents
    return confirmation or "Okay."


def run_conversation() -> None:
    """Loop one or more turns without requiring re-wake.

    Routing priority — first match wins, Stage 2 LLM is the LAST resort:

      1. end-phrase                -> goodbye, return
      2. new-session phrase        -> reset, continue
      3. vocative dispatch         -> "Claude, X" / "Codex, X" -> direct
      4. pure mode-switch          -> "switch to codex" -> direct mode
      5. back-to-halo              -> exit direct mode
      6. status query / replay     -> registry, no LLM
      7. tool fast-path            -> open chrome, calculator, etc.
      8. direct-dialogue active    -> straight to current agent (no LLM)
      9. Stage 2 LLM (fallback)    -> only if nothing above matched AND
                                      we're not in direct mode

    Mode auto-enters DIRECT_<agent> the moment a job for that agent
    starts. User exits with "back to halo" or "talk to me".
    """
    last_activity = time.monotonic()
    direct_agent: str | None = None

    while True:
        # Speak any background-job results that landed since last turn.
        _drain_completed_jobs()

        # Idle timeout — extend whenever a job is in flight.
        idle = time.monotonic() - last_activity
        if idle > CONVERSATION_IDLE_SEC:
            if active_jobs():
                last_activity = time.monotonic()
            else:
                print(f"\nconversation idle for {CONVERSATION_IDLE_SEC:.0f}s -> sleeping")
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
        cleaned = (decision.get("cleaned_text") or "").strip()
        print(f"  heard: {cleaned!r}")

        # 1. End phrase
        if _is_end_phrase(cleaned):
            print("  end-phrase -> closing conversation")
            say("Goodbye.", blocking=True)
            return

        # 2. New-session phrase
        if _is_new_session_phrase(cleaned):
            print("  reset-session phrase -> dropping agent continuation")
            reset_session()
            direct_agent = None
            say("Fresh session.", blocking=True)
            continue

        # 3. Vocative dispatch — "Claude, X" / "Codex, X". Goes straight
        #    to the named agent. Saves the 3-5 s Stage 2 LLM round-trip.
        voc_agent, voc_text = _vocative_dispatch(cleaned)
        if voc_agent and voc_text:
            print(f"  vocative dispatch -> {voc_agent}: {voc_text!r}")
            direct_agent = voc_agent
            phrase = _start_agent_and_ack(voc_agent, voc_text)
            if phrase:
                say(phrase, blocking=True)
            last_activity = time.monotonic()
            continue

        # 4. Pure mode-switch ("switch to codex" with no instruction after)
        switch_target = _agent_switch_target(cleaned)
        if switch_target is not None:
            direct_agent = switch_target
            spoken = session_name(switch_target)
            print(f"  pure switch -> {switch_target} ({spoken})")
            say(f"Switching to {spoken}.", blocking=True)
            continue

        # 5. Back to Halo
        if _wants_back_to_halo(cleaned):
            if direct_agent:
                print(f"  leaving direct dialogue with {direct_agent}")
            direct_agent = None
            say("Halo here.", blocking=True)
            continue

        # 6. Status / replay
        if _is_status_query(cleaned):
            summary = status_summary()
            print(f"  status: {summary}")
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
                say(summary, blocking=True)
                continue

        # 8. Direct-dialogue mode — pipe to active agent.
        if direct_agent is not None:
            if not cleaned:
                print("  direct-dialogue: empty after wake-strip, ignoring")
                continue
            print(f"  direct-dialogue -> {direct_agent}")
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
        t0 = time.monotonic()
        lm_decision = understand_and_route(cleaned)
        print(f"  stage 2: {(time.monotonic() - t0) * 1000:.0f}ms")
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
                direct_agent = agent_dispatched
                print(f"  entering direct-dialogue mode with {direct_agent}")

        last_activity = time.monotonic()


def main() -> None:
    # On Windows, Python's default SIGINT handling can stall behind
    # sounddevice's C callbacks; install the default handler explicitly.
    signal.signal(signal.SIGINT, signal.default_int_handler)

    # Pre-load every heavy thing so the first turn doesn't pay cold-start.
    _get_model()
    preload_audio_models()
    preload_router()
    preload_voice()
    print(f"Halo ready. Say the wake word ('{WAKE_WORD}') to start. (Ctrl+C to stop)\n")
    if voice_available():
        say("Halo online.", blocking=False)

    try:
        while True:
            listen_for_wake()
            print("\nwake word detected -- entering conversation\n")
            say("I'm here.", blocking=True)
            run_conversation()
            print("conversation ended -- back to wake-listening\n")
    except KeyboardInterrupt:
        print("\nstopped")
        voice_stop()


if __name__ == "__main__":
    main()
