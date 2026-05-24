# Changelog

All notable changes to Halo. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning is SemVer-ish (still pre-1.0, expect breaking changes).

## [Unreleased]

(none yet)

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
