"""Fetch Kokoro TTS model files into MODELS_DIR.

Exposed as `halo download-models`. faster-whisper, openWakeWord, and
silero-vad all download themselves on first use; only Kokoro needs an
explicit fetch because its release artifacts are too big for a pip
dependency to bundle.
"""

from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

from halo.config import MODELS_DIR

_FILES: tuple[tuple[str, str], ...] = (
    (
        "kokoro-v1.0.fp16.onnx",
        "https://github.com/thewh1teagle/kokoro-onnx/releases/download/"
        "model-files-v1.0/kokoro-v1.0.fp16.onnx",
    ),
    (
        "voices-v1.0.bin",
        "https://github.com/thewh1teagle/kokoro-onnx/releases/download/"
        "model-files-v1.0/voices-v1.0.bin",
    ),
)


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _download(url: str, dest: Path) -> None:
    print(f"  -> {dest.name}")
    print(f"     {url}")
    tmp = dest.with_suffix(dest.suffix + ".part")
    state = {"last_pct": -1}

    def hook(blocks: int, block_size: int, total: int) -> None:
        if total <= 0:
            return
        done = blocks * block_size
        pct = min(100, int(done * 100 / total))
        if pct != state["last_pct"] and pct % 5 == 0:
            print(f"     {pct:3d}%  ({_human(done)} / {_human(total)})")
            state["last_pct"] = pct

    urllib.request.urlretrieve(url, tmp, reporthook=hook)
    tmp.replace(dest)


def download_all() -> int:
    """Download every Kokoro file into MODELS_DIR. Returns shell exit code."""
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Downloading Kokoro model files into: {MODELS_DIR}\n")
    for name, url in _FILES:
        dest = MODELS_DIR / name
        if dest.exists() and dest.stat().st_size > 0:
            print(f"  [skip] {name} already present ({_human(dest.stat().st_size)})")
            continue
        try:
            _download(url, dest)
        except Exception as exc:
            print(f"  ERROR downloading {name}: {exc}", file=sys.stderr)
            return 1
    print("\nDone. Run `halo` to start the voice loop.")
    return 0


if __name__ == "__main__":
    sys.exit(download_all())
