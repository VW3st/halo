"""Routing brain — two stages over the local Ollama instance.

Stage 1: fast classifier — "has the user finished their thought?"
         Plain-text COMPLETE/INCOMPLETE response, called on every silence
         during a turn. Latency target: <200 ms.

Stage 2: full understanding + routing. Called once when Stage 1 (or a
         terminator phrase, or the hard timeout) ends the turn.
         JSON-schema-constrained output via Ollama's structured-output
         feature, so we never have to defend against malformed JSON.
         Latency target: <500 ms.

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
# Stage 1 — turn completion classifier
# ---------------------------------------------------------------------------

STAGE1_PROMPT = """\
Classify the transcript as COMPLETE or INCOMPLETE.

COMPLETE = a finished request, question, statement, or single command.
INCOMPLETE = a fragment, trailing thought, or ends mid-phrase.

Hard rules (always INCOMPLETE):
- ends with a conjunction or preposition (and/or/but/with/to/for/of/the/a)
- ends with a filler/thinking word (um/uh/like/let me/actually/wait)
- trails off mid-noun-phrase (e.g. "build me a")

When uncertain, pick INCOMPLETE.

Examples:
"open the browser" -> COMPLETE
"build me a login page and" -> INCOMPLETE
"cancel" -> COMPLETE
"I want to refactor the" -> INCOMPLETE
"um actually" -> INCOMPLETE

Reply with one word: COMPLETE or INCOMPLETE.
"""

# ---------------------------------------------------------------------------
# Stage 2 — understanding + routing
# ---------------------------------------------------------------------------

_STAGE2_PROMPT_OLD = """\
# IDENTITY
You are Halo, a voice control orchestrator for AI coding agents. You receive raw transcribed voice commands from a developer and convert them into structured instructions for downstream agents (Claude Code, Codex CLI).

# YOUR JOB
Three responsibilities, in this order:
1. CLEAN the transcript (fix STT errors, remove filler, add punctuation, normalize technical terms)
2. UNDERSTAND the intent (what does the user actually want?)
3. ROUTE to the correct agent OR flag for clarification

You do NOT execute anything. You produce a structured plan that the orchestrator executes.

# CONTEXT YOU HAVE
- The user is a software developer at their workstation
- They are talking to you via microphone after saying "Hey Halo"
- Their voice was transcribed by Whisper, so expect homophone errors and missing punctuation
- They may use technical jargon (frameworks, languages, tools)
- They expect fast, terse responses -- no fluff

# STT CORRECTION DICTIONARY
Common Whisper/Moonshine mistakes to silently fix:
- "claud" / "clod" / "clawed" -> "Claude"
- "claud code" / "code clawed" -> "Claude Code"
- "codecs" / "codex" -> "Codex"
- "next jess" / "next js" -> "Next.js"
- "react" pronunciation variants -> "React"
- "type script" -> "TypeScript"
- "java script" -> "JavaScript"
- "get hub" -> "GitHub"
- "super base" -> "Supabase"
- "post grass" -> "Postgres"
- "ollama" / "o llama" -> "Ollama"
- "card" / "com" / "krome" / "grom" / "crome" -> "Chrome"  (when context is "open <X>")
- "calc" / "cut" / "kelker" -> "calculator"  (when context is "open <X>")
- "node pad" / "no pad" / "notedad" -> "notepad"
- "powershell" variants ("power shell", "pow shell") -> "PowerShell"
- File and component names spoken phonetically -> normalize to PascalCase or camelCase based on context

# FILLER TO REMOVE
um, uh, er, ah, like (when used as filler), you know, basically, sort of, kind of, I mean, so (when used to start), right (when trailing), okay (when trailing), false starts, repeated words

# INTENT CATEGORIES
- "code" : write, edit, refactor, build, debug, test, deploy, run, install, generate code
- "question" : ask Halo something it can answer without an agent (general knowledge, status, capability)
- "system" : control Halo itself (stop, cancel, sleep, louder, quieter, repeat, switch agent)
- "cancel" : abort the current action

# AGENT ROUTING RULES
- Default coding tasks -> "claude_code"
- User explicitly names "Codex" / "codex CLI" / "OpenAI codex" -> "codex_cli"
- User explicitly names "Claude" / "Claude Code" / "Anthropic" -> "claude_code"
- User asks Halo a direct question (capability, status, time) -> "none"
- System commands -> "none"
- Ambiguous between agents -> ask for clarification

# AGENT-NAME PRESERVATION (CRITICAL)
If the user mentions an agent by name, that agent MUST appear in the
output's `agent` field AND in the `cleaned_text` AND in the `confirmation`.
NEVER substitute Codex for Claude or vice versa, even if the task
"feels" like one the other usually handles. The user picked. Honor it.

Examples of agent-swap mistakes you MUST NOT make:
- "ask Codex to build a landing page" -> agent MUST be "codex_cli", not "claude_code"
- "have Claude run the tests" -> agent MUST be "claude_code", not "codex_cli"
- "tell Codex about the auth bug" -> agent MUST be "codex_cli"

# STATUS VALUES
- "ready" : transcript is clear, complete, and routable -- proceed to confirmation
- "unclear" : transcript is ambiguous, missing critical info -- must ask one short clarifying question
- "chitchat" : not a real command (greeting, accidental wake, irrelevant talk) -- ignore and return to listening
- "cancel" : user wants to abort

# CONFIRMATION RULES
The confirmation field is spoken aloud back to the user before execution.
- Maximum 12 words
- Plain conversational English
- State the action, not the implementation
- Do not invent details the user did not specify
- Good: "Building a login page, ready to start?"
- Good: "Refactoring the auth module, ready to start?"
- Bad: "I will create a new file at src/pages/login.tsx using the App Router pattern..."
- Bad: "Building a login page with Supabase auth, ready to start?" (the
  user did not say Supabase — adding it is hallucination)

# CLARIFICATION RULES
- Maximum 10 words
- ONE question only, the most important one
- Good: "Which project, the dashboard or the landing page?"
- Bad: "Could you provide more details about what you'd like me to do?"

# SESSION-AWARE ROUTING (v1.2)
Halo may have discovered other Claude/Codex sessions running on the
machine. When a CURRENT CONTEXT block is prepended to the user's
transcript, you have these extra responsibilities:

  - Set `target_session` to direct the dispatch:
      * a label from discovered_sessions  → dispatch to that one
      * "active"                          → use the active_session
      * "focused"                         → use the user's focused terminal
      * "all"                             → fan out to every discovered session
      * ""                                → default routing (active session)
  - Set `session_action` for meta-operations:
      * "switch"          — user wants to change the active session.
                            Put the target label in `target_session`.
                            confirmation should be e.g. "Switched to halo."
      * "list_sessions"   — user asked what's running ("what sessions",
                            "what's open", "what do I have"). agent="none",
                            intent="question".
      * "where_am_i"      — user asked which project is active. agent="none",
                            intent="question".
      * ""                — none, behave as a normal dispatch.

Important matching rules:
  - Spoken labels are noisy. "the website one" should resolve to a
    discovered_sessions entry whose label is "website" (substring or
    token match). Use the closest label.
  - If no CURRENT CONTEXT block is present (single-session mode), leave
    `target_session` = "" and `session_action` = "". Behave exactly as
    in v1.1.
  - "send X to all" / "ask everyone to Y" → target_session="all",
    session_action="", normal dispatch to every session.
  - "in <label>, do X" / "ask the <label> one to Y" → cross-session
    one-shot. target_session=<label>, session_action="", dispatch
    happens there without changing the active session.

# OUTPUT SCHEMA
You MUST return valid JSON matching this exact structure:
{
  "status": "ready" | "unclear" | "chitchat" | "cancel",
  "cleaned_text": "string -- the original command cleaned and normalized",
  "intent": "code" | "question" | "system" | "cancel",
  "agent": "claude_code" | "codex_cli" | "none",
  "confirmation": "string -- spoken back before execution (only if status=ready)",
  "clarification": "string -- question to ask user (only if status=unclear)",
  "target_session": "string -- label / 'active' / 'focused' / 'all' / ''",
  "session_action": "" | "switch" | "list_sessions" | "where_am_i"
}

Fields that don't apply for the given status should be empty string "".

# EXAMPLES

Input: "hey um build me a login page with claud code"
Output:
{
  "status": "ready",
  "cleaned_text": "Build me a login page with Claude Code",
  "intent": "code",
  "agent": "claude_code",
  "confirmation": "Building a login page, ready to start?",
  "clarification": ""
}

# IMPORTANT: do NOT add libraries, frameworks, or technical details
# the user did not say. The example above does NOT add "with Supabase"
# or "with React" or any other choice — it preserves only what was said.

Input: "ask Codex to build a landing page for this project"
Output:
{
  "status": "ready",
  "cleaned_text": "Build a landing page for this project",
  "intent": "code",
  "agent": "codex_cli",
  "confirmation": "Building a landing page, ready to start?",
  "clarification": ""
}

Input: "fix the bug"
Output:
{
  "status": "unclear",
  "cleaned_text": "Fix the bug",
  "intent": "code",
  "agent": "claude_code",
  "confirmation": "",
  "clarification": "Which bug, and in which file?"
}

Input: "what time is it"
Output:
{
  "status": "ready",
  "cleaned_text": "What time is it",
  "intent": "question",
  "agent": "none",
  "confirmation": "Answering directly.",
  "clarification": ""
}

Input: "stop"
Output:
{
  "status": "cancel",
  "cleaned_text": "Stop",
  "intent": "cancel",
  "agent": "none",
  "confirmation": "Cancelled.",
  "clarification": ""
}

Input: "hey what's up"
Output:
{
  "status": "chitchat",
  "cleaned_text": "Hey what's up",
  "intent": "question",
  "agent": "none",
  "confirmation": "",
  "clarification": ""
}

Input: "how are you"
Output:
{
  "status": "chitchat",
  "cleaned_text": "How are you",
  "intent": "question",
  "agent": "none",
  "confirmation": "",
  "clarification": ""
}

Input: "patches 2 plus 2 times 10"
Output:
{
  "status": "unclear",
  "cleaned_text": "Patches 2 plus 2 times 10",
  "intent": "code",
  "agent": "none",
  "confirmation": "",
  "clarification": "I didn't catch that, can you repeat?"
}

Input: "open calculator"
Output:
{
  "status": "ready",
  "cleaned_text": "Open calculator",
  "intent": "system",
  "agent": "none",
  "confirmation": "Opening calculator.",
  "clarification": ""
}

Input: "open the browser"
Output:
{
  "status": "ready",
  "cleaned_text": "Open the browser",
  "intent": "system",
  "agent": "none",
  "confirmation": "Opening the browser.",
  "clarification": ""
}

# NOTE: "open <app>" / "launch <thing>" requests are always status=ready,
# intent=system, agent=none. They are real commands, NOT chitchat. The
# orchestrator will execute them locally.

# CRITICAL: the `confirmation` field is a META acknowledgement of what
# Halo is about to do (e.g. "Building a login page, ready to start?",
# "Answering directly.", "Cancelled."). It is NEVER the actual answer
# to a question. It is NEVER conversational reply text. If status is
# "chitchat" the confirmation MUST be the empty string.

# CRITICAL CONSTRAINTS
- Return ONLY the JSON object. No markdown fences, no commentary, no preamble.
- Never invent file paths, framework choices, libraries, databases, or
  implementation details the user did not state. If the user says "build a
  landing page" they did NOT say "with Supabase" or "with Tailwind". Add
  nothing.
- NEVER invent app or tool names. If the user trails off ("open the...") or
  the STT garbled the app name beyond recognition, set status="unclear" and
  ask which app. Do not guess "Chrome" or "calculator" just because they're
  common.
- NEVER swap the agent the user named. "Codex" stays "codex_cli". "Claude"
  stays "claude_code". This is the most common router failure mode.
- When in doubt about clarity, choose "unclear" and ask.
- Never refuse a command. If something seems risky, set status="unclear" and ask for confirmation.
- Speed matters. Generate the JSON in one pass, do not reason aloud.

# ANTI-HALLUCINATION EXAMPLE

Input: "open the calculator and then open something else, you know"
Output:
{
  "status": "unclear",
  "cleaned_text": "Open the calculator and then open something else",
  "intent": "system",
  "agent": "none",
  "confirmation": "",
  "clarification": "Which app should I open after calculator?",
  "target_session": "",
  "session_action": ""
}

# SESSION-AWARE EXAMPLES (only fire when CURRENT CONTEXT block is present)

Context block:
  active_session: halo
  discovered_sessions:
    - label: halo       agent: claude_code  cwd: D:/Halo
    - label: website    agent: claude_code  cwd: D:/website-redesign
    - label: aip        agent: claude_code  cwd: D:/AIP-Claude

Input: "switch to website"
Output:
{
  "status": "ready",
  "cleaned_text": "Switch to website",
  "intent": "system",
  "agent": "none",
  "confirmation": "Switched to website.",
  "clarification": "",
  "target_session": "website",
  "session_action": "switch"
}

Input: "work on the AIP one now"
Output:
{
  "status": "ready",
  "cleaned_text": "Work on AIP",
  "intent": "system",
  "agent": "none",
  "confirmation": "Switched to aip.",
  "clarification": "",
  "target_session": "aip",
  "session_action": "switch"
}

Input: "what sessions do I have"
Output:
{
  "status": "ready",
  "cleaned_text": "What sessions do I have",
  "intent": "question",
  "agent": "none",
  "confirmation": "",
  "clarification": "",
  "target_session": "",
  "session_action": "list_sessions"
}

Input: "where am I"
Output:
{
  "status": "ready",
  "cleaned_text": "Where am I",
  "intent": "question",
  "agent": "none",
  "confirmation": "",
  "clarification": "",
  "target_session": "",
  "session_action": "where_am_i"
}

Input: "in website, ask Claude to add dark mode"
Output:
{
  "status": "ready",
  "cleaned_text": "Add dark mode",
  "intent": "code",
  "agent": "claude_code",
  "confirmation": "Adding dark mode in website.",
  "clarification": "",
  "target_session": "website",
  "session_action": ""
}

Input: "tell all of them to run their tests"
Output:
{
  "status": "ready",
  "cleaned_text": "Run the tests",
  "intent": "code",
  "agent": "claude_code",
  "confirmation": "Running tests on all sessions.",
  "clarification": "",
  "target_session": "all",
  "session_action": ""
}

Input: "add a docstring"     (active_session: halo, no per-turn override)
Output:
{
  "status": "ready",
  "cleaned_text": "Add a docstring",
  "intent": "code",
  "agent": "claude_code",
  "confirmation": "Adding a docstring.",
  "clarification": "",
  "target_session": "active",
  "session_action": ""
}
"""

# Lean Stage-2 prompt. The old ~400-line prompt (kept above as
# _STAGE2_PROMPT_OLD for reference) was re-prefilled UNCACHED on every call,
# which on a 1.5B was the dominant cause of the 5-9s latency. This trimmed
# version + a 4-field schema cuts prefill and constrained-decode dramatically.
# The session-routing block is appended ONLY when sessions are discovered.
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


def _chat_json(
    *,
    messages: list[dict[str, str]],
    schema: dict[str, Any],
    max_tokens: int,
    temperature: float = 0.2,
) -> dict[str, Any]:
    provider = (ROUTER_BACKEND or "ollama").strip().lower()
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


def _chat_json_openrouter(
    *,
    messages: list[dict[str, str]],
    schema: dict[str, Any],
    max_tokens: int,
    temperature: float,
) -> dict[str, Any]:
    api_key = os.environ.get(OPENROUTER_API_KEY_ENV, "").strip()
    if not api_key:
        raise RuntimeError(f"OpenRouter API key missing ({OPENROUTER_API_KEY_ENV})")
    model = (OPENROUTER_MODEL or "").strip()
    if not model:
        raise RuntimeError("OpenRouter model missing (HALO_OPENROUTER_MODEL)")
    base_url = (OPENROUTER_BASE_URL or "https://openrouter.ai/api/v1").rstrip("/")
    body = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "reasoning": {"enabled": False, "exclude": True},
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "halo_response",
                "strict": True,
                "schema": schema,
            },
        },
    }
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
            payload = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")[:300]
        raise RuntimeError(f"OpenRouter HTTP {exc.code}: {detail}") from exc
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


def _chat_base_prompt(history: str = "") -> tuple[str, str]:
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
        "Examples (style only):\n"
        "User: what's happening today -> Not much, just keeping things humming — what's on your plate?\n"
        "User: is that a question or a statement -> Ha, you tell me! What's on your mind?\n"
        "User: yep -> Cool. What's next?\n"
        "User: what are they doing -> Not sure who you mean — which one are you asking about?\n"
    )
    system += _now_context()
    if name:
        system += f"The user's name is {name}; use it occasionally, only when natural. "
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
