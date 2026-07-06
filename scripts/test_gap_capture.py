"""Unit tests for record.GapCapture buffering/gating — no audio hardware.

feed() is exercised directly (the stream callback is just feed + an
is-speaking guard), proving:
  - take() returns audio only when it contains speech-level energy
  - room tone / silence is discarded, never seeded into the next turn
  - take() clears the buffer (no double-seeding)
  - the ring keeps only the most recent max_sec seconds
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from halo.config import SAMPLE_RATE
from halo.record import SPEECH_RMS_FLOOR, VAD_CHUNK, GapCapture


def _chunk(level: float) -> np.ndarray:
    """One VAD_CHUNK of int16 audio at roughly `level` RMS (a sine wave's RMS
    is amplitude/sqrt(2), so scale up to land near the target)."""
    t = np.arange(VAD_CHUNK, dtype=np.float32) / SAMPLE_RATE
    wave = np.sin(2 * np.pi * 220.0 * t) * level * np.sqrt(2.0)
    return (np.clip(wave, -1.0, 1.0) * 32767).astype(np.int16)


def main() -> int:
    f = 0

    def check(label, got, want) -> None:
        nonlocal f
        ok = got == want
        f += 0 if ok else 1
        print(f"  [{'OK' if ok else 'FAIL'}] {label:52} -> {got} (want {want})")

    # Speech-level audio is returned.
    g = GapCapture(max_sec=5.0)
    for _ in range(20):
        g.feed(_chunk(0.1))  # well above SPEECH_RMS_FLOOR
    audio = g.take()
    check("speech-level buffer -> returned", audio.size > 0, True)
    check("take() clears the buffer", g.take().size, 0)

    # Near-silent room tone is discarded.
    g2 = GapCapture(max_sec=5.0)
    for _ in range(20):
        g2.feed(_chunk(SPEECH_RMS_FLOOR * 0.3))
    check("room tone -> discarded", g2.take().size, 0)

    # One real word inside mostly-quiet audio still passes (windowed RMS).
    g3 = GapCapture(max_sec=5.0)
    for _ in range(15):
        g3.feed(_chunk(0.001))
    for _ in range(5):  # ~160ms of speech
        g3.feed(_chunk(0.1))
    check("short word in quiet gap -> returned", g3.take().size > 0, True)

    # Ring bound: only the most recent max_sec is kept.
    g4 = GapCapture(max_sec=1.0)
    max_chunks = int(1.0 * SAMPLE_RATE / VAD_CHUNK)
    for _ in range(max_chunks * 3):
        g4.feed(_chunk(0.1))
    audio4 = g4.take()
    check("ring keeps <= max_sec", audio4.size <= max_chunks * VAD_CHUNK, True)

    # Empty capture -> empty, not an error.
    g5 = GapCapture()
    check("empty -> empty array", g5.take().size, 0)

    total = 6
    print(f"\n{total - f}/{total} passed")
    return 1 if f else 0


if __name__ == "__main__":
    raise SystemExit(main())
