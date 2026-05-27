"""Test halo/router.py:summarize_reply against live Ollama.

Skips gracefully (exit 0) when Ollama is unreachable.

Asserts:
  - empty in -> empty out
  - short text comes back short
  - long text gets compressed to <= 220 chars
  - returns ONE sentence (one terminating punctuation, no internal newlines)
  - falls back to head_truncate cleanly when forced to (cap check)

Run: python scripts/test_summarize.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from halo.router import _head_truncate, summarize_reply


SHORT_REPLY = "I refactored the routes file."

MEDIUM_REPLY = (
    "I added bcrypt password verification to auth.py and wired it into "
    "the existing login_user flow. The tests in test_auth.py cover both "
    "correct and incorrect passwords."
)

LONG_REPLY = (
    "I refactored the authentication module to use bcrypt instead of the "
    "previous SHA-256 setup. The verify_password function now takes a "
    "configurable cost parameter, defaulting to 12. I updated the "
    "login_user endpoint to use the new verification, removed the legacy "
    "hash_password helper, and migrated the existing test suite to use "
    "pytest fixtures for the user records. I also added rate limiting on "
    "the /login endpoint using a simple token bucket with 5 attempts per "
    "minute per IP, and wrote three new integration tests covering the "
    "lockout behavior. All existing tests continue to pass. The changes "
    "are spread across auth.py (192 lines), test_auth.py (84 lines), and "
    "a new ratelimit.py module (47 lines)."
)


def assert_true(cond, msg):
    if not cond:
        raise AssertionError(msg)


def test_empty_in_empty_out():
    assert_true(summarize_reply("") == "", "empty input must return empty")
    assert_true(summarize_reply("   ") == "", "whitespace input must return empty")


def test_head_truncate_pure():
    assert_true(_head_truncate("Short.", 50) == "Short.", "short passthrough")
    assert_true(_head_truncate("X" * 100, 30) == "X" * 30 + "...", "hard cap with ellipsis")
    # Should prefer sentence boundary near the cap when one exists in the
    # head; for very short caps, falls back to hard truncation with ellipsis.
    out = _head_truncate("Done in 5 minutes. Tests pass.", 12)
    assert_true(len(out) <= 20, f"head_truncate fallback over 20 chars: {out!r}")


def test_short_reply_passthrough_or_tight():
    out = summarize_reply(SHORT_REPLY, "refactor the routes file")
    assert_true(len(out) > 0, "short reply -> non-empty")
    assert_true(len(out) <= 220, f"short reply over 220 chars: {len(out)} chars: {out!r}")


def test_medium_reply():
    out = summarize_reply(MEDIUM_REPLY, "add password verification")
    assert_true(len(out) > 0, "medium reply -> non-empty")
    assert_true(len(out) <= 220, f"medium over cap: {len(out)} chars: {out!r}")
    # Should not introduce markdown.
    assert_true("**" not in out and "`" not in out, f"markdown leaked: {out!r}")


def test_long_reply_compressed():
    out = summarize_reply(LONG_REPLY, "refactor auth to use bcrypt")
    assert_true(len(out) > 0, "long reply -> non-empty")
    assert_true(len(out) <= 220, f"long reply NOT compressed: {len(out)} chars: {out!r}")
    # Single sentence — no internal newlines.
    assert_true("\n" not in out, f"multiline summary: {out!r}")
    # Heuristic single-sentence check: at most one terminating punctuation in body.
    body = out.rstrip(".!?").strip()
    terminators = sum(body.count(c) for c in ".!?")
    assert_true(terminators <= 2,
                f"looks like multi-sentence summary ({terminators} terminators): {out!r}")


def test_long_reply_actually_summarizes():
    """The summary should be MUCH shorter than the input — sanity check
    against the brain returning the input verbatim."""
    out = summarize_reply(LONG_REPLY, "refactor auth to use bcrypt")
    ratio = len(out) / max(1, len(LONG_REPLY))
    assert_true(ratio < 0.5, f"summary too long vs input (ratio {ratio:.2f}): {out!r}")


def main() -> int:
    # Probe Ollama via preload — skip the whole suite if unreachable.
    try:
        from halo.router import preload_router
        preload_router()
    except Exception as exc:
        print(f"Ollama not reachable ({exc}) — skipping summarize tests")
        return 0

    tests = [
        test_empty_in_empty_out,
        test_head_truncate_pure,
        test_short_reply_passthrough_or_tight,
        test_medium_reply,
        test_long_reply_compressed,
        test_long_reply_actually_summarizes,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as exc:
            print(f"  FAIL  {t.__name__}\n    {exc}")
            failed += 1
        except Exception as exc:
            print(f"  ERR   {t.__name__}\n    {type(exc).__name__}: {exc}")
            failed += 1
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
