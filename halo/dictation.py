"""System-wide dictation mode — type spoken words into the focused field.

This is the "click into ANY text field, talk, and have it typed there"
feature. It is deliberately NOT a command turn: there is no routing and no
agent. We just capture each spoken utterance and inject the text into
whatever control currently has keyboard focus (editor, browser box, chat,
terminal) via halo.desktop_control.type_into_focus.

Two things make it work where the normal turn pipeline wouldn't:

  * RAW transcription (BatchTranscriber.transcribe(raw=True)) — the command
    pipeline drops bare "no" / "okay" / "hello" / "thanks" as hallucinations;
    in dictation those are real words the user wants typed.
  * Foreground-focus injection via KEYEVENTF_UNICODE — types into the live
    focus with no clipboard side effects and no forced Enter.

The start/stop phrases are user-configurable ([dictation] in the config),
defaulting to "dictate" to start and "stop" to end — short words that
transcribe reliably across accents. `smart_stop` additionally fuzzy-matches
the stop word so an accented or slightly-misheard "stop" (stob / stap /
"okay stop") still ends dictation.

Each chunk is sanitized before it is typed (`autocorrect`): a fast local
cleanup always runs, and when the router model is reachable an LLM
correction fixes spelling / transcription slips (fail-open, time-boxed, so
it never blocks typing).

A small spoken control grammar lets the user punctuate and exit hands-free
(matched only when the whole utterance IS the command, so "put a comma here"
is still typed verbatim):

  "new line" / "new paragraph"          -> Enter / blank line
  "period" "comma" "question mark" ...  -> literal punctuation
  "send it" / "press enter"             -> Enter (submit the field)
  configured stop phrases (e.g. "stop") -> leave dictation mode
"""

from __future__ import annotations

import queue
import re
import sys
import threading
import time

from halo import bus, desktop_control, router, voice
from halo.config import (
    DICTATION_CORRECT_TIMEOUT_SEC,
    DICTATION_IDLE_SEC,
    DICTATION_MAX_UTTERANCE_SEC,
    TURN_END_SILENCE_MS,
)
from halo.record import (
    RecorderState,
    SPEECH_RMS_FLOOR,
    open_stream,
    play_chime,
)
from halo.stt import BatchTranscriber
from halo.userconfig import cfg as user_cfg

# Editing commands. ("punct", char) inserts punctuation tight against the
# previous word; ("newline"/"paragraph"/"enter", _) press keys.
_CONTROL_WORDS: dict[str, tuple[str, str]] = {
    "new line": ("newline", ""),
    "newline": ("newline", ""),
    "next line": ("newline", ""),
    "new paragraph": ("paragraph", ""),
    "period": ("punct", "."),
    "full stop": ("punct", "."),
    "comma": ("punct", ","),
    "question mark": ("punct", "?"),
    "exclamation mark": ("punct", "!"),
    "exclamation point": ("punct", "!"),
    "colon": ("punct", ":"),
    "semicolon": ("punct", ";"),
    # NOTE: "send it" / "submit" / "press enter" / "enter" are handled earlier
    # by _is_send_exit (submit + return to conversation), not here.
}


def _normalize_cmd(text: str) -> str:
    t = (text or "").strip().lower()
    t = re.sub(r"[.!?,]+$", "", t).strip()
    t = re.sub(r"\s+", " ", t)
    return t


def _lev(a: str, b: str) -> int:
    """Levenshtein edit distance (inputs are short single words)."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _is_stop(text: str) -> bool:
    """True when `text` should end dictation.

    Exact (normalized) match against the configured stop_phrases, plus an
    accent-robust fuzzy pass (smart_stop) that catches a single mis-heard
    "stop" — but only on short utterances, so a long dictated sentence that
    merely contains "stop" is typed, not cut off.
    """
    t = _normalize_cmd(text)
    if not t:
        return False
    stops = {_normalize_cmd(p) for p in user_cfg.dictation.stop_phrases if p.strip()}
    if t in stops:
        return True
    if not user_cfg.dictation.smart_stop:
        return False
    toks = t.split()
    if len(toks) > 3:
        return False
    for tok in toks:
        if tok.startswith("stop"):  # stop / stops / stopping / stopp
            return True
        if len(tok) >= 4 and _lev(tok, "stop") <= 1:  # stob / stap / stup / stoop
            return True
    return False


# "Send"/"submit" family that means: press Enter (submit the field) AND exit
# dictation back to the conversation. This is the natural "I'm done, send it"
# intent — distinct from the in-dictation "new line" formatting controls. The
# control vocabulary only matched EXACTLY, so "send the message" / "send" /
# "enter" used to get typed as text instead of submitting.
_SEND_EXIT_PHRASES = {
    "send", "send it", "send this", "send that", "send the message",
    "send message", "send the messages", "send the line", "send the text",
    "send text", "send now", "send it now", "send please", "send it please",
    "submit", "submit it", "submit the message", "submit message",
    "enter", "press enter", "hit enter",
}
# Benign filler that can follow "send"/"submit" in a SHORT command without it
# being dictated content ("send it now" -> submit; "send the report" -> typed).
_SEND_OK_REST = {
    "it", "this", "that", "the", "message", "messages", "line", "text",
    "now", "please", "them", "email", "in",
}


def _is_send_exit(text: str) -> bool:
    """True when the utterance means submit-and-return-to-conversation."""
    t = _normalize_cmd(text)
    if not t:
        return False
    if t in _SEND_EXIT_PHRASES:
        return True
    toks = t.split()
    # Short "send …" / "submit …" imperative made only of benign filler.
    if 1 <= len(toks) <= 3 and toks[0] in ("send", "submit"):
        return all(w in _SEND_OK_REST for w in toks[1:])
    return False


# Words commonly prepended before the real command; ignored when matching
# the start trigger ("hey hey look dictate" -> "dictate").
_LEAD_WORDS = {
    "hey", "halo", "hello", "okay", "ok", "please", "so", "um", "umm", "uh",
    "uhh", "look", "now", "yeah", "alright", "right", "the", "a",
}


def _words(text: str) -> list[str]:
    t = (text or "").lower()
    t = re.sub(r"[^a-z0-9'\s]", " ", t)
    return [w for w in t.split() if w]


def _strip_lead_words(words: list[str]) -> list[str]:
    i = 0
    while i < len(words) and words[i] in _LEAD_WORDS:
        i += 1
    return words[i:] or words


def matches_start(text: str) -> bool:
    """True when `text` is a dictation START trigger.

    Accent-robust: exact match against the configured start_phrases, plus a
    fuzzy pass on short utterances so mis-transcriptions of "dictate"
    ("dictade", "dictat", "dictating") still enter dictation. Bounded to
    short utterances so "dictate an email about X" stays a normal request.
    """
    words = _strip_lead_words(_words(text))
    if not words:
        return False
    phrase = " ".join(words)
    starts = [_words(p) for p in user_cfg.dictation.start_phrases if p.strip()]
    if phrase in {" ".join(w) for w in starts if w}:
        return True
    if len(words) > 3:
        return False
    # A distinctive "dict..." token is the strongest accent-proof signal
    # (few English words start with "dict": dictate, dictation, ...).
    for w in words:
        if w.startswith("dict") and len(w) >= 4:
            return True
    # Fuzzy whole-phrase match for multi-word triggers ("type mode" etc.).
    for sp in starts:
        if sp and len(sp) == len(words) and all(_lev(a, b) <= 1 for a, b in zip(words, sp)):
            return True
    return False


# Leading vocal fillers safe to drop (never intended as dictated text).
# Deliberately excludes "ok"/"so"/"like" which are real words.
_FILLER_LEAD = re.compile(r"^(?:(?:um+|uh+|er+|ah+|hmm+|mm+|mhm)\b[\s,]*)+", re.IGNORECASE)
_SPACE_BEFORE_PUNCT = re.compile(r"\s+([,.!?;:])")


def _local_sanitize(text: str) -> str:
    """Fast, conservative cleanup that never needs the network: collapse
    whitespace, drop leading vocal fillers, tighten spaces before
    punctuation. Casing is left to Whisper / the LLM pass."""
    t = (text or "").strip()
    if not t:
        return ""
    t = re.sub(r"\s+", " ", t)
    t = _FILLER_LEAD.sub("", t).strip()
    t = _SPACE_BEFORE_PUNCT.sub(r"\1", t)
    return t


def _autocorrect(text: str) -> str:
    """Time-boxed LLM correction. Fail-open: returns `text` if disabled, if
    the model errors, or if it doesn't answer within the budget."""
    if not user_cfg.dictation.autocorrect or not text:
        return text
    box: dict[str, str | None] = {"v": None}

    def _work() -> None:
        try:
            box["v"] = router.correct_text(text)
        except Exception:
            box["v"] = None

    th = threading.Thread(target=_work, daemon=True)
    th.start()
    th.join(timeout=DICTATION_CORRECT_TIMEOUT_SEC)
    return box["v"] or text


def _sanitize(text: str) -> str:
    """Local cleanup, then optional LLM correction, then (unless auto_period)
    a trailing-period trim so dictating in fragments doesn't add a period
    after every pause. Internal '.' and trailing '?' / '!' are kept."""
    local = _local_sanitize(text)
    if not local:
        return ""
    out = _autocorrect(local).strip() or local
    if not user_cfg.dictation.auto_period:
        out = re.sub(r"\s*[.…]+\s*$", "", out)
    return out


# Connectors/articles that the per-chunk autocorrect over-capitalizes at a
# continuation boundary ("...improve the" / "And" / "The functionality"). When
# the previous chunk did NOT end a sentence, these get lowercased so dictated
# fragments read like one flowing sentence. Real sentence-openers (pronouns,
# verbs, names) are left alone, so a genuine new sentence stays capitalized.
_CONTINUATION_LOWER = {
    "and", "but", "or", "so", "then", "nor", "yet", "the", "a", "an",
    "to", "for", "of", "with", "in", "on", "at", "by", "from", "as",
    "that", "which", "also", "plus", "because", "if", "when", "while",
    "though", "although", "into", "onto", "about", "over", "under",
}


def _flow_case(text: str, sentence_start: bool) -> str:
    """Fix cross-chunk casing for dictation (issue #D).

    `sentence_start=True`  -> capitalize the first letter (new sentence).
    `sentence_start=False` -> a continuation; lowercase a leading
        connector/article the per-chunk corrector wrongly capitalized.
    """
    if not text:
        return text
    if sentence_start:
        return text[0].upper() + text[1:] if text[0].islower() else text
    m = re.match(r"[A-Za-z']+", text)
    if m:
        w = m.group(0)
        if w[0].isupper() and w.lower() in _CONTINUATION_LOWER:
            return w[0].lower() + text[1:]
    return text


def _capture_utterance(first_wait: float) -> str | None:
    """Capture one dictated utterance. Returns verbatim text, or None on
    timeout (no speech within `first_wait`)."""
    transcriber = BatchTranscriber()
    state = RecorderState(
        min_silence_ms=max(150, user_cfg.dictation.silence_ms or TURN_END_SILENCE_MS),
        on_audio=transcriber.feed,
    )
    try:
        with open_stream(state):
            if not state.wait_for_speech(first_wait):
                # VAD can under-score a quiet mic; trust the buffer only if
                # it actually had speech-level energy (else it's room tone).
                fallback = transcriber.transcribe(raw=True)
                if (
                    fallback
                    and fallback.strip()
                    and transcriber.peak_rms() >= SPEECH_RMS_FLOOR
                ):
                    return fallback.strip()
                return None
            state.clear_speech_event()

            deadline = time.monotonic() + DICTATION_MAX_UTTERANCE_SEC
            while True:
                now = time.monotonic()
                if now >= deadline:
                    break
                if not state.wait_for_silence(timeout=deadline - now):
                    break
                state.clear_silence_event()
                text = transcriber.transcribe(raw=True)
                if text and text.strip():
                    return text.strip()
                # Silence fired but nothing decoded yet — give the speaker a
                # short beat to resume before giving up on this utterance.
                if not state.wait_for_speech(timeout=0.6):
                    break
                state.clear_speech_event()

            final = transcriber.transcribe(raw=True)
            return final.strip() if final and final.strip() else None
    finally:
        try:
            transcriber.stop()
        except Exception:
            pass


def _apply_control(cmd: tuple[str, str], pending_space: bool) -> bool:
    """Execute an editing command. Returns the new pending_space flag."""
    kind, payload = cmd
    if kind == "punct":
        # Tuck punctuation against the previous word: drop the trailing space
        # we left after it, then type "<punct> ".
        if pending_space:
            desktop_control.press_backspace()
        desktop_control.type_into_focus(payload + " ")
        return True
    if kind == "newline":
        if pending_space:
            desktop_control.press_backspace()
        desktop_control.press_enter()
        return False
    if kind == "paragraph":
        if pending_space:
            desktop_control.press_backspace()
        desktop_control.press_enter()
        desktop_control.press_enter()
        return False
    if kind == "enter":  # submit the field
        desktop_control.press_enter()
        return False
    return pending_space


def run_dictation() -> str:
    """Run the dictation loop until the user stops it or it idles out.

    Blocks the caller (the conversation loop) for its whole duration; returns
    a short phrase for Halo to speak when it ends.

    Pipelined: the capture loop pushes each raw chunk onto a queue and
    immediately listens for the next utterance, while a single typist thread
    sanitizes + LLM-corrects + types chunks IN ORDER. The slow per-chunk
    correction (~0.7s cloud round-trip) thus overlaps the time you spend
    speaking the next phrase, instead of stalling the typing.
    """
    if sys.platform != "win32":
        return "Dictation is only available on Windows right now."

    # Make sure the entry instruction has finished playing before the mic
    # opens, so we don't capture Halo's own voice.
    try:
        voice.wait_until_silent(timeout=20.0)
    except Exception:
        pass

    bus.emit("dictation.start")
    play_chime()

    work: queue.Queue = queue.Queue()
    # Shared state owned by the single typist thread (so pending_space etc.
    # stay consistent without locks — only the typist mutates them).
    # sentence_end=True at the very start so the first chunk is capitalized.
    st = {"pending_space": False, "typed": False, "errors": 0, "fatal": None,
          "sentence_end": True}

    def _typist() -> None:
        while True:
            item = work.get()
            if item is None:
                return
            try:
                if item[0] == "control":
                    st["pending_space"] = _apply_control(item[1], st["pending_space"])
                    kind, payload = item[1]
                    if kind in ("newline", "paragraph", "enter"):
                        st["sentence_end"] = True
                    elif kind == "punct":
                        st["sentence_end"] = payload in (".", "?", "!", "…")
                    st["typed"] = True
                    bus.emit("dictation.control", command=item[2])
                    continue
                raw = item[1]
                clean = _sanitize(raw)
                if not clean:
                    continue
                # Flow casing across chunks: lowercase a continuation that the
                # per-chunk corrector over-capitalized; capitalize a new
                # sentence. Then remember whether THIS chunk ended a sentence.
                clean = _flow_case(clean, st["sentence_end"])
                st["sentence_end"] = bool(re.search(r"[.!?…]$", clean))
                res = desktop_control.type_into_focus(clean + " ")
                if not res.ok:
                    st["errors"] += 1
                    bus.emit("dictation.error", text=res.message)
                    if st["errors"] >= 2 and not st["fatal"]:
                        st["fatal"] = res.message or "I couldn't type into that window."
                    continue
                st["errors"] = 0
                st["pending_space"] = True
                st["typed"] = True
                if clean != raw:
                    print(f"  [dictation] typed: {clean!r}  (raw: {raw!r})")
                else:
                    print(f"  [dictation] typed: {clean!r}")
                bus.emit("dictation.text", text=clean, raw=raw)
            except Exception as exc:
                print(f"  [dictation] typist error: {exc}")

    typist = threading.Thread(target=_typist, daemon=True, name="halo-dictation-typist")
    typist.start()

    reason = "idle"
    try:
        while True:
            if st["fatal"]:
                reason = "error"
                break
            text = _capture_utterance(DICTATION_IDLE_SEC)
            if text is None:
                reason = "idle"
                break
            # Stop + control words are matched on the RAW transcript (fast,
            # no cleanup) so ending and punctuation never wait on the LLM.
            if _is_stop(text):
                reason = "command"
                break
            # "Send it" / "send the message" / "enter": submit the field AND
            # return to conversation. Queue the Enter, then end dictation.
            if _is_send_exit(text):
                work.put(("control", ("enter", ""), "send"))
                reason = "sent"
                break
            norm = _normalize_cmd(text)
            cmd = _CONTROL_WORDS.get(norm)
            if cmd is not None:
                work.put(("control", cmd, norm))
            else:
                work.put(("text", text))
    except Exception as exc:  # never let dictation crash the conversation loop
        print(f"  [dictation] capture error: {exc}")
        reason = "error"
    finally:
        # Drain: let the typist finish anything still queued/correcting.
        work.put(None)
        typist.join(timeout=DICTATION_CORRECT_TIMEOUT_SEC + 5.0)

    if st["fatal"]:
        bus.emit("dictation.stop", reason="error")
        return st["fatal"]
    bus.emit("dictation.stop", reason=reason)
    if reason == "sent":
        return "Sent."
    if reason == "command" or st["typed"]:
        return "Dictation off."
    return "Dictation off. I didn't catch anything."
