"""Routing brain — two stages.

Stage 1: turn-completion — "has the user finished their thought?" Now
         PURE RULES (check_turn_complete), ~0 ms. The old LLM classifier cost
         2-4s from KV-cache thrash and was removed.

Stage 2: full understanding + routing (understand_and_route). One
         JSON-schema-constrained call to the configured brain (local Ollama
         or cloud OpenRouter) when the turn ends. Structured output means we
         never have to defend against malformed JSON.

v1.2 adds **session context injection**: when Halo has discovered other
running Claude/Codex sessions on the machine, every Stage 2 call gets a
CURRENT CONTEXT block listing them. The schema gains `target_session`
and `session_action` fields so the brain can route to a specific
discovered session, switch the active one, or list what's running —
without us having to write more orchestrator regex.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Optional

import ollama

from halo import bus
from halo import prompts
from halo.config import (
    OLLAMA_HOST,
    OLLAMA_MODEL,
    OPENROUTER_API_KEY_ENV,
    OPENROUTER_APP_NAME,
    OPENROUTER_BASE_URL,
    OPENROUTER_CHAT_MODEL,
    OPENROUTER_MODEL,
    OPENROUTER_SITE_URL,
    OPENROUTER_TIMEOUT_SEC,
    ROUTER_BACKEND,
)

# Keep the model resident in VRAM as long as the process is alive —
# 30 min wasn't enough; we saw 11s cold reloads mid-session. 24h
# basically means "never unload unless we crash or you pull the model".
_KEEP_ALIVE = "24h"

# Deterministic fast-path: catch obvious patterns without a model call.
# In real usage Stage 1 latency is dominated by KV-cache flipping
# between Stage 1/Stage 2 prompts (2-4 s cold), so the more we can
# decide here the snappier the loop feels.

_TRAILING_INCOMPLETE_RE = re.compile(
    r"\b("
    # conjunctions / prepositions / determiners / fillers — always a fragment
    r"and|but|or|so|because|with|to|for|in|on|at|of|the|a|an|my|your|this|that|"
    r"these|those|some|any|um|uh|er|ah|like|well|hmm|actually|okay|right|"
    r"alright|now|"
    # "thinking out loud" tails — a person mid-sentence ("what I need to do
    # is", "I'm going to", "I want to"). These almost never end a finished
    # command, so extending the turn (one ~2s window) lets the user finish
    # instead of being cut off and cascading into 'unclear'. Deliberately
    # excludes imperatives (build/run/do) that DO end real commands.
    r"is|are|was|were|am|be|need|needs|want|wants|going|gonna|wanna|trying|"
    r"exactly|really|just|literally|about|regarding|called|named"
    r")[\s.,!?]*$",
    re.IGNORECASE,
)

# Single-word commands that are unambiguously COMPLETE.
_SHORT_COMPLETE_WORDS = {
    "cancel", "stop", "go", "yes", "no", "okay", "ok", "done",
    "exit", "quit", "pause", "resume", "continue", "help",
    "thanks", "thank you", "never mind", "nevermind",
}

# Ends in a sentence-terminator -> almost always COMPLETE.
_HARD_TERMINATOR_RE = re.compile(r"[?!](?:\s|$)|[.](?:\s+\S|\s*$)")

# ---------------------------------------------------------------------------
# Stage 2 — understanding + routing
# ---------------------------------------------------------------------------
# Lean Stage-2 prompt. An older ~400-line prompt was re-prefilled UNCACHED on
# every call, which on a 1.5B was the dominant cause of the 5-9s latency. This
# trimmed version + a 4-field schema cuts prefill and constrained-decode
# dramatically. The session-routing block is appended ONLY when sessions are
# discovered.
STAGE2_PROMPT = """You are Halo, a voice router for AI coding agents (Claude Code, Codex CLI). Turn ONE transcribed voice command into a small JSON object. Output JSON only, no prose.

Fix speech-to-text errors: claud/clod/clawed->Claude, codecs->Codex, get hub->GitHub, type script->TypeScript, java script->JavaScript, next js->Next.js, super base->Supabase, post grass->Postgres, o llama->Ollama. Drop filler (um, uh, like, you know). If the transcript OPENS with a stray greeting (hello, hey, hi, okay) from a misheard wake word, IGNORE it and route the real command after it.

Put the lightly-cleaned command in cleaned_text. Do NOT rephrase, expand, summarize, or invent details (files, frameworks, libraries, databases) the user did not say — copy their words.

status: ready (clear, complete, actionable) | unclear (truly ambiguous or missing essential info) | chitchat (greeting / accidental wake / not a command) | cancel (abort)
intent: code (write/edit/build/debug/run/deploy) | question (ask something) | system (control Halo, or "open <app>", or date/time) | cancel
agent: claude_code (default for coding) | codex_cli (ONLY if the user names Codex or OpenAI) | none (questions, system, chitchat). NEVER swap the named agent.

Rules: "open <app>" / "launch <x>" / "what time/date is it" => ready, intent=system or question, agent=none (real commands, not chitchat). A normal imperative is "ready", not "unclear". Only use "unclear" when you genuinely can't tell what's wanted.

Examples:
"um build me a login page" -> {"status":"ready","cleaned_text":"Build me a login page","intent":"code","agent":"claude_code"}
"ask codex to refactor the auth module" -> {"status":"ready","cleaned_text":"Refactor the auth module","intent":"code","agent":"codex_cli"}
"hello, open chrome" -> {"status":"ready","cleaned_text":"Open Chrome","intent":"system","agent":"none"}
"what's the date and time" -> {"status":"ready","cleaned_text":"What's the date and time","intent":"question","agent":"none"}
"fix the bug" -> {"status":"unclear","cleaned_text":"Fix the bug","intent":"code","agent":"claude_code"}
"hey how are you" -> {"status":"chitchat","cleaned_text":"How are you","intent":"question","agent":"none"}
"what is happening my friend" -> {"status":"chitchat","cleaned_text":"What is happening my friend","intent":"question","agent":"none"}
"i'm working on something amazing" -> {"status":"chitchat","cleaned_text":"I'm working on something amazing","intent":"question","agent":"none"}
"what do you think about that" -> {"status":"chitchat","cleaned_text":"What do you think about that","intent":"question","agent":"none"}
"stop" -> {"status":"cancel","cleaned_text":"Stop","intent":"cancel","agent":"none"}

A casual remark, opinion, feeling, or open-ended question that is NOT a tool command and NOT a coding task is "chitchat" — never "unclear". Only use "unclear" for a real command missing essential detail.

If a "# RECENT CONVERSATION" block is present, use it to resolve references ("it", "the other one", "do that again") and fill in obvious context, then route ONLY the USER TRANSCRIPT line. A short fragment that the recent conversation makes clear is "ready", not "unclear".
"""

# Appended to STAGE2_PROMPT only when discovered sessions exist. Adds the
# two extra fields target_session + session_action.
STAGE2_SESSION_BLOCK = """
# MULTI-SESSION
Other Claude/Codex sessions are running (listed in the CURRENT CONTEXT block).
Also set:
- target_session: a discovered label to dispatch there | "active" (default) | "all" (fan out) | "" (default routing). Resolve noisy labels to the closest discovered label ("the website one" -> "website").
- session_action: "switch" (change active session; put label in target_session) | "list_sessions" (user asked what's running) | "where_am_i" (user asked which is active) | "" (normal dispatch).
"in <label>, do X" / "ask the <label> one to Y" -> target_session=<label>, session_action="".
Examples:
"switch to website" -> {"status":"ready","cleaned_text":"Switch to website","intent":"system","agent":"none","target_session":"website","session_action":"switch"}
"what sessions do I have" -> {"status":"ready","cleaned_text":"What sessions do I have","intent":"question","agent":"none","target_session":"","session_action":"list_sessions"}
"in website add dark mode" -> {"status":"ready","cleaned_text":"Add dark mode","intent":"code","agent":"claude_code","target_session":"website","session_action":""}
"""

# JSON schema passed to Ollama's `format` parameter so the model is
# decode-time constrained to valid output — no JSON parsing errors,
# no missing fields.
#
# v1.2 additions:
#   target_session  — label of a discovered session to dispatch to, OR
#                     one of the pseudo-targets "active" / "focused" /
#                     "all", OR empty string to use the default routing.
#   session_action  — when set, this turn is a meta-operation on the
#                     registry rather than a normal agent dispatch:
#                       "switch"          — change active session
#                       "list_sessions"   — speak what's running
#                       "where_am_i"      — speak the active session
#                       ""                — none, behave normally
ROUTE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["ready", "unclear", "chitchat", "cancel"]},
        "cleaned_text": {"type": "string"},
        "intent": {"type": "string", "enum": ["code", "question", "system", "cancel"]},
        "agent": {"type": "string", "enum": ["claude_code", "codex_cli", "none"]},
        "confirmation": {"type": "string"},
        "clarification": {"type": "string"},
        "target_session": {"type": "string"},
        "session_action": {
            "type": "string",
            "enum": ["", "switch", "list_sessions", "where_am_i"],
        },
    },
    "required": [
        "status", "cleaned_text", "intent", "agent",
        "confirmation", "clarification",
        "target_session", "session_action",
    ],
}

# Lean schema for the common single-session case: only the four fields the
# orchestrator actually needs from the model. Fewer constrained output
# tokens = markedly faster decode. confirmation/clarification are synthesized
# locally; session fields default to "".
ROUTE_SCHEMA_LEAN: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["ready", "unclear", "chitchat", "cancel"]},
        "cleaned_text": {"type": "string"},
        "intent": {"type": "string", "enum": ["code", "question", "system", "cancel"]},
        "agent": {"type": "string", "enum": ["claude_code", "codex_cli", "none"]},
    },
    "required": ["status", "cleaned_text", "intent", "agent"],
}

_FALLBACK_DECISION = {
    "status": "unclear",
    "cleaned_text": "",
    "intent": "question",
    "agent": "none",
    "confirmation": "",
    "clarification": "Sorry, the router brain didn't respond. Try again.",
    "target_session": "",
    "session_action": "",
}


@dataclass(frozen=True)
class SessionContext:
    """Snapshot of session state passed into Stage 2 for every turn.

    `active_label` is what Halo currently considers the default target;
    `discovered` is the list of {label, cwd, agent_kind} dicts from the
    registry. When `discovered` is empty, Stage 2 behaves as in v1.1
    (single-session, no target_session output).
    """

    active_label: Optional[str]
    discovered: list[dict]  # each: {"label", "cwd", "agent"}
    focused_label: Optional[str] = None  # Phase 2 (focus binding) — unused for now


def _format_context_block(ctx: Optional[SessionContext]) -> str:
    """Render the SessionContext into the prefix block we prepend to the
    user's transcript before Stage 2.

    Returns "" when there's no context worth providing (single-session
    mode or registry empty) — keeps the prompt token-cheap on the happy
    path."""
    if ctx is None:
        return ""
    if not ctx.discovered and ctx.active_label is None:
        return ""

    lines = ["# CURRENT CONTEXT (you decide target_session from this)"]
    if ctx.active_label:
        lines.append(f"active_session: {ctx.active_label}")
    else:
        lines.append("active_session: (none)")
    if ctx.focused_label:
        lines.append(f"focused_terminal_session: {ctx.focused_label}")
    if ctx.discovered:
        lines.append("discovered_sessions:")
        for s in ctx.discovered:
            lines.append(
                f"  - label: {s.get('label')}  "
                f"agent: {s.get('agent')}  "
                f"cwd: {s.get('cwd')}"
            )
    else:
        lines.append("discovered_sessions: (none — only the active session is targetable)")
    lines.append("")  # blank line between context and the user's actual transcript
    return "\n".join(lines)

_client: ollama.Client | None = None


def _get_client() -> ollama.Client:
    global _client
    if _client is None:
        _client = ollama.Client(host=OLLAMA_HOST)
    return _client


def _chat_json_for(
    provider: str,
    *,
    messages: list[dict[str, str]],
    schema: dict[str, Any],
    max_tokens: int,
    temperature: float,
) -> dict[str, Any]:
    """One structured-output call to a SPECIFIC provider."""
    if provider == "openrouter":
        return _chat_json_openrouter(
            messages=messages,
            schema=schema,
            max_tokens=max_tokens,
            temperature=temperature,
        )
    if provider != "ollama":
        raise RuntimeError(f"unknown router provider {provider!r}")
    response = _get_client().chat(
        model=OLLAMA_MODEL,
        messages=messages,
        format=schema,
        options={"temperature": temperature, "num_predict": max_tokens},
        keep_alive=_KEEP_ALIVE,
    )
    return json.loads(response["message"]["content"])


def _fallback_provider(primary: str) -> str | None:
    """The other brain provider to try if `primary` fails — cloud<->local. Keeps
    Halo working when a paid key is wrong or a local daemon is down instead of
    degrading straight to an 'unclear' fallback. None when the alternate can't
    work (e.g. ollama->openrouter with no API key configured)."""
    if primary == "openrouter":
        return "ollama"  # local; may be down, but worth a try before giving up
    if primary == "ollama":
        if os.environ.get(OPENROUTER_API_KEY_ENV, "").strip() and OPENROUTER_MODEL:
            return "openrouter"
        return None
    return None


def _chat_json(
    *,
    messages: list[dict[str, str]],
    schema: dict[str, Any],
    max_tokens: int,
    temperature: float = 0.2,
) -> dict[str, Any]:
    primary = (ROUTER_BACKEND or "ollama").strip().lower()
    try:
        return _chat_json_for(
            primary, messages=messages, schema=schema,
            max_tokens=max_tokens, temperature=temperature,
        )
    except Exception as exc:
        fb = _fallback_provider(primary)
        if not fb:
            raise
        print(f"  router: {primary} failed ({type(exc).__name__}); "
              f"falling back to {fb}")
        bus.emit("router.fallback", primary=primary, fallback=fb, error=str(exc))
        try:
            return _chat_json_for(
                fb, messages=messages, schema=schema,
                max_tokens=max_tokens, temperature=temperature,
            )
        except Exception:
            raise exc  # both providers failed -> surface the original error


def _openrouter_request(body: dict[str, Any]) -> dict[str, Any]:
    """POST a chat/completions `body` to OpenRouter and return the parsed JSON
    payload. Shared header/auth/base-url prep for BOTH the structured-output path
    and the tool-call path (Phase 7), killing the copy-pasted request building."""
    api_key = os.environ.get(OPENROUTER_API_KEY_ENV, "").strip()
    if not api_key:
        raise RuntimeError(f"OpenRouter API key missing ({OPENROUTER_API_KEY_ENV})")
    base_url = (OPENROUTER_BASE_URL or "https://openrouter.ai/api/v1").rstrip("/")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if OPENROUTER_SITE_URL:
        headers["HTTP-Referer"] = OPENROUTER_SITE_URL
    if OPENROUTER_APP_NAME:
        headers["X-OpenRouter-Title"] = OPENROUTER_APP_NAME
    req = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=float(OPENROUTER_TIMEOUT_SEC)) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")[:300]
        raise RuntimeError(f"OpenRouter HTTP {exc.code}: {detail}") from exc


def _chat_json_openrouter(
    *,
    messages: list[dict[str, str]],
    schema: dict[str, Any],
    max_tokens: int,
    temperature: float,
) -> dict[str, Any]:
    model = (OPENROUTER_MODEL or "").strip()
    if not model:
        raise RuntimeError("OpenRouter model missing (HALO_OPENROUTER_MODEL)")
    body = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "reasoning": {"enabled": False, "exclude": True},
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "halo_response", "strict": True, "schema": schema},
        },
    }
    payload = _openrouter_request(body)
    content = payload["choices"][0]["message"]["content"]
    if isinstance(content, list):
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return json.loads(content)


def check_turn_complete(partial_transcript: str) -> bool:
    """Stage 1: pure-rules turn-completion classifier (no LLM).

    Returns True for COMPLETE, False for INCOMPLETE.

    The LLM Stage 1 was costing 2-4 s per call in real use (KV cache
    flipping between Stage 1 and Stage 2 prefixes). Rules are 0 ms and
    catch 95%+ of cases. The few edges that slip through default to
    COMPLETE — Stage 2 (or the user picking up where they left off in
    the next extension window) handles the rest.
    """
    text = partial_transcript.strip()
    if not text:
        return False

    # Trailing conjunctions/fillers/prepositions -> INCOMPLETE.
    if _TRAILING_INCOMPLETE_RE.search(text):
        return False

    # Short single-word commands -> COMPLETE.
    lowered = text.lower().rstrip(".!?,")
    if lowered in _SHORT_COMPLETE_WORDS:
        return True

    # Question mark / exclamation at the end -> COMPLETE.
    if _HARD_TERMINATOR_RE.search(text):
        return True

    # Default: COMPLETE. Cutting off a thinker is fine because the
    # orchestrator gives them an extension window to keep talking;
    # stranding a snappy command is the worse failure mode.
    return True


# --- session_action fallback ----------------------------------------------
# The 1.5B router model frequently emits target_session correctly but
# forgets to set session_action — or skips both for clear list/where_am_i
# phrases. These regex layers post-process the brain's decision so the
# orchestrator sees consistent {session_action, target_session} even on
# small-model misses.

_LIST_PATTERN = re.compile(
    r"\b("
    r"what(?:'s| is)?\s+(?:running|open|going|sessions)|"
    r"what\s+(?:sessions|projects)|"
    r"list\s+(?:sessions|projects|everything)|"
    r"show\s+(?:me\s+)?(?:sessions|projects)"
    r")\b",
    re.IGNORECASE,
)
_WHERE_PATTERN = re.compile(
    r"\b("
    r"where\s+am\s+i|"
    r"what(?:'s| is)?\s+(?:my\s+)?(?:active|current)(?:\s+(?:session|project))?|"
    r"what\s+project\s+am\s+i\s+on|"
    r"which\s+(?:session|project)\s+am\s+i"
    r")\b",
    re.IGNORECASE,
)
# NOTE: deliberately NO "open" here — "open a browser/calculator/app" is a
# LOCAL tool action, not a session switch. Including it made "open a browser"
# (when it reached Stage 2) get mis-synthesized into a switch to a running
# session instead of opening the app.
_SWITCH_VERB_PATTERN = re.compile(
    r"\b(switch|jump|move|go|work on|use|activate)\b",
    re.IGNORECASE,
)
_CODING_VERB_PATTERN = re.compile(
    r"\b(write|make|build|create|add|fix|refactor|delete|update|"
    r"test|run|deploy|implement|change|edit)\b",
    re.IGNORECASE,
)


def _apply_session_action_fallback(decision: dict, has_context: bool) -> None:
    """Mutate decision in-place to synthesize session_action when the
    brain emitted target_session but forgot the action, or when the
    cleaned_text obviously asks for list/where_am_i.

    No-op when has_context=False (single-session mode has no session
    actions to fire) or when the brain already populated session_action.
    """
    if not has_context:
        return
    if decision.get("session_action"):
        return

    cleaned = (decision.get("cleaned_text") or "").strip()
    if not cleaned:
        return

    if _LIST_PATTERN.search(cleaned):
        decision["session_action"] = "list_sessions"
        decision["intent"] = "question"
        decision["agent"] = "none"
        decision["target_session"] = ""
        return

    if _WHERE_PATTERN.search(cleaned):
        decision["session_action"] = "where_am_i"
        decision["intent"] = "question"
        decision["agent"] = "none"
        decision["target_session"] = ""
        return

    target = (decision.get("target_session") or "").strip()
    if target and target not in ("active", "focused", "all"):
        # Pure-switch detection: looks like a switch verb AND has no
        # coding-imperative verb (otherwise it's a cross-session dispatch
        # like "in website, fix the bug").
        looks_like_switch = _SWITCH_VERB_PATTERN.search(cleaned) is not None
        is_coding_verb = _CODING_VERB_PATTERN.search(cleaned) is not None
        if looks_like_switch and not is_coding_verb:
            decision["session_action"] = "switch"
            decision["intent"] = "system"
            decision["agent"] = "none"


def _validate_session_action(decision: dict) -> None:
    """Clear a session_action the words don't actually support. The small
    multi-session brain hallucinates switch/list_sessions/where_am_i for
    ordinary chatter; only honor one when the transcript really asks for it."""
    action = (decision.get("session_action") or "").strip()
    if not action:
        return
    cleaned = (decision.get("cleaned_text") or "").lower()
    if action == "list_sessions" and not _LIST_PATTERN.search(cleaned):
        decision["session_action"] = ""
        return
    if action == "where_am_i" and not _WHERE_PATTERN.search(cleaned):
        decision["session_action"] = ""
        return
    if action == "switch":
        target = (decision.get("target_session") or "").lower().strip()
        has_verb = _SWITCH_VERB_PATTERN.search(cleaned) is not None
        named = (
            target not in ("", "active", "focused", "all")
            and re.search(rf"\b{re.escape(target)}\b", cleaned) is not None
        )
        # A real switch says a switch verb AND names a session (or at least
        # names the target). "close" / "no why did you do that" do neither.
        if not (has_verb and (named or re.search(r"\bsession\b", cleaned))):
            decision["session_action"] = ""
            decision["target_session"] = ""


def understand_and_route(
    full_transcript: str,
    context: Optional[SessionContext] = None,
    history: str = "",
) -> dict:
    """Stage 2: clean + route the full transcript. Returns the schema dict.

    `context` (v1.2) — when provided and non-empty, a CURRENT CONTEXT
    block is prepended to the user message describing the active session
    and discovered sessions. The brain uses this to set `target_session`
    and `session_action`. When None or empty, behaves exactly as v1.1
    (single-session mode).

    `history` (v1.3) — a compact "# RECENT CONVERSATION" block of the last
    few user/Halo turns. Prepended so the brain can resolve references
    ("the other one", "do it again") and stop marking context-dependent
    fragments as unclear. Empty = no history (behaves as before).

    On any error, returns a safe `unclear` fallback so callers can keep
    going without exception handling. The fallback also contains the new
    v1.2 fields as empty strings so callers can read them unconditionally.
    """
    history = (history or "").strip()
    ctx_block = _format_context_block(context)
    blocks: list[str] = []
    if history:
        blocks.append(history)
    if ctx_block:
        blocks.append(ctx_block)
    if blocks:
        blocks.append(
            "# USER TRANSCRIPT (route THIS line; everything above is context)\n"
            + full_transcript
        )
        user_payload = "\n".join(blocks)
    else:
        user_payload = full_transcript
    if ctx_block:
        system_prompt = STAGE2_PROMPT + "\n" + STAGE2_SESSION_BLOCK
        schema = ROUTE_SCHEMA
    else:
        # Single-session: lean prompt + 4-field schema = much faster.
        system_prompt = STAGE2_PROMPT
        schema = ROUTE_SCHEMA_LEAN

    t0 = time.monotonic()
    bus.emit("thinking.start", stage="route", text=full_transcript)
    try:
        decision = _chat_json(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_payload},
            ],
            schema=schema,
            # Full 8-field schema (multi-session) needs more headroom or the
            # JSON truncates -> parse failure -> dropped turn. Constrained
            # decode means a higher cap is free when output is short.
            max_tokens=96 if schema is ROUTE_SCHEMA_LEAN else 220,
            temperature=0.2,
        )
        # Lean schema omits these — default them so callers read uniformly.
        decision.setdefault("confirmation", "")
        decision.setdefault("clarification", "")
        decision.setdefault("target_session", "")
        decision.setdefault("session_action", "")
        # Post-process: 1.5B brain frequently emits target_session but
        # forgets session_action. Regex fallback synthesizes the missing
        # action so the orchestrator sees consistent decisions.
        _apply_session_action_fallback(
            decision, has_context=ctx_block != "",
        )
        # Anti-hallucination: with several sessions discovered, the small brain
        # invents session_action=switch/list for things that aren't session
        # commands ("close" -> switch, "no I told you that" -> list_sessions).
        # Drop any session_action the actual words don't support.
        _validate_session_action(decision)
        bus.emit(
            "thinking.end",
            stage="route",
            ms=int((time.monotonic() - t0) * 1000),
            status=decision.get("status", "?"),
            intent=decision.get("intent", "?"),
            agent=decision.get("agent", "none"),
        )
        return decision
    except Exception as exc:
        bus.emit(
            "thinking.end",
            stage="route",
            ms=int((time.monotonic() - t0) * 1000),
            error=str(exc),
        )
        return {**_FALLBACK_DECISION, "cleaned_text": full_transcript, "_error": str(exc)}


def preload_router() -> None:
    """Send a warm-up Stage 2 call so the first real turn doesn't pay
    the model-load + KV-cache-fill cost. Stage 1 is rules-only now,
    so it doesn't need a warmup."""
    print("warming up router model (Stage 2)...")
    understand_and_route("hello")


# ---------------------------------------------------------------------------
# Stage 3 — one-sentence summarizer (v1.2.1)
# ---------------------------------------------------------------------------

SUMMARIZE_PROMPT = """\
You are a voice-channel summarizer. The user asked an AI coding agent a
question; the agent's full reply is below. The user will hear YOUR
summary read aloud through text-to-speech.

Compress the agent's reply into ONE short sentence (target 15-25 words,
absolute max ~30). Capture the most important outcome: what was done,
the answer, or the key result. Skip implementation detail — the user
can read the full reply in their terminal.

ABSOLUTE RULES:
- Exactly one sentence. No markdown. No code. No fenced blocks.
- File basenames only — never full paths.
- Write in first person AS the agent. No "The agent did X" / "Claude
  says" preamble; Halo prepends the agent name itself.
- If the reply is itself short and clean, just trim/lightly rephrase
  — don't add fluff.

Examples:

Reply: "I added a `verify_password` function to `auth.py` that uses
bcrypt with cost factor 12 for the work parameter. I also wired it
into the existing `login_user` flow at line 47, replacing the previous
plaintext check. The tests in `test_auth.py` cover both correct and
incorrect passwords."
Summary: "I added bcrypt password verification to auth.py and wired
it into login_user with tests."

Reply: "Done. I refactored the routes file."
Summary: "I refactored the routes file."

Reply: "The function exists in utils/helpers.py on line 84. It takes
a list of dicts and returns the unique values by key. It's used in
three places: the data importer, the dedup script, and the merge
worker."
Summary: "It's in helpers.py line 84 — dedupes a list of dicts by key,
used by the importer, dedup, and merge worker."

# USER'S ORIGINAL REQUEST
{ORIGINAL}

# AGENT'S REPLY TO SUMMARIZE
{REPLY}

Return JSON: {{"one_sentence": "..."}}.
"""

_SUMMARIZE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "one_sentence": {"type": "string"},
    },
    "required": ["one_sentence"],
}


def summarize_reply(
    reply_text: str,
    original_prompt: str = "",
    *,
    max_chars: int = 220,
) -> str:
    """Use the brain to compress a long agent reply into one spoken sentence.

    Falls back to head-truncation on Ollama failure so the caller always
    gets a non-empty speakable string when reply_text is non-empty.

    `max_chars` is a safety cap on the output; if the brain ignores the
    word-count instruction and returns a long sentence, we head-truncate
    to the nearest sentence boundary under max_chars.
    """
    if not reply_text or not reply_text.strip():
        return ""

    prompt = SUMMARIZE_PROMPT.format(
        ORIGINAL=(original_prompt or "(not provided)")[:500],
        REPLY=reply_text[:4000],  # cap input length to bound latency
    )

    t0 = time.monotonic()
    bus.emit("thinking.start", stage="summarize", chars=len(reply_text))
    try:
        data = _chat_json(
            messages=[{"role": "user", "content": prompt}],
            schema=_SUMMARIZE_SCHEMA,
            max_tokens=96,
            temperature=0.2,
        )
        sentence = (data.get("one_sentence") or "").strip()
        bus.emit(
            "thinking.end",
            stage="summarize",
            ms=int((time.monotonic() - t0) * 1000),
            chars_out=len(sentence),
        )
        if not sentence:
            return _head_truncate(reply_text, max_chars)
        # Cap output length even if brain ignored the word limit.
        if len(sentence) > max_chars:
            return _head_truncate(sentence, max_chars)
        return sentence
    except Exception as exc:
        # Brain unreachable / errored — head-truncate so the user still
        # hears something instead of dead silence.
        bus.emit(
            "thinking.end",
            stage="summarize",
            ms=int((time.monotonic() - t0) * 1000),
            error=str(exc),
        )
        print(f"  [summarize] brain unreachable ({exc}) — head-truncating")
        return _head_truncate(reply_text, max_chars)


def _head_truncate(text: str, max_chars: int) -> str:
    """Pure-string fallback for summarize_reply: trim at the nearest
    sentence terminator under max_chars, else hard-cap with ellipsis."""
    text = text.strip()
    if len(text) <= max_chars:
        return text
    head = text[:max_chars]
    for stop in (". ", "! ", "? ", "\n\n"):
        idx = head.rfind(stop)
        if idx > max_chars * 0.4:
            return head[: idx + 1].strip()
    return head.rstrip() + "..."


_CORRECT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"corrected": {"type": "string"}},
    "required": ["corrected"],
}

_CORRECT_PROMPT = (
    "You clean up dictation captured by speech-to-text from a speaker with a "
    "strong non-native accent, so it often contains misspellings and wrong "
    "words. Fix spelling, obvious transcription errors, capitalization, and "
    "punctuation. Do NOT translate, rephrase, summarize, answer, explain, or "
    "add or remove meaning — return the SAME sentence, only corrected. "
    'Reply as JSON: {"corrected": "<the corrected text>"}.\n\nText: '
)

_REPAIR_PROMPT = (
    "You repair a voice-assistant transcript captured by speech-to-text from "
    "a speaker with a strong non-native accent, so some words are garbled or "
    "mis-recognized. Using the recent conversation for context, replace ONLY "
    "the words that are clearly mis-transcriptions so the sentence makes "
    "sense as something the speaker would say next. Keep the speaker's "
    "wording, word order, and meaning. Do NOT answer the request, rephrase, "
    "summarize, translate, or add or remove content. If the text already "
    "makes sense, return it unchanged. "
    'Reply as JSON: {"corrected": "<the fixed transcript>"}.'
)


# --- user-adjustable prompt overrides --------------------------------------
# The literals above are the baked DEFAULTS. Captured here for `halo prompts
# --init`, then each is replaced by a ~/.halo/prompts/<name>.txt override if the
# user created one. Routing schema/validation stay in code — only wording is
# tunable. Behavior is unchanged unless an override file exists.
PROMPT_DEFAULTS: dict[str, str] = {
    "stage2_router": STAGE2_PROMPT,
    "stage2_session": STAGE2_SESSION_BLOCK,
    "summarize": SUMMARIZE_PROMPT,
    "stt_correct": _CORRECT_PROMPT,
    "stt_repair": _REPAIR_PROMPT,
}
STAGE2_PROMPT = prompts.get("stage2_router", STAGE2_PROMPT)
STAGE2_SESSION_BLOCK = prompts.get("stage2_session", STAGE2_SESSION_BLOCK)
SUMMARIZE_PROMPT = prompts.get("summarize", SUMMARIZE_PROMPT)
_CORRECT_PROMPT = prompts.get("stt_correct", _CORRECT_PROMPT)
_REPAIR_PROMPT = prompts.get("stt_repair", _REPAIR_PROMPT)


_CHAT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"reply": {"type": "string"}},
    "required": ["reply"],
}


def _chat_persona() -> tuple[str, str]:
    """(user_name, halo_character) from the live config, best-effort."""
    try:
        from halo.userconfig import cfg as _ucfg

        return (
            (_ucfg.persona.user_name or "").strip(),
            (_ucfg.persona.halo_character or "").strip(),
        )
    except Exception:
        return "", ""


def _now_context() -> str:
    """Current local time, fed to the chat brain so it can answer time/date
    questions (including 'what time is it in Brisbane' — it converts from
    this) instead of guessing."""
    import datetime

    try:
        now = datetime.datetime.now().astimezone()
        tzname = now.tzname() or "local time"
        return (
            "(Reference only — do NOT mention the date, time, or timezone "
            "unless the user explicitly asks for it.) Current local time: "
            f"{now.strftime('%A, %B %d, %Y, %I:%M %p')} {tzname}. "
            "Convert from this for other timezones only if asked. "
        )
    except Exception:
        return ""


def _chat_base_prompt(history: str = "", tools_available: bool = False) -> tuple[str, str]:
    """Shared chat system prompt (persona + current time + recent history),
    WITHOUT any output-format instruction. Returns (system_prompt, user_name)."""
    name, character = _chat_persona()
    persona = character or "a friendly, quick-witted voice assistant"
    system = (
        f"You are Halo, {persona}, having a casual SPOKEN conversation. Reply "
        "with ONE short, natural sentence (8 to 16 words) — like a quick-witted "
        "friend would say it out loud.\n\n"
        "CRITICAL: Respond directly. NEVER restate, echo, paraphrase, or "
        "analyze what the user said. Banned openings: \"you're asking...\", "
        "\"you're confirming...\", \"you want to know...\", \"it seems you...\", "
        "\"that's a...\". Just answer or react, the way a person would.\n\n"
        "If it's small talk, banter back. If it's a real question, answer it. "
        "If you truly can't tell what they mean, say so in a few words and ask "
        "what they need. Do not offer to write code unless asked; if they want "
        'a coding task, say they can ask "Claude". If asked about earlier '
        "conversation not shown below, say you don't recall exactly — never "
        "invent it. No markdown, lists, emojis, or code.\n\n"
        "IDENTITY: you ARE Halo, the user's own voice assistant running on "
        "their PC. Never call yourself an AI, an LLM, or a 'cloud model', and "
        "never name Anthropic, OpenAI, OpenRouter, or 'servers'. If the user "
        "teases you about what you are or where you run, play along lightly in "
        "one line — don't argue, correct them, or repeat the same point twice.\n\n"
        "Examples (style only):\n"
        "User: what's happening today -> Not much, just keeping things humming — what's on your plate?\n"
        "User: is that a question or a statement -> Ha, you tell me! What's on your mind?\n"
        "User: yep -> Cool. What's next?\n"
        "User: what are they doing -> Not sure who you mean — which one are you asking about?\n"
    )
    if tools_available:
        system += (
            "\nYou have TOOLS for live data (weather, files, web). Call a tool ONLY "
            "when you genuinely need fresh or external info to answer; for small talk "
            "or things you already know, just answer. After a tool returns, reply in "
            "ONE short spoken sentence.\n"
        )
    system += _now_context()
    if name:
        system += (
            f"The user's name is {name} (you know it from their settings — never "
            "claim they told it to you earlier); use it occasionally, only when natural. "
        )
    history = (history or "").strip()
    if history:
        system += (
            "\n\n# RECENT CONVERSATION (context only — reply to the latest user line)\n"
            + history
            + "\n"
        )
    return system, name


def chat_reply(text: str, history: str = "") -> str:
    """Hold an actual conversation — the spoken reply for chit-chat and
    casual questions that aren't a tool command or a coding task.

    Non-streaming variant (returns the whole reply). See `chat_reply_stream`
    for the low-latency sentence-by-sentence path used by the voice loop.

    FAIL-OPEN: returns "" on any model error so the caller can fall back
    to a short canned line — a dead turn is worse than a generic reply.
    """
    msg = (text or "").strip()
    if not msg:
        return ""
    system, _name = _chat_base_prompt(history)
    system += '\nReply as JSON: {"reply": "<your spoken reply>"}.'

    t0 = time.monotonic()
    bus.emit("thinking.start", stage="chat", chars=len(msg))
    try:
        data = _chat_json(
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": msg},
            ],
            schema=_CHAT_SCHEMA,
            max_tokens=60,
            temperature=0.6,
        )
        reply = (data.get("reply") or "").strip()
        bus.emit(
            "thinking.end",
            stage="chat",
            ms=int((time.monotonic() - t0) * 1000),
            chars_out=len(reply),
        )
        return reply
    except Exception as exc:
        bus.emit(
            "thinking.end",
            stage="chat",
            ms=int((time.monotonic() - t0) * 1000),
            error=str(exc),
        )
        print(f"  [chat] brain unreachable ({exc}) — using canned reply")
        return ""


# Streaming sentence splitter. A terminator preceded by an abbreviation
# ("E." in "E. Australia", "Mr.", "e.g.") is NOT a real sentence end — emitting
# there spoke fragments like "...here in E." Skip those and wait for the next
# real boundary.
_TERMINATOR_RE = re.compile(r"[.!?…]+(?=\s|$)")
_ABBREV_BEFORE_RE = re.compile(
    r"(?:\b[A-Za-z]|\b(?:mr|mrs|ms|dr|st|vs|etc|e\.g|i\.e|a\.m|p\.m|no|fig))$",
    re.IGNORECASE,
)


def _flush_sentences(buffer: str, on_sentence) -> str:
    """Emit complete sentences from `buffer` via on_sentence; return the
    remainder (an incomplete trailing sentence still being generated)."""
    if on_sentence is None:
        return buffer
    emitted_to = 0
    for m in _TERMINATOR_RE.finditer(buffer):
        before = buffer[:m.start()]
        if _ABBREV_BEFORE_RE.search(before):
            continue  # abbreviation (E., Mr., e.g.) — not a sentence end
        sentence = buffer[emitted_to:m.end()].strip()
        if sentence:
            on_sentence(sentence)
        emitted_to = m.end()
    return buffer[emitted_to:]


def chat_reply_stream(text: str, history: str = "", on_sentence=None) -> str:
    """Streaming chat reply: the model streams tokens and `on_sentence` is
    called with each complete sentence as it finishes — so the voice loop can
    start SPEAKING the first sentence ~0.4 s in instead of waiting ~1.3 s for
    the whole reply. Returns the full reply text.

    FAIL-OPEN: on any streaming error, falls back to the non-streaming
    `chat_reply` and emits the whole thing through on_sentence once.
    """
    msg = (text or "").strip()
    if not msg:
        return ""
    system, _name = _chat_base_prompt(history)
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": msg},
    ]
    provider = (ROUTER_BACKEND or "ollama").strip().lower()
    t0 = time.monotonic()
    bus.emit("thinking.start", stage="chat", chars=len(msg))
    try:
        if provider == "openrouter":
            full = _chat_stream_openrouter(messages, on_sentence)
        else:
            full = _chat_stream_ollama(messages, on_sentence)
        bus.emit(
            "thinking.end",
            stage="chat",
            ms=int((time.monotonic() - t0) * 1000),
            chars_out=len(full),
        )
        return full
    except Exception as exc:
        bus.emit(
            "thinking.end", stage="chat",
            ms=int((time.monotonic() - t0) * 1000), error=str(exc),
        )
        print(f"  [chat stream] failed ({exc}) — falling back to non-streaming")
        full = chat_reply(text, history=history)
        if full and on_sentence:
            on_sentence(full)
        return full


# --- Phase 7: brain MCP tool-calling loop ----------------------------------
# Lets the router brain call standard MCP tools (weather/files/web) directly,
# without spawning a coding agent. The MCP client + the gating flag live in
# __main__/mcp_client; this is the provider-agnostic loop. Fail-open to plain chat.


def _chat_tools_openrouter(messages, tools, max_tokens) -> dict:
    """Non-streaming OpenRouter call WITH tools; returns the assistant message."""
    model = (OPENROUTER_CHAT_MODEL or OPENROUTER_MODEL or "").strip()
    if not model:
        raise RuntimeError("OpenRouter model missing")
    body = {
        "model": model,
        "messages": messages,
        "temperature": 0.4,
        "max_tokens": max_tokens,
        "reasoning": {"enabled": False, "exclude": True},
        "tools": tools,
        "tool_choice": "auto",
    }
    return _openrouter_request(body)["choices"][0]["message"]


def _chat_tools_ollama(messages, tools, max_tokens) -> dict:
    """Non-streaming Ollama call WITH tools; returns the assistant message."""
    resp = _get_client().chat(
        model=OLLAMA_MODEL,
        messages=messages,
        tools=tools,
        options={"temperature": 0.4, "num_predict": max_tokens},
        keep_alive=_KEEP_ALIVE,
    )
    return dict(resp["message"])


def _tool_calls_from(message: dict) -> list[dict]:
    """Normalize tool_calls across OpenRouter (OpenAI shape) and Ollama."""
    out = []
    for c in (message.get("tool_calls") or []):
        fn = c.get("function") or {}
        args = fn.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args) if args.strip() else {}
            except Exception:
                args = {}
        out.append({"id": c.get("id") or fn.get("name", ""), "name": fn.get("name", ""), "args": args or {}})
    return out


def _message_text(message: dict) -> str:
    content = message.get("content") or ""
    if isinstance(content, list):
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return (content or "").strip()


def _chat_with_tools(messages, *, tools, execute, on_sentence, max_hops) -> str:
    """Provider-agnostic tool-use loop. The model may request tool calls; we run
    them via execute(name, args) and feed results back, up to max_hops, then
    stream the final text answer via on_sentence. Returns the full answer."""
    provider = (ROUTER_BACKEND or "ollama").strip().lower()
    call_provider = _chat_tools_openrouter if provider == "openrouter" else _chat_tools_ollama
    convo = list(messages)
    for _hop in range(max(1, max_hops)):
        message = call_provider(convo, tools, 220)
        tool_calls = _tool_calls_from(message)
        if not tool_calls:
            text = _message_text(message)
            if on_sentence and text:
                rem = _flush_sentences(text, on_sentence).strip()
                if rem:
                    on_sentence(rem)
            return text
        convo.append({
            "role": "assistant",
            "content": message.get("content") or "",
            "tool_calls": message.get("tool_calls"),
        })
        for call in tool_calls:
            try:
                result = execute(call["name"], call["args"])
            except Exception as exc:
                result = f"[tool error] {exc}"
            print(f"  brain tool: {call['name']}({call['args']}) -> {str(result)[:80]!r}")
            bus.emit("brain.tool", name=call["name"], args=call["args"])
            convo.append({
                "role": "tool",
                "tool_call_id": call["id"],
                "name": call["name"],
                "content": str(result),
            })
    raise RuntimeError(f"tool hop cap ({max_hops}) reached")


def chat_reply_with_tools(text: str, history: str = "", on_sentence=None) -> str:
    """Tool-augmented chat reply — the brain may call MCP tools before answering.
    FAIL-OPEN: any error (no client, provider error, hop cap) falls back to the
    plain streaming chat so the turn never dies. Wired in __main__._converse."""
    msg = (text or "").strip()
    if not msg:
        return ""
    try:
        from halo.mcp_client import get_brain_client

        client = get_brain_client()
        if client is None or not client.available:
            return chat_reply_stream(text, history=history, on_sentence=on_sentence)
        tools = client.openai_tools()
        from halo.userconfig import cfg as _cfg

        max_hops = int(getattr(getattr(_cfg, "mcp", None), "brain_max_tool_hops", 4))
        system, _name = _chat_base_prompt(history, tools_available=True)
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": msg},
        ]
        t0 = time.monotonic()
        bus.emit("thinking.start", stage="chat_tools", chars=len(msg))
        full = _chat_with_tools(
            messages, tools=tools, execute=client.call,
            on_sentence=on_sentence, max_hops=max_hops,
        )
        bus.emit("thinking.end", stage="chat_tools",
                 ms=int((time.monotonic() - t0) * 1000), chars_out=len(full))
        return full
    except Exception as exc:
        print(f"  [chat tools] failed ({exc}) — falling back to plain chat")
        return chat_reply_stream(text, history=history, on_sentence=on_sentence)


def _chat_stream_ollama(messages: list[dict[str, str]], on_sentence) -> str:
    parts: list[str] = []
    buf = ""
    stream = _get_client().chat(
        model=OLLAMA_MODEL,
        messages=messages,
        options={"temperature": 0.6, "num_predict": 60},
        keep_alive=_KEEP_ALIVE,
        stream=True,
    )
    for chunk in stream:
        delta = (chunk.get("message") or {}).get("content") or ""
        if not delta:
            continue
        parts.append(delta)
        buf += delta
        buf = _flush_sentences(buf, on_sentence)
    tail = buf.strip()
    if tail and on_sentence:
        on_sentence(tail)
    return "".join(parts).strip()


def _chat_stream_openrouter(messages: list[dict[str, str]], on_sentence) -> str:
    api_key = os.environ.get(OPENROUTER_API_KEY_ENV, "").strip()
    if not api_key:
        raise RuntimeError(f"OpenRouter API key missing ({OPENROUTER_API_KEY_ENV})")
    # Use the dedicated chat model when configured (smarter conversation),
    # falling back to the routing model.
    model = (OPENROUTER_CHAT_MODEL or OPENROUTER_MODEL or "").strip()
    if not model:
        raise RuntimeError("OpenRouter model missing (HALO_OPENROUTER_MODEL)")
    base_url = (OPENROUTER_BASE_URL or "https://openrouter.ai/api/v1").rstrip("/")
    body = {
        "model": model,
        "messages": messages,
        "temperature": 0.6,
        "max_tokens": 60,
        "stream": True,
        "reasoning": {"enabled": False, "exclude": True},
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
    if OPENROUTER_SITE_URL:
        headers["HTTP-Referer"] = OPENROUTER_SITE_URL
    if OPENROUTER_APP_NAME:
        headers["X-OpenRouter-Title"] = OPENROUTER_APP_NAME
    req = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    parts: list[str] = []
    buf = ""
    with urllib.request.urlopen(req, timeout=float(OPENROUTER_TIMEOUT_SEC)) as r:
        for raw in r:
            line = raw.decode("utf-8", errors="ignore").strip()
            if not line or not line.startswith("data:"):
                continue
            payload = line[len("data:"):].strip()
            if payload == "[DONE]":
                break
            try:
                obj = json.loads(payload)
                delta = obj["choices"][0].get("delta", {}).get("content") or ""
            except (ValueError, KeyError, IndexError):
                continue
            if not delta:
                continue
            parts.append(delta)
            buf += delta
            buf = _flush_sentences(buf, on_sentence)
    tail = buf.strip()
    if tail and on_sentence:
        on_sentence(tail)
    return "".join(parts).strip()


def correct_text(text: str) -> str:
    """Fix STT spelling/transcription slips in a dictated chunk.

    Best-effort and FAIL-OPEN: returns the input unchanged on any model
    error, an empty result, or an implausibly different output (a guard
    against the model rewriting instead of correcting). The caller is
    responsible for any wall-clock timeout — keep dictation responsive.
    """
    raw = (text or "").strip()
    if not raw:
        return text
    try:
        data = _chat_json(
            messages=[{"role": "user", "content": _CORRECT_PROMPT + raw}],
            schema=_CORRECT_SCHEMA,
            max_tokens=max(48, min(400, len(raw))),
            temperature=0.0,
        )
        out = (data.get("corrected") or "").strip()
    except Exception as exc:
        print(f"  [dictation correct] model unreachable ({exc}) — using raw")
        return raw
    if not out:
        return raw
    # Reject editorializing: a correction shouldn't balloon the length.
    if len(out) > max(40, int(len(raw) * 2.5)):
        return raw
    return out


# --- semantic repair (mic "translation" pass) -------------------------------
# When Whisper itself was UNSURE of a transcript (low avg-logprob), the words
# that came out may be phonetic neighbours of what was said. This pass gives
# the brain the transcript + the recent conversation and asks it to fix ONLY
# clear mis-recognitions — "interpret what the user actually meant", never
# rewrite. Confident transcripts skip it entirely, so good turns pay zero
# latency. Tune the gate with HALO_STT_REPAIR_LOGPROB (default -0.5; whisper
# is ~-0.1..-0.3 when sure of the words).

_REPAIR_MIN_WORDS = 3
_REPAIR_LOGPROB = float(os.getenv("HALO_STT_REPAIR_LOGPROB", "-0.5"))


def needs_repair(text: str, quality: float | None) -> bool:
    """Pure gate: True only for a real-length transcript Whisper was unsure
    of. Short commands are excluded — repairing "open chrome" risks more than
    it fixes, and control phrases must stay byte-exact."""
    if quality is None:
        return False
    if len((text or "").split()) < _REPAIR_MIN_WORDS:
        return False
    return quality < _REPAIR_LOGPROB


def repair_transcript(text: str, context: str = "") -> str:
    """Fix mis-recognized words in a low-confidence transcript using the
    recent conversation as context. FAIL-OPEN like correct_text: returns the
    input on any model error, empty output, or a ballooned rewrite. The
    caller owns the wall-clock timeout."""
    raw = (text or "").strip()
    if not raw:
        return text
    ctx = (context or "").strip()
    if len(ctx) > 800:
        ctx = ctx[-800:]
    content = _REPAIR_PROMPT
    if ctx:
        content += "\n\n" + ctx
    content += "\n\nTranscript: " + raw
    try:
        data = _chat_json(
            messages=[{"role": "user", "content": content}],
            schema=_CORRECT_SCHEMA,
            max_tokens=max(48, min(400, len(raw))),
            temperature=0.0,
        )
        out = (data.get("corrected") or "").strip()
    except Exception as exc:
        print(f"  [stt repair] model unreachable ({exc}) — using raw")
        return raw
    if not out:
        return raw
    # Reject editorializing: a repair shouldn't balloon the length.
    if len(out) > max(40, int(len(raw) * 2.0)):
        return raw
    return out
