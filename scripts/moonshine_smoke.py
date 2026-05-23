"""Smoke test Moonshine on the JFK sample.

- Loads a streaming model, feeds the WAV in 100 ms chunks at real-time
  pacing, and prints the running transcript as it grows.
- Reports first-token latency and total transcription time.
"""

from __future__ import annotations

import sys
import time
import wave
from pathlib import Path

import numpy as np

from moonshine_voice import ModelArch, get_model_for_language
from moonshine_voice.transcriber import Transcriber

JFK_WAV = Path(sys.argv[1] if len(sys.argv) > 1 else r"C:\Users\agenc\AppData\Local\Temp\jfk.wav")


def read_wav_float32(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as wf:
        sr = wf.getframerate()
        n = wf.getnframes()
        raw = wf.readframes(n)
    audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    return audio, sr


def main() -> None:
    model_path, model_arch = get_model_for_language("en", ModelArch.SMALL_STREAMING)
    print(f"model path: {model_path}")
    print(f"model arch: {model_arch.name}")
    t0 = time.monotonic()
    transcriber = Transcriber(model_path, model_arch, update_interval=0.3)
    print(f"loaded in {(time.monotonic() - t0) * 1000:.0f}ms")

    audio, sr = read_wav_float32(JFK_WAV)
    print(f"audio: {len(audio) / sr:.2f}s @ {sr}Hz")

    latest: list[str] = [""]
    first_partial_at: list[float | None] = [None]
    started_at = time.monotonic()

    def on_event(event) -> None:
        text = event.line.text
        if text and first_partial_at[0] is None:
            first_partial_at[0] = (time.monotonic() - started_at) * 1000
        latest[0] = text
        marker = " (complete)" if getattr(event.line, "is_complete", False) else ""
        print(f"  [{(time.monotonic() - started_at) * 1000:6.0f}ms]{marker} {text!r}")

    stream = transcriber.create_stream(update_interval=0.3)
    stream.add_listener(on_event)
    stream.start()

    # Real-time pace: feed 100ms chunks every 100ms.
    chunk_size = int(sr * 0.1)
    for i in range(0, len(audio), chunk_size):
        stream.add_audio(audio[i : i + chunk_size].tolist(), sr)
        time.sleep(0.1)

    final = stream.stop()
    finished_at = (time.monotonic() - started_at) * 1000
    print(f"\nfinal transcript:")
    for line in final.lines:
        print(f"  {line.text!r}  (latency reported: {line.last_transcription_latency_ms}ms)")
    print(f"\nfirst partial at: {first_partial_at[0]:.0f}ms")
    print(f"total wall time:  {finished_at:.0f}ms (audio duration: {len(audio) / sr * 1000:.0f}ms)")

    transcriber.close()


if __name__ == "__main__":
    main()
