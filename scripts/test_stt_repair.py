"""Unit tests for the semantic-repair pass (router.needs_repair /
router.repair_transcript) — the confidence gate and the fail-open contract.
The LLM call is monkeypatched; no model or network needed."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from halo import router


def main() -> int:
    f = 0

    def check(label, got, want) -> None:
        nonlocal f
        ok = got == want
        f += 0 if ok else 1
        print(f"  [{'OK' if ok else 'FAIL'}] {label:56} -> {got!r} (want {want!r})")

    print("needs_repair (pure gate):")
    check("no quality info -> False",
          router.needs_repair("some garbled words here", None), False)
    check("confident decode -> False",
          router.needs_repair("build me a landing page", -0.2), False)
    check("low confidence + real length -> True",
          router.needs_repair("we need to fix the cogic in the touter", -0.8), True)
    check("low confidence but SHORT command -> False",
          router.needs_repair("open chrome", -0.9), False)
    check("empty text -> False", router.needs_repair("", -0.9), False)
    check("boundary: exactly at threshold -> False",
          router.needs_repair("three words minimum here", router._REPAIR_LOGPROB), False)

    print("\nrepair_transcript (fail-open contract, LLM mocked):")
    original = router._chat_json
    RAW = "we need to fix the cogic in the touter"

    # Normal repair: model output is used.
    router._chat_json = lambda **kw: {"corrected": "we need to fix the logic in the router"}
    check("model fix accepted",
          router.repair_transcript(RAW, "You: the router logic is broken"),
          "we need to fix the logic in the router")

    # Model errors -> raw text unchanged.
    def _boom(**kw):
        raise RuntimeError("model down")
    router._chat_json = _boom
    check("model error -> raw", router.repair_transcript(RAW), RAW)

    # Empty output -> raw.
    router._chat_json = lambda **kw: {"corrected": ""}
    check("empty output -> raw", router.repair_transcript(RAW), RAW)

    # Ballooned rewrite (editorializing) -> rejected, raw kept.
    router._chat_json = lambda **kw: {"corrected": RAW * 4}
    check("ballooned rewrite -> raw", router.repair_transcript(RAW), RAW)

    # Empty input -> returned as-is, no model call.
    router._chat_json = _boom
    check("empty input -> unchanged", router.repair_transcript(""), "")

    router._chat_json = original

    total = 11
    print(f"\n{total - f}/{total} passed")
    return 1 if f else 0


if __name__ == "__main__":
    raise SystemExit(main())
