"""Real AI image generation for Halo.

The coding agents (Claude Code / Codex) hand-draw SVGs when asked to "generate
an image" — they write markup, they don't call a raster image model. This
module is the actual generator: it calls an image-generation API, decodes the
returned PNG, writes it to disk, and returns the path. Halo's tool layer
(`halo/tools.py`) wires "generate an image of X" to it as a LOCAL action, so it
never gets dispatched to a coding agent.

Two backends behind one `generate_image()`:

  * openrouter (default) — POST /chat/completions with `modalities:["image",
    "text"]`. Uses the OPENROUTER_API_KEY Halo already has; default model is
    Google's "Nano Banana 2" (gemini-3.1-flash-image-preview): fast, cheap
    (~$0.002/image), renders text acceptably. The image comes back base64 in
    `choices[0].message.images[0].image_url.url` as a data URI.
  * openai (opt-in) — POST /v1/images/generations with gpt-image-1.5 (best
    text-in-image / editing). Needs OPENAI_API_KEY. Image is base64 in
    `data[0].b64_json`.

Pure urllib — no new dependency, matching halo/router.py's HTTP style.
Configure via HALO_IMAGE_PROVIDER / HALO_IMAGE_MODEL / HALO_IMAGE_DIR.
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

from halo.config import (
    IMAGE_DIR,
    IMAGE_MODEL,
    IMAGE_OPENAI_MODEL,
    IMAGE_PROVIDER,
    IMAGE_SIZE,
    OPENROUTER_API_KEY_ENV,
    OPENROUTER_BASE_URL,
)

_IMAGE_TIMEOUT_SEC = 90.0  # image gen runs 5-30s; give cloud latency room

# Words that mean the user WANTS text rendered in the image — only then do we
# skip the "no text" instruction (the models honor an inline negative prompt).
_WANTS_TEXT_RE = re.compile(
    r"\b(text|caption|title|heading|headline|words?|letter|label|sign|logo|"
    r"says?|saying|written|quote|slogan|tagline|poster|meme)\b",
    re.IGNORECASE,
)


def build_prompt(subject: str) -> str:
    """Turn a short spoken subject into a fuller image prompt. Light touch —
    modern models parse natural language well; we just add a couple of quality
    nudges and a 'no text' negative unless the user asked for text."""
    subject = (subject or "").strip().rstrip(".")
    if not subject:
        return ""
    prompt = subject
    if not _WANTS_TEXT_RE.search(subject):
        prompt += ". No text, no watermark, no letters."
    prompt += " High detail, sharp focus."
    return prompt


def _out_path() -> Path:
    out_dir = Path(IMAGE_DIR).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir / f"halo-image-{int(time.time())}.png"


def _generate_openrouter(prompt: str, model: str) -> bytes:
    api_key = os.environ.get(OPENROUTER_API_KEY_ENV, "").strip()
    if not api_key:
        raise RuntimeError(f"OpenRouter API key missing ({OPENROUTER_API_KEY_ENV})")
    base_url = (OPENROUTER_BASE_URL or "https://openrouter.ai/api/v1").rstrip("/")
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "modalities": ["image", "text"],
    }
    req = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/VW3st/halo",
            "X-Title": "Halo",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=_IMAGE_TIMEOUT_SEC) as resp:
        payload = json.loads(resp.read())
    # The image is base64 in message.images[].image_url.url, NOT in content.
    msg = (payload.get("choices") or [{}])[0].get("message", {})
    images = msg.get("images") or []
    if not images:
        # Some models echo a refusal in content instead of returning an image.
        text = (msg.get("content") or "").strip()
        raise RuntimeError(text[:200] or "no image returned")
    data_uri = images[0].get("image_url", {}).get("url", "")
    b64 = data_uri.split(",", 1)[1] if "," in data_uri else data_uri
    if not b64:
        raise RuntimeError("empty image data")
    return base64.b64decode(b64)


def _generate_openai(prompt: str, model: str, size: str) -> bytes:
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OpenAI API key missing (OPENAI_API_KEY)")
    body = {"model": model, "prompt": prompt, "size": size, "n": 1}
    req = urllib.request.Request(
        "https://api.openai.com/v1/images/generations",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=_IMAGE_TIMEOUT_SEC) as resp:
        payload = json.loads(resp.read())
    b64 = (payload.get("data") or [{}])[0].get("b64_json", "")
    if not b64:
        raise RuntimeError("no image returned")
    return base64.b64decode(b64)


def generate_image(
    prompt: str,
    *,
    provider: str | None = None,
    model: str | None = None,
    size: str | None = None,
) -> str:
    """Generate a real image from `prompt`, save it as a PNG, return the path.

    Raises RuntimeError with a short, speech-friendly message on failure
    (missing key, content policy, network) so the caller can fail open.
    """
    if not (prompt or "").strip():
        raise RuntimeError("empty prompt")
    provider = (provider or IMAGE_PROVIDER or "openrouter").strip().lower()
    try:
        if provider == "openai":
            data = _generate_openai(
                prompt, model or IMAGE_OPENAI_MODEL, size or IMAGE_SIZE
            )
        else:
            data = _generate_openrouter(prompt, model or IMAGE_MODEL)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")[:200]
        raise RuntimeError(f"image API HTTP {exc.code}: {detail}") from exc
    path = _out_path()
    path.write_bytes(data)
    return str(path)
