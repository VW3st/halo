"""Benchmark router latency on canned transcripts.

Stage 1 target: <200 ms per call.
Stage 2 target: <500 ms per call.

Run with:
    .venv\\Scripts\\python.exe scripts\\bench_router.py
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from halo.router import check_turn_complete, understand_and_route


STAGE1_CASES = [
    ("build me a login page", True),
    ("build me a login page and", False),
    ("I want to refactor the um", False),
    ("stop", True),
    ("open the dashboard file", True),
]

STAGE2_CASES = [
    "hey um build me a login page with claud code using super base",
    "fix the bug",
    "what time is it",
    "stop",
    "use codex to refactor the auth module",
]


def main() -> None:
    print("warming up the model (first call always pays for weights load)...")
    t0 = time.monotonic()
    check_turn_complete("hello")
    print(f"  warmup: {(time.monotonic() - t0) * 1000:.0f}ms\n")

    print("=== Stage 1: check_turn_complete ===")
    s1_times = []
    s1_wrong = 0
    for text, expected in STAGE1_CASES:
        t0 = time.monotonic()
        got = check_turn_complete(text)
        ms = (time.monotonic() - t0) * 1000
        s1_times.append(ms)
        ok = "OK" if got is expected else "WRONG"
        if got is not expected:
            s1_wrong += 1
        label = "COMPLETE" if got else "INCOMPLETE"
        print(f"  [{ms:6.0f} ms] [{ok:5}] {text!r:60} -> {label}")
    print(f"  avg: {sum(s1_times) / len(s1_times):.0f}ms, "
          f"max: {max(s1_times):.0f}ms, "
          f"correct: {len(STAGE1_CASES) - s1_wrong}/{len(STAGE1_CASES)}")

    print("\n=== Stage 2: understand_and_route ===")
    s2_times = []
    for text in STAGE2_CASES:
        t0 = time.monotonic()
        decision = understand_and_route(text)
        ms = (time.monotonic() - t0) * 1000
        s2_times.append(ms)
        print(f"\n  [{ms:6.0f} ms] {text!r}")
        print("    " + json.dumps(decision, indent=2).replace("\n", "\n    "))
    print(f"\n  avg: {sum(s2_times) / len(s2_times):.0f}ms, "
          f"max: {max(s2_times):.0f}ms")

    print("\n=== Summary ===")
    s1_avg = sum(s1_times) / len(s1_times)
    s2_avg = sum(s2_times) / len(s2_times)
    print(f"  Stage 1 avg: {s1_avg:.0f}ms (target <200ms) "
          f"{'PASS' if s1_avg < 200 else 'MISS'}")
    print(f"  Stage 2 avg: {s2_avg:.0f}ms (target <500ms) "
          f"{'PASS' if s2_avg < 500 else 'MISS'}")
    print(f"  Stage 1 + Stage 2 (wake-to-confirmation budget): "
          f"{s1_avg + s2_avg:.0f}ms (excluding STT/wake)")


if __name__ == "__main__":
    main()
