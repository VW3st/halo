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
RECORDINGS_DIR = PROJECT_ROOT / "recordings"

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

# Conversation mode: stay awake after wake until either an explicit
# end phrase or N seconds of idle silence (with no active agent jobs).
# Short by design — the orchestrator extends this whenever an agent
# job is mid-flight, so this only fires when there's truly nothing
# happening.
CONVERSATION_IDLE_SEC = 5.0

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
