"""Diagnostic command: `halo doctor`.

Read-only health check. Probes every external dependency Halo needs and
prints a checklist with the exact command to fix anything that's missing.
Never installs, modifies, or runs anything beyond `--version` style
probes against binaries that are already on PATH.

Run via the CLI: `halo doctor`. Exit code is 0 when every check passes,
1 otherwise.
"""

from __future__ import annotations

import shutil
import sys
import urllib.error
import urllib.request
import os
from typing import Callable

from halo.agents import AGENTS, check_availability
from halo.config import MODELS_DIR, OLLAMA_HOST, OLLAMA_MODEL
from halo.userconfig import cfg

OK = "[ok]   "
WARN = "[warn] "
FAIL = "[fail] "

_KOKORO_FILES = ("kokoro-v1.0.fp16.onnx", "voices-v1.0.bin")


def _check_python() -> tuple[bool, str, str]:
    v = sys.version_info
    if v >= (3, 11):
        return True, OK, f"Python {v.major}.{v.minor}.{v.micro}"
    return False, FAIL, (
        f"Python {v.major}.{v.minor}.{v.micro} — Halo requires 3.11+. "
        f"Install a newer Python and recreate the venv."
    )


def _check_ollama() -> tuple[bool, str, str]:
    """Reachable + the routing model is pulled?"""
    url = f"{OLLAMA_HOST.rstrip('/')}/api/tags"
    try:
        with urllib.request.urlopen(url, timeout=3.0) as r:
            body = r.read().decode("utf-8", errors="ignore")
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        return False, FAIL, (
            f"Ollama not reachable at {OLLAMA_HOST} ({exc.__class__.__name__}). "
            f"Install: https://ollama.com/download — it auto-starts on localhost:11434."
        )
    if OLLAMA_MODEL not in body:
        return False, WARN, (
            f"Ollama is running but model '{OLLAMA_MODEL}' isn't pulled. "
            f"Fix: ollama pull {OLLAMA_MODEL}"
        )
    return True, OK, f"Ollama up @ {OLLAMA_HOST}; model '{OLLAMA_MODEL}' present"


def _check_agents() -> list[tuple[bool, str, str]]:
    """Per-agent binary + responsiveness probe."""
    out: list[tuple[bool, str, str]] = []
    statuses = check_availability(refresh=True)
    for key, cfg in AGENTS.items():
        st = statuses.get(key, {})
        binary = st.get("binary", cfg.first_call[0])
        hint = st.get("install_hint", "")
        if st.get("installed") and st.get("responsive"):
            out.append((True, OK, f"{cfg.spoken_name} ({binary}) responsive"))
        elif st.get("installed"):
            out.append((False, WARN, (
                f"{cfg.spoken_name} ({binary}) on PATH but `--version` failed. "
                f"Usually means you haven't logged in yet. Hint: {hint}"
            )))
        else:
            out.append((False, FAIL, (
                f"{cfg.spoken_name} ({binary}) not on PATH. Install: {hint}"
            )))
    return out


def _check_kokoro() -> tuple[bool, str, str]:
    missing = [f for f in _KOKORO_FILES if not (MODELS_DIR / f).exists()]
    if not missing:
        return True, OK, f"Kokoro models present in {MODELS_DIR}"
    return False, WARN, (
        f"Kokoro missing: {', '.join(missing)} in {MODELS_DIR}. "
        f"Fix: halo download-models (Halo runs in silent mode without these)"
    )


def _check_openrouter() -> tuple[bool, str, str]:
    key_env = cfg.router.openrouter_api_key_env
    if not os.environ.get(key_env):
        return False, FAIL, f"OpenRouter key missing. Set {key_env} in .env."
    if not cfg.router.openrouter_model:
        return False, FAIL, "OpenRouter model missing. Set HALO_OPENROUTER_MODEL in .env."
    return True, OK, f"OpenRouter configured with model '{cfg.router.openrouter_model}'"


def _check_elevenlabs() -> tuple[bool, str, str]:
    key_env = cfg.voice.elevenlabs_api_key_env
    if not os.environ.get(key_env):
        return False, WARN, f"ElevenLabs key missing. Set {key_env} in .env."
    if not cfg.voice.elevenlabs_voice_id:
        return False, WARN, "ElevenLabs voice id missing. Set HALO_ELEVENLABS_VOICE_ID in .env."
    return True, OK, f"ElevenLabs configured with voice '{cfg.voice.elevenlabs_voice_id}'"


def _check_optional_binaries() -> list[tuple[bool, str, str]]:
    """Nice-to-haves that don't block Halo from starting but enable
    specific features (Node for installing claude/codex via npm)."""
    out: list[tuple[bool, str, str]] = []
    if shutil.which("npm"):
        out.append((True, OK, "npm on PATH (needed to install claude/codex)"))
    else:
        out.append((False, WARN,
            "npm not on PATH. Required only if you want to install "
            "claude/codex CLIs via npm. Install Node.js from nodejs.org."))
    return out


def run() -> int:
    print("halo doctor — checking dependencies")
    print("=" * 50)

    checks: list[tuple[str, Callable[[], object]]] = [("Python", _check_python)]
    router_provider = (cfg.router.provider or "ollama").strip().lower()
    voice_provider = (cfg.voice.provider or "kokoro").strip().lower()
    checks.append(("OpenRouter", _check_openrouter) if router_provider == "openrouter" else ("Ollama", _check_ollama))
    checks.append(("ElevenLabs", _check_elevenlabs) if voice_provider == "elevenlabs" else ("Kokoro", _check_kokoro))

    any_fail = False

    for label, fn in checks:
        ok, tag, msg = fn()
        if not ok and tag == FAIL:
            any_fail = True
        print(f"{tag}{label:8s} {msg}")

    print(f"\n  Coding agents")
    for ok, tag, msg in _check_agents():
        if not ok and tag == FAIL:
            any_fail = True
        print(f"  {tag}{msg}")

    print(f"\n  Optional")
    for ok, tag, msg in _check_optional_binaries():
        # Optional checks never count as a fail.
        print(f"  {tag}{msg}")

    print("=" * 50)
    if any_fail:
        print("Some required checks failed. Fix the [fail] items above, then "
              "re-run `halo doctor`.")
        return 1
    print("All required checks passed. Run `halo` to start the voice loop.")
    return 0


if __name__ == "__main__":
    sys.exit(run())
