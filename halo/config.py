"""Halo configuration.

Single source of truth for paths, flags, and shared constants. Keep
hardware/model knobs here so individual modules stay focused on logic.
"""

from __future__ import annotations

import os
from pathlib import Path

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
ROUTER_BACKEND = "ollama"

OLLAMA_HOST = "http://localhost:11434"
# qwen2.5:1.5b-instruct is the latency-tier default — non-reasoning,
# small enough to clear the <200ms stage-1 / <500ms stage-2 budget on
# modest GPUs. qwen2.5:3b-instruct is more accurate but ~3x slower on
# the same hardware; qwen3:4b emits chain-of-thought (unusable for voice).
OLLAMA_MODEL = "qwen2.5:1.5b-instruct"

CLAUDE_MODEL = "claude-haiku-4-5"

# Turn-taking thresholds (step 3.5 — adaptive).
# Tight 600 ms base silence at the VAD layer; the orchestrator then
# adds mode-dependent extra wait on top (see halo/turn.py:detect_mode).
TURN_END_SILENCE_MS = 600
TURN_EXTENSION_SEC = 2.0
TURN_MAX_SEC = 30.0
TURN_MAX_WAIT_FOR_FIRST_SPEECH_SEC = 5.0
TURN_MAX_INCOMPLETE_EXTENSIONS = 3
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
CONVERSATION_IDLE_SEC = 5.0
CONVERSATION_IDLE_ENGAGED_SEC = 90.0

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

# Step 3.5 adaptive constants. Total silence-to-commit per mode
# (silero base + orchestrator extra). See detect_mode() in halo/turn.py.
MODE_SILENCE_SEC = {
    "snappy": 0.8,
    "thinking": 2.5,
    "composing": 4.0,
}
HOLD_SUPPRESS_SEC = 10.0
BACKCHANNEL_AFTER_SEC = 3.0
BACKCHANNEL_FREQ_HZ = 200
BACKCHANNEL_DURATION_MS = 200
BACKCHANNEL_VOLUME = 0.08
