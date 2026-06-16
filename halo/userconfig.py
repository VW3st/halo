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
_PROJECT_ENV = Path.cwd() / ".env"


# ---------------------------------------------------------------------------
# Schema — every overridable knob lives here.
# ---------------------------------------------------------------------------


@dataclass
class WakeConfig:
    # Phrase the user says to wake Halo. Must match a model file at
    # <models_dir>/<word>.onnx, or one of openWakeWord's built-ins
    # (alexa, hey_jarvis, hey_mycroft, hey_rhasspy).
    word: str = "halo"
    # Wake-model score (0..1) the wake must reach to fire. LOWER = fires more
    # easily (more sensitive, more false positives). Say the wake word a few
    # times, watch the "heard" scores Halo prints, and set this just below
    # your typical score.
    threshold: float = 0.4
    # silero-VAD must ALSO score above this before a wake counts — a gate
    # against wake-DNN hallucinations on room noise. BUT on a quiet mic it
    # suppresses real speech and the wake never fires; lower it (or 0.0 to
    # disable) if wakes are being MISSED, raise it if room noise false-fires.
    vad_gate: float = 0.5
    # Min seconds between consecutive wake fires (debounce).
    cooldown_sec: float = 2.0
    # How many CONSECUTIVE 80 ms frames must score >= threshold before a wake
    # fires. The per-mic lever for the sensitivity/false-fire tradeoff:
    #   1 = fire on a single qualifying frame (default). Needed for mics whose
    #       "halo" peaks for just one frame (e.g. a Blue Snowball ~0.52). Safe
    #       while the mic's idle noise floor stays well under `threshold`.
    #   2 = require ~160 ms of sustained activation. Use on a hot mic where a
    #       lone transient frame false-fires on random speech (e.g. a BRIO
    #       spiking to 0.70). Raise to 3 if that still isn't enough.
    # (VAD does NOT help with speech false-fires — your speech passes VAD.)
    min_consecutive: int = 1
    # Second-stage wake gate: after the wake DNN fires, transcribe the ~1 s of
    # audio captured just before the fire and confirm the wake word was
    # actually spoken. This is what kills false fires the threshold can't —
    # ordinary speech that scores like "halo" gets rejected because it doesn't
    # transcribe TO "halo". Adds ~0.2-0.4 s on a fire (the model is already
    # loaded). Conservative: only rejects when STT clearly heard non-wake
    # speech, so it won't drop a quiet real "halo". Set false to disable.
    verify: bool = True


@dataclass
class VoiceConfig:
    # "kokoro" = fully local TTS. "elevenlabs" = cloud TTS.
    provider: str = "kokoro"
    # Kokoro voice id (see hexgrad/Kokoro-82M/VOICES.md for the catalog).
    # Notable: af_heart (warm female, A/A grade), af_bella, am_michael.
    kokoro_voice: str = "af_heart"
    # Playback speed multiplier (0.8 = slower, 1.2 = faster).
    kokoro_speed: float = 1.0
    # ElevenLabs settings. Keep the actual secret in env, not TOML.
    elevenlabs_api_key_env: str = "ELEVENLABS_API_KEY"
    elevenlabs_voice_id: str = ""
    elevenlabs_model_id: str = "eleven_flash_v2_5"
    elevenlabs_output_format: str = "pcm_24000"
    elevenlabs_timeout_sec: float = 15.0


@dataclass
class RouterConfig:
    # "ollama" = fully local Stage 2 brain. "openrouter" = cloud brain.
    provider: str = "ollama"
    # Ollama server URL (must be reachable from Halo's machine).
    ollama_host: str = "http://localhost:11434"
    # Ollama model used for Stage 2 routing. qwen2.5:1.5b-instruct is
    # the latency default; qwen2.5:3b-instruct trades 3x latency for
    # better accuracy.
    ollama_model: str = "qwen2.5:1.5b-instruct"
    # OpenRouter settings. Keep the actual secret in env, not TOML.
    openrouter_api_key_env: str = "OPENROUTER_API_KEY"
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_model: str = ""
    # Optional SEPARATE model for casual conversation (chit-chat / Q&A). The
    # routing brain wants a small fast model (granite); natural chat wants a
    # smarter one. Empty = reuse openrouter_model. Routing speed is unaffected.
    openrouter_chat_model: str = ""
    openrouter_site_url: str = ""
    openrouter_app_name: str = "Halo"
    openrouter_timeout_sec: float = 20.0


@dataclass
class ConversationConfig:
    # Idle seconds before sleeping when no command has been processed yet
    # (wake fired but the user hasn't spoken). STT wake-verification now
    # rejects phantom wakes BEFORE a conversation opens, so this can be a
    # relaxed "take a beat to start talking" window rather than a tight
    # phantom-wake guillotine.
    idle_sec: float = 8.0
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
class PersonaConfig:
    # How Halo should address the operator.
    user_name: str = ""
    # Optional role/context passed to agents, e.g. "founder", "developer".
    user_role: str = ""
    # Short behavioral flavor for Halo itself.
    halo_character: str = "direct, practical voice coding assistant"


@dataclass
class PathsConfig:
    # Override the model directory. Empty string = auto-detect (./models
    # in dev checkouts, ~/.halo/models when pip-installed).
    models_dir: str = ""
    # Same precedence rules as models_dir.
    recordings_dir: str = ""


@dataclass
class DictationConfig:
    # Whole-utterance phrases that START dictation (type your speech into
    # the focused field). Pick words your accent transcribes reliably.
    start_phrases: list[str] = field(default_factory=lambda: [
        "dictate", "dictation", "type mode", "start typing", "take dictation",
        "type what i say",
    ])
    # Whole-utterance phrases that STOP dictation.
    stop_phrases: list[str] = field(default_factory=lambda: [
        "stop", "stop typing", "stop dictation", "smart stop", "stop stop",
        "that's it", "i'm done", "done typing",
    ])
    # Fuzzy-match the stop word so accented / slightly-misheard versions
    # ("stob", "stap", "stup", "okay stop") still end dictation. Only applies
    # to short utterances (<= 3 words) so a long dictated sentence containing
    # "stop" is not cut off.
    smart_stop: bool = True
    # Silence (ms) after you stop talking before a phrase commits and is
    # typed. Lower = snappier (text appears sooner) but commits on shorter
    # pauses; raise if phrases get chopped mid-sentence.
    silence_ms: int = 400
    # When False (default), strip the trailing period the STT/corrector adds
    # at the end of each phrase, so dictating in fragments doesn't sprinkle a
    # period after every pause. Internal sentence breaks are kept, and you can
    # still say "period" to insert one. Set True to keep auto-periods.
    auto_period: bool = False
    # Clean each chunk before it is typed — fix spelling, obvious
    # speech-to-text errors, capitalization and punctuation. Always runs a
    # fast local pass; when the router model is reachable it also runs an
    # LLM correction (fail-open, so a slow/missing model never blocks
    # typing). Adds a small per-phrase delay; set false for fastest raw
    # typing.
    autocorrect: bool = True


@dataclass
class McpConfig:
    # When True, Halo passes an MCP server config to every Claude session it
    # spawns, giving the agent extra tools (desktop control, etc.) via Model
    # Context Protocol. Opt-in — desktop control is powerful and Halo runs
    # Claude with bypassPermissions (no approval prompts by voice).
    enabled: bool = False
    # Path to an mcp.json (Claude Code --mcp-config format). Empty = use the
    # bundled default at ~/.halo/mcp.json, which Halo writes on first use to
    # run the Windows-MCP desktop-control server via `uvx windows-mcp serve`.
    config_path: str = ""
    # True = load ONLY the servers in config_path (ignore any you've added
    # with `claude mcp add`). False = also keep those.
    strict: bool = False


@dataclass
class HaloConfig:
    wake: WakeConfig = field(default_factory=WakeConfig)
    voice: VoiceConfig = field(default_factory=VoiceConfig)
    router: RouterConfig = field(default_factory=RouterConfig)
    conversation: ConversationConfig = field(default_factory=ConversationConfig)
    mic: MicConfig = field(default_factory=MicConfig)
    agents: AgentsConfig = field(default_factory=AgentsConfig)
    persona: PersonaConfig = field(default_factory=PersonaConfig)
    paths: PathsConfig = field(default_factory=PathsConfig)
    dictation: DictationConfig = field(default_factory=DictationConfig)
    mcp: McpConfig = field(default_factory=McpConfig)


# Env-var override map — only the knobs users tweak frequently get a
# short env var. Less-common ones can still be set via TOML.
_ENV_OVERRIDES: dict[str, tuple[str, str, type]] = {
    "HALO_WAKE_WORD":           ("wake", "word", str),
    "HALO_WAKE_THRESHOLD":      ("wake", "threshold", float),
    "HALO_WAKE_VAD_GATE":       ("wake", "vad_gate", float),
    "HALO_WAKE_MIN_CONSECUTIVE": ("wake", "min_consecutive", int),
    "HALO_WAKE_VERIFY":          ("wake", "verify", bool),
    "HALO_VOICE_PROVIDER":      ("voice", "provider", str),
    "HALO_VOICE":               ("voice", "kokoro_voice", str),
    "HALO_VOICE_SPEED":         ("voice", "kokoro_speed", float),
    "HALO_ELEVENLABS_API_KEY_ENV": ("voice", "elevenlabs_api_key_env", str),
    "HALO_ELEVENLABS_VOICE_ID": ("voice", "elevenlabs_voice_id", str),
    "HALO_ELEVENLABS_MODEL":    ("voice", "elevenlabs_model_id", str),
    "HALO_ELEVENLABS_OUTPUT_FORMAT": ("voice", "elevenlabs_output_format", str),
    "HALO_ELEVENLABS_TIMEOUT_SEC": ("voice", "elevenlabs_timeout_sec", float),
    "HALO_ROUTER_PROVIDER":     ("router", "provider", str),
    "HALO_OLLAMA_HOST":         ("router", "ollama_host", str),
    "HALO_OLLAMA_MODEL":        ("router", "ollama_model", str),
    "HALO_OPENROUTER_API_KEY_ENV": ("router", "openrouter_api_key_env", str),
    "HALO_OPENROUTER_BASE_URL": ("router", "openrouter_base_url", str),
    "HALO_OPENROUTER_MODEL":    ("router", "openrouter_model", str),
    "HALO_OPENROUTER_CHAT_MODEL": ("router", "openrouter_chat_model", str),
    "HALO_OPENROUTER_SITE_URL": ("router", "openrouter_site_url", str),
    "HALO_OPENROUTER_APP_NAME": ("router", "openrouter_app_name", str),
    "HALO_OPENROUTER_TIMEOUT_SEC": ("router", "openrouter_timeout_sec", float),
    "HALO_IDLE_SEC":            ("conversation", "idle_sec", float),
    "HALO_IDLE_ENGAGED_SEC":    ("conversation", "idle_engaged_sec", float),
    "HALO_MIC_NOISE_GATE":      ("mic", "noise_gate_rms", float),
    "HALO_CLI_VISIBLE":         ("agents", "cli_visible", bool),
    "HALO_USER_NAME":           ("persona", "user_name", str),
    "HALO_USER_ROLE":           ("persona", "user_role", str),
    "HALO_CHARACTER":           ("persona", "halo_character", str),
    "HALO_DICTATION_SMART_STOP":  ("dictation", "smart_stop", bool),
    "HALO_DICTATION_AUTOCORRECT": ("dictation", "autocorrect", bool),
    "HALO_MCP_ENABLED":           ("mcp", "enabled", bool),
    "HALO_MCP_CONFIG_PATH":       ("mcp", "config_path", str),
}


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _load_project_env(path: Path = _PROJECT_ENV) -> list[str]:
    """Load simple KEY=VALUE lines from ./env before env overrides.

    Existing process environment wins, so shell-set secrets are not
    overwritten. This intentionally handles the common dotenv subset only.
    """
    if not path.is_file():
        return []
    notes: list[str] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception as exc:
        return [f"  {path}: failed to read .env ({exc})"]
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key or not value or key in os.environ:
            continue
        os.environ[key] = value
    return notes


def _apply_dict(cfg: HaloConfig, data: dict, source: str) -> list[str]:
    """Apply a TOML-loaded dict onto `cfg`. Returns notes on unknown keys."""
    notes: list[str] = []
    for section_name, section_data in data.items():
        # Per-host overlays live under [profiles.<hostname>.<section>] and are
        # applied separately by _apply_profile — skip them here.
        if section_name == "profiles":
            continue
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


def _apply_profile(cfg: HaloConfig, data: dict, source: str, host: str) -> list[str]:
    """Apply a per-machine overlay from [profiles.<host>.<section>].

    This is the "adapts to the machine it runs on" mechanism: the same config
    file can carry e.g. a Blue Snowball's wake threshold on one PC and a BRIO's
    on another, keyed by hostname. `halo calibrate` writes these blocks. The
    overlay sits above the base config but below env vars in precedence.
    """
    profiles = data.get("profiles")
    if not isinstance(profiles, dict):
        return []
    prof = profiles.get(host)
    if not isinstance(prof, dict):
        return []
    notes = _apply_dict(cfg, prof, f"{source} [profiles.{host}]")
    if any(isinstance(v, dict) for v in prof.values()):
        notes.append(f"  applied host profile [profiles.{host}] from {source}")
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


def _toml_value(v: object) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    return '"' + str(v).replace('"', '\\"') + '"'


def write_host_profile(section_name: str, values: dict, host: str | None = None) -> Path:
    """Write/replace a [profiles.<host>.<section>] block in the USER config
    (~/.halo/config.toml). Used by `halo calibrate` to persist per-machine
    wake settings without touching the rest of the file."""
    import socket

    if host is None:
        host = socket.gethostname().strip()
    path = _USER_CONFIG
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    header = f"[profiles.{host}.{section_name}]"
    # Drop any existing block with this exact header (up to the next table).
    out: list[str] = []
    skip = False
    for line in existing.splitlines():
        s = line.strip()
        if s == header:
            skip = True
            continue
        if skip and s.startswith("["):
            skip = False
        if not skip:
            out.append(line)
    body = [header] + [f"{k} = {_toml_value(v)}" for k, v in values.items()]
    text = ("\n".join(out).rstrip() + "\n\n" + "\n".join(body) + "\n").lstrip("\n")
    path.write_text(text, encoding="utf-8")
    return path


def load_config() -> tuple[HaloConfig, list[str], list[Path]]:
    """Build the effective config. Returns (cfg, notes, sources_used)."""
    import socket

    cfg = HaloConfig()
    notes: list[str] = []
    sources: list[Path] = []
    notes += _load_project_env()
    if _PROJECT_ENV.is_file():
        sources.append(_PROJECT_ENV)
    try:
        host = socket.gethostname().strip()
    except Exception:
        host = ""
    user_data = _load_toml(_USER_CONFIG) if _USER_CONFIG.is_file() else {}
    proj_data = _load_toml(_PROJECT_CONFIG) if _PROJECT_CONFIG.is_file() else {}
    # 1. User config (lower precedence than project + env)
    if user_data:
        notes += _apply_dict(cfg, user_data, str(_USER_CONFIG))
        sources.append(_USER_CONFIG)
    # 2. Project config (overrides user)
    if proj_data:
        notes += _apply_dict(cfg, proj_data, str(_PROJECT_CONFIG))
        sources.append(_PROJECT_CONFIG)
    # 2b. Per-machine profile overlay [profiles.<host>] (overrides base config).
    if host:
        notes += _apply_profile(cfg, user_data, str(_USER_CONFIG), host)
        notes += _apply_profile(cfg, proj_data, str(_PROJECT_CONFIG), host)
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

# Wake-DNN activation threshold (0.0-1.0). LOWER = more sensitive (fires
# more easily). Say the wake word a few times, watch the "heard" scores
# Halo prints, and set this just below your typical score. Raise if it
# false-fires on room noise.
threshold = 0.4

# silero-VAD must ALSO score above this before a wake counts. Filters
# wake-DNN hallucinations on non-speech noise — but on a quiet mic it
# suppresses your real "halo" and the wake never fires. Lower it (or set
# 0.0 to disable) if wakes are being MISSED; raise it if noise false-fires.
# NOTE: VAD can't stop your own SPEECH from false-triggering (speech passes
# VAD); for that, use min_consecutive below.
vad_gate = 0.5

# Consecutive 80 ms frames required to fire — the per-mic sensitivity lever.
# 1 (default) fires on a single frame >= threshold; needed for mics whose
# "halo" peaks for one frame only (e.g. a Blue Snowball ~0.52). Raise to 2-3
# on a hot mic where random speech false-fires on a lone transient frame.
min_consecutive = 1

# Second-stage wake gate. After the DNN fires, transcribe the ~1 s before the
# fire and confirm "halo" was really said — rejects false fires that the
# threshold can't (ordinary speech that scores high but isn't the wake word).
# Adds ~0.2-0.4 s on a fire. true = recommended; false = DNN score only.
verify = true

[voice]
# TTS provider: "kokoro" for fully local, "elevenlabs" for cloud TTS.
provider = "kokoro"

# Kokoro voice id. See hexgrad/Kokoro-82M/VOICES.md for the catalog.
# Notable: af_heart (warm female, A/A grade), af_bella, am_michael.
kokoro_voice = "af_heart"

# Playback speed multiplier. 1.0 = natural, 1.2 = noticeably faster.
kokoro_speed = 1.0

# ElevenLabs options. Put the real key in .env as ELEVENLABS_API_KEY.
elevenlabs_api_key_env = "ELEVENLABS_API_KEY"
elevenlabs_voice_id = ""
elevenlabs_model_id = "eleven_flash_v2_5"
elevenlabs_output_format = "pcm_24000"
elevenlabs_timeout_sec = 15.0

[router]
# Router provider: "ollama" for fully local, "openrouter" for cloud LLM.
provider = "ollama"

# Ollama server URL.
ollama_host = "http://localhost:11434"

# Stage 2 routing model. qwen2.5:1.5b-instruct is the latency tier;
# qwen2.5:3b-instruct trades 3x latency for better accuracy.
ollama_model = "qwen2.5:1.5b-instruct"

# OpenRouter options. Put the real key in .env as OPENROUTER_API_KEY.
openrouter_api_key_env = "OPENROUTER_API_KEY"
openrouter_base_url = "https://openrouter.ai/api/v1"
openrouter_model = ""
openrouter_site_url = ""
openrouter_app_name = "Halo"
openrouter_timeout_sec = 20.0

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

[persona]
# How Halo addresses you.
user_name = ""

# Optional context passed to agents, e.g. "founder", "developer".
user_role = ""

# Short behavioral flavor for Halo.
halo_character = "direct, practical voice coding assistant"

[paths]
# Override the model dir. Empty = auto-detect (./models in dev,
# ~/.halo/models when pip-installed).
models_dir = ""
recordings_dir = ""

[dictation]
# Say one of these (on its own) to START dictation — Halo types your
# speech into whatever text field has focus. Pick words your accent
# transcribes reliably.
start_phrases = ["dictate", "type mode", "start typing", "take dictation"]

# Say one of these to STOP dictation.
stop_phrases = ["stop", "stop typing", "stop dictation", "that's it", "i'm done"]

# Fuzzy-match the stop word so an accented or slightly-misheard "stop"
# (stob / stap / stup / "okay stop") still ends dictation. Only applies to
# short utterances so a long sentence containing "stop" isn't cut off.
smart_stop = true

# Silence (ms) after you stop talking before a phrase is typed. Lower =
# snappier; raise if phrases get chopped mid-sentence.
silence_ms = 400

# Strip the trailing period added at the end of each phrase so dictating in
# fragments doesn't add a period after every pause (say "period" to insert
# one). Set true to keep auto-periods.
auto_period = false

# Clean each chunk before typing (fix spelling / transcription slips /
# punctuation). Always does a fast local cleanup; also runs an LLM
# correction via your router model when reachable (fail-open). Set false
# for fastest raw typing with no cleanup delay.
autocorrect = true

[mcp]
# Give the Claude sessions Halo spawns extra tools via MCP. With desktop
# control you can say: "Claude, take a screenshot and open Notepad",
# "click the Save button", etc.
#
# One-time setup: install uv  ->  pip install uv
# Then set enabled = true and restart Halo. On first use Halo writes a
# default ~/.halo/mcp.json that runs the Windows-MCP server via
# `uvx windows-mcp serve` (uv fetches its own Python 3.13; your venv is
# untouched; first launch downloads the server, ~30-60s).
enabled = false

# Path to an mcp.json (Claude --mcp-config format). Empty = bundled default.
config_path = ""

# true = load ONLY config_path's servers (ignore `claude mcp add` ones).
strict = false

# ---------------------------------------------------------------------------
# Per-machine overlays. Any [section] above can be repeated under
# [profiles.<hostname>.<section>] to override it on ONE machine only — handy
# when the same config syncs across PCs with different mics. Profiles win over
# the base sections above; env vars still win over everything.
#
# `halo calibrate` writes the wake block for you (measures your mic + voice):
#
#   [profiles.MY-DESKTOP.wake]
#   threshold = 0.45
#   min_consecutive = 2
#   verify = true
#
#   [profiles.MY-LAPTOP.wake]
#   threshold = 0.35
#   min_consecutive = 1
"""
