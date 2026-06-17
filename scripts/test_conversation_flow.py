"""Regression tests for conversation-loop helpers in halo/__main__.py.

Covers the end-phrase strong/weak split (so a casual "That's it?" no longer
slams the session shut) and the name-personalization punctuation fix
("What's up?, Valentino." -> "What's up, Valentino?").
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from halo import __main__ as m  # noqa: E402
from halo import dictation as d  # noqa: E402
import halo.userconfig as uc  # noqa: E402
import halo.tools as _tools  # noqa: E402

_tools.set_dry_run(True)  # never launch real apps from the test suite

_ran = 0


def check(label: str, ok: bool) -> int:
    global _ran
    _ran += 1
    print(f"  [{'OK' if ok else 'FAIL'}] {label}")
    return 0 if ok else 1


def main() -> int:
    failed = 0

    # --- end-phrase: weak / reaction-prone enders -------------------------
    # The reported bug: "That's it?" (a reaction = "is that all?") ended the
    # conversation. The raw transcript keeps the "?" cleaned_text strips.
    failed += check(
        "weak ender as a question does NOT end",
        m._is_end_phrase("That's it", "That's it?") is False,
    )
    failed += check(
        "weak ender as a statement DOES end",
        m._is_end_phrase("that's it", "That's it.") is True,
    )
    failed += check(
        "weak ender with leading filler ends",
        m._is_end_phrase("okay that's all", "okay that's all") is True,
    )
    failed += check(
        "weak ender embedded in a longer sentence does NOT end",
        m._is_end_phrase("that's it for the bug but keep going", "") is False,
    )
    failed += check(
        "bare 'bye' (statement) ends",
        m._is_end_phrase("bye", "bye") is True,
    )

    # --- end-phrase: strong enders fire anywhere --------------------------
    failed += check(
        "strong ender fires mid-utterance",
        m._is_end_phrase("ok halo go to sleep now", "") is True,
    )
    failed += check(
        "goodbye ends", m._is_end_phrase("goodbye", "goodbye") is True
    )
    failed += check(
        "stop listening ends",
        m._is_end_phrase("hey stop listening please", "") is True,
    )

    # --- name personalization punctuation ---------------------------------
    uc.cfg.persona.user_name = "Valentino"
    failed += check(
        "name tucked before '?' not after",
        m._personalize_reply("Good. What's up?") == "Good. What's up, Valentino?",
    )
    failed += check(
        "name before final period",
        m._personalize_reply("Good.") == "Good, Valentino.",
    )
    failed += check(
        "non-greeting reply left untouched",
        m._personalize_reply("The build failed.") == "The build failed.",
    )

    # --- direct-mode cross-agent redirect ---------------------------------
    # The "not listening to me" bug: in direct dialogue with Claude, an
    # explicit hand-off to Codex ("spawn Codex", "ask Krodex to ...") used to
    # get piped to Claude. Whisper garbles "Codex" -> "Kodex"/"Krodex" too.
    C, X = "claude_code", "codex_cli"
    failed += check(
        "garbled 'ask Krodex to generate images' redirects to Codex",
        m._direct_redirect(
            "in the meantime can you open Kodex as well and ask Krodex to "
            "generate some images for",
            C,
        ) == (X, "generate some images for"),
    )
    failed += check(
        "'switch and spawn codex' is a pure switch to Codex",
        m._direct_redirect("now switch and spawn codex to", C) == (X, ""),
    )
    failed += check(
        "real word 'codec' does NOT redirect",
        m._direct_redirect("use the codec library for audio", C) is None,
    )
    failed += check(
        "real word 'cloud' does NOT redirect",
        m._direct_redirect("the cloud function is slow", C) is None,
    )
    failed += check(
        "same-agent mention does NOT redirect",
        m._direct_redirect("ask claude to add tests", C) is None,
    )
    failed += check(
        "mention without a targeting verb does NOT redirect",
        m._direct_redirect("the codex run looks good", C) is None,
    )
    failed += check(
        "from Codex, 'switch to claude' switches to Claude",
        m._direct_redirect("switch to claude please", X) == (C, ""),
    )

    # --- session back-navigation (MRU) ------------------------------------
    # Build history: started with Claude, jumped to Codex.
    m._session_mru = []
    m._nav_visit("claude_code", None)
    m._nav_visit("codex_cli", None)
    claude, codex = ("claude_code", None), ("codex_cli", None)
    failed += check(
        "'go back' from Codex returns to Claude",
        m._resolve_nav("go back", codex) == claude,
    )
    failed += check(
        "'the other one' returns to Claude",
        m._resolve_nav("the other one", codex) == claude,
    )
    failed += check(
        "'go to the previous session' returns to Claude",
        m._resolve_nav("okay go to the previous session", codex) == claude,
    )
    failed += check(
        "named 'back to claude' returns to the Claude frame",
        m._resolve_nav("back to claude please", codex) == claude,
    )
    m._nav_visit("claude_code", None)  # now back in Claude
    failed += check(
        "toggle: 'go back' from Claude returns to Codex",
        m._resolve_nav("go back", claude) == codex,
    )
    failed += check(
        "instruction containing 'the last one' does NOT navigate",
        m._resolve_nav("use the last one for the header", claude) is None,
    )
    failed += check(
        "'roll back the migration' does NOT navigate",
        m._resolve_nav("tell codex to roll back the migration", claude) is None,
    )
    failed += check(
        "'back to halo' is not a nav target (handled elsewhere)",
        m._resolve_nav("back to halo", claude) is None,
    )
    m._session_mru = []
    failed += check(
        "empty history: 'go back' resolves to nothing",
        m._resolve_nav("go back", (None, None)) is None,
    )

    # --- "open a Claude/Codex/cloud session" -> spin up the agent -----------
    failed += check(
        "'open a cloud session' opens a Claude session (not an app)",
        m._agent_open_intent("open a cloud session please") == (["claude_code"], ""),
    )
    failed += check(
        "'open a codex session' opens Codex",
        m._agent_open_intent("okay open a codex session") == (["codex_cli"], ""),
    )
    failed += check(
        "'open clod' (garble) opens Claude",
        m._agent_open_intent("open clod") == (["claude_code"], ""),
    )
    failed += check(
        "two-agent open returns both in order, no task",
        m._agent_open_intent("open a session with Claude and a session with Codex")
        == (["claude_code", "codex_cli"], ""),
    )
    failed += check(
        "'open codex AND generate X' carries the task",
        m._agent_open_intent("open codex and generate a hero")
        == (["codex_cli"], "generate a hero"),
    )
    failed += check(
        "'open the browser' is NOT an agent open",
        m._agent_open_intent("open the browser") == ([], ""),
    )
    failed += check(
        "'open the cloud storage' (no session ctx) is NOT an agent open",
        m._agent_open_intent("open the cloud storage") == ([], ""),
    )

    # --- follow-up gate: design/media commands reach the agent -------------
    import halo.followup_gate as fg
    failed += check(
        "'I want to generate a hero' reaches the agent (was dropped)",
        fg.passes("I want to generate a hero", "codex_cli")[0] is True,
    )
    failed += check(
        "'generate a hero' (novel object) reaches the agent",
        fg.passes("generate a hero", "codex_cli")[0] is True,
    )
    failed += check(
        "'design a logo' reaches the agent",
        fg.passes("design a logo", "codex_cli")[0] is True,
    )
    failed += check(
        "'make three slides' reaches the agent",
        fg.passes("make three slides", "claude_code")[0] is True,
    )
    failed += check(
        "a phone aside is still dropped",
        fg.passes("hey John, can you hear me?", "claude_code")[0] is False,
    )

    # --- narration vs command (stop re-opening apps when MENTIONED) --------
    for narration in (
        "okay so you did open paint",
        "you did open paint",
        "I opened it already",
        "as you can see I say open and then you open",
        "so you opened the browser",
        "did you open the calculator",
    ):
        failed += check(
            f"narration not a command: {narration!r}",
            m._is_narrated_action(narration) is True,
        )
    for command in (
        "open paint",
        "I want you to open paint",
        "can you open paint",
        "open chrome and open paint",
        "please open notepad",
    ):
        failed += check(
            f"command still fires: {command!r}",
            m._is_narrated_action(command) is False,
        )

    # --- confirmation: "yes please" and friends ----------------------------
    failed += check("'Yes, please' confirms", m._is_yes("Yes, please") is True)
    failed += check("'yeah go ahead' confirms", m._is_yes("yeah go ahead") is True)
    failed += check(
        "'yes but make it blue' is NOT a bare yes",
        m._is_yes("yes but make it blue") is False,
    )

    # --- barge-in echo guard ("never lose the mic" / capture-while-talking) -
    import halo.voice as v
    import halo.config as cfg
    v._note_spoken("Want me to put Claude on it?")
    failed += check(
        "exact echo of Halo's words is detected as echo",
        v.recently_spoke("want me to put claude on it") is True,
    )
    failed += check(
        "user echoing one word ('yes claude do it now') is NOT echo",
        v.recently_spoke("yes claude do it now please") is False,
    )
    failed += check(
        "a fresh interruption is NOT echo",
        v.recently_spoke("actually also add a dark mode toggle") is False,
    )
    failed += check(
        "continuation grace is bounded below the hard ceiling",
        cfg.TURN_MAX_SEC < cfg.TURN_MAX_HARD_SEC
        and cfg.TURN_CONTINUATION_GRACE_SEC > 0,
    )

    # --- dictation voice exit ---------------------------------------------
    failed += check(
        "'back to the session' exits dictation",
        d._is_exit_dictation("back to the session") is True,
    )
    failed += check(
        "'get back to the session' exits dictation",
        d._is_exit_dictation("get back to the session") is True,
    )
    failed += check(
        "'back to halo' exits dictation",
        d._is_exit_dictation("back to halo") is True,
    )
    failed += check(
        "dictated content with 'back' is NOT an exit",
        d._is_exit_dictation("go back to the file and edit it") is False,
    )

    print(f"\n{_ran - failed}/{_ran} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
