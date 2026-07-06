# Halo — Architecture & Conceptual Model

> Status: written 2026-06-18 from a full read of the codebase (~13k lines, 24 modules).
> Purpose: one shared map of how Halo actually works today, the conceptual model
> behind it, and the honest gaps — so the refactor and the new features land
> cleanly instead of bolting onto a system nobody has a picture of.
>
> This is a **map of the present**, not a wish list. The plan lives in `ROADMAP.md`.
>
> **Delta — 2026-07-06 (v1.7.0):** since this map was written, the roadmap's
> phases 0–2 and 4–7 landed. New modules on top of the pipeline below:
> `floor.py` (conversational floor / promise-reclaim), `utterance.py`
> (mic-grounded command/filler/noise/background classifier), `skills.py`,
> `hotkey.py` (global dictation hotkey), `mcp_client.py` (brain-side MCP
> tools), `prompts.py` (user prompt overrides). The audio path gained: gap
> capture between turns (`record.GapCapture`), a commit-race guard in
> `turn.run_turn`, one shared VAD threshold (`config.VAD_THRESHOLD`) used by
> BOTH the recorder and Whisper's internal filter, `large-v3-turbo` STT, and a
> confidence-gated LLM transcript repair (`router.repair_transcript`). The
> Phase 3 structural refactor is still pending — this map remains accurate for
> the module boundaries it describes.

---

## 0. One-paragraph mental model

Halo is a **Windows-first voice front-end** that sits in front of agentic coding
CLIs (Claude Code, Codex). You say a wake word; Halo listens, decides whether
what you said is chit-chat, a local action, or real work; chats back itself for
small things, runs local tools (open apps, dictate), or dispatches the work to a
coding agent and narrates the result. It runs **100% local and free by default**
(local wake word, local speech-to-text, local brain, local text-to-speech) and
can opt into paid cloud upgrades (ElevenLabs voice, OpenRouter brain). The whole
thing is one Python process with a Flask dashboard on `:7070`.

```
openWakeWord ──▶ mic (sounddevice) ──▶ silero-VAD ──▶ faster-whisper ──▶ ROUTER BRAIN
  (wake.py)        (record.py)         (record.py)      (stt.py)         (router.py)
                                                                              │
                         ┌────────────────────────────────────────────────────┤
                         ▼                        ▼                            ▼
                   local tools             chat back (brain)            coding agent
                   (tools.py)              (router chat_reply)      (agents.py/sessions.py)
                         │                        │                            │
                         └──────────────▶ TTS (voice.py) ◀──────────────────────┘
                            Kokoro (local) or ElevenLabs (cloud)
```

The orchestrator that wires all of this together is `__main__.py` —
specifically the 775-line `run_conversation()` loop.

---

## 1. Who is the user? — identity & configuration

**Config is two layers** (`userconfig.py` is the source of truth; `config.py` is a
thin compatibility shim re-exporting hot constants):

- `userconfig.py` defines typed dataclass sections — `WakeConfig`, `VoiceConfig`,
  `RouterConfig`, `ConversationConfig`, `MicConfig`, `AgentsConfig`,
  `PersonaConfig`, `PathsConfig`, `DictationConfig`, `McpConfig`, `MemoryConfig` —
  composed into one `HaloConfig`, built once as the singleton `cfg = load_config()`.
- `config.py` re-exports values like `OLLAMA_MODEL`, `ROUTER_BACKEND`,
  `CONVERSATION_IDLE_SEC` so legacy `from halo.config import X` still works.

**Precedence (highest wins)** — `load_config()` `userconfig.py:447`:
1. **Env vars** (curated subset via `_ENV_OVERRIDES`)
2. **Per-machine profile** `[profiles.<hostname>.<section>]` — from user *and* project TOML
3. **Project** `./halo-config.toml`
4. **User** `~/.halo/config.toml`
5. **Dataclass defaults**

`.env` (cwd) loads first and only fills keys not already in the environment.

**Identity** lives in `PersonaConfig` `userconfig.py:154`: `user_name`,
`user_role`, `halo_character`. It is injected two places — into agents via
`_system_prompt_for_session` (`agents.py:588`, fills `{USER_NAME}/{USER_ROLE}/
{HALO_CHARACTER}`) and into the brain's chat replies via `_chat_base_prompt`
(`router.py:1206`). Overridable with `HALO_USER_NAME` / `HALO_USER_ROLE` /
`HALO_CHARACTER`.

**Per-machine "host profiles" are the spine of the "adapts to the machine" goal.**
`halo calibrate` measures this PC's mic + your voice and writes a
`[profiles.<host>.wake]` block into `~/.halo/config.toml` (`write_host_profile`
`userconfig.py:417`). One synced config can then carry a different wake threshold
per machine.

> **Security note:** `/halo-config.toml` and `.env` ARE gitignored
> (`.gitignore:72` and `:79`) and untracked — verified. Keep it that way; a
> refactor must never remove those lines.

---

## 2. The brain (routing) — and where MCP isn't

The "brain" is a **small structured-output model**, not an agent. Two stages:

**Stage 1 — `check_turn_complete()` `router.py:765`:** decides "have you finished
talking?" It is now **pure rules, ~0 ms** (the old LLM version cost 2–4 s from
KV-cache thrash and was ripped out). Trailing conjunction → incomplete; short
word / terminal punctuation → complete; **default complete**. *(The module
docstring still describes an LLM Stage 1 — it lies; see tech-debt.)*

**Stage 2 — `understand_and_route()` `router.py:911`:** the real classifier.
Returns a decision dict — `status` (ready/unclear/chitchat/cancel), `cleaned_text`,
`intent` (code/question/system/cancel), `agent` (claude_code/codex_cli/none), plus
`confirmation`/`clarification` and session fields. Two prompt/schema variants: a
**lean 4-field** path for the common single-session case (fast), and a full
8-field path when multiple sessions exist. **Fail-open:** any error returns an
`unclear` fallback so the loop never crashes.

**Providers:** `ollama` (local, default) or `openrouter` (cloud), dispatched in
`_chat_json` `router.py:690`. There is **no Anthropic/Claude brain backend** —
"claude_code"/"codex_cli" are dispatch *targets*, not brain backends.

**MCP:** **the brain has no MCP and no tool-calling at all.** It emits constrained
JSON, full stop. MCP exists only as a flag that adds `--mcp-config` to the
*spawned Claude subprocess* (`agents.py:496`), giving Claude Windows desktop
control. Your goal of "standard MCP directly to the brain" is **not implemented**
— it would require adding an MCP client + tool-use loop to `router.py`.

---

## 3. Agents & sessions — the two-worlds problem

There are **two separate "session" worlds** that barely talk to each other. This
is the single biggest structural issue in the codebase.

- **World A — agents Halo spawns itself** (`agents.py` + `sessions.py`): Halo forks
  Claude/Codex, streams the reply to TTS, tracks them keyed by `agent@cwd`.
- **World B — agents already running on the machine** (`discovery.py` +
  `registry.py`): a psutil scanner finds *other* terminals running `claude`/`codex`
  and lets you voice-route to them by spoken **label** (basename of cwd).

They use the word "session" for different things, keep **three independent
registries under three independent locks** (`_sessions_active` in agents.py keyed
`agent@cwd`; `_sessions` in sessions.py; `_sessions` in registry.py keyed by
label), and share **no common identifier**. Nothing reconciles "I discovered a
Claude in `D:\proj`" with "I spawned my own Claude in `D:\proj`."

**Agent types:**
- **Claude = persistent.** One long-lived `claude -p --input-format stream-json …`
  process per session (`sessions.py:116`); each turn is a JSON envelope on stdin,
  replies stream back as events. The *process is the session*.
- **Codex = one-shot.** Fresh `codex exec` (or `resume --last`) per turn — Codex
  has no stream-json stdin mode, so it can't be persistent.

**Session lifecycle (what your phrases map to):**
| You say | What happens | Where |
|---|---|---|
| new session | drop continuation, kill persistent proc, rotate name | `reset_session` `agents.py:420` |
| (direct chat) | reuse the live process / `continue_call` | `dispatch` `agents.py:1009` |
| keep it open | process stays alive the whole Halo run | `sessions.py` |
| switch to X | select which *discovered* session routes (World B only) | `set_active` `registry.py:173` |
| back to Halo | **not in this layer** — the brain just stops dispatching | `__main__`/routing |

**Voice floor:** only one job narrates live at a time (`_voice_floor_job_id`
`agents.py:1130`); the most recent dispatch wins the mic. Other jobs keep running
and feed the dashboard + an end-of-job summary. Jobs are keyed per `(agent, cwd)`,
so two *projects* run in parallel but one project can't double-dispatch.

**Skills hook-in (your "make a poster for AIP" ask):** enumeration belongs next to
`check_availability()` (`agents.py:302`) as a cached `list_skills(agent_key)`
probe; per-agent skill metadata on the `AgentConfig` dataclass; routing a named
skill is a prompt prefix injected where the system prompt is assembled
(`agents.py:986`); spoken skill→name matching reuses `registry.resolve()`'s fuzzy
matcher (`registry.py:187`). None of this exists yet.

---

## 4. Voice & turn-taking — and the "give me 10 seconds" gap

The audio path (all 16 kHz int16 mono): **wake** (`wake.py`, openWakeWord + a
1 s pre-wake ring buffer so "Halo open calculator" in one breath isn't lost) →
**mic + VAD** (`record.py`, a realtime callback enqueues, a worker runs silero-VAD
*and* an RMS-energy fallback for quiet mics) → **STT** (`stt.py`, faster-whisper
`distil-large-v3` with gain-normalization, a confidence gate, and a hallucination
blocklist for "thank you"/"subscribe" artifacts) → **turn orchestration**
(`turn.py`) → **TTS** (`voice.py`, Kokoro local or ElevenLabs cloud, non-blocking).

**Turn-taking (`run_turn` `turn.py:359`)** is **adaptive but strictly half-duplex
and user-led.** `detect_mode` classifies the transcript (snappy 0.9 s / thinking
3.2 s / composing 4.5 s of silence-to-commit); terminators ("go", "do it") commit
instantly; `HOLD_RE` ("give me a sec") makes Halo *wait longer*; "never lose the
mic" extends a still-talking turn up to 40 s. The v1.6.0 work added a layered
**barge-in** path so you can interrupt Halo while it speaks.

**The "10-second" problem is not modeled at all.** Every mechanism assumes *you*
hold the floor and Halo only waits for you to stop. Specifically:
- `HOLD_RE` is **backwards** for this — it handles *you* asking to wait, not Halo
  promising a delay. When Halo says "give me 10 seconds," nothing schedules a
  return turn.
- Your counting/thinking-aloud gets **mis-handled**: Halo either isn't listening
  (already back at the wake loop) or treats "one… two… three…" as a *command* and
  routes it. There is **no utterance classifier** separating real-command vs
  counting/thinking-aloud vs rumble/noise vs testing-the-AI.

**Exact hooks for the fix (already identified):**
- **Utterance classifier** → `turn.py:498`, right after `transcribe()`, before
  `detect_mode`. The signals it needs are already in hand there: `peak_rms()`
  (loudness → rumble vs speech), the mode shape, the Stage-1 verdict, and
  VAD-vs-RMS provenance. New lexical rules (digit-words, filler density) would be
  added — `_THINKING_MARKERS` (`turn.py:108`) is the seed.
- **Floor / turn-reclaim** → `voice.py:354` (`_note_spoken` is the one choke point
  where *every* Halo utterance is observed — perfect for a "promise detector") plus
  the orchestration loop's say→re-listen seam. Needs a new explicit `floor` state
  (USER / AI / CONTESTED) in `run_conversation`, which today has no such variable.

---

## 5. Dictation — works well, but not anywhere-anytime

`dictation.py` is a self-contained loop (no routing, no agent) that types your
speech into **whatever has OS keyboard focus**, system-wide, via Win32 Unicode
injection (`desktop_control.type_into_focus`). It's pipelined: capture pushes raw
utterances onto a queue and keeps listening while a typist thread sanitizes →
LLM-autocorrects (time-boxed, fail-open) → types in order.

**Is it high-end?** Solid for a local tool — RAW transcription so real words aren't
swallowed, accent-robust start/stop matching, two-stage cleanup, spoken control
grammar ("new line", "press enter", "back to halo"). **Gaps vs commercial:** no
live word-by-word partials (batch per-utterance), no custom vocabulary, autocorrect
needs the router model reachable, Windows-only.

**Can you activate it anywhere/anytime? No — conversation mode first.** The path is
**wake word → turn → transcript matches a "dictate" trigger → `run_dictation()`**.
There is **no global hotkey / always-listening entry point**. Once active it does
type into any focused field; the constraint is purely on *entry*. (Closing this —
a true global dictation hotkey — is on the roadmap.)

Limits: idle-exit 30 s, single-utterance cap 20 s, autocorrect budget 6 s, two
type failures abort.

---

## 6. Prompts & personality — what's baked vs adjustable

- **User-adjustable today: essentially just the persona** — `user_name` and
  `halo_character` (via config/env), interpolated into the chat prompt and agent
  system prompt.
- **Everything else is baked into Python string literals** with no config/file/env
  override: the Stage-2 routing prompt, the STT-correction dictionary, all
  anti-hallucination examples, the summarizer prompt, the session block, the
  follow-up gate's keyword sets.

So the current split is: **personality = 2 fields; all routing/behavior prompts =
hardcoded.** Making prompts user-adjustable (your ask) means externalizing
`STAGE2_PROMPT`, the few-shots, and the dictionaries into editable files while
keeping the schema/validation logic in code. *(Bonus: `router.py` carries ~400
lines of **dead** old prompt — `_STAGE2_PROMPT_OLD` — that should just be deleted.)*

---

## 7. Free vs paid + fallbacks — the honest state

| Layer | Free / local (default) | Paid / cloud | Fallback if cloud misconfigured |
|---|---|---|---|
| Voice (TTS) | Kokoro ONNX | ElevenLabs | **Goes SILENT** — no auto-fallback to Kokoro (`voice.py:232`) |
| Brain | Ollama qwen2.5:1.5b | OpenRouter | Degrades to `unclear` / canned lines — **no cloud→local hop** (`router.py:996`) |
| STT | faster-whisper (local) | — | n/a |
| Wake | openWakeWord (local) | — | n/a |

**"100% free local" is real and is the default and only zero-config path.** Cost of
free: `ollama pull`, `halo download-models` (~200 MB Kokoro), faster-whisper
auto-download (~1.5 GB). No API keys for Halo itself; the *agents* (Claude/Codex)
auth separately.

**The fallback gaps are a robustness bug, not a feature:** choosing a cloud
provider and misconfiguring it makes Halo mute / brain-dead instead of falling back
to the working local path. There is no provider-to-provider failover anywhere. The
README's "fuzzy-fallback dispatcher" is a *routing*-resilience path (named-agent
dispatch when the brain is unreachable), not a provider fallback.

---

## 8. Memory & self-improvement

`memory.py` is a local SQLite store (`~/.halo/halo_memory.db`): **turns** (pruned
after `retention_days`) and **facts** (never pruned, importance-weighted, bumped on
restatement). Capture via explicit "remember that X" and a conservative auto-fact
pass; retrieval is **keyword-overlap + recency only** (no embeddings). A compact
`# MEMORY` block is injected into the brain each turn. Designed so a vector backend
(Mem0) can drop in later.

**Self-improvement does not exist beyond fact accumulation.** Nothing rewrites
prompts, tunes thresholds from outcomes, or adapts behavior autonomously. `halo
calibrate` (per-mic wake tuning, user-triggered) is the only adaptive loop.

---

## 9. Tools & desktop control

`tools.py` is **pure regex matching, zero LLM**: calculator/browser/notepad/
explorer/terminal/datetime + a ~25-entry app-alias table (paint, vscode, spotify…),
cross-platform, **no hardcoded install paths**. `is_pure_tool()` is the strict
pre-LLM gate; `execute_system_intent()` splits on conjunctions and fires each tool.
A `HALO_TOOLS_DRY_RUN=1` chokepoint exists because tool tests really launch apps.

`desktop_control.py` is Windows-only Win32 via ctypes — Unicode injection (the
dictation primitive) and focus-a-terminal-and-paste (the "type into the session"
primitive). Direct OS control, **not** MCP.

---

## 10. Packaging — ready for others?

**In principle yes; in practice not yet.** Clean hatchling package, console script
`halo`, portable path resolution (`~/.halo/...`, no absolute user paths in source),
cross-platform fallbacks, a `halo doctor`. But:
- **Git-install only** (`pip install git+…`); `halo-voice` on PyPI is roadmapped,
  not published.
- **Model-heavy, multi-step** setup (Kokoro + whisper + ollama pull + external
  `claude`/`codex` CLIs + optional `uvx` for MCP). All surfaced by `doctor`, none
  auto-installed.
- **Windows-first** for the flagship features (desktop control, dictation, MCP,
  CLI-tail windows). Tools/TTS have Mac/Linux paths; the headline UX assumes
  Windows + NVIDIA.
- **`DEBUG = True` is hardcoded** (`config.py:15`) — ships debug wav dumps on.
- **`halo config` subcommand is documented but not registered** in `cli.py`.
- **README is substantially out of date** (mythology session names that were
  replaced, stale thresholds, stale idle timing).

---

## 11. Cross-cutting tech-debt (the "it's very messy" inventory)

Concrete, from the full read — this is the refactor target:

**Monolith functions:**
- `run_conversation()` — **775 lines, ~25 inline routing branches** (`__main__.py:2257`). The #1 target.
- `_run` (agents) ~167 lines; `start_job` ~150 (4 nested closures); `dispatch` ~116; `understand_and_route` ~92; `run_turn` ~210.

**Duplication:**
- **Agent-name/alias resolution implemented 6× ** with drifting garble tables
  ("clawde"/"kodex"/"crodex") across `__main__.py` → should be one `agent_match.py`.
- **The mic InputStream+queue+consumer pattern hand-rolled 3–4×** (record, wake,
  calibrate×2); capture loops duplicated between `run_turn` and `dictation`.
- **OpenRouter request-building copy-pasted** between routing and streaming paths.
- **Three session registries / three "current" notions** under three locks (§3).
- **Circular import** between `agents.py` and `sessions.py` (shared stream-parsing
  primitives should move to a neutral module).

**Dead / stale:**
- ~400-line `_STAGE2_PROMPT_OLD` and orphaned `STAGE1_PROMPT` in `router.py`.
- Stale "Moonshine" STT comments (it's faster-whisper); stale module docstrings;
  dead `CLAUDE_MODEL` constant; dead `_run` voice-ticker for persistent agents.

**Config traps:**
- **`MIC_NOISE_GATE_RMS` is defined in TWO places with TWO env-var names**
  (`HALO_MIC_NOISE_GATE_RMS` in config.py vs `HALO_MIC_NOISE_GATE` in userconfig.py)
  — same physical knob, two defaults. A real footgun.
- `idle_sec` default disagrees across dataclass (8) / template (5) / README (5).
- Many magic numbers that should be config; env coverage is a partial subset.

**Fragile state:**
- `_persistent_disabled` is **sticky for the whole process** — one transient Claude
  spawn failure permanently downgrades to one-shot with no recovery.
- Two independent VAD detectors writing the same events in `record.py` (subtle
  early/late commits).
- Best-effort thread joins with short timeouts hide wedged daemon threads.

---

## 12. The conceptual model in one picture

```
                       ┌─────────────────────────── YOU (PersonaConfig: name/role/character) ───────────────────────────┐
                       │                                                                                                │
   wake word ──▶ [LISTEN] ──▶ [TURN] ──▶ [CLASSIFY: chitchat? local action? real work?] ──┐                            │
   (per-machine                turn.py        router brain (local Ollama / cloud OpenRouter) │                            │
    calibration)                              (NO MCP, NO tool-calling — JSON only)          │                            │
                                                                                            ▼                            │
                       ┌──────────────────────┬──────────────────────────────┬────────────────────────────┐            │
                       ▼                      ▼                              ▼                            ▼            │
                  chat back            local tools                    DICTATION                    coding AGENTS       │
                  (brain voice)        (open apps, say)           (type anywhere —              Claude (persistent)    │
                       │               desktop control            but must wake first)          Codex (one-shot)       │
                       │                                                                          via World A registry  │
                       │                                                                          + World B discovery   │
                       └───────────────────────────── TTS: Kokoro (free) / ElevenLabs (paid) ──────────────────────────┘
                                                          (no auto-fallback between them)

   MEMORY (local SQLite: facts + turns, keyword recall) feeds the brain each turn.
   The whole loop is HALF-DUPLEX and USER-LED today: no "Halo holds the floor" state.
```

**The five honest gaps your vision targets:**
1. **Turn-taking** — no floor model, no utterance classifier → the "10-second" problem.
2. **Skills** — no enumeration, no skill routing.
3. **MCP-to-brain** — none; brain can't call tools.
4. **Dictation entry** — wake-gated, not anywhere-anytime.
5. **Prompts/personality** — only 2 fields adjustable; rest baked.
   Plus: **provider fallbacks** fail to silence, and the codebase has a handful of
   **monoliths + 6× agent-name duplication + 3 session registries** to untangle.
