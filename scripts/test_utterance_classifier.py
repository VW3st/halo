"""Unit tests for halo.utterance.classify_utterance — the COMMAND/FILLER/NOISE
classifier that stops counting / thinking-aloud from being routed as commands."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from halo.utterance import BACKGROUND, COMMAND, FILLER, NOISE, classify_utterance


def check(label, text, expected, **kw) -> int:
    got = classify_utterance(text, **kw)
    ok = got == expected
    print(f"  [{'OK' if ok else 'FAIL'}] {label:34} {text!r:34} -> {got} (want {expected})")
    return 0 if ok else 1


def main() -> int:
    f = 0

    print("COMMAND (must route):")
    f += check("plain imperative", "build me a login page", COMMAND)
    f += check("open app", "open chrome", COMMAND)
    f += check("question", "what time is it", COMMAND)
    f += check("filler prefix + command", "okay open chrome", COMMAND)
    f += check("agent dispatch", "ask codex to refactor the auth module", COMMAND)
    f += check("numbers inside a command", "build me 3 landing pages", COMMAND)
    f += check("thinking phrase + real content", "let me think about the database schema", COMMAND)
    f += check("so-prefixed command", "so add dark mode to the website", COMMAND)

    print("\nFILLER (counting / thinking-aloud / backchannel — do NOT route):")
    f += check("counting words", "one two three", FILLER)
    f += check("counting digits", "1 2 3 4 5", FILLER)
    f += check("counting longer", "one two three four five six", FILLER)
    f += check("single number", "ten", FILLER)
    f += check("hold phrase", "hold on", FILLER)
    f += check("give me a second", "give me a second", FILLER)
    f += check("let me see", "let me see", FILLER)
    f += check("pure filler", "um uh like well", FILLER)
    f += check("backchannel", "mhm", FILLER)
    f += check("backchannel yeah", "yeah", FILLER)

    print("\nNOISE (drop):")
    f += check("empty", "", NOISE)
    f += check("whitespace", "   ", NOISE)
    f += check("too quiet (rumble)", "thank you", NOISE, peak_rms=0.005)
    f += check("loud enough passes gate", "open the terminal", COMMAND, peak_rms=0.2)

    print("\nDecision words are NOT filler (orchestrator acts on them):")
    f += check("bare no", "no", COMMAND)
    f += check("bare yes", "yes", COMMAND)

    print("\nBACKGROUND (mic-grounded third detection — side speech / TV):")
    f += check("not-addressed + quiet", "so anyway i told him we'd meet at noon",
               BACKGROUND, peak_rms=0.025, addressed=False)
    f += check("not-addressed + no silero", "the game was on pretty late last night",
               BACKGROUND, addressed=False, vad_fired=False, energy_only_onset=True)
    f += check("not-addressed but LOUD near-speaker -> command",
               "open the dashboard for me", COMMAND, peak_rms=0.08, vad_fired=True, addressed=False)
    f += check("addressed overrides loudness -> command",
               "now also add some tests", COMMAND, peak_rms=0.02, addressed=True)
    f += check("rumble provenance -> noise", "thanks for watching everyone",
               NOISE, peak_rms=0.025, vad_fired=False, energy_only_onset=True)
    f += check("backchannel beats background", "yeah", FILLER, addressed=False)
    f += check("no address signal -> still command", "build me a login page", COMMAND)

    total = 31
    print(f"\n{total - f}/{total} passed")
    return 1 if f else 0


if __name__ == "__main__":
    raise SystemExit(main())
