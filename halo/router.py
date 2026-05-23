"""Routing brain — two stages over the local Ollama instance.

Stage 1: fast classifier — "has the user finished their thought?"
         Plain-text COMPLETE/INCOMPLETE response, called on every silence
         during a turn. Latency target: <200 ms.

Stage 2: full understanding + routing. Called once when Stage 1 (or a
         terminator phrase, or the hard timeout) ends the turn.
         JSON-schema-constrained output via Ollama's structured-output
         feature, so we never have to defend against malformed JSON.
         Latency target: <500 ms.
"""

from __future__ import annotations

import json
import re
from typing import Any

import ollama

from halo.config import OLLAMA_HOST, OLLAMA_MODEL

# Keep the model resident in VRAM as long as the process is alive —
# 30 min wasn't enough; we saw 11s cold reloads mid-session. 24h
# basically means "never unload unless we crash or you pull the model".
_KEEP_ALIVE = "24h"

# Deterministic fast-path: catch obvious patterns without a model call.
# In real usage Stage 1 latency is dominated by KV-cache flipping
# between Stage 1/Stage 2 prompts (2-4 s cold), so the more we can
# decide here the snappier the loop feels.

_TRAILING_INCOMPLETE_RE = re.compile(
    r"\b(and|but|or|so|because|with|to|for|in|on|at|of|the|a|my|some|"
    r"um|uh|er|ah|like|well|hmm|actually|okay|right|alright|now)"
    r"[\s.,!?]*$",
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

STAGE2_PROMPT = """\
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
- User explicitly says "use codex" or "with OpenAI" -> "codex_cli"
- User asks Halo a direct question (capability, status, time) -> "none"
- System commands -> "none"
- Ambiguous between agents -> ask for clarification

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
- Good: "Building a login page with Supabase auth, ready to start?"
- Bad: "I will create a new file at src/pages/login.tsx using the App Router pattern..."

# CLARIFICATION RULES
- Maximum 10 words
- ONE question only, the most important one
- Good: "Which project, the dashboard or the landing page?"
- Bad: "Could you provide more details about what you'd like me to do?"

# OUTPUT SCHEMA
You MUST return valid JSON matching this exact structure:
{
  "status": "ready" | "unclear" | "chitchat" | "cancel",
  "cleaned_text": "string -- the original command cleaned and normalized",
  "intent": "code" | "question" | "system" | "cancel",
  "agent": "claude_code" | "codex_cli" | "none",
  "confirmation": "string -- spoken back before execution (only if status=ready)",
  "clarification": "string -- question to ask user (only if status=unclear)"
}

Fields that don't apply for the given status should be empty string "".

# EXAMPLES

Input: "hey um build me a login page with claud code using super base"
Output:
{
  "status": "ready",
  "cleaned_text": "Build me a login page with Claude Code using Supabase",
  "intent": "code",
  "agent": "claude_code",
  "confirmation": "Building a login page with Supabase, ready to start?",
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
- Never invent file paths, framework choices, or implementation details the user did not state.
- NEVER invent app or tool names. If the user trails off ("open the...") or
  the STT garbled the app name beyond recognition, set status="unclear" and
  ask which app. Do not guess "Chrome" or "calculator" just because they're
  common.
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
  "clarification": "Which app should I open after calculator?"
}
"""

# JSON schema passed to Ollama's `format` parameter so the model is
# decode-time constrained to valid output — no JSON parsing errors,
# no missing fields.
ROUTE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["ready", "unclear", "chitchat", "cancel"]},
        "cleaned_text": {"type": "string"},
        "intent": {"type": "string", "enum": ["code", "question", "system", "cancel"]},
        "agent": {"type": "string", "enum": ["claude_code", "codex_cli", "none"]},
        "confirmation": {"type": "string"},
        "clarification": {"type": "string"},
    },
    "required": ["status", "cleaned_text", "intent", "agent", "confirmation", "clarification"],
}

_FALLBACK_DECISION = {
    "status": "unclear",
    "cleaned_text": "",
    "intent": "question",
    "agent": "none",
    "confirmation": "",
    "clarification": "Sorry, the router brain didn't respond. Try again.",
}

_client: ollama.Client | None = None


def _get_client() -> ollama.Client:
    global _client
    if _client is None:
        _client = ollama.Client(host=OLLAMA_HOST)
    return _client


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


def understand_and_route(full_transcript: str) -> dict:
    """Stage 2: clean + route the full transcript. Returns the schema dict.

    On any error, returns a safe `unclear` fallback so callers can keep
    going without exception handling.
    """
    try:
        response = _get_client().chat(
            model=OLLAMA_MODEL,
            messages=[
                {"role": "system", "content": STAGE2_PROMPT},
                {"role": "user", "content": full_transcript},
            ],
            format=ROUTE_SCHEMA,
            options={"temperature": 0.2},
            keep_alive=_KEEP_ALIVE,
        )
        return json.loads(response["message"]["content"])
    except Exception as exc:
        return {**_FALLBACK_DECISION, "cleaned_text": full_transcript, "_error": str(exc)}


def preload_router() -> None:
    """Send a warm-up Stage 2 call so the first real turn doesn't pay
    the model-load + KV-cache-fill cost. Stage 1 is rules-only now,
    so it doesn't need a warmup."""
    print("warming up router model (Stage 2)...")
    understand_and_route("hello")
