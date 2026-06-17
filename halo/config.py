"""Halo configuration.

Single source of truth for paths, flags, and shared constants. Keep
hardware/model knobs here so individual modules stay focused on logic.
"""

from __future__ import annotations

import os
from pathlib import Path

from halo.userconfig import cfg

# Flip to False once we're past the build-and-debug phase.
DEBUG = True

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _resolve_models_dir() -> Path:
    """Pick where Kokoro / faster-whisper model files live.

    Precedence:
      1. $HALO_MODELS_DIR  — explicit override, wins always.
      2. <project-root>/models — dev mode (running from a git checkout)
         when that directory already exists.
      3. ~/.halo/models    — installed mode (pip-installed package, no
         dev tree). Created on demand by `halo download-models`.
    """
    override = os.environ.get("HALO_MODELS_DIR")
    if override:
        return Path(override).expanduser().resolve()
    dev_dir = PROJECT_ROOT / "models"
    if dev_dir.exists():
        return dev_dir
    return Path.home() / ".halo" / "models"


MODELS_DIR = _resolve_models_dir()


def _resolve_recordings_dir() -> Path:
    """Same precedence as MODELS_DIR — env override, then dev tree, then
    user data dir. Only used when DEBUG=True (debug wav dumps)."""
    override = os.environ.get("HALO_RECORDINGS_DIR")
    if override:
        return Path(override).expanduser().resolve()
    dev_dir = PROJECT_ROOT / "recordings"
    if (PROJECT_ROOT / "halo").is_dir():
        return dev_dir
    return Path.home() / ".halo" / "recordings"


RECORDINGS_DIR = _resolve_recordings_dir()

# Audio format used end-to-end (wake → record → STT).
SAMPLE_RATE = 16_000

# Router backend for command interpretation. Stage 2 of the routing brain.
# - "ollama": local, free, default. Requires `ollama pull qwen2.5:1.5b-instruct` once.
# - "claude": Anthropic API. Requires ANTHROPIC_API_KEY env var. (Not wired.)
ROUTER_BACKEND = cfg.router.provider

OLLAMA_HOST = cfg.router.ollama_host
# qwen2.5:1.5b-instruct is the latency-tier default — non-reasoning,
# small enough to clear the <200ms stage-1 / <500ms stage-2 budget on
# modest GPUs. qwen2.5:3b-instruct is more accurate but ~3x slower on
# the same hardware; qwen3:4b emits chain-of-thought (unusable for voice).
OLLAMA_MODEL = cfg.router.ollama_model
OPENROUTER_BASE_URL = cfg.router.openrouter_base_url
OPENROUTER_MODEL = cfg.router.openrouter_model
# Optional separate model for chat; falls back to the routing model when blank.
OPENROUTER_CHAT_MODEL = cfg.router.openrouter_chat_model or cfg.router.openrouter_model
OPENROUTER_API_KEY_ENV = cfg.router.openrouter_api_key_env
OPENROUTER_SITE_URL = cfg.router.openrouter_site_url
OPENROUTER_APP_NAME = cfg.router.openrouter_app_name
OPENROUTER_TIMEOUT_SEC = cfg.router.openrouter_timeout_sec

# Image generation (halo/image_gen.py). "generate an image of X" is a LOCAL
# action — Halo calls an image model directly and opens the PNG, rather than
# dispatching to a coding agent (which only draws SVGs). Default backend reuses
# the OPENROUTER_API_KEY Halo already has, via Google's "Nano Banana 2" (fast,
# cheap, renders text). Set HALO_IMAGE_PROVIDER=openai + OPENAI_API_KEY to use
# gpt-image-1.5 instead (best text-in-image).
IMAGE_PROVIDER = os.getenv("HALO_IMAGE_PROVIDER", "openrouter").strip().lower()
IMAGE_MODEL = os.getenv(
    "HALO_IMAGE_MODEL", "google/gemini-3.1-flash-image-preview"
).strip()
IMAGE_OPENAI_MODEL = os.getenv("HALO_IMAGE_OPENAI_MODEL", "gpt-image-1.5").strip()
IMAGE_SIZE = os.getenv("HALO_IMAGE_SIZE", "1024x1024").strip()
IMAGE_DIR = os.getenv("HALO_IMAGE_DIR", str(Path.home() / "Pictures" / "Halo"))

CLAUDE_MODEL = "claude-haiku-4-5"

# Turn-taking thresholds (step 3.5 — adaptive).
# Tight 600 ms base silence at the VAD layer; the orchestrator then
# adds mode-dependent extra wait on top (see halo/turn.py:detect_mode).
TURN_END_SILENCE_MS = 600
TURN_EXTENSION_SEC = 2.0
TURN_MAX_SEC = 14.0  # soft cap — commit a long-but-paused utterance here
TURN_MAX_WAIT_FOR_FIRST_SPEECH_SEC = 5.0
TURN_MAX_INCOMPLETE_EXTENSIONS = 3
# "Never lose the mic": if you're STILL talking when the soft cap hits, don't
# chop you off mid-sentence — extend the window in grace chunks up to a hard
# ceiling, so a genuinely long thought finishes. Only the continuously-speaking
# path extends (a paused/rambling turn still commits at the soft cap), so this
# can't reintroduce the run-on pile-up the 14s cap was added to prevent.
# Set HALO_TURN_CONTINUATION=0 to disable.
TURN_CONTINUATION = os.getenv("HALO_TURN_CONTINUATION", "1").strip().lower() not in (
    "0", "false", "no", "off",
)
TURN_CONTINUATION_GRACE_SEC = 6.0   # extra window granted per extension
TURN_MAX_HARD_SEC = 40.0            # absolute ceiling even while still talking
# Barge-in CAPTURE (opt-in): while Halo is speaking, treat a substantive
# utterance that ISN'T an echo of Halo's own voice as a real interruption —
# stop talking and route it, instead of only honoring "stop"/"wait". OFF by
# default because it depends on your room's echo: without echo cancellation a
# loud speaker can make Halo hear itself. Enable with HALO_BARGE_IN_CAPTURE=1
# and test; the voice.recently_spoke() echo guard filters Halo's own words.
BARGE_IN_CAPTURE = os.getenv("HALO_BARGE_IN_CAPTURE", "0").strip().lower() in (
    "1", "true", "yes", "on",
)
BARGE_IN_CAPTURE_MIN_WORDS = 4   # ignore short blips; real commands are longer
CHIME_FREQ_HZ = 800
CHIME_DURATION_MS = 60

# Mic-side noise gate. Audio chunks with RMS below this floor are
# treated as silence and dropped before they reach silero-VAD / STT.
# Conservative default (0.005) — well below normal speech levels
# (typically 0.05+) so it only filters true ambient room noise.
# Raise to 0.015-0.020 if your environment is noisy and you still
# get spurious wake-ups or "Thank you" hallucinations. Disable by
# setting to 0.0. NVIDIA Broadcast / Krisp / OS-level noise filters
# remove the need for this entirely.
MIC_NOISE_GATE_RMS = 0.005

# Conversation mode: stay awake after wake until either an explicit
# end phrase or N seconds of idle silence (with no active agent jobs).
#
# Two budgets:
# - PRE-engagement (no commands processed yet, e.g. an accidental wake):
#   short, so a phantom wake doesn't hold the mic open. 5s.
# - POST-engagement (you've spoken at least one command):
#   generous, so you can think, read Halo's reply, decide what to ask
#   next, all without being cut off. 90s. End it earlier with any of
#   the explicit end phrases ("goodbye", "go to sleep", "over and out").
#
# Direct-dialogue mode never auto-sleeps regardless of these (see
# run_conversation in __main__).
# Wired to [conversation] config (was a hardcoded literal). The pre-engagement
# window can be generous now that STT wake-verification rejects phantom wakes
# before a conversation even opens — a real wake gets a calm beat to start.
CONVERSATION_IDLE_SEC = cfg.conversation.idle_sec
CONVERSATION_IDLE_ENGAGED_SEC = cfg.conversation.idle_engaged_sec

# Persistent CLI session visibility. When True, Halo echoes each event
# from the agent's stdout to its own terminal — you watch Claude write
# text deltas in real time, see tool calls (Bash / Edit / Read), and
# see the terminating result event. When False, only the parsed
# sentence chunks reach the TTS callback and the rest is silent.
# Per-agent override via `AgentConfig.cli_visible` (None = use this).
AGENT_CLI_VISIBLE = True

# Follow-up gate. After a dispatch puts Halo in direct-dialogue mode,
# the mic stays hot so follow-ups ("now also add tests") don't need
# to re-state the agent name. Without a gate, every transcribed
# utterance — including phone-call audio and side conversations —
# gets piped to the active agent.
#
# When True (default), each direct-mode utterance passes through
# halo.followup_gate.passes() first. Side conversation is dropped
# silently and a `side_convo.ignored` bus event is emitted so the
# dashboard event log shows what was filtered.
#
# Set to False to disable the gate entirely (old v1.1.0 behaviour:
# every direct-mode transcript reaches the agent).
FOLLOWUP_GATE_ENABLED = True

# --- v1.2.1 reply-summarization knobs ---------------------------------
# Long agent replies are exhausting to listen to. The brain (qwen2.5)
# summarizes them to one spoken sentence; the full text is still
# printed to the terminal so nothing is lost.
#
# Streaming agents (Claude): speak the first N sentences live as the
# agent generates them (so the user gets immediate feedback that the
# agent is alive and working). After that, stop speaking; buffer
# silently. At job completion, if the reply turned out long, send the
# untold remainder through summarize_reply() and speak ONE sentence
# capping the result.
#
# Batch agents (Codex): no sentence streaming. If the full result
# is shorter than REPLY_SUMMARIZE_THRESHOLD_CHARS, speak it as-is.
# Otherwise summarize.
LIVE_STREAM_MAX_SENTENCES = 2
REPLY_SUMMARIZE_THRESHOLD_CHARS = 400

# --- dictation mode knobs ---------------------------------------------
# System-wide dictation ("take dictation" -> type my speech into the
# focused field). DICTATION_IDLE_SEC: how long to wait for the next
# utterance before auto-ending dictation. Generous so the user can think
# between sentences. DICTATION_MAX_UTTERANCE_SEC: hard cap on a single
# captured chunk before it commits and gets typed.
DICTATION_IDLE_SEC = 30.0
DICTATION_MAX_UTTERANCE_SEC = 20.0
# Wall-clock budget for the optional per-chunk LLM autocorrect pass. If the
# router model doesn't answer in time, dictation types the locally-cleaned
# text instead so typing never stalls on a slow/offline model.
DICTATION_CORRECT_TIMEOUT_SEC = 6.0

# Step 3.5 adaptive constants. Total silence-to-commit per mode
# (silero base + orchestrator extra). See detect_mode() in halo/turn.py.
MODE_SILENCE_SEC = {
    "snappy": 0.9,
    "thinking": 3.2,   # was 2.5 — gave thinkers too little room, cut them mid-sentence
    "composing": 4.5,  # was 4.0
}
HOLD_SUPPRESS_SEC = 10.0
BACKCHANNEL_AFTER_SEC = 3.0
BACKCHANNEL_FREQ_HZ = 200
BACKCHANNEL_DURATION_MS = 200
BACKCHANNEL_VOLUME = 0.08
