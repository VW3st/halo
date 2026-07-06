"""Composition test for the BACKGROUND filter: follow-up gate (content address)
-> classify_utterance (mic signal), mirroring __main__._classify_with_address,
WITHOUT importing __main__. Proves side-talk becomes BACKGROUND and a real
follow-up stays COMMAND."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from halo.followup_gate import passes
from halo.utterance import BACKGROUND, COMMAND, classify_utterance


def _classify(text, agent, **sig):
    """The same glue __main__._classify_with_address uses: ONLY a clear
    side_conversation is background-eligible; no_signal (talk to Halo) is not."""
    addressed = None
    try:
        allow, reason = passes(text, agent)
        if allow:
            addressed = True
        elif reason == "side_conversation":
            addressed = False
    except Exception:
        addressed = None
    return classify_utterance(text, addressed=addressed, **sig)


def main() -> int:
    f = 0

    def check(label, got, want):
        nonlocal f
        ok = got == want
        f += 0 if ok else 1
        print(f"  [{'OK' if ok else 'FAIL'}] {label:50} -> {got} (want {want})")

    AGENT = "claude_code"

    # Clear side conversation (gate -> side_conversation) + quiet mic -> BACKGROUND.
    check("side-talk + quiet -> background",
          _classify("hey john can you hear me right now", AGENT, peak_rms=0.025),
          BACKGROUND)
    check("side-talk + no silero -> background",
          _classify("do you want to grab lunch sometime", AGENT,
                    vad_fired=False, energy_only_onset=True),
          BACKGROUND)
    # A real follow-up to the agent (gate -> addressed) stays COMMAND even if quiet.
    check("follow-up to agent -> command",
          _classify("now also add some tests", AGENT, peak_rms=0.025),
          COMMAND)
    check("addressed build -> command",
          _classify("fix the login bug", AGENT, peak_rms=0.07, vad_fired=True),
          COMMAND)
    # THE FIX: a no_signal question to Halo (not side-talk) is NEVER dropped, even
    # quiet — it must reach the brain, not be classified BACKGROUND.
    check("Halo question (no_signal) + quiet -> NOT background",
          _classify("when did we start this project", AGENT, peak_rms=0.025),
          COMMAND)
    check("Halo question (no_signal) + no silero -> NOT background",
          _classify("how long have we been working on this", AGENT,
                    vad_fired=False, energy_only_onset=True),
          COMMAND)

    total = 6
    print(f"\n{total - f}/{total} passed")
    return 1 if f else 0


if __name__ == "__main__":
    raise SystemExit(main())
