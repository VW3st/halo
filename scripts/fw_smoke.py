"""Smoke test faster-whisper on the JFK sample.

Reports per-segment text + latency, total wall time, and (best-effort)
the actual backend in use. Tries CUDA + int8_float16 first; falls back
to CPU int8 if CUDA isn't available.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

JFK = Path(sys.argv[1] if len(sys.argv) > 1 else r"D:\Halo\recordings\jfk.wav")
MODEL = "distil-large-v3"


def _add_nvidia_dll_dirs() -> None:
    """Make ctranslate2 find cuBLAS/cuDNN from the pip-installed nvidia wheels.

    Must run BEFORE `import faster_whisper`. We prepend to PATH because
    ctranslate2's native DLL uses Windows' default DLL search path for
    its transitive dependencies, which os.add_dll_directory doesn't reach.
    """
    import os
    import site
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


def main() -> None:
    _add_nvidia_dll_dirs()
    from faster_whisper import WhisperModel

    print(f"loading {MODEL}...")
    t0 = time.monotonic()
    try:
        model = WhisperModel(MODEL, device="cuda", compute_type="int8_float16")
        backend = "cuda int8_float16"
    except Exception as exc:
        print(f"  cuda failed ({exc}); falling back to cpu")
        model = WhisperModel(MODEL, device="cpu", compute_type="int8")
        backend = "cpu int8"
    print(f"loaded in {(time.monotonic() - t0) * 1000:.0f}ms  backend={backend}")

    print(f"\ntranscribing {JFK}...")
    t0 = time.monotonic()
    segments, info = model.transcribe(
        str(JFK),
        language="en",
        beam_size=5,
        vad_filter=False,
        without_timestamps=False,
    )
    segs = list(segments)
    elapsed = (time.monotonic() - t0) * 1000

    print(f"\ntranscript ({elapsed:.0f}ms wall, audio duration {info.duration:.1f}s):")
    for s in segs:
        print(f"  [{s.start:5.2f}s] {s.text!r}")

    print(f"\nrealtime factor: {info.duration * 1000 / elapsed:.1f}x")


if __name__ == "__main__":
    main()
