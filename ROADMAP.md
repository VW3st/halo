# Halo — Refactor & Feature Roadmap

> Companion to `ARCHITECTURE.md`. Sequenced lowest-risk-first. **No big-bang
> rewrite** — every phase ships independently, carries its own tests, leaves the
> app working, and decomposes the monoliths *incrementally* as it touches them.
>
> Principle: each phase is a real PR with green tests (`scripts/test_*.py`) +
> `py_compile`, and where behavior is observable, a live verification. We don't
> claim "done" off a green unit test alone.

Legend: 🟢 low risk · 🟡 medium · 🔴 high (touches load-bearing state).

---

## Status — 2026-07-06 (v1.7.0)

| Phase | Status |
|---|---|
| 0 — Cleanup & footguns | ✅ done (dead prompt code deleted, `HALO_DEBUG` env-driven, README accuracy pass) |
| 1 — Turn-taking | ✅ done (`floor.py` + `utterance.py` + merge + compose-then-send; tests green) |
| 2 — Skills registry | ✅ done (`skills.py`, 31 skills discovered, phrase routing) |
| 3 — Structural refactor | ⏳ **next** — deliberately deferred until the v1.7 features prove out in live use |
| 4 — Providers & fallbacks | ✅ done (ElevenLabs→Kokoro lazy fallback; OpenRouter↔Ollama failover in `_chat_json`) |
| 5 — Adjustable prompts | ✅ done (`prompts.py`, `~/.halo/prompts/`, `halo prompts --init`) |
| 6 — Dictation anywhere | ✅ done (`hotkey.py`, Ctrl+Alt+D global hotkey, works while idle) |
| 7 — MCP to the brain | ✅ done (`mcp_client.py` + tool loop in router, opt-in `[mcp] brain_tools`) |
| 8 — Self-improvement | ⏸ deferred by choice ("needs more thought") |
| 9 — Packaging for others | ⏸ deferred by choice (after 8) |

Also landed outside the plan (v1.7.0): the **seamless-conversation fixes** —
commit-race guard, between-turns gap capture, shared VAD threshold (recorder +
Whisper's internal filter), answer-aware hallucination gate, large-v3-turbo,
speech-window gain normalization, confidence-gated semantic repair, time
awareness, ask-before-replying to discovered sessions, and the wedged-WMI
startup guard. See `CHANGELOG.md` §1.7.0.

---

## Phase 0 — Cleanup & footgun removal 🟢  *(recommended first; zero behavior change)*

Shrinks the surface and removes traps before any feature lands. Nothing here
changes runtime behavior; all of it is covered by existing tests + compile.

- Delete dead code: `_STAGE2_PROMPT_OLD` (~400 lines), `STAGE1_PROMPT`,
  `CLAUDE_MODEL` constant, the dead `_run` voice-ticker path for persistent agents.
- Fix the **`MIC_NOISE_GATE_RMS` double-definition** config trap (one knob, one env
  var name, one default — unify `config.py` and `userconfig.py`).
- Resolve the `idle_sec` default drift (dataclass 8 vs template/README 5 → pick one).
- Either register the documented `halo config` subcommand in `cli.py` or remove its
  docs (the helper functions already exist and are unreachable).
- Make `DEBUG` env-driven (`HALO_DEBUG`), default off for installed users.
- Fix stale comments/docstrings (Moonshine→faster-whisper, the lying Stage-1
  docstring) and do a README accuracy pass (mythology names, thresholds, idle).

**Done when:** dead code gone, one noise-gate knob, README matches reality, tests
green. ~½ day.

---

## Phase 1 — Turn-taking: "give me 10 seconds" 🟡  *(your headline; recommended second)*

Make Halo aware of **who holds the floor** and able to **reclaim its turn**, and
stop it from treating filler as commands. Two independent, testable pieces.

**1a — Utterance classifier** (hook: `turn.py:498`, right after `transcribe()`).
Classify every committed transcript into `command | thinking_aloud | counting |
noise | testing` using signals already in hand — `peak_rms()`, the `detect_mode`
shape, the Stage-1 turn-complete verdict, and VAD-vs-RMS onset provenance — plus
new lexical rules (digit-word runs, filler density; seed from `_THINKING_MARKERS`).
Rule-based first (fast, no latency, unit-tested like `_barge_in_decision`), with an
optional LLM tie-breaker only for the ambiguous middle. Non-`command` classes are
dropped/acknowledged, **not routed**.

**1b — Floor state + promise/reclaim.** Add an explicit `floor` (USER / AI /
CONTESTED) to `run_conversation`. A **promise detector** at `voice.py:_note_spoken`
(`voice.py:354` — the one place every Halo utterance is observed) recognizes "give
me N seconds / one moment / let me think" and arms a **reclaim timer**; on expiry
Halo grabs the floor and speaks the result/update instead of passively waiting.
Continuous listening during the window still hears "ok go" / interruptions (reuses
the v1.6.0 barge-in path).

**Risk control:** behind `HALO_TURN_FLOOR=1` until proven; heavy unit tests for the
classifier; live verification of the actual "count to 10" scenario.

**Done when:** Halo can say "give me 10 seconds," let you count, and come back on
its own; counting/thinking-aloud is no longer routed as a command. New
`scripts/test_utterance_classifier.py` + `scripts/test_floor.py`.

---

## Phase 2 — Skills registry & routing 🟢  *(self-contained, demoable)*

Your "make a poster for AIP → use the skill if it exists" ask.

- **Enumerate** Claude + Codex skills on startup (cached probe next to
  `check_availability()`, `agents.py:302`); announce the count in the boot summary;
  re-probe so newly-added skills appear.
- Store per-agent skill metadata on the `AgentConfig` dataclass.
- **Route** a natural command to a matching skill: reuse `registry.resolve()`'s
  fuzzy matcher (`registry.py:187`) to map "make a poster for AIP" → a skill name,
  then inject the skill as a prompt prefix at the dispatch seam (`agents.py:986`).
- Voice affordance: "what skills do you have?" lists them.

**Done when:** Halo lists skills at boot, picks up new ones, and routes a phrase to
the right skill. `scripts/test_skills_registry.py` (matching is pure/testable).

---

## Phase 3 — Structural refactor 🔴  *(the "it's very messy" core; deliberate, heavily tested)*

The cross-cutting untangle that isn't tied to one feature. Done after 1–2 so we
refactor against a known target, not in the dark.

- **Decompose `run_conversation()`** (775 lines) into a `(matcher, handler)`
  dispatch table — each numbered branch becomes a named handler. This also creates
  the clean seam the Phase-1 floor logic plugs into.
- **Extract `agent_match.py`** — collapse the **6 duplicate** agent-name/alias
  tables into one source of truth.
- **Reconcile the three session worlds** — a shared session identity so spawned
  (World A) and discovered (World B) agents in the same cwd aren't two strangers;
  rename the misleading `sessions.get_or_create(agent_key=…)` that actually takes a
  `session_key`.
- **Kill the `agents.py ↔ sessions.py` circular import** — move shared
  stream-parsing (`_SentenceBuffer`, `_extract_*`) to a neutral module.
- De-duplicate the hand-rolled mic InputStream pattern (record/wake/calibrate) and
  the OpenRouter request-building.
- Fix `_persistent_disabled` sticky-for-life (add recovery).

**Risk control:** pure-refactor PRs, behavior-preserving, one subsystem at a time;
the full `scripts/test_*` suite must stay green at each step; live smoke test after
each.

**Done when:** `run_conversation` is a handler loop, one agent-name matcher, no
circular import, suite green, app behaves identically.

---

## Phase 4 — Robust providers & fallbacks 🟡

Fix the fail-to-silence gaps so cloud misconfig degrades *gracefully to local*.

- Voice: ElevenLabs error/missing-key → **auto-fall back to Kokoro** (don't go mute).
- Brain: OpenRouter unreachable → **fall back to local Ollama** before the `unclear`
  fallback (true provider failover, currently absent).
- One shared provider-call wrapper (kills the duplicated OpenRouter boilerplate).
- Surface the active/fallback provider in the dashboard + `doctor`.

**Done when:** pulling the cloud key mid-run keeps Halo fully functional on local.
`scripts/test_provider_fallback.py`.

---

## Phase 5 — Adjustable prompts & personality 🟡

Separate **baked logic** from **user-tunable voice**, and let you edit prompts.

- Externalize `STAGE2_PROMPT`, the STT-correction dictionary, summarizer, and
  few-shots into editable files under `~/.halo/prompts/` with the in-code versions
  as defaults/fallback.
- Keep schema/validation in code (those aren't prompts).
- Clear three-tier model: **baked** (schema/anti-hallucination logic) · **persona**
  (name/role/character — already adjustable) · **prompts** (now editable).
- `halo config` surfaces where each lives.

**Done when:** editing a prompt file changes behavior without code changes; defaults
still ship. `scripts/test_prompt_loading.py`.

---

## Phase 6 — Dictation anywhere 🟡

Close the "must wake first" gap from `ARCHITECTURE.md` §5.

- Global hotkey / always-available entry to `run_dictation()` independent of the
  wake→turn path (Halo can be idle).
- Optional upgrades: live partials (word-by-word), a custom-vocabulary/user
  dictionary, smarter casing.

**Done when:** dictation starts from a hotkey with Halo idle and types system-wide.

---

## Phase 7 — MCP to the brain 🔴

Your "standard MCP directly to the brain." Today MCP only reaches the *spawned
Claude*; the brain emits JSON with no tool-calling.

- Add an MCP client + tool-use loop to `router.py` so the brain itself can call
  standard MCP tools (weather, files, web, your own servers) without spinning up a
  coding agent.
- Reuse the existing `McpConfig` + `~/.halo/mcp.json`.
- Gate behind config; keep the fast no-tool path for plain routing.

**Done when:** the brain answers a tool-requiring question by calling an MCP tool
directly, no agent dispatch.

---

## Phase 8 — Self-improvement 🟡

Beyond fact-accumulation (the only adaptivity today is `halo calibrate`).

- Outcome signals: did the dispatch succeed, did you correct/cancel, did you repeat
  yourself → tune thresholds and disambiguation over time.
- Optional vector memory backend (the `memory.py` interface is already built for a
  Mem0 drop-in) for semantic recall instead of keyword-overlap.
- Auto-suggest persona/prompt tweaks from recurring corrections.

**Done when:** Halo measurably reduces a recurring mis-route after you correct it a
few times.

---

## Phase 9 — Packaging for others 🟢

Make it installable by someone who isn't you.

- `DEBUG` off by default (done in Phase 0); first-run config wizard (`halo config
  --init`); doctor-driven setup.
- Publish to PyPI (`pip install halo-voice`); pin model-download UX.
- Honest cross-platform story (Windows-first features clearly labeled; graceful
  degradation elsewhere).
- README/docs rewrite to match reality.

**Done when:** a fresh machine goes from `pip install` to a working wake→dispatch in
documented steps.

---

## Suggested order & why

`0 → 1 → 2` first: cleanup clears the footguns, then the two headline features
(turn-taking, skills) land on a tidy-enough base and prove out. Then `3` (the big
structural refactor) cleans everything those features touched. `4–9` are
independent and can be reordered by what you want next. Phases `3` and `7` are the
only 🔴 — they get pure-refactor discipline and live smoke tests.

**My recommendation: start Phase 0 now** (zero risk, immediate momentum, removes
the ~400 lines of dead prompt and the noise-gate footgun), then go straight into
**Phase 1** since the "10-second" problem is the one you care about most and it
builds directly on the v1.6.0 barge-in work.
