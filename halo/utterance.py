"""Utterance classification — "is this a turn for the AI, or not?"

The turn orchestrator (`turn.py`) commits ONE utterance per turn based purely on
acoustics (speech → silence). But not every committed utterance is a command:
while Halo has promised a beat ("give me ten seconds") the user counts "one, two,
three…", or thinks aloud "um, let me see…", or the mic catches a rumble. Routing
those as commands is the "AI never gets its turn" bug.

This module is a PURE, fast, rule-based classifier (no LLM, no audio, no I/O) so
the policy is unit-testable in isolation (see scripts/test_utterance_classifier.py).
It labels a transcript as:

  COMMAND  — a real instruction/question/statement → route it normally.
  FILLER   — counting, thinking-aloud, or backchannel → do NOT route; if Halo owes
             a turn, ignore it and let the floor reclaim happen.
  NOISE    — too quiet (rumble) or non-speech residue → drop silently.

It is deliberately CONSERVATIVE: COMMAND is the default. FILLER/NOISE only fire on
clear signals, so a real command is never swallowed.
"""

from __future__ import annotations

import re

COMMAND = "command"
FILLER = "filler"
NOISE = "noise"
BACKGROUND = "background"  # real speech, but NOT directed at Halo (side-talk / TV)

# Spelled-out numbers + magnitudes. Used to detect counting ("one two three").
# Ordinals are intentionally excluded — "second" collides with the time unit.
_NUMBER_WORDS = frozenset(
    """zero one two three four five six seven eight nine ten eleven twelve
    thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty thirty
    forty fifty sixty seventy eighty ninety hundred thousand""".split()
)

# Pure filler / discourse markers — words that carry no command content.
# Pure filler / discourse markers. Decision-bearing words (yes/no/nope/cancel)
# are deliberately EXCLUDED — those carry meaning the orchestrator acts on.
_FILLER_WORDS = frozenset(
    """um umm uhm uh uhh er erm hmm hm mm mmm mhm uh-huh okay ok kay alright
    right yeah yep yup sure cool nice well so like anyway anyways
    actually basically literally just lemme""".split()
)

# Backchannels / acknowledgements that, alone, are not commands.
_BACKCHANNEL = frozenset(
    """mhm mm uh-huh yeah yep yup ok okay right sure cool nice gotcha""".split()
)

# Thinking-aloud / hold phrases: the user is buying time, not commanding.
_THINKING_RE = re.compile(
    r"\b("
    r"let me think|let me see|let'?s see|give me (?:a |a few )?(?:sec|second|seconds|moment|minute|minutes)|"
    r"hold on|hang on|one (?:sec|second|moment|minute)|just a (?:sec|second|moment|minute)|"
    r"bear with me|wait wait|hmm+|uh+|um+|let me check"
    r")\b",
    re.IGNORECASE,
)

_TOKEN_RE = re.compile(r"[a-z0-9']+")


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall((text or "").lower())


def classify_utterance(
    text: str,
    *,
    peak_rms: float | None = None,
    min_rms: float = 0.02,
    vad_fired: bool | None = None,
    energy_only_onset: bool = False,
    addressed: bool | None = None,
    background_rms: float = 0.035,
) -> str:
    """Classify a committed transcript into COMMAND / FILLER / NOISE / BACKGROUND.

    Mic-grounded "third detection": besides the words, the decision uses the
    actual audio signal so it can tell real speech FOR Halo from noise and from
    background talk that isn't directed at it.

      peak_rms          loudest 100 ms window (0..1). Below min_rms = NOISE.
      vad_fired         did silero VAD actually fire (True), or only the RMS
                        energy fallback (False)? None = caller couldn't tell.
      energy_only_onset the first onset came from energy, not silero (rumble
                        trips energy but not silero).
      addressed         content address verdict (True/False/None) — the caller
                        computes it from the follow-up gate; kept OUT of this
                        module so it stays dependency-free + trivially testable.
      background_rms    loudness band between rumble-floor and near-speaker; a
                        not-addressed utterance below it is a far speaker / TV.

    COMMAND is the conservative default; BACKGROUND only fires when the content
    says "not for me" AND the mic corroborates (quiet, or silero never agreed).
    """
    toks = _tokens(text)
    if not toks:
        return NOISE

    # Loudness gate: a near-silent "utterance" is rumble, not speech — UNLESS
    # silero VAD actually fired on it. A quiet mic (e.g. far from an NVIDIA
    # Broadcast input) produces real speech at low RMS that silero still confirms;
    # never drop silero-confirmed speech as noise just for being quiet.
    if peak_rms is not None and peak_rms < min_rms and vad_fired is not True:
        return NOISE

    # Provenance rumble arm: energy tripped but silero never agreed, and it's
    # quiet -> rumble that whisper hallucinated into words, not real speech.
    if (
        energy_only_onset
        and vad_fired is False
        and peak_rms is not None
        and peak_rms < background_rms
    ):
        return NOISE

    n = len(toks)

    # Counting: the utterance is mostly number-words / digits ("one two three",
    # "1 2 3 4"). A real command rarely is ("build me 3 pages" is 1/4 numbers).
    num = sum(1 for t in toks if t in _NUMBER_WORDS or t.isdigit())
    if num and num / n >= 0.6:
        return FILLER

    # Backchannel: a single short acknowledgement on its own.
    if n <= 2 and all(t in _BACKCHANNEL or t in _FILLER_WORDS for t in toks):
        return FILLER

    # Thinking-aloud / hold phrase with little substantive remainder. Strip the
    # phrase + filler words; if almost nothing real is left, it's filler.
    if _THINKING_RE.search(text or ""):
        residual = _THINKING_RE.sub(" ", text or "")
        residual_content = [
            t for t in _tokens(residual)
            if t not in _FILLER_WORDS and t not in _NUMBER_WORDS
        ]
        if len(residual_content) <= 1:
            return FILLER

    # Entirely filler words (e.g. "um uh like well").
    if all(t in _FILLER_WORDS for t in toks):
        return FILLER

    # BACKGROUND: a real-length sentence the CONTENT gate says isn't addressed to
    # Halo, AND the MIC corroborates (quiet far-speaker/TV, or silero never
    # agreed). A loud near-speaker utterance falls through to COMMAND even if the
    # text gate was unsure — conservative, never silently drops a real command.
    if (
        n >= 3
        and addressed is False
        and (
            (peak_rms is not None and peak_rms < background_rms)
            or (vad_fired is False and energy_only_onset)
        )
    ):
        return BACKGROUND

    return COMMAND
