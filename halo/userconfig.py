"""User-facing config layer for Halo.

Single source of truth for every knob a user might want to tune without
editing Halo's source. Resolution order, highest precedence first:

    1. Environment variable     (e.g. HALO_WAKE_THRESHOLD=0.85)
    2. Project-local TOML       (./halo-config.toml next to the cwd)
    3. User TOML                (~/.halo/config.toml)
    4. Built-in defaults        (the dataclass field values below)

The `cfg` module-level object exposes everything via attribute access:

    from halo.userconfig import cfg
    print(cfg.wake.threshold)        # 0.75 or whatever was loaded
    print(cfg.router.ollama_model)

`halo/config.py` re-exports the most-imported constants from `cfg` for
backwards compatibility, so the legacy `from halo.config import
CONVERSATION_IDLE_SEC` style still works.

`halo config` subcommand prints the effective config + the source file
path. `halo config --init` dumps a fully-commented template to
`~/.halo/config.toml`.
"""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass, field, asdict, fields, is_dataclass
from pathlib import Path
from typing import Any


_USER_CONFIG = Path.home() / ".halo" / "config.toml"
_PROJECT_CONFIG = Path.cwd() / "halo-config.toml"


# ---------------------------------------------------------------------------
# Schema — every overridable knob lives here.
# ---------------------------------------------------------------------------


@dataclass
class WakeConfig:
    # Phrase the user says to wake Halo. Must match a model file at
    # <models_dir>/<word>.onnx, or one of openWakeWord's built-ins
    # (alexa, hey_jarvis, hey_mycroft, hey_rhasspy).
    word: str = "halo"
    # Score above which the wake fires (0.0-1.0).
    threshold: float = 0.75
    # silero-VAD must also score above this. Acts as a gate against
    # wake-DNN hallucinations on pure room noise. 0.0 disables the gate.
    vad_gate: float = 0.7
    # Min seconds between consecutive wake fires (debounce).
    cooldown_sec: float = 2.0


@dataclass
class VoiceConfig:
    # Kokoro voice id (see hexgrad/Kokoro-82M/VOICES.md for the catalog).
    # Notable: af_heart (warm female, A/A grade), af_bella, am_michael.
    kokoro_voice: str = "af_heart"
    # Playback speed multiplier (0.8 = slower, 1.2 = faster).
    kokoro_speed: float = 1.0


@dataclass
class RouterConfig:
    # Ollama server URL (must be reachable from Halo's machine).
    ollama_host: str = "http://localhost:11434"
    # Ollama model used for Stage 2 routing. qwen2.5:1.5b-instruct is
    # the latency default; qwen2.5:3b-instruct trades 3x latency for
    # better accuracy.
    ollama_model: str = "qwen2.5:1.5b-instruct"


@dataclass
class ConversationConfig:
    # Idle seconds before sleeping when no command has been processed yet
    # (e.g. wake fired but the user never spoke). Kept short so phantom
    # wakes close out fast.
    idle_sec: float = 5.0
    # Idle seconds AFTER the user has spoken at least one command this
    # conversation. Generous so you have time to think/read Halo's reply.
    idle_engaged_sec: float = 90.0


@dataclass
class MicConfig:
    # Drop audio chunks whose RMS is below this floor before silero-VAD
    # sees them. Conservative default; raise to 0.015-0.020 in noisy
    # rooms. 0.0 disables the gate.
    noise_gate_rms: float = 0.005


@dataclass
class AgentsConfig:
    # When True, Claude's stdout JSON events are mirrored to a separate
    # PowerShell console window (one per session) live-tailing
    # ~/.halo/runtime/<agent>.log. False = headless. Per-agent override
    # via AgentConfig.cli_visible in halo/agents.py.
    cli_visible: bool = True


@dataclass
class PathsConfig:
    # Override the model directory. Empty string = auto-detect (./models
    # in dev checkouts, ~/.halo/models when pip-installed).
    models_dir: str = ""
    # Same precedence rules as models_dir.
    recordings_dir: str = ""


@dataclass
class HaloConfig:
    wake: WakeConfig = field(default_factory=WakeConfig)
    voice: VoiceConfig = field(default_factory=VoiceConfig)
    router: RouterConfig = field(default_factory=RouterConfig)
    conversation: ConversationConfig = field(default_factory=ConversationConfig)
    mic: MicConfig = field(default_factory=MicConfig)
    agents: AgentsConfig = field(default_factory=AgentsConfig)
    paths: PathsConfig = field(default_factory=PathsConfig)


# Env-var override map — only the knobs users tweak frequently get a
# short env var. Less-common ones can still be set via TOML.
_ENV_OVERRIDES: dict[str, tuple[str, str, type]] = {
    "HALO_WAKE_WORD":           ("wake", "word", str),
    "HALO_WAKE_THRESHOLD":      ("wake", "threshold", float),
    "HALO_WAKE_VAD_GATE":       ("wake", "vad_gate", float),
    "HALO_VOICE":               ("voice", "kokoro_voice", str),
    "HALO_VOICE_SPEED":         ("voice", "kokoro_speed", float),
    "HALO_OLLAMA_HOST":         ("router", "ollama_host", str),
    "HALO_OLLAMA_MODEL":        ("router", "ollama_model", str),
    "HALO_IDLE_SEC":            ("conversation", "idle_sec", float),
    "HALO_IDLE_ENGAGED_SEC":    ("conversation", "idle_engaged_sec", float),
    "HALO_MIC_NOISE_GATE":      ("mic", "noise_gate_rms", float),
    "HALO_CLI_VISIBLE":         ("agents", "cli_visible", bool),
}


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _apply_dict(cfg: HaloConfig, data: dict, source: str) -> list[str]:
    """Apply a TOML-loaded dict onto `cfg`. Returns notes on unknown keys."""
    notes: list[str] = []
    for section_name, section_data in data.items():
        section = getattr(cfg, section_name, None)
        if section is None or not is_dataclass(section):
            notes.append(f"  {source}: unknown section [{section_name}]")
            continue
        if not isinstance(section_data, dict):
            notes.append(f"  {source}: [{section_name}] should be a table")
            continue
        valid_keys = {f.name for f in fields(section)}
        for key, value in section_data.items():
            if key not in valid_keys:
                notes.append(f"  {source}: unknown key [{section_name}].{key}")
                continue
            # Coerce booleans from TOML / env strings
            field_type = next(f.type for f in fields(section) if f.name == key)
            if field_type is bool or field_type == "bool":
                value = _coerce_bool(value)
            setattr(section, key, value)
    return notes


def _apply_env(cfg: HaloConfig) -> list[str]:
    notes: list[str] = []
    for env_name, (section_name, key, caster) in _ENV_OVERRIDES.items():
        raw = os.environ.get(env_name)
        if raw is None or raw == "":
            continue
        try:
            value = _coerce_bool(raw) if caster is bool else caster(raw)
        except (TypeError, ValueError) as exc:
            notes.append(f"  env {env_name}={raw!r}: {exc} — keeping default")
            continue
        section = getattr(cfg, section_name)
        setattr(section, key, value)
        notes.append(f"  env {env_name} -> {section_name}.{key} = {value!r}")
    return notes


def _load_toml(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except Exception as exc:
        print(f"  warning: failed to read {path}: {exc}", file=sys.stderr)
        return {}


def load_config() -> tuple[HaloConfig, list[str], list[Path]]:
    """Build the effective config. Returns (cfg, notes, sources_used)."""
    cfg = HaloConfig()
    notes: list[str] = []
    sources: list[Path] = []
    # 1. User config (lower precedence than project + env)
    if _USER_CONFIG.is_file():
        notes += _apply_dict(cfg, _load_toml(_USER_CONFIG), str(_USER_CONFIG))
        sources.append(_USER_CONFIG)
    # 2. Project config (overrides user)
    if _PROJECT_CONFIG.is_file():
        notes += _apply_dict(cfg, _load_toml(_PROJECT_CONFIG), str(_PROJECT_CONFIG))
        sources.append(_PROJECT_CONFIG)
    # 3. Env vars (highest precedence)
    notes += _apply_env(cfg)
    return cfg, notes, sources


cfg, _notes, _sources = load_config()
# Print non-default notes once at import. Quiet if everything is default.
if _notes:
    print("halo config:")
    for line in _notes:
        print(line)


# ---------------------------------------------------------------------------
# Helpers for the `halo config` CLI
# ---------------------------------------------------------------------------


def effective_config_as_dict() -> dict:
    """Plain dict of the current effective config (for printing)."""
    return asdict(cfg)


def config_sources() -> list[Path]:
    """Files that were actually merged into the effective config."""
    return list(_sources)


def user_config_path() -> Path:
    """The standard `~/.halo/config.toml` location, whether it exists or not."""
    return _USER_CONFIG


def write_template(path: Path | None = None, overwrite: bool = False) -> Path:
    """Dump a fully-commented TOML template to `path` (default: user config).
    Refuses to overwrite an existing file unless `overwrite=True`."""
    target = path or _USER_CONFIG
    if target.exists() and not overwrite:
        raise FileExistsError(
            f"{target} already exists — pass overwrite=True to replace it"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_TEMPLATE, encoding="utf-8")
    return target


_TEMPLATE = """# Halo configuration. Every key is optional — omit any line to keep the
# built-in default. Precedence: env var > project ./halo-config.toml >
# this file > defaults.
#
# Run `halo config` to print the currently effective values.

[wake]
# Phrase that wakes Halo. Must match either a built-in openWakeWord
# model name (alexa, hey_jarvis, hey_mycroft, hey_rhasspy) OR a custom
# ONNX file at <models_dir>/<word>.onnx.
word = "halo"

# Wake-DNN activation threshold (0.0-1.0). 0.75 is the shipped default
# for the custom "halo" model. Raise if false-firing on room noise;
# lower if real "halo" sometimes doesn't fire.
threshold = 0.75

# silero-VAD must ALSO score above this before a wake counts. Filters
# wake-DNN hallucinations on noise that isn't actually human speech.
# Set to 0.0 to disable the gate.
vad_gate = 0.7

[voice]
# Kokoro voice id. See hexgrad/Kokoro-82M/VOICES.md for the catalog.
# Notable: af_heart (warm female, A/A grade), af_bella, am_michael.
kokoro_voice = "af_heart"

# Playback speed multiplier. 1.0 = natural, 1.2 = noticeably faster.
kokoro_speed = 1.0

[router]
# Ollama server URL.
ollama_host = "http://localhost:11434"

# Stage 2 routing model. qwen2.5:1.5b-instruct is the latency tier;
# qwen2.5:3b-instruct trades 3x latency for better accuracy.
ollama_model = "qwen2.5:1.5b-instruct"

[conversation]
# Seconds of silence before Halo sleeps when no command has been
# processed yet this conversation (e.g. accidental wake).
idle_sec = 5.0

# Seconds of silence AFTER you've spoken at least one command. Long so
# you can think between turns.
idle_engaged_sec = 90.0

[mic]
# Drop audio chunks below this RMS floor before silero-VAD sees them.
# Conservative default; raise to 0.015-0.020 in noisy rooms.
noise_gate_rms = 0.005

[agents]
# When true, each persistent Claude session opens a separate PowerShell
# console window live-tailing its event log. False = headless.
cli_visible = true

[paths]
# Override the model dir. Empty = auto-detect (./models in dev,
# ~/.halo/models when pip-installed).
models_dir = ""
recordings_dir = ""
"""
