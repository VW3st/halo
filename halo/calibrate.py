"""`halo calibrate` — tune the wake word to THIS machine's mic + voice.

The wake DNN scores differently on every microphone (a Blue Snowball tops out
~0.52 on "halo"; a BRIO can spike to 0.70+ on random speech). One global
threshold can't fit both — so this measures your actual scores and writes a
per-host overlay into ~/.halo/config.toml under [profiles.<hostname>.wake].
That's the "adapts to the machine it runs on" promise made real.

Flow:
  1. Record ~3 s of silence -> background score ceiling.
  2. Record you saying "halo" a few times -> the real wake peaks.
  3. Pick a threshold between the two, plus min_consecutive, and save them.

Interactive — run it in a terminal: `halo calibrate`.
"""

from __future__ import annotations

import queue
import statistics
import time

import numpy as np
import sounddevice as sd

from halo.config import SAMPLE_RATE

_SAMPLES = 5           # how many "halo" utterances to record
_HALO_WINDOW_SEC = 3.0  # capture window per utterance (generous so timing is forgiving)
_NOISE_WINDOW_SEC = 3.0


def _stream_scores(model, key: str, seconds: float) -> list[float]:
    """Run the wake model over `seconds` of live mic audio; return per-frame
    scores. Prediction runs on this thread (not the audio callback) exactly as
    the real listener does, so the numbers match production."""
    from halo.wake import CHUNK_SIZE

    q: queue.Queue = queue.Queue()

    def _cb(indata, frames, time_info, status):  # noqa: ANN001
        q.put(indata[:, 0].copy())

    scores: list[float] = []
    model.reset()
    deadline = time.monotonic() + seconds
    with sd.InputStream(
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype="int16",
        blocksize=CHUNK_SIZE,
        latency="high",
        callback=_cb,
    ):
        while time.monotonic() < deadline:
            try:
                chunk = q.get(timeout=0.1)
            except queue.Empty:
                continue
            scores.append(float(model.predict(chunk).get(key, 0.0)))
    return scores


def _capture_peak(model, key: str, max_wait: float = 6.0, tail_quiet: float = 0.9):
    """Wait for SPEECH, then track the wake-score peak through the utterance.

    Speech-triggered (energy/score onset) so the user's timing relative to the
    prompt doesn't matter — a blind fixed window kept catching mostly silence
    and missing the word. Returns (peak, per_frame_scores, heard_speech)."""
    from halo.wake import CHUNK_SIZE

    q: queue.Queue = queue.Queue()

    def _cb(indata, frames, time_info, status):  # noqa: ANN001
        q.put(indata[:, 0].copy())

    model.reset()
    peak = 0.0
    scores: list[float] = []
    started = False
    quiet = 0.0
    frame_sec = CHUNK_SIZE / SAMPLE_RATE
    deadline = time.monotonic() + max_wait
    with sd.InputStream(
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype="int16",
        blocksize=CHUNK_SIZE,
        latency="high",
        callback=_cb,
    ):
        while time.monotonic() < deadline:
            try:
                chunk = q.get(timeout=0.1)
            except queue.Empty:
                continue
            s = float(model.predict(chunk).get(key, 0.0))
            rms = float(np.sqrt(np.mean((chunk.astype(np.float32) / 32768.0) ** 2)))
            scores.append(s)
            peak = max(peak, s)
            if rms > 0.006 or s > 0.05:
                started = True
                quiet = 0.0
            elif started:
                quiet += frame_sec
                if quiet >= tail_quiet:
                    break  # word finished -> done
    return peak, scores, started


def _max_run(frames: list[float], threshold: float) -> int:
    best = run = 0
    for s in frames:
        if s >= threshold:
            run += 1
            best = max(best, run)
        else:
            run = 0
    return best


def run() -> int:
    import socket

    import halo.wake as wake
    from halo.userconfig import write_host_profile

    host = socket.gethostname().strip()
    print(f"Calibrating the wake word for this machine ({host}).")
    print("Loading the wake model...")
    try:
        model = wake._get_model()
        key = wake._active_wake_key
    except Exception as exc:
        print(f"Couldn't load the wake model: {exc}")
        return 1

    try:
        print(f"\nMic: {sd.query_devices(kind='input')['name']}")
    except Exception:
        pass

    # 1. Background noise ceiling.
    print(f"\n[1/2] Stay SILENT for {_NOISE_WINDOW_SEC:.0f} seconds...")
    time.sleep(0.4)
    noise = _stream_scores(model, key, _NOISE_WINDOW_SEC)
    noise_peak = max(noise) if noise else 0.0
    print(f"      background score ceiling: {noise_peak:.2f}")

    # 2. Real wake-word peaks. Speech-triggered, so say it whenever you like
    #    after pressing Enter.
    word = (getattr(wake, "WAKE_WORD", "halo") or "halo").replace("_", " ")
    print(f"\n[2/2] Now say '{word}' {_SAMPLES} times — once after each prompt.")
    halo_peaks: list[float] = []
    frame_sets: list[list[float]] = []
    for i in range(_SAMPLES):
        try:
            input(f"      press Enter, then say '{word}' ({i + 1}/{_SAMPLES})... ")
        except EOFError:
            print("      (no interactive input — aborting)")
            return 1
        peak, frames, heard = _capture_peak(model, key)
        halo_peaks.append(peak)
        frame_sets.append(frames)
        if heard:
            print(f"      heard peak {peak:.2f}")
        else:
            print(f"      (didn't hear you — say '{word}' right after Enter)")

    # Robust stats: you said "halo" each time, but a mistimed/clipped window can
    # miss one — so drop the single weakest sample as a likely capture miss
    # rather than letting it tank the whole calibration (the old min() did).
    peaks_sorted = sorted(halo_peaks)
    reliable = peaks_sorted[1:] if len(peaks_sorted) >= 4 else peaks_sorted
    reliable_min = min(reliable)
    mean_halo = statistics.mean(reliable)
    shown = ", ".join(f"{p:.2f}" for p in peaks_sorted)
    print(f"\nResults: background {noise_peak:.2f} | '{word}' peaks [{shown}]")
    print(f"         -> reliable min {reliable_min:.2f}, avg {mean_halo:.2f}")

    if reliable_min < 0.15:
        print(f"\n'{word}' barely registered even at your best. Almost always a low")
        print("INPUT LEVEL — check the mic's gain/distance and speak closer — OR a")
        print("wake word too soft to separate from speech. Nothing written; raise the")
        print("level and re-run, or try a more distinct wake word (e.g. hey_jarvis).")
        return 1

    # Threshold: comfortably below your reliable 'halo' floor and above the
    # noise ceiling. Lean LOW — STT verification (verify=true) catches false
    # fires, so a missed wake is the worse failure to optimize against.
    threshold = max(noise_peak + 0.08, reliable_min - 0.10)
    threshold = round(max(0.20, min(threshold, 0.80)), 2)
    weak = reliable_min <= noise_peak + 0.15

    # min_consecutive: 2 only if the FIRING samples reliably sustain >=2 frames
    # at the chosen threshold; else 1 (a single-frame mic like the Snowball
    # would miss with 2). Misses (run 0) are excluded from the decision.
    fired_runs = [r for r in (_max_run(f, threshold) for f in frame_sets) if r >= 1]
    min_consecutive = 2 if fired_runs and min(fired_runs) >= 2 else 1

    values = {"threshold": threshold, "min_consecutive": min_consecutive, "verify": True}
    path = write_host_profile("wake", values, host=host)

    print("\nWrote per-machine wake settings:")
    print(f"  [profiles.{host}.wake]")
    for k, v in values.items():
        print(f"  {k} = {v}")
    print(f"  -> {path}")
    if weak:
        print("\nNote: separation between your speech and 'halo' is tight on this mic.")
        print("STT wake-verification (verify=true) will catch the false fires the")
        print("threshold can't. If it's still twitchy, a more distinct wake word helps.")
    print("\nRestart Halo to use the new settings.")
    return 0
