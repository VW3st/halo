# HALO dashboard (futuristic build)

React + TypeScript + Vite frontend for the Halo voice harness. Renders the
holographic command center: a WebGL voice core (React Three Fiber + bloom),
audio-reactive waveforms (Web Audio API), animated panels (Motion), and live
state from the Halo Python backend (Zustand store + polling).

## Stack
- **Vite + React 18 + TypeScript**
- **React Three Fiber / three.js / @react-three/postprocessing** — the 3D core, particles, rings, bloom
- **Web Audio API** — mic `AnalyserNode` → time-domain / FFT / level
- **Motion** — panel + card animation
- **Tailwind CSS** — layout + neon design system
- **Zustand** — store fed by the backend event bus

## How it connects
The Python server (`halo/web.py`) exposes `/api/events`, `/api/state`,
`/api/system-metrics`, `/api/control/reset-sessions`. `src/lib/api.ts` polls
them and translates the bus events into store updates + timeline bumps. The
components react.

## Build (production — what Flask serves)
```
cd dashboard
npm install
npm run build        # outputs to ../halo/web_static (index.html + assets/)
```
Then run Halo normally (`python -m halo`) and open http://127.0.0.1:7070.

## Develop (hot reload against a running Halo)
```
python -m halo        # in one terminal (serves the API on :7070)
cd dashboard && npm run dev   # Vite dev server; /api is proxied to :7070
```

## Layout
```
src/
  main.tsx, App.tsx, index.css
  store/voiceHarnessStore.ts   Zustand store
  lib/
    types.ts          backend + UI types
    api.ts            poller: bus events -> store
    audioAnalyser.ts  Web Audio singleton
    timeline.ts       per-lane ring buffers
  components/
    Header, ListeningPanel, AudioWaveform, VoiceCore3D, CenterStage,
    ToolRouter, AgentCards, SystemMetrics, StateTimeline, OutputPanel,
    HologramPanel, Footer
```
