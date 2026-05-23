"""Batch speech-to-text via faster-whisper + distil-large-v3.

Moonshine streaming was fast but its transcription accuracy on the
user's voice/mic was unworkable ("Chrome" -> "Cut" / "card" / "crack").
faster-whisper with distil-large-v3 cuts WER in half (~6-8% vs ~12-15%)
at the cost of being batch-only — but Stage 1 is rules-only now, so we
don't actually need partials during speech. The trade is worth it.

Flow per turn:
  - BatchTranscriber buffers every audio chunk via .feed()
  - On silence, turn.py calls .transcribe() to get the full text
  - .reset() clears the buffer for the next turn
"""

from __future__ import annotations

import os
import site
import threading
from pathlib import Path
from typing import Optional

import numpy as np

from halo.config import SAMPLE_RATE


def _add_nvidia_dll_dirs() -> None:
    """Make ctranslate2 find cuBLAS/cuDNN from the pip-installed nvidia wheels.

    Must run BEFORE `import faster_whisper`. We prepend to PATH because
    ctranslate2's native DLL uses Windows' default DLL search path for
    its transitive dependencies, which os.add_dll_directory doesn't reach.
    """
    extra: list[str] = []
    bases = list(site.getsitepackages())
    bases.append(site.getusersitepackages())
    for base in bases:
        for sub in ("nvidia/cublas/bin", "nvidia/cudnn/bin", "nvidia/cuda_nvrtc/bin"):
            path = Path(base) / sub
            if path.is_dir():
                extra.append(str(path))
                try:
                    os.add_dll_directory(str(path))
                except (AttributeError, OSError):
                    pass
    if extra:
        os.environ["PATH"] = os.pathsep.join(extra + [os.environ.get("PATH", "")])


_add_nvidia_dll_dirs()

# Late import so the PATH/DLL fix above takes effect first.
from faster_whisper import WhisperModel  # noqa: E402

# Distil-Whisper large-v3: English-only, ~6x faster than full large-v3,
# similar WER on clean speech. int8_float16 on RTX 3060 uses ~800 MB
# VRAM (fits alongside Ollama 1.5b + Windows).
_MODEL_NAME = "distil-large-v3"
_CUDA_COMPUTE = "int8_float16"
_CPU_COMPUTE = "int8"
_MIN_AUDIO_SEC = 0.2  # any shorter and Whisper will hallucinate

_model: Optional[WhisperModel] = None
_load_lock = threading.Lock()
_backend: str = "(uninitialized)"


def preload_model() -> None:
    """Load distil-large-v3 eagerly + warm CUDA kernels so the first
    real turn doesn't pay the 5-7s cold-start tax we saw on first run."""
    global _model, _backend
    with _load_lock:
        if _model is not None:
            return
        print(f"loading faster-whisper {_MODEL_NAME} (first run downloads ~1.5 GB)...")
        try:
            _model = WhisperModel(_MODEL_NAME, device="cuda", compute_type=_CUDA_COMPUTE)
            _backend = f"cuda {_CUDA_COMPUTE}"
        except Exception as exc:
            print(f"  cuda failed ({exc}); falling back to cpu")
            _model = WhisperModel(_MODEL_NAME, device="cpu", compute_type=_CPU_COMPUTE)
            _backend = f"cpu {_CPU_COMPUTE}"
        print(f"  faster-whisper loaded: backend={_backend}")

        # Warm CUDA kernels with 0.5 s of silence. The first inference
        # compiles GPU kernels and was costing 5-7 s on the first turn.
        # After warmup, real first turns land in the usual 0.5-1.5 s range.
        print("  warming up faster-whisper kernels...")
        silence = np.zeros(int(SAMPLE_RATE * 0.5), dtype=np.float32)
        try:
            segments, _info = _model.transcribe(
                silence, language="en", beam_size=1, vad_filter=False,
                without_timestamps=True, condition_on_previous_text=False,
            )
            list(segments)  # consume generator to actually run the model
        except Exception as exc:
            print(f"  warmup failed (non-fatal): {exc}")


class BatchTranscriber:
    """Per-turn audio buffer + on-demand transcription.

    Construct once per turn, .feed() chunks as they arrive, .transcribe()
    when the orchestrator wants the current text. Cheap to call repeatedly
    since faster-whisper is fast — but the orchestrator should still avoid
    calling more than once per silence event.
    """

    def __init__(self) -> None:
        preload_model()
        self._chunks: list[np.ndarray] = []
        self._lock = threading.Lock()

    def feed(self, audio_int16: np.ndarray) -> None:
        """Append one chunk of int16 PCM audio. Safe to call from the
        sounddevice audio callback — pure list append."""
        with self._lock:
            self._chunks.append(audio_int16)

    def seed(self, audio_int16: np.ndarray) -> None:
        """Prepend a chunk (used for the pre-wake audio capture)."""
        if audio_int16.size == 0:
            return
        with self._lock:
            self._chunks.insert(0, audio_int16)

    def _snapshot(self) -> np.ndarray:
        with self._lock:
            if not self._chunks:
                return np.zeros(0, dtype=np.int16)
            return np.concatenate(self._chunks)

    def transcribe(self) -> str:
        """Run Whisper on everything buffered so far. Returns "" if too short."""
        audio_i16 = self._snapshot()
        if audio_i16.size < int(SAMPLE_RATE * _MIN_AUDIO_SEC):
            return ""
        audio_f32 = audio_i16.astype(np.float32) / 32768.0
        assert _model is not None
        segments, _info = _model.transcribe(
            audio_f32,
            language="en",
            beam_size=5,
            vad_filter=False,
            without_timestamps=True,
            condition_on_previous_text=False,  # avoid hallucinated continuations
        )
        return " ".join(seg.text.strip() for seg in segments).strip()

    def stop(self) -> str:
        """Symmetric API with the old streaming transcriber. Returns final text."""
        return self.transcribe()
