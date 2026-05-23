# Halo

Voice front-end for agentic coding tools. Say a wake word, talk to
Claude Code or Codex CLI in plain English, and hear the result back.
Halo is the audio layer; the agents do the work.

Status: **v0.5 — Claude streams its response live, sentence by sentence.**

See [CHANGELOG.md](./CHANGELOG.md) for the full history. Licensed MIT
([LICENSE](./LICENSE)).

```
You: "Hey Jarvis. Claude, write a one-line python script that prints hello."
Halo: "On it. I'm calling this session Mercury. I'll let you know."
        ... (Claude works in the background, you can keep talking)
Halo: "Mercury says, I wrote it to hello.py."

You: "now make it print goodbye instead."
Halo: "Mercury is working. I'll let you know."
Halo: "Mercury says, done."

You: "Codex, refactor it into a function."
Halo: "On it. I'm calling this session Neptune. I'll let you know."

You: "back to halo. open chrome."
Halo: "Halo here. Opened browser."

You: "goodbye."
Halo: "Goodbye."
```

100% local out of the box (Ollama + faster-whisper + Kokoro). No API
keys required to run Halo — the agents themselves (Claude Code / Codex)
authenticate against their own services.

---

## Stack

| Layer        | Tech                                              | Notes |
|--------------|---------------------------------------------------|-------|
| Wake word    | [openWakeWord](https://github.com/dscripka/openWakeWord) | `hey_jarvis` (placeholder until custom `hey_halo` is trained) |
| Mic          | [sounddevice](https://python-sounddevice.readthedocs.io/) | 16 kHz mono int16 |
| VAD          | [silero-vad v5](https://github.com/snakers4/silero-vad)   | 600 ms base silence, mode-adaptive extensions |
| STT          | [faster-whisper](https://github.com/SYSTRAN/faster-whisper) + distil-large-v3 | int8_float16 on CUDA, ~500 ms per utterance |
| Router       | [Ollama](https://ollama.com) + `qwen2.5:1.5b-instruct`    | Stage 2 LLM, fires only when no local handler matches |
| TTS          | [Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M) ONNX (fp16) | `af_heart` voice, sanitized for Markdown |
| Agents       | [Claude Code](https://github.com/anthropics/claude-code) + [Codex CLI](https://github.com/openai/codex) subprocesses | Persistent sessions across Halo's lifetime |

Hardware target: Windows + NVIDIA GPU (RTX 3060 4 GB is enough). Mac /
Linux paths exist for tools and TTS; CUDA-specific bits gracefully
fall back to CPU.

---

## Setup

### 1. Python deps

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

`silero-vad` pulls PyTorch (~800 MB). To save bandwidth/disk on a CPU-only
laptop:

```powershell
pip install torch --index-url https://download.pytorch.org/whl/cpu
pip install -r requirements.txt
```

First run downloads on demand:
- openWakeWord pretrained ONNX models (~few MB)
- silero-vad model (~2 MB)
- faster-whisper distil-large-v3 (~1.5 GB, from HuggingFace)

### 2. Kokoro TTS model files

Download into `models/`:
- [`kokoro-v1.0.fp16.onnx`](https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.fp16.onnx) (~170 MB)
- [`voices-v1.0.bin`](https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin) (~28 MB)

```
D:\Halo\models\
    kokoro-v1.0.fp16.onnx
    voices-v1.0.bin
```

Without these, Halo runs in silent (text-only) mode.

### 3. Ollama + routing model

Install Ollama from <https://ollama.com/download>, then:

```powershell
ollama pull qwen2.5:1.5b-instruct
```

Ollama auto-starts a background service on `localhost:11434`. Halo
talks to it over HTTP.

**Why `qwen2.5:1.5b-instruct`?** Non-reasoning, ~1.5 GB, hits sub-100 ms
on a warm KV cache. Reasoning models (qwen3, deepseek-r1) emit `<think>`
tokens and blow the per-turn budget to 20 s+. Optional alternatives via
`OLLAMA_MODEL` in `halo/config.py`: `qwen2.5:3b-instruct` (better
accuracy, ~3× slower), `qwen2.5:7b-instruct` (best accuracy, needs more
VRAM).

### 4. Coding agents (Claude Code and/or Codex CLI)

Install whichever you want Halo to dispatch to. At least one is needed
unless you only plan to use local tools.

```powershell
# Claude Code (requires Anthropic auth — `claude login` once)
npm install -g @anthropic-ai/claude-code

# Codex CLI (requires OpenAI auth — `codex login` once)
npm install -g @openai/codex
```

Both must be on PATH. Verify with `claude --version` and `codex --version`.

---

## Run

```powershell
cd D:\Halo
.\.venv\Scripts\Activate.ps1
python -m halo
```

You'll see preload messages, then `Halo ready. Say the wake word...`
and hear `"Halo online."` Speak the wake word ("Hey Jarvis"), then
your command.

---

## How conversations work

Once a wake word fires, you enter a **conversation** — you don't have
to re-say "Hey Jarvis" between turns. Each thing you say is matched
against this priority list, **first match wins**:

```
1. End phrase             "over and out" / "goodbye" / "go to sleep"      -> exit conversation
2. New session            "new task" / "start over" / "forget that"       -> reset agent sessions
3. Vocative dispatch      "Claude, build X" / "Codex, run Y"              -> direct to that agent (no LLM)
4. Pure mode switch       "switch to codex" / "talk to claude"            -> set direct-dialogue agent
5. Back to Halo           "back to halo" / "talk to me"                   -> exit direct-dialogue mode
6. Status query           "what's happening" / "are you done"             -> read job registry
7. Replay last result     "what did Claude say" / "repeat what Codex..."  -> re-speak last response
8. Local tool             "open chrome" / "launch calculator"             -> system app handlers
9. Direct-dialogue active sends utterance straight to current agent       -> --continue
10. Stage 2 LLM (fallback) only fires when nothing above matched          -> qwen2.5 routing
```

After 5 s of true idleness (no jobs running, no speech), Halo goes back
to wake-listening.

### Vocative dispatch — the killer feature

Naming the agent up front skips the router LLM entirely. Comma is
required (faster-whisper adds it reliably):

```
"Claude, build a login page with Supabase"
"Codex, run the tests and fix anything red"
"Claude code, summarize the README"
"Hey Codex, list the open issues"
```

After the first dispatch, you're automatically in **direct-dialogue
mode** with that agent — every follow-up goes straight to its
`--continue`, no LLM round-trip:

```
You:   "Claude, build a hello world script."
Halo:  "On it. I'm calling this session Mercury."
       ... Mercury responds
You:   "now add a docstring."     (direct -> Mercury)
You:   "also make it take a name." (direct -> Mercury)
```

To switch agents mid-flow:

```
"switch to codex" / "talk to codex"        -> direct dialogue with Codex
"Codex, ..."                                -> dispatches to Codex AND switches
"back to halo" / "talk to me"               -> exit direct dialogue
```

### Mythology names

When a new session starts for an agent, Halo assigns it a random
Roman-mythology name (Mars, Mercury, Juno, Vesta, Apollo, ...). The
name persists across follow-ups and resets on "new task". This makes
it clear which agent is talking when both are working: *"Mercury says
done. Neptune had a problem with the auth tests."*

### Persistent sessions

Each agent's `--continue` thread stays alive for the entire Halo
process. Walk away, come back, say a wake word, give Claude a
follow-up — it picks up where it left off. Explicit reset with
"new task" / "fresh session" / "forget that" rotates the name and
drops the continuation flag.

### Async + concurrent

`Claude` and `Codex` can run jobs at the same time. While one is
working, the other is free to take a new task, you can fire local
tools (`"open chrome"`), or ask for status (`"what's happening"`).
Job results land between turns so they don't talk over you.

---

## Voice quality

- **Voice**: `af_heart` — the only A/A-graded voice in the Kokoro
  lineup. Change in `halo/voice.py:DEFAULT_VOICE`.
- **Sanitizer**: every spoken string passes through `_clean_for_speech()`,
  which strips Markdown (`**bold**`, `` `code` ``, em-dashes, bullets,
  headings, link syntax), collapses long file paths to basenames,
  drops mojibake (`â€"`), and turns newlines into sentence breaks.
- **Agent voice prompt**: Claude and Codex are told they're on a voice
  channel and to keep responses to 2 short sentences. They're told to
  write code/long content to files and just say "I wrote it to <name>."

---

## File layout

```
halo/
  __main__.py     main loop: wake -> conversation -> routing priority
  config.py       paths, sample rate, model name, timing constants
  wake.py         openWakeWord listener + pre-wake audio ring buffer
  record.py       silero-vad RecorderState, chime, backchannel tone
  stt.py          faster-whisper BatchTranscriber (CUDA + DLL fixup)
  router.py       Stage 1 rules + Stage 2 LLM (Ollama qwen2.5 + JSON schema)
  turn.py         per-turn orchestration (record/transcribe; routing in __main__)
  tools.py        cross-platform local tools (browser, calc, notepad, ...)
  agents.py       agent registry, dispatch, background jobs, session names
  voice.py        Kokoro TTS + Markdown sanitizer

models/
  kokoro-v1.0.fp16.onnx       Kokoro 82M voice model
  voices-v1.0.bin             Kokoro voice pack

scripts/
  bench_router.py             Stage 1 + Stage 2 latency benchmark
  fw_smoke.py                 faster-whisper accuracy smoke test
  test_detect_mode.py         adaptive turn-taking unit tests
  test_vocative.py            vocative dispatch unit tests
  test_voice_mode.py          TTS sanitizer + mode-switch tests
  test_fixes_round.py         regression suite for recent bug fixes
```

---

## Adding a new agent

Drop one entry into `AGENTS` in `halo/agents.py`. No other code
changes needed:

```python
AGENTS["aider"] = AgentConfig(
    key="aider",
    spoken_name="Aider",
    voice_triggers=("aider",),
    first_call=("aider", "--message", "{PROMPT}", "--yes-always"),
    continue_call=("aider", "--message", "{PROMPT}", "--yes-always"),
    parses_json=False,
)
```

Tokens `{PROMPT}` and `{CWD}` get substituted at call time. Then teach
the Stage 2 router prompt about the new agent if you want voice routing
("aider, fix this") or just rely on the vocative dispatch in
`__main__.py:_vocative_dispatch` (you'll need to extend its regex).

---

## Wake word note

openWakeWord ships `alexa`, `hey_jarvis`, `hey_mycroft`, `hey_rhasspy`
out of the box. There is no built-in `hey_halo`. Step 1 uses
`hey_jarvis` as a placeholder.

To train a real `hey_halo` model:
1. Record ~100 samples of yourself saying "Halo" using openWakeWord's
   [training notebook](https://github.com/dscripka/openWakeWord#training-new-models)
2. Drop the resulting `.onnx` into `models/`
3. Update `halo/wake.py:WAKE_WORD`

Threshold lives in `halo/wake.py:THRESHOLD` (default 0.5). Raise if
you get false positives, lower if it takes too many tries.

---

## Latency budget (RTX 3060)

| Path                                        | Cold      | Warm       |
|---------------------------------------------|-----------|------------|
| Wake word detect                            | instant   | instant    |
| Speech → silero silence (600 ms base)       | ~0.6 s    | ~0.6 s     |
| faster-whisper STT (1-5 s utterance)        | ~5 s      | ~0.5-1 s   |
| Stage 2 LLM (qwen2.5:1.5b JSON output)      | ~5-7 s    | ~3-4 s     |
| Tool fast-path (open app)                   | <50 ms    | <50 ms     |
| Vocative agent spawn                        | <100 ms   | <100 ms    |
| Agent task (Claude/Codex code work)         | varies    | varies     |

**Typical wake-to-action timings:**
- `"Hey Jarvis. Open chrome."`              → ~1.5 s
- `"Hey Jarvis. Claude, write hello.py."`   → ~2 s (then agent works)
- `"build me a website"` (unnamed, fallback)→ ~5 s (Stage 2 LLM fires)

---

## Roadmap

1. ✅ Wake word listener
2. ✅ Record + faster-whisper STT (replaced whisper.cpp and Moonshine)
3. ✅ Two-stage router (rules + Ollama)
4. ✅ Local tool dispatch (browser, calc, notepad, explorer, terminal)
5. ✅ Kokoro TTS + Markdown sanitizer
6. ✅ Claude Code subprocess + persistent session
7. ✅ Codex CLI subprocess + persistent session
8. ✅ Async background jobs, status queries, replay
9. ✅ Conversation mode, end phrases, idle sleep
10. ✅ Direct-dialogue mode, mode switches, mythology names
11. ✅ Vocative dispatch — bypass router for explicit agent calls
12. ✅ Streaming Claude output → live TTS sentence-by-sentence
13. Custom `hey_halo` wake model (needs voice samples)
14. Streaming Codex (Codex CLI doesn't expose stream-json yet; tracking)
15. Premium TTS provider abstraction (ElevenLabs etc., opt-in)
16. Agent registry from external TOML — `halo init` to scaffold new agents
17. Package + publish (`pip install halo-voice`)

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `cublas64_12.dll not found` | `pip install nvidia-cublas-cu12 nvidia-cudnn-cu12` — `halo/stt.py` adds them to PATH on import |
| Wake takes many tries | Lower `THRESHOLD` in `halo/wake.py` (try 0.4) or train a custom wake model |
| Voice sounds robotic | Try `af_bella` or `bf_emma` in `halo/voice.py:DEFAULT_VOICE` |
| `Claude Code CLI not found` | `npm install -g @anthropic-ai/claude-code` and re-open terminal |
| Codex auth prompts mid-run | Run `codex login` once in a normal terminal first |
| Halo speaks Claude's Markdown literally | Should be sanitized — check `_clean_for_speech` in `halo/voice.py` |
| `Input must be provided` from Claude | Wake-strip left an empty prompt; current code guards this — file an issue if it recurs |
| Ctrl-C doesn't stop Halo | Fixed — wake stream now polls with 250 ms timeout so SIGINT propagates |
