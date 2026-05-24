# Changelog

All notable changes to Halo. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning is SemVer-ish (still pre-1.0, expect breaking changes).

## [Unreleased]

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
