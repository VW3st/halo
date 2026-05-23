"""Unit-test the sentence buffer + stream-json event parsers."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from halo.agents import _SentenceBuffer, _extract_final_result, _extract_text_delta


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'OK' if ok else 'FAIL'}] {label}{(' — ' + detail) if detail else ''}")


def main() -> None:
    print("=== Sentence buffer chunk handling ===")
    spoken: list[str] = []
    buf = _SentenceBuffer(on_sentence=spoken.append)
    for chunk in [
        "I created hello.py",
        ". It contains a",
        " single print statement",
        ". Let me know if",
        " you want changes.",
    ]:
        buf.feed(chunk)
    buf.flush()
    expected = [
        "I created hello.py.",
        "It contains a single print statement.",
        "Let me know if you want changes.",
    ]
    check(f"3 sentences emitted from 5 streamed chunks", spoken == expected,
          f"got {spoken!r}")
    check("full_text() concatenates", buf.full_text() == " ".join(expected),
          f"got {buf.full_text()!r}")

    print("\n=== Paragraph-break also flushes ===")
    spoken2: list[str] = []
    buf2 = _SentenceBuffer(on_sentence=spoken2.append)
    buf2.feed("First paragraph here\n\nSecond paragraph.")
    buf2.flush()
    check("paragraph splits cleanly", spoken2 == ["First paragraph here", "Second paragraph."],
          f"got {spoken2!r}")

    print("\n=== Event extraction ===")
    cases = [
        # (event, expected_text_delta, expected_final)
        ({"type": "system", "subtype": "init"}, "", None),
        ({"type": "stream_event",
          "event": {"type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "Hello"}}},
         "Hello", None),
        ({"type": "stream_event",
          "event": {"type": "content_block_delta",
                    "delta": {"type": "input_json_delta"}}},
         "", None),
        ({"type": "result", "result": "final summary text"},
         "", "final summary text"),
        ({"type": "assistant", "message": {"content": "ignored"}},
         "", None),
    ]
    for ev, exp_delta, exp_final in cases:
        d = _extract_text_delta(ev)
        f = _extract_final_result(ev, "result")
        ok = (d == exp_delta) and (f == exp_final)
        kind = ev.get("type")
        check(f"event type={kind!r:18} -> delta={d!r}, final={f!r}",
              ok, f"expected delta={exp_delta!r}, final={exp_final!r}")


if __name__ == "__main__":
    main()
