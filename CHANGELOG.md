# Changelog

All notable changes to Halo. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning is SemVer-ish (still pre-1.0, expect breaking changes).

## [Unreleased]

(none yet)

---

## [1.4.1] — 2026-06-17

Conversation-feel fixes from live testing — stop the wrong agent swallowing
your commands, stop a casual "that's it?" hanging up on you, and stop dropping
the half of a command Halo can't do locally.

### Fixed
- **Direct-mode cross-agent hand-off** — while talking to one agent, naming the
  OTHER ("switch to Codex", "spawn Codex", "ask Codex to …") used to get piped
  to the current agent instead. Whisper hearing "Codex" as "Kodex"/"Krodex"
  made it worse — the strict dispatch regexes missed the garble, so it fell
  through to the active session. New `_direct_redirect` (+ curated, garble-
  tolerant `_AGENT_REDIRECT_ALIASES`) intercepts a verb-marked mention of a
  different agent in direct mode and switches/dispatches THERE. Real words that
  collide with fuzzy triggers ("codec", "cloud") are excluded so a normal
  instruction never yanks the conversation. Extra Whisper garbles added to the
  agents' `fuzzy_triggers`.
- **End phrase too eager** — a reaction like "That's it?" (= "is that all?")
  ended the conversation. End phrases are now split into STRONG (unambiguous:
  "go to sleep", "goodbye", "stop listening" — fire anywhere) and WEAK
  ("that's it", "that's all", "bye" — only end when they're the WHOLE utterance
  AND not a question). The raw transcript's trailing "?" (which `cleaned_text`
  strips) is the question signal.
- **Name punctuation** — "Good. What's up?" + name produced "What's up?,
  Valentino." The name is now tucked in before the final terminal punctuation:
  "What's up, Valentino?".

### Added
- **Delegate the part Halo can't do** — a chained command does its local parts
  and hands back the remainder instead of dropping it: "open a browser AND
  search the web for X" opens the browser, then offers "I can't do that part
  myself — want me to put Claude on it?". `execute_system_intent` now returns
  the unhandled leftover; the loop offers to delegate (honoring "confirm before
  Claude"). A "yes" dispatches it.
- **Conversational hand-off ack** — dispatching to an agent now says "On it —
  starting Claude. Anything else while that runs?" instead of dead air, so you
  can keep going while the job works in the background.
- `scripts/test_conversation_flow.py` (18 checks: end-phrase split, name
  punctuation, direct-mode redirect). Tool regressions now cover the chained-
  command leftover (28 checks total).

---

## [1.4.0] — 2026-06-16

Persistent memory — no more amnesia.

### Added
- **Persistent memory layer** (`halo/memory.py`) — local SQLite store at
  `~/.halo/halo_memory.db`, three tiers:
  - short-term (recent turns, this + last sessions),
  - long-term (all turns, pruned after `retention_days`),
  - important facts (durable, never pruned).
  Every conversation turn + tool action is logged via the conversation brief's
  write-through; a compact `# MEMORY` block (top facts + recent/relevant turns)
  is injected into the routing brain and chat model each turn, so Halo answers
  "what did you open first?" / "what did we talk about" across sleep/wake AND
  restarts.
- **"remember that X"** voice command → stores a durable fact. Salient
  statements ("I'm working on …", "I prefer …", "every day …") are
  auto-extracted as facts (project / preference / routine).
- `[memory]` config (`enabled`, `path`, `retention_days`, `max_turns`,
  `max_facts`) + `HALO_MEMORY_ENABLED`. Local-only; nothing leaves the machine.
  Interface is backend-agnostic so a vector/graph store (e.g. Mem0) can replace
  the keyword retrieval later.
- `scripts/test_memory.py` regression suite (10 checks).

---

## [1.3.1] — 2026-06-16

### Fixed
- **Multitask local tools** — "open calculator in the browser" / "open the
  calculator and paint" / "open chrome and spotify and notepad" now open ALL
  named apps. The tool loop fired only the first match per phrase, and a bare
  list item ("…and paint") lost its verb after the conjunction split. Both
  `is_pure_tool` and `execute_system_intent` propagate the open verb now.
- **Questions never confirm-dispatch to Claude** — "what app did you open
  first?" is a memory question, but it contains "open" so it got a
  "Send to Claude?" prompt. `_needs_confirmation` now returns False for
  `intent=question`; questions go to the chat brain (with memory). Halo's tool
  actions are recorded in the conversation brief so it can answer them.
- Chat no longer claims it was "told" the user's name earlier (it's from
  settings).

---

## [1.3.0] — 2026-06-16

Conversational, machine-adaptive, dictate-anywhere.

### Added
- **Real conversation** — chit-chat / casual questions get a streamed spoken
  reply from the brain (`router.chat_reply_stream`, speaks sentence-by-sentence
  so the first words land in ~0.4 s). Optional separate, smarter chat model via
  `HALO_OPENROUTER_CHAT_MODEL` (routing stays on the fast model).
- **Conversation memory** — a rolling brief (user + Halo + agent turns) persists
  across sleep/wake and is fed to the router and chat brain, so references
  ("the other one", "what did we just do") resolve.
- **Dictate-anywhere (Windows)** — `dictation.py` + `desktop_control.py`: say
  "dictate", click any field, speak; words typed at the caret via SendInput
  Unicode injection with accent-aware cleanup. "send it" submits and returns.
- **`halo calibrate`** (`calibrate.py`) — measures this mic + your voice and
  writes tuned wake settings to `[profiles.<hostname>]`. Per-machine config
  overlays (`userconfig._apply_profile`).
- **STT wake-verification** — a second Whisper pass confirms the wake word after
  the DNN fires, rejecting false fires the threshold can't (`wake.verify`).
- **Local "say"** — "say <text>" / "tell the audience <text>" speaks it aloud.
- **Generic app launcher** — "open <app>" (Paint, Spotify, Word, …) runs locally
  instead of spawning an agent; politeness wrappers ("can you open … please")
  tolerated.
- **Desktop control via MCP** (opt-in) — spawned Claude sessions can drive the
  desktop (Windows-MCP). `[mcp]` config.
- **Paid providers** — ElevenLabs TTS + OpenRouter brain behind provider
  switches (still 100% local by default).
- **New React/Vite/Tailwind dashboard** (`dashboard/`), mobile-responsive;
  the backend reuses an open browser tab across restarts instead of spawning
  new ones.

### Changed / fixed
- **Confirm before Claude** — Halo no longer silently spawns an agent; an
  un-named coding task asks first. `intent=system` never auto-dispatches.
- **Turn-taking** — no longer cuts you off on a trailing "is/are/need/want/to".
- **"unclear" stays silent** instead of nagging, and no longer merges fragments
  into a blob.
- **Session-action hallucination guard** — drops invented switch/list actions
  the words don't support (`router._validate_session_action`).
- **STT gain normalization** — boosts quiet mics (e.g. NVIDIA Broadcast ~0.05)
  before Whisper, fixing garbled transcripts.
- Wired several dead config literals (idle timers, wake knobs) back to config.

---

## [1.2.3] — 2026-05-25

UX fix — "hello, can you hear me?" no longer spawns a Claude session.

### The bug
v1.2.2 trace from live testing:
```
heard: "Hello, what's happening right now? What are you working on?
        This is a test. Do you hear me?"
no local handler matched -> Stage 2 LLM
stage 2: 4667ms
decision: status=ready  intent=system  agent=none
no tool matched ... -> falling through to claude_code
dispatching to Janus / claude (persistent session, starting)
```
The user said "hello, are you there?" and Halo spawned a fresh Claude
session to answer it. 12-second round-trip, real tokens used, surprising
behavior. Two layers conspired:
- The 1.5B Stage 2 LLM mis-classified conversational pings as
  `status=ready, intent=system` instead of `status=chitchat`.
- The orchestrator's `intent=system + no tool matched` branch blanket-
  dispatched to Claude on the theory that "falling through beats a flat
  I-don't-know." That theory is wrong for chitchat.

### Added (chitchat-to-Halo regex layer)
- **`_CHITCHAT_PATTERNS`** in `halo/__main__.py` — 10 regex patterns
  with hand-picked canned replies (multiple variants per pattern,
  randomly picked so Halo doesn't sound robotic when you hammer her
  with tests). Covers:
  - existence/presence checks (*"are you there?"* / *"are you
    listening?"* / *"are you awake?"*)
  - audio checks (*"can you hear me?"* / *"do you hear me?"* / *"am I
    coming through?"*)
  - wake-only greetings (*"hi halo"* / *"hey there"* / *"yo"*)
  - test pings — bare *"test"*, *"testing"*, repeated (*"test test"*),
    counters (*"1 2 3"*, *"one two three"*, *"1 2 3 test"*), mic checks
    (*"mic check"*, *"audio check"*, *"this is a test"*)
  - activity checks (*"what are you doing?"* / *"what are you working
    on?"* / *"what are you up to?"*)
  - status pings (*"what's up?"* / *"what's happening?"* / *"how are
    you?"*)
  - thanks (*"thanks"* / *"good job"* — Halo says *"anytime."*)
- **`_chitchat_reply(text)`** returns a canned reply string when any
  pattern fires, else None. Random pick from the variant list.

### Changed (orchestrator priority list)
- New step **6b. Chitchat-to-Halo** inserted between Status/Replay
  (step 6) and Tool fast-path (step 7). Runs BEFORE Stage 2 LLM —
  saves the 4-5 s round-trip on every chitchat utterance. Halo speaks
  the canned reply via Kokoro, stays awake (no return), and continues
  to the next turn.
- **Tightened `intent=system` fall-through.** New `_looks_like_real_task`
  heuristic gates the auto-dispatch to Claude: only proceeds if the
  cleaned text contains a coding-imperative verb (write / fix /
  refactor / etc.) OR a technical noun (file / function / bug / route
  / etc.). Otherwise Halo says *"I'm not sure what you'd like me to
  do."* — polite refusal, no Claude session spawned, no surprise.

### Verified
- 19/19 chitchat detector cases pass (13 chitchat-shape + 6 real-task
  must NOT match chitchat).
- 7/7 real-task heuristic cases pass.
- All 5 existing test suites still pass (50/50 total: 14 registry +
  15 discovery + 7 agents_multicwd + 8 brain_routing + 6 summarize).

### What this changes in practice
| Said | Old (v1.2.2) | New (v1.2.3) |
|---|---|---|
| *"Hello, are you there?"* | spawn Claude → 12s round-trip | *"Yes, I'm here."* — instant |
| *"Test test"* / *"1 2 3"* | spawn Claude | *"Test received."* |
| *"What are you doing?"* | spawn Claude | *"Just listening. What would you like me to do?"* |
| *"Open the dishwasher"* | spawn Claude (real-task heuristic still fires on "open") | spawn Claude — unchanged for actual unknown system commands |
| *"Claude, build me a fizzbuzz"* | spawn Claude — unchanged | spawn Claude — unchanged |
| *"open chrome"* | local tool fires — unchanged | local tool fires — unchanged |

The chitchat layer never blocks a real command — it requires a clear
conversational pattern at the start or as the whole utterance.

---

## [1.2.2] — 2026-05-25

Bug fix — discovery was missing real Claude sessions on Windows
WinGet-installed Claude. v1.2.0 shipped with fingerprints written for
the npm-installed Claude only; users with the WinGet binary saw
`discovery: found 0` even when 5+ Claude sessions were running.

### Fixed
- **`halo/discovery.py:_classify_cmdline`** now slash-normalizes the
  cmdline (`\` -> `/`) before fingerprint matching. Windows npm-installed
  Claude (whose cmdline is `node ...\\@anthropic-ai\\claude-code\\...`)
  now matches the same `@anthropic-ai/claude-code` fingerprint that
  catches POSIX installs. v1.2.0's `claude-code\\cli` backslash variant
  removed in favor of the normalized check.
- **`_AGENT_FINGERPRINTS`** extended with `/WinGet/Links/claude.exe`
  (Windows WinGet install path) and `/claude.exe` (any standalone
  binary). Same treatment for codex.
- **`_HALO_SPAWNED_MARKERS`** extended to filter out the Claude
  Desktop Electron app (`/WindowsApps/Claude_`, `/Claude.app/Contents`)
  and its child processes (`--type=renderer`, `--type=gpu-process`,
  `--type=utility`, `--type=crashpad-handler`) so they don't get
  mis-detected as CLI sessions.
- **`DiscoveredSession.__str__`** uses ASCII `<-` instead of Unicode
  `←` so the live-scan diagnostic doesn't crash with
  `UnicodeEncodeError` on Windows cp1252 consoles.

### Tests
- 6 new cases in `scripts/test_discovery.py` covering WinGet install
  paths, Claude Desktop app exclusion (renderer / gpu-process /
  top-level), and an MCP-server false-positive guard.
- Live-scan smoke now prints the discovered sessions (was crashing
  silently before this fix on Windows).

### Verified on V's machine
`halo sessions` correctly enumerates all 5 interactive Claude sessions
across `D:\\vclaude`, `D:\\livestream`, `D:\\AIPCLAUDE`, `D:\\Halo`,
`D:\\socialmanager` — none of which v1.2.1 could see.

---

## [1.2.1] — 2026-05-25

Two surgical patches on top of v1.2.0:
- Hybrid streaming + one-sentence reply summaries (the user's request:
  "when the model reply might be very long, our brain has to summarize
  it in one sentence — so it's not reading a full story back").
- Regex fallback layer for the 1.5B brain's session-action misses
  (caught during v1.2 live testing: the model emits `target_session`
  but forgets `session_action`, which was breaking switch / list /
  where_am_i flows).

### Added (Stage 3 — one-sentence summarizer)
- **`router.py:summarize_reply()`** — new Ollama-backed compressor.
  Schema-constrained output `{one_sentence: str}`, target 15-25 words.
  Falls back to `_head_truncate()` on Ollama failure so the caller
  always gets a speakable string. Includes 3 worked examples in the
  prompt (short / medium / long) to anchor the model's brevity.
- **Hybrid streaming in `__main__.py`** — `_StreamState` per job tracks
  `sentences_spoken` / `chars_spoken` / `was_truncated`. For agents
  with `streams_text_deltas=True` (Claude), the first
  `LIVE_STREAM_MAX_SENTENCES` (default **2**) sentences are spoken
  live via Kokoro as they generate. After the budget, `_speak_chunk`
  silently buffers without calling `say()`. At job completion,
  `_drain_completed_jobs` checks `was_truncated`; if true and the
  unsaid remainder is ≥ 60 chars, calls `summarize_reply()` on the
  remainder and speaks one cap sentence: *"And, I added bcrypt
  verification and wired it into login_user."*
- **Batch-agent summarization (Codex)** — same threshold. Replies
  under `REPLY_SUMMARIZE_THRESHOLD_CHARS` (default **400**) speak as
  before via `summarize_for_speech` head-trim; longer replies go
  through `summarize_reply()` for a real LLM-backed one-sentence cap.

### Added (config)
- `LIVE_STREAM_MAX_SENTENCES = 2` — how many sentences a streaming
  agent gets to speak live before Halo cuts to summary mode.
- `REPLY_SUMMARIZE_THRESHOLD_CHARS = 400` — only summarize when the
  reply is genuinely long. Short replies (< 400 chars) speak verbatim.

### Added (regex fallback for session actions)
- **`router.py:_apply_session_action_fallback()`** — applied inside
  `understand_and_route()` after the LLM call. When `has_context=True`
  AND the brain emitted `target_session` but no `session_action`, this
  layer inspects `cleaned_text` for switch / list_sessions /
  where_am_i patterns and synthesizes the action. Fixes the v1.2.0
  failure mode where "switch to website" would resolve target_session
  correctly but then fall through to a regular dispatch (sending the
  literal string "Switch to website." as a prompt to Claude in the
  website cwd).
- Pure-switch detection requires a switch verb (switch / jump / move /
  go / work on / use / activate / open) **without** a coding-imperative
  verb (write / make / build / fix / refactor / etc.) — otherwise
  "in website, fix the bug" would get mis-classified as a switch.

### Tests
- **`scripts/test_summarize.py`** (6 cases) — empty input, head-truncate
  fallback, short/medium/long reply lengths under cap, single-sentence
  shape (no internal newlines, <=2 terminators), compression ratio.
  Ollama-dependent (graceful skip).
- Updated `scripts/test_brain_routing.py` — all 8 cases now pass
  (4/8 → 8/8) thanks to the session-action fallback layer.

### Verified
- 44/44 tests pass on a live machine with Ollama up (30 offline +
  8 brain routing + 6 summarize).
- `import halo.__main__` is warning-free under `-W error::SyntaxWarning`.

### Why this exists
Without summarization, every long Claude reply (e.g. "I refactored
auth.py + test_auth.py + ratelimit.py and here's everything I did
across 11 sentences") gets fully read aloud — exhausting, and you
can't interrupt cleanly. With hybrid mode you hear *"On it. I'm
calling this session Mercury. I refactored the authentication module
to use bcrypt."* live, then silence while Claude finishes its work,
then *"And, I added rate limiting and three new integration tests."*
Total spoken: ~6 seconds instead of ~45 seconds. The full reply is
still in your terminal scrollback if you want details.

### Knobs to tune
- Want even more live narration before summary? Bump
  `LIVE_STREAM_MAX_SENTENCES`.
- Want more replies to speak verbatim?
  Raise `REPLY_SUMMARIZE_THRESHOLD_CHARS`.
- Want the old "speak everything" v1.1 behavior?
  Set both to very large numbers.

### Known limitations
- Summarization adds ~300 ms (one extra Ollama round-trip) at the end
  of every long reply. Acceptable on a warm KV cache; cold the first
  call after model load can be a few seconds.
- If Ollama crashes mid-job the fallback head-truncates instead, so
  you always hear *something* — but it'll be the lossy first-N-chars
  cut, not a real summary.
- Brain-summary quality is tied to qwen2.5:1.5b — usually fine but
  occasionally re-orders or drops a detail. Full text in the
  terminal is the source of truth; TTS is the executive summary.

---

## [1.2.0] — 2026-05-25

The "talk to whichever Claude I want" release. v1.1 bound Halo to one
Claude in its launch cwd; v1.2 auto-discovers every running coding-agent
session on the machine and lets the brain route to them by voice.

### Added (multi-session discovery)
- **`halo/discovery.py`** — psutil-based process scanner. Walks every
  process every 2 s, classifies coding-agent CLIs by cmdline fingerprint
  (handles Windows npm-shim case where `claude` is `node.exe
  ...\\@anthropic-ai\\claude-code\\cli.js`), records `{pid, cwd, label,
  parent_pid, parent_name}` per session. Excludes Halo's own spawned
  Claudes (`--input-format stream-json`) and Halo's own Codex
  (`codex exec`) so the discovery list shows only sessions the user
  started themselves. Cheap (O(n_processes) per scan, two psutil calls
  per match). Runs on a `DiscoveryThread` daemon with a fast initial
  synchronous scan.
- **`halo/registry.py`** — `SessionRegistry` keyed by label.
  Collision-resolves two cwds with the same basename via
  parent-disambiguation (`client-a/halo` vs `client-b/halo`, then
  pid suffix as last resort). 5-tier fuzzy `resolve()`:
  pseudo-targets ("all", "focused", "active", "here") → exact label →
  substring (both directions) → token overlap. Strips filler words
  ("the", "project", "thing", "one") before matching so *"the website
  project"* still resolves to `website`. Voice-friendly `speak_list()`
  and `speak_active()` for spoken summaries.

### Added (brain becomes session-aware)
- **`SessionContext` + `_format_context_block()`** in `halo/router.py`.
  Every Stage 2 LLM call now optionally receives a prepended CURRENT
  CONTEXT block listing `active_session`, `discovered_sessions[]`
  (label/cwd/agent), and `focused_terminal_session` (reserved for
  Phase 2 focus binding). Block is **omitted entirely** when the
  registry is empty — single-session mode pays zero token overhead and
  behaves byte-identically to v1.1.
- **Stage 2 schema extended** with two new fields:
  - `target_session` — label / `"active"` / `"focused"` / `"all"` / `""`
  - `session_action` — `""` / `"switch"` / `"list_sessions"` / `"where_am_i"`
  Ollama's structured-output `format` enforces them, so the orchestrator
  always sees strings, never `KeyError`.
- **Stage 2 prompt** gained a new SESSION-AWARE ROUTING section + 6
  worked examples (switch, list, where_am_i, cross-session one-shot,
  fanout, implicit-active default).

### Added (agents become per-cwd)
- **`halo/agents.py:session_key(agent_key, cwd)`** — stable composite
  key (`"claude_code@<abspath>"`). All session state (`_sessions_active`,
  `_session_names`, `_last_by_session`) is now keyed by this composite.
  Path is `resolve()`-normalised so `"D:\\Halo"` and `"D:/Halo/."` hash
  identically.
- `dispatch(...)`, `start_job(...)`, `session_name(...)`,
  `reset_session(...)`, `last_result_for(...)` all accept an optional
  `cwd` parameter (defaults to `DEFAULT_CWD` for back-compat with v1.1
  single-session callers).
- `AgentJob` gained a `cwd` field + `session_key` property so the
  dashboard and registry can distinguish jobs that target the same agent
  in different projects.
- `AgentBusy` is now per-(agent, cwd) — two projects can have parallel
  jobs for the same agent kind.
- `session_status()` keeps the v1.1 `{agent_key: bool}` shape (aggregated
  over all cwds for back-compat); new `session_status_detail()` exposes
  the full composite-keyed map for the orchestrator and dashboard.
- `halo/sessions.py` (persistent Claude subprocess pool) is now keyed
  by the composite session_key as well, so each project gets its own
  long-lived `claude -p --input-format stream-json` process. Log files
  / popup-console titles are labelled `<agent>-<basename>` so multiple
  watch windows stay distinguishable.

### Added (orchestrator wiring)
- **`halo/__main__.py`** spawns the `DiscoveryThread` on startup
  (silently no-ops when psutil isn't installed). Initial synchronous
  scan happens before the voice loop opens so the first wake-fire
  already sees other sessions.
- Module-level `REGISTRY = SessionRegistry()` singleton, with
  `_on_discovery_change` callback emitting a `discovery.changed` bus
  event so the dashboard sees fresh state.
- `_build_session_context()` snapshots the registry into a
  `SessionContext` for every Stage 2 call.
- `_cwd_for_dispatch(target_session)` resolves brain-emitted targets
  to concrete `Path`s, with fuzzy fallback when the brain emits a
  slightly-off label.
- `_handle_session_action()` intercepts `session_action` results
  (switch / list_sessions / where_am_i) BEFORE the normal dispatch
  branch, so meta-operations don't dispatch to an agent.
- `_dispatch_to_all()` fans the same prompt out to every discovered
  session when `target_session == "all"`.
- `_start_agent_and_ack` now takes a `cwd` kwarg and threads it
  through `start_job` so per-project session state is correct.
- Startup announcement extends to mention discovered count:
  *"Halo online. Claude is connected. 3 sessions discovered."*

### Added (CLI)
- **`halo sessions`** — read-only one-shot discovery. Prints
  `label / agent / pid / cwd` for every running coding-agent CLI.
  Exit 0 on any sessions found, 1 on none. Use to verify multi-session
  discovery works on your machine without booting the voice loop.

### Added (turn endpointing tweaks)
- **`turn.py:TERMINATORS_RE`** gained synonyms the user explicitly
  asked for: `start now`, `please go`, `let's go`, `let's do it`,
  `fire it off`, `ship it`, `run it`, `execute`. The default
  silence-commit (0.8–4 s, mode-adaptive) remains the primary turn-end
  signal — these are explicit "fire it now" overrides.
- **`__main__.py:_END_CONVERSATION_RE`** added `stand by` / `standby`
  as synonyms of `end conversation` — matches what the user asked for
  ("if I say stand by it will end and go back to wake-listening").

### Tests
- **`scripts/test_registry.py`** (14 cases) — empty registry, collision
  disambiguation, exact / case-insensitive / substring / token-overlap
  / pseudo-target matching, set_active behaviour, speak_list/active.
- **`scripts/test_discovery.py`** (9 cases) — cmdline classification
  (npm shims, Halo-spawned exclusion, unrelated processes), label
  derivation, live `scan_once()` smoke test that doesn't crash on
  PermissionError.
- **`scripts/test_agents_multicwd.py`** (7 cases) — session_key
  normalization, per-cwd naming, aggregated/detail status, reset by
  all / agent / (agent,cwd), `last_result_for` across cwds.
- **`scripts/test_brain_routing.py`** — Ollama-dependent integration
  test for the upgraded Stage 2 brain (switch / list / where_am_i /
  cross-session / fanout / no-context baseline). Gracefully skips
  when Ollama is unreachable.

### Dependencies
- **psutil>=5.9** added to `pyproject.toml` for process discovery.
  Soft-imported — Halo runs in single-session mode if psutil is absent.

### Migration notes
- Single-session usage (one `halo` in one project) is byte-identical
  to v1.1: discovery returns 0 sessions → no CURRENT CONTEXT block →
  brain emits empty new fields → orchestrator routes to `DEFAULT_CWD`
  as before.
- If you want the old behaviour even when other Claudes are running,
  uninstall psutil (`pip uninstall psutil`). Discovery silently
  disables; everything else works.
- The dashboard's `/api/state` shape is unchanged for v1.1 fields;
  new `discovery.changed` events are additive.

### Known limitations
- Phase 2 (auto-switch on focused-terminal change) and Phase 3
  (keystroke injection into terminals Halo didn't spawn) are NOT in
  this release. Phase 1 — brain-aware routing into Halo-managed
  per-project sessions — gets you 80% of the multi-session ask without
  the cross-terminal SendInput hairball. Land Phase 2/3 in v1.3.0
  after live use confirms the brain-routing model feels right.
- Discovery cmdline fingerprints are tuned for npm-installed
  Claude/Codex on Windows + POSIX. If you install Claude from an
  unusual path, add a fingerprint to `_AGENT_FINGERPRINTS` in
  `halo/discovery.py`.

---

## [1.1.1] — 2026-05-25

Single feature, single failure mode killed.

### Added (follow-up gate)

- **`halo/followup_gate.py`** — new module gating every direct-dialogue
  mode utterance before it reaches the agent. Background: once you say
  *"Claude, build fizzbuzz"* Halo enters direct mode and the mic stays
  hot so follow-ups don't need to re-state the agent name. Without a
  filter, anything the mic captured in the next 90s (phone calls, side
  conversations, your own muttering) would get transcribed and
  dispatched straight to Claude. The gate is a 4-rule keyword filter:
  - **Rule 1 — agent name mentioned.** Whole-word match against any
    agent's `voice_triggers` + `fuzzy_triggers` (so Whisper-mangled
    "Cloud" still counts). Most reliable signal.
  - **Rule 2 — continuation marker on short utterance (≤ 12 words).**
    Opens with `now` / `also` / `then` / `instead` / `and now` / `now
    also` / `actually` / `wait` / `scratch that` / etc. Catches the
    natural mid-session phrasings (*"now also add tests"*).
  - **Rule 3 — coding imperative + technical signal.** Verb from a
    65-word allowlist (`write` / `add` / `fix` / `refactor` / `delete`
    / `test` / `commit` / `deploy` / …) **AND** at least one anchor:
    coding noun (`file` / `function` / `bug` / `endpoint` / `branch` /
    `commit` / `prop` / `state` / …), named tech (`Python` / `React` /
    `TypeScript` / `Postgres` / …), or a continuation marker.
  - **Rule 4 — explicit side-conversation patterns + default DROP.**
    Phone openers (*"hello?"* / *"can you hear me"* / *"let me call you
    back"*), social chatter (*"grab lunch"* / *"see you tomorrow"*),
    and greetings to named third parties (*"hey John,"*) hard-drop
    early. Anything else that didn't match Rules 1-3 also drops.
- **`FOLLOWUP_GATE_ENABLED` config flag** (default `True`). Set to
  `False` for the old v1.1.0 behaviour where every direct-mode utterance
  reaches the agent.
- **`side_convo.ignored` bus event** emitted on every drop with
  `{text, reason, agent}`. Reason is one of `agent_name` /
  `continuation` / `coding_intent` (allow tags) or `empty` /
  `side_conversation` / `no_signal` (drop tags) for log/audit.
- **Dashboard surfacing** — new `k-side` event-log style (greyed
  italic, `(filtered)` suffix) so dropped utterances appear in the
  event log with their rejection reason and the transcript that got
  filtered. The route stage briefly flashes *"side-talk dropped"* when
  the gate rejects so the user can see it happening live.

### Changed
- **`halo/__main__.py`** direct-dialogue branch (line ~662): wraps the
  existing dispatch in `if not FOLLOWUP_GATE_ENABLED or
  followup_gate_passes(cleaned, direct_agent)`. On drop, prints
  `[side-talk dropped: <reason>] <text>` and emits the bus event;
  stays in direct mode but does NOT reset `last_activity` (so an
  ambient phone call doesn't keep the 90s engaged window alive
  indefinitely).
- README: new **Follow-up gate** section explaining the 4 rules with
  examples. v1.1 highlights opens with the gate entry. File-layout
  block lists `followup_gate.py`. Hero example shows the
  phone-call-during-session scenario being filtered.

### Rationale
The gate is intentionally regex-only — no Ollama round-trip, no model
load, ~0 ms latency. Rules 1-2 are nearly precision-perfect; Rule 3
is the recall workhorse. Rule 4 (default DROP) does the heavy lifting:
the cost of dropping a legitimate vague follow-up (*"make it bigger"*
with no clear noun → drops) is one retry; the cost of dispatching a
phone-call sentence to Claude is real damage. We chose safety.

### Known limitations
- A vague follow-up that lacks both a coding noun AND a continuation
  marker (*"make it bigger"*, *"do that again"*) will drop. Workaround:
  say *"Claude, make it bigger"* or rephrase with a noun (*"make the
  button bigger"*). Future work: optional 300 ms Stage 2 fallback for
  borderline Rule-3 cases.
- The gate sees only the cleaned transcript, not speaker identity.
  A second person in the room saying *"Claude, undo that"* would still
  pass Rule 1. Speaker fingerprinting (`resemblyzer` / `pyannote`) is
  the next layer if this becomes a real problem; not in this release.

---

## [1.1.0] — 2026-05-24

Big release — custom wake word, persistent Claude sessions, dashboard
upgrades, voice-mode permission flow, doctor command, noise suppression
pipeline, separate-console "watch Claude" popup. Everything below has
been live-tested.

### Added (custom "halo" wake word)
- **Trained a custom `halo.onnx` openWakeWord model** via
  `bbarrick/wakeword_trainer` on Python 3.12 (separate `.venv-train`
  outside the main runtime). Generated ~188 positive samples (8 phrase
  variants × 22 ElevenLabs voices: "Halo", "halo", "Hey low", "hey
  low", "ha low", "Ha-low", "halo halo", "halow") + 287 common-phrase
  negatives + 237 confusables ("hello", "hollow", "hallow", "Hawaii",
  etc.) at 4-6 voices each + 25 silence samples. Trained on RTX 3060
  with `drop_last=True` (workaround for bbarrick's last-batch shape
  bug). Final F1 0.80, FPR 0.094 — generalises well to new voices/
  accents in live testing.
- **`halo/wake.py:_resolve_model()`** loads `models/halo.onnx` if
  present, falls back to openWakeWord-builtin `hey_jarvis` if missing
  (so a fresh checkout still boots).
- **`WAKE_VAD_THRESHOLD = 0.7`** — silero-VAD gate on top of the wake
  model via openWakeWord's `vad_threshold` arg. Wake fire requires
  both the DNN AND silero to agree there's speech. Dropped false-
  positive rate to near zero on noisy mics.
- **`THRESHOLD = 0.75`** (was 0.5) — tuned after live testing showed
  the 188-sample model occasionally finding "halo" patterns in pure
  noise at 0.65+.

### Added (CLI popup for "watch Claude work")
- **`halo/sessions.py:_spawn_monitor_console`** — when
  `AGENT_CLI_VISIBLE = True` (default), a SEPARATE PowerShell window
  pops up per session, live-tailing `~/.halo/runtime/<agent>.log`. The
  reader thread writes a per-event trace (text deltas, tool calls,
  result events) to the log; the popup renders it via
  `Get-Content -Wait`. Two windows = clean separation of Halo's
  terminal vs Claude's activity.
- **`AGENT_CLI_VISIBLE` config flag** + per-agent override on
  `AgentConfig.cli_visible`. `True` / `False` / `None` (= follow
  global).
- **Text-delta buffering** (`ClaudeSession._delta_buf` +
  `_flush_delta_buf`) — accumulates incoming chunks until a sentence
  terminator or 200-char limit, then flushes. Pre-buffering the popup
  rendered choppy single-char-per-line output; whole phrases now.
- **`_redact_argv()`** — strips the full `VOICE_SYSTEM_PROMPT` from
  the argv display in the popup header (was a 3 KB wall of text).

### Added (noise suppression pipeline)
- **`MIC_NOISE_GATE_RMS = 0.005`** in `halo/config.py`. Audio chunks
  below this RMS floor are dropped before silero-VAD sees them.
  Tunable per environment.
- **`vad_filter=True`** on faster-whisper transcribe — Whisper's
  internal silero pass strips non-speech segments BEFORE transcription.
  Massively reduces the *"Thank you."* / *"Thanks for watching."*
  hallucinations that distil-large-v3 invents on near-silence.
- **`_HALLUCINATION_PHRASES`** in `halo/stt.py` — silent post-decode
  filter for the residual YouTube-caption artifacts that survive
  vad_filter: *"Thank you"*, *"you"*, *"hello"*, *"hi"*, *"halo"*,
  *"thanks for watching"*, etc. Returns empty string instead of
  dispatching the artifact to Claude.

### Added (orchestrator robustness)
- **`engaged` flag in `run_conversation`** — once you've spoken at
  least one command this conversation, the idle timeout jumps from 5s
  to `CONVERSATION_IDLE_ENGAGED_SEC = 90.0`. Stops the "spoke once,
  Halo went to sleep" frustration.
- **Mic-mute during TTS** (`record.py:_callback` checks
  `voice.is_speaking()`) — drops mic chunks while Halo is talking.
  Eliminates the feedback loop where Halo transcribes its own TTS
  output and dispatches it back to Claude.
- **`voice.is_speaking()` / `voice.wait_until_silent()`** API +
  thread-safe counter (`_active_say_count`). Used by the orchestrator
  and the mic-mute guard.
- **`_fuzzy_agent_match()` Stage 2 fallback** — when the router LLM
  is unreachable AND the user's utterance contains an agent name or a
  Whisper mis-transcription of one (`AgentConfig.fuzzy_triggers`:
  *"cloud"/"claud"/"clawed"* → Claude, *"codec"/"kodex"* → Codex),
  dispatch directly instead of speaking *"Sorry, the router brain
  didn't respond."*. Skips dispatch if the matched agent isn't
  connected.
- **Verbal dispatch** (`_VERBAL_DISPATCH_RE`) — *"ask Codex to X"* /
  *"tell Claude to Y"* / *"have Codex deploy"* bypasses Stage 2 just
  like vocative dispatch does for *"Codex, X"*. Same regex priority
  3b in the routing list.
- **Transfer phrases** in `_TALK_TO_AGENT_RE`: *"transfer me to
  codex"* / *"transfer to claude"* / *"transfer back to halo"*.
- **System-intent fall-through** in `_handle_decision`: when Stage 2
  routes to `intent=system` but no local tool matches, dispatch to
  Claude instead of refusing. Catches mixed-intent prompts like
  *"open hallo.html and change the colors"*.
- **`open <filename>` tool** in `halo/tools.py` — *"open
  landing.html"*, *"open my notes.md"* opens the file in the OS
  default app. Cross-platform (Win/Mac/Linux).
- **`halo doctor` CLI** — read-only diagnostic. Checks Ollama, agent
  binaries + auth, Kokoro model files. Prints `[ok]/[warn]/[fail]`
  per dependency with the exact install command for any miss.
- **Agent availability check** (`halo.agents.check_availability`) —
  cached per Halo process. Used by doctor, startup announcement,
  dashboard agent panel, dispatch pre-flight.
- **Startup announce** — *"Halo online. Claude and Codex are
  connected."* (or whichever agents responded to `--version`).
- **Dispatch pre-flight** refuses to dispatch to a disconnected
  agent and speaks *"Codex isn't connected. Run halo doctor for
  setup help."* instead of letting the subprocess fail with a
  confusing *"had a problem"*.
- **Voice prompt rules 7-9** in `VOICE_SYSTEM_PROMPT`: agents now
  speak time estimates for slow tasks, name files clearly when
  finishing, and treat bare-filename utterances as `Read` not `Write`.

### Added (dashboard)
- **Header agents bar** — colored chips per agent: green = ready,
  cyan = running, yellow = installed but unresponsive (auth?), red
  = not installed. Hover for install-hint tooltip.
- **Recent files panel** — right column. Files in launch-cwd
  modified since session start. Click to open in OS default app.
- **Reset sessions / Clear log buttons** in the event-log header.
- New API endpoints: `GET /api/recent-files`, `POST /api/open-file`
  (path-traversal protected), `POST /api/control/reset-sessions`.
- Browser auto-opens to dashboard on startup (set `HALO_NO_BROWSER=1`
  to suppress).
- Bumped dashboard label `v0.6 → v1.0 → v1.1`.

### Added (end-conversation phrases)
- *"end session"* / *"end of session"* / *"session end"* / *"that's
  enough"* / *"that's it"* (plus all the old ones) close the
  conversation explicitly.

### Changed
- **`RECORDINGS_DIR`** follows the same precedence as `MODELS_DIR`:
  `$HALO_RECORDINGS_DIR` → `./recordings/` (dev) → `~/.halo/recordings/`
  (installed).
- **Mic RMS print floor** bumped from 0.01 → 0.05 so only actual
  speech-level audio triggers the `mic level:` diagnostic line. Cut
  terminal noise dramatically.
- **`audio status: input overflow`** prints throttled to once per 5s
  (`_AUDIO_STATUS_THROTTLE_SEC`).
- **Polling-mode quiet** in `run_turn` — when `first_wait < 3.0` (the
  orchestrator's polling cadence while an agent job runs), suppress
  the per-iteration `[speak now]` and `no speech detected` lines.
- **Stage 2 prompt** in `halo/router.py`: removed the *"Building a
  login page with Supabase"* positive example (the trigger for the
  "Build me X with Supabase" hallucination), added explicit
  `AGENT-NAME PRESERVATION` rule + a Codex-named example.
- Streaming agents no longer get the *"Saturn is done."* TTS cap
  after their streamed response (was redundant — the streamed text
  was the response).

### Added (persistent Claude CLI session)
- **`halo/sessions.py` + `ClaudeSession`** — one long-lived `claude` process
  per Halo session, kept open across every voice turn. Spawned with
  `--input-format stream-json --output-format stream-json --verbose
  --include-partial-messages --permission-mode bypassPermissions
  --append-system-prompt <VOICE_SYSTEM_PROMPT>`. Each turn is a single
  newline-delimited JSON envelope written to stdin; the same
  `_SentenceBuffer` + `_extract_text_delta` helpers parse text deltas
  off stdout for live TTS, and the terminal `result` event becomes the
  return value of `send()`.
- `AgentConfig.session_kind` (`"one-shot" | "persistent"`) and
  `persistent_call` template. Claude is `persistent`; Codex stays
  `one-shot` (no `--input-format stream-json` flag in Codex CLI).
- `agent.session_spawned` / `agent.respawn` / `agent.session_closed`
  bus events for the dashboard.
- Process-exit cleanup via `atexit.register(close_all)` so persistent
  subprocesses don't outlive the Halo process.

### Changed (no more per-turn Claude spawns)
- **`agents.dispatch()` now branches on `session_kind`.** Persistent
  agents go through `_dispatch_persistent` → `ClaudeSession.send()`;
  one-shot agents keep the existing `_run()` path verbatim. This kills
  the `exit None` race where a follow-up dispatch arriving while the
  previous Claude was still streaming would either pick the wrong
  `--continue` target or close the pipe mid-flight.
- `reset_session()` now also calls `sessions.close(key)` for persistent
  agents so "new task" / "start over" tears down the running subprocess
  before the next dispatch spawns a fresh one.
- Terminal log on dispatch now distinguishes `(persistent session,
  starting)` vs `(persistent session, continuing)` vs the existing
  `(starting/continuing session)` for one-shot agents.

### Fixed
- Eliminated `Claude failed: exit None` failures caused by per-turn
  subprocess churn racing the streaming reader against the next
  dispatch's pipe open.
- "starting session" no longer prints twice when the user expected
  "continuing session" — there's now exactly one process per session,
  so the state can't drift.

### Known limitations
- If the installed `claude` CLI is too old to accept
  `--input-format stream-json`, the spawn-probe (0.5s after launch)
  detects the immediate exit, logs the stderr tail, marks the agent
  as `_persistent_disabled`, and falls back to one-shot dispatch for
  the rest of the Halo process. Restart Halo after upgrading the CLI
  to get back on the persistent path.
- Respawn-on-dead-process happens at most once per `send()` call. If
  the second spawn also dies, the dispatch returns an error and the
  user is told Claude had a problem; the next turn will retry from
  scratch.

### Changed (voice-mode permission flow — the user can't manually approve)
- **Claude Code now runs with `--permission-mode bypassPermissions`.**
  `acceptEdits` (the previous mode) still pops a TUI confirmation on every
  Bash call — running tests, git ops, package installs — and Halo has no
  way to click yes. Confirmed via `claude --help` that `bypassPermissions`
  is the documented "skip all permission checks" mode.
- **Codex CLI now runs with `-c approval_policy="never"`.** Verified against
  OpenAI's official config reference: *"`never` for non-interactive runs."*
  `--sandbox workspace-write` is kept, so Codex's file writes stay
  constrained to the project root and network is still blocked; only the
  approval-prompt step is bypassed.
- Per-agent `sandbox_label` updated to reflect the new mode (Claude:
  `bypassPermissions`).

### Changed (router hallucination fix)
- **Removed the `Building a login page with Supabase` example from
  `router.py:STAGE2_PROMPT`** — both the positive ("Good:") example in
  the CONFIRMATION RULES section and the matching example in the
  EXAMPLES section. The model was copy-pasting "Supabase" into
  completely unrelated prompts (observed: user said "ask Codex to build
  a landing page", router output `"agent": "claude_code", "cleaned_text":
  "Build me a landing page with Claude Code using Supabase"`).
- Added explicit `# AGENT-NAME PRESERVATION (CRITICAL)` section forbidding
  Claude↔Codex substitution. The Codex-named example *"ask Codex to build
  a landing page for this project"* is now in the EXAMPLES list so the
  model has a concrete pattern to follow.
- Added `# CRITICAL CONSTRAINTS` rule: "NEVER swap the agent the user
  named" and "never invent libraries, frameworks, databases".

### Added (dashboard upgrades)
- **Header "agents" bar** — `● CLAUDE  ● CODEX` colored chips, always
  visible. Green = ready, cyan = running, yellow = installed but
  unresponsive (needs login), red = not installed. Hover for the
  install-hint tooltip.
- **Recent files panel** — right-column, below sessions. Lists files in
  the launch-cwd modified since this Halo session started. Click any row
  to open it in the OS default app — directly addresses the "show me
  what you built" flow.
- **Reset sessions / Clear log** control buttons in the event-log
  header. `reset sessions` calls a new `POST /api/control/reset-sessions`
  endpoint; `clear log` is frontend-only.
- New API endpoints: `GET /api/recent-files`, `POST /api/open-file`
  (path-traversal protected — refuses paths outside the launch-cwd),
  `POST /api/control/reset-sessions`.
- Bumped dashboard version label `v0.6 → v1.0`.

### Changed
- `RECORDINGS_DIR` now follows the same precedence as `MODELS_DIR`:
  `$HALO_RECORDINGS_DIR` → `./recordings/` (dev) → `~/.halo/recordings/`
  (installed). Pip users won't end up writing debug WAVs into
  site-packages.

### Known limitations
- **No mid-speech barge-in.** If you want to interrupt Halo while it's
  talking back, you currently have to wait for the TTS queue to drain.
  Always-on VAD during TTS is non-trivial and lands in a future round.
- **Stage 2 LLM still hallucinates occasionally.** The prompt is now
  much tighter, but the 1.5B router model can still drift. Use vocative
  or verbal dispatch (`"Codex, X"` / `"ask Codex to X"`) to skip Stage 2
  entirely — those are pure regex matches and 100% reliable.

### Added
- **`halo doctor`** — read-only diagnostic. Checks Python version, Ollama
  reachable + routing model pulled, each registered agent's binary +
  responsiveness (`--version` probe), and Kokoro model files. Prints
  `[ok]/[warn]/[fail]` per dependency with the exact install command for
  any miss. Exits 0 if all required checks pass.
- **Agent availability check** (`halo.agents.check_availability`) — used
  by the doctor, the startup announcement, the dashboard agent panel,
  and the dispatch pre-flight. Cached for the Halo process lifetime so
  the dashboard's 750 ms `/api/state` poll doesn't re-fork `--version`.
- **Startup announce** — Halo now speaks *"Halo online. Claude and Codex
  are connected."* (or the actual subset that responded). If nothing
  responds: *"No coding agents connected. Run halo doctor."*
- **Dispatch pre-flight** — `_start_agent_and_ack` refuses to spawn an
  unavailable agent and speaks *"Codex isn't connected. Run halo doctor
  for setup help."* instead of letting the subprocess fail mid-flight
  with a confusing "had a problem" message.
- **`open <filename>` tool** — say *"open landing.html"* / *"launch
  hello.py"* / *"open my notes.md"* and Halo opens the file in your OS
  default app (Windows `os.startfile`, macOS `open`, Linux `xdg-open`).
  Resolves relative to the launch cwd. Lets you act on whatever an agent
  just built without leaving voice.
- **"transfer me to X"** mode-switch phrase + **"transfer me back to
  halo"** exit phrase. Synonyms of the existing `switch to` / `back to
  halo`. Natural English the user might say first.
- **Voice prompt rules 7 + 8** in `VOICE_SYSTEM_PROMPT`:
  - For slow tasks (>~10 s), the agent must say what it'll do and a
    rough time estimate up front, so the user isn't left in silence.
  - When the agent finishes something openable (HTML, image, doc), it
    must state the filename clearly so the user can say "open <file>".

### Changed
- **Direct dialogue never auto-sleeps.** While `direct_agent is not None`
  (you're in a session with Claude or Codex), the 5 s idle timer no
  longer triggers a sleep. The conversation stays open until an explicit
  end phrase (`"goodbye"`, `"go to sleep"`, `"over and out"`, etc.) or
  Ctrl-C. Plain Halo mode (no direct dialogue, no jobs) keeps the
  existing 5 s idle sleep.
- **`tools.py` `_OPEN` made non-capturing** so the new file-open regex's
  `.group(1)` is the filename, not the verb. No behaviour change to the
  existing tool patterns — they only `.search()`, never `.group()`.
- Dashboard agent panel now shows a colored connection dot per agent:
  green = ready, cyan = running, yellow = installed but unresponsive
  (likely auth), red = not installed. Install hint rendered below the
  row when disconnected.

### Roadmap (rolled forward)
- Publish to PyPI (currently install via `pip install git+https://github.com/VW3st/halo.git`).
- Custom `hey_halo` wake model (needs voice samples + openWakeWord training notebook).
- Premium TTS provider abstraction (ElevenLabs, etc.) behind an opt-in flag.
- External `agents.toml` so adding agents doesn't require Python edits.
- `halo project add / use <name>` for multi-project flows.
- GitHub Actions CI running `scripts/test_*.py`.

---

## [1.0.0] — 2026-05-24

### Added
- **Packaged as `halo-voice`** — `pip install git+https://github.com/VW3st/halo.git`
  installs a real `halo` shell command, no more `python -m halo` needed.
- **`pyproject.toml`** (hatchling backend) — proper PEP 621 metadata,
  console-script entry point, `[gpu-windows]` and `[moonshine]` optional extras.
- **`halo/cli.py`** — argparse-driven CLI with subcommands:
  `halo run` (default), `halo download-models`, `halo version`.
- **`halo download-models`** — fetches Kokoro fp16 ONNX + voices bin
  (~200 MB) into `~/.halo/models/` so pip users don't have to curl
  release artifacts manually.
- **`HALO_MODELS_DIR` env var** — override the model location.
  `config.py:_resolve_models_dir()` prefers the override, then a local
  `./models/` (dev checkout), then `~/.halo/models/` (installed).
- `halo/__init__.py` now exports `__version__`.

### Changed
- `requirements.txt` removed — dependencies live in `pyproject.toml`.
  Dev install: `pip install -e .[gpu-windows,dev]`.

---

## [0.6.0] — 2026-05-24

### Added
- **Local web dashboard** at `http://127.0.0.1:7070`, auto-launched on
  Halo startup. Single-file HTML (no build step). Dark slate UI, mono
  typography, cyan accent.
  - Live **pipeline visualization** (wake → record → transcribe →
    route → agent → voice) with stages lighting up as they fire.
  - Top-of-page **mode pill** (LOCAL / DIRECT · MERCURY).
  - **Big live transcript** of what you just said + a "now speaking"
    indicator showing who's talking.
  - **Agents panel** listing each session by Roman name with state
    (running / ready / idle), elapsed time, and current prompt.
  - Color-coded **event log** streaming every wake / stt / route /
    agent / tts event with timestamps.
- **`halo/bus.py`** — thread-safe ring-buffer event bus. `emit(kind, **data)`
  from anywhere; web layer polls `events_since(seq)`.
- **`halo/web.py`** — Flask app on a daemon thread. `/api/events?since=N`
  for polling, `/api/state` for snapshot.
- `flask>=3.0` added to requirements.

### Changed
- `halo/__main__.py`, `halo/wake.py`, `halo/turn.py`, `halo/agents.py`,
  `halo/voice.py` all emit bus events at key transitions.
- Werkzeug logger silenced (no per-poll log spam in the terminal).

---

## [0.5.0] — 2026-05-24

### Added
- **Live streaming narration from Claude Code.** Switched the Claude dispatcher
  to `--output-format stream-json --verbose --include-partial-messages`. New
  `_SentenceBuffer` breaks `text_delta` events into TTS-able sentences. The
  orchestrator passes an `on_text_chunk` callback that speaks each sentence
  through Kokoro as Claude generates it — no more dead air on long tasks.
- `AgentConfig.streams_text_deltas` flag — opt-in per agent. Claude on, Codex
  still batch (Codex CLI doesn't expose a stream-json mode).
- `_drain_completed_jobs` now speaks a short "X is done." cap when the agent
  was streaming (the response itself was already narrated).
- `CHANGELOG.md` (this file).
- `LICENSE` (MIT).

### Changed
- `dispatch()` and `start_job()` accept an `on_text_chunk` keyword.
- The completion announcement for streaming agents is just a 1-phrase cap
  instead of repeating the full summary.

### Tests
- `scripts/test_streaming.py` — sentence buffer + event extractor.
- End-to-end verified: a 3-sentence Claude response streamed back as 3 spoken
  chunks in ~9 seconds.

---

## [0.4.0] — 2026-05-23

### Added
- **Vocative dispatch.** `"Claude, build X"` / `"Codex, refactor Y"` bypasses
  the Stage 2 router LLM entirely, saving ~3–5s per turn.
- **Routing priority** in the conversation loop. Stage 2 LLM demoted to last
  resort — fires only when no local handler matched AND we're not in direct
  mode. Priority: end-phrase → new-session → vocative → pure mode-switch →
  back-to-halo → status/replay → tool fast-path → direct-dialogue → Stage 2.
- **Direct-dialogue mode** with mode switches (`"switch to codex"`,
  `"back to halo"`).
- **Roman mythology session names.** Each agent session gets a random name
  (Mars, Mercury, Juno, …) so TTS can disambiguate ("Mercury says…").
- **Async agent jobs.** `start_job()` runs Claude/Codex in a background
  thread; the conversation keeps accepting tool commands, status queries,
  and parallel dispatches to other agents. `_drain_completed_jobs()` between
  turns speaks results when they land.
- **Status / replay queries.** `"what's happening?"` / `"are you done?"` read
  the registry; `"what did Claude say?"` replays the last result.
- **Persistent sessions across wake cycles.** Claude/Codex `--continue` threads
  outlive a single conversation; only reset on explicit `"new task"` /
  `"start over"` / `"fresh session"`.
- **Local tool dispatch** (cross-platform): `open browser`, `open calculator`,
  `open notepad`, `open file explorer`, `open terminal`. Multi-tool split on
  "and"/"then"/comma. Win/Mac/Linux launchers.
- **Kokoro-82M TTS** (`af_heart` voice, fp16 ONNX). Markdown sanitizer scrubs
  `**bold**`, backticks, em-dashes, bullet lists, code fences, mojibake, and
  long paths before TTS.
- **Voice-mode system prompt** injected into Claude (via
  `--append-system-prompt`) and prepended to Codex's prompt — tells the agent
  it's on a voice channel and to keep responses short with no Markdown.
- **Conversation mode.** Stay awake after wake until an explicit end-phrase
  (`"over and out"`, `"goodbye"`, `"go to sleep"`) or 5s of true idle with no
  active jobs. Idle timer extends while a job is mid-flight.
- **Backchannel tone.** A soft 200 Hz tone after 3s of silence in "thinking"
  or "composing" mode, so slow talkers know Halo is still listening.

### Changed
- `claude.exe -p` calls now use `stdin=DEVNULL` to work around the Windows
  TTY-hang bug (anthropics/claude-code#9026).
- `keep_alive` for Ollama bumped to 24h — earlier 30m saw mid-session
  cold-reloads burning 5–11s.

### Fixed
- Pre-wake audio ring buffer (`halo/wake.py`) had a `global` declaration on
  the wrong function; assignments inside the audio callback silently created
  a local variable so "Hey Jarvis open calculator" said in one breath lost
  the command. Moved `global` into the callback.
- Mode-switch regex was swallowing prompts like
  `"ask Claude Code to build a landing page"` — tightened to match only
  *pure* switches with no instruction after the agent name.
- Empty-prompt guard so `"Hey Jarvis"` (which strips to `""`) doesn't
  dispatch to Claude and trigger
  `Error: Input must be provided either through stdin or as a prompt argument`.
- `_drain_stderr` no longer crashes on
  `ValueError: I/O operation on closed file` when the subprocess closes
  stderr mid-iterate.
- `"what's happening?"` no longer interrupts conversational flow with
  "Everything is idle" — status query only intercepts when there's actually
  a job running or a recent completed result.
- Wake threshold 0.62 → 0.5 (was making the user shout three times).
- Voice prompt rewritten with hard "NO MARKDOWN. ZERO." plus good/bad
  examples; sanitizer remains the safety net.

---

## [0.3.0] — 2026-05-22

### Changed (major)
- **STT engine swap: Moonshine → faster-whisper + distil-large-v3.**
  Moonshine SMALL/MEDIUM was mistranscribing "Chrome" as "Cut"/"card"/"crack"
  on the user's voice. faster-whisper int8_float16 on CUDA fixes both
  accuracy (~half the WER) and gives clean transcripts for short command
  words. Lost streaming partials, gained ~6× accuracy on short commands.
- `halo/stt.py` adds NVIDIA cuBLAS/cuDNN wheels to PATH at import so CUDA
  loads on Windows. Auto-falls back to CPU int8 if CUDA is unavailable.

### Added
- Faster-whisper warm-up call at startup so the first turn doesn't pay the
  ~5–7s CUDA-kernel cold start.
- Stage 1 reduced to pure rules (trailing connectors/fillers → INCOMPLETE).
  LLM Stage 1 was costing 2–4s per call because Ollama's KV cache flipped
  every time we alternated Stage 1 and Stage 2 prefixes.
- Tool fast-path expanded with synonyms: `web`/`internet` → browser,
  `calc`/`math` → calculator, `console`/`cmd` → terminal.
- `"Hey Jarvis"` (and `hi`/`hello`/`yo`/`halo`/`hi jarvis` variants)
  stripped from the front of every transcript before any routing.

### Fixed
- Audio buffer overflow during chime — open the input stream first, then
  play the chime.

---

## [0.2.0] — 2026-05-22

### Added
- **Routing brain (Stage 1 + Stage 2)** via Ollama + `qwen2.5:1.5b-instruct`.
  Stage 1 = turn-completion classifier (later replaced by pure rules).
  Stage 2 = JSON-schema-constrained understanding + routing.
- Pre-wake audio ring buffer so commands said in the same breath as the
  wake word ("Hey Jarvis open calculator") aren't lost.
- Adaptive turn-taking (`detect_mode`) — silence threshold scales with
  transcript shape: 0.8s snappy / 2.5s thinking / 4.0s composing.
- Hold trigger ("wait", "give me a sec") suppresses the turn-complete
  check for 10s.
- Backchannel tone after 3s of silence in thinking/composing mode.
- `scripts/bench_router.py`, `scripts/test_detect_mode.py`.

### Changed
- Stage 2 prompt rewritten for stricter "NEVER invent app names" /
  "no markdown in confirmation".

---

## [0.1.0] — 2026-05-22

Initial scaffolding.

### Added
- `halo/wake.py` — openWakeWord listener (`hey_jarvis` placeholder).
- `halo/record.py` — sounddevice mic + silero-vad silence detection + chime.
- `halo/stt.py` — whisper.cpp subprocess wrapper (replaced in 0.3 by
  faster-whisper).
- `halo/__main__.py` — basic orchestrator.
- `requirements.txt`, `README.md`.

### Models
- whisper.cpp prebuilt Windows binaries + distil-large-v3 GGML model
  (later retired in 0.3).
- silero-vad v5 (~2 MB).
- openWakeWord pretrained models.
