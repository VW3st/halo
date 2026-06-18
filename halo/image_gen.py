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
import shutil
import subprocess
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


_CODEX_TIMEOUT_SEC = 240.0


def _png_mtimes(dirs: list[Path]) -> dict[str, float]:
    out: dict[str, float] = {}
    for d in dirs:
        try:
            for p in d.glob("*.png"):
                out[str(p)] = p.stat().st_mtime
        except Exception:
            pass
    return out


def _generate_codex(subject: str) -> str:
    """Run the Codex CLI's built-in image_gen tool (gpt-image-2) exactly how a
    human would — `codex login` (ChatGPT auth), NO separate API key. Snapshots
    PNGs before/after so we can find whatever Codex produced and return it."""
    out_dir = Path(IMAGE_DIR).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"halo-image-{int(time.time())}.png"
    watch = [out_dir, Path.home() / ".codex" / "generated_images"]
    before = _png_mtimes(watch)

    # NAME the destination — the imagegen skill's save-path rule is "if the user
    # names a destination, move/copy the output there"; without one it treats
    # the request as a preview and writes NO file. reasoning_effort=low keeps the
    # agent loop fast (the user's default xhigh makes it crawl).
    prompt = (
        f"Generate an image of: {subject}. Use your built-in image_gen tool, "
        f"then save the final PNG to exactly this path: {dest}"
    )
    cmd = [
        "codex", "exec",
        "--sandbox", "workspace-write",
        "--skip-git-repo-check",  # the image dir isn't a git repo
        "-c", 'approval_policy="never"',
        "-c", 'model_reasoning_effort="low"',
        "-C", str(out_dir),
        prompt,
    ]
    try:
        proc = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",  # Codex emits UTF-8 box chars; cp1252 would crash
            timeout=_CODEX_TIMEOUT_SEC,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "Codex CLI not found — install it (codex login) or set "
            "HALO_IMAGE_PROVIDER=openrouter."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Codex image generation timed out.") from exc

    # Preferred: Codex wrote the file to the path we named.
    if dest.exists() and dest.stat().st_size > 0:
        return str(dest)
    # Fallback: it saved under $CODEX_HOME/generated_images instead — grab the
    # freshest new PNG and copy it to our dir.
    after = _png_mtimes(watch)
    fresh = [p for p, m in after.items() if p not in before or m > before[p] + 0.5]
    if fresh:
        src = max(fresh, key=lambda p: after[p])
        try:
            if Path(src).resolve() != dest.resolve():
                shutil.copy(src, dest)
                return str(dest)
        except Exception:
            pass
        return src
    tail = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()[-280:]
    raise RuntimeError("Codex produced no image. " + tail)


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
    subject: str,
    *,
    provider: str | None = None,
    model: str | None = None,
    size: str | None = None,
) -> str:
    """Generate a real image of `subject`, save it as a PNG, return the path.

    Default provider is "codex" (the Codex CLI's built-in image_gen tool — no
    API key, your ChatGPT login). "openrouter" / "openai" are API fallbacks.
    Raises RuntimeError with a short, speech-friendly message on failure so the
    caller can fail open.
    """
    if not (subject or "").strip():
        raise RuntimeError("empty subject")
    provider = (provider or IMAGE_PROVIDER or "codex").strip().lower()
    if provider == "codex":
        return _generate_codex(subject)
    # API backends build a fuller prompt and save the returned bytes themselves.
    prompt = build_prompt(subject)
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
