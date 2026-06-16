import { create } from 'zustand'
import type { Agent, Metrics } from '../lib/types'

interface MicState {
  access: boolean
  denied: boolean
  error?: string
}

interface HaloState {
  // connection + pipeline
  connected: boolean
  thinking: boolean
  speaking: boolean
  modeDirect: string | null
  lastStage2Ms: number
  convoStartTs: number | null
  sessionsDiscovered: number

  // overlays (transcript captions over the core)
  overlayYou: string
  overlayHalo: string

  // backend snapshots
  agents: Agent[]
  metrics: Partial<Metrics>
  mic: MicState

  // monotonically increasing counters used by panels (memory lobe)
  toolFires: number
  speakCount: number

  // actions
  setConnected: (v: boolean) => void
  setThinking: (v: boolean) => void
  setSpeaking: (v: boolean) => void
  setMode: (v: string | null) => void
  setStage2Ms: (v: number) => void
  setConvoStart: (v: number | null) => void
  setSessions: (v: number) => void
  setOverlayYou: (v: string) => void
  setOverlayHalo: (v: string) => void
  setAgents: (v: Agent[]) => void
  setMetrics: (v: Partial<Metrics>) => void
  setMic: (v: MicState) => void
  incTool: () => void
  incSpeak: () => void
}

export const useHaloStore = create<HaloState>((set) => ({
  connected: false,
  thinking: false,
  speaking: false,
  modeDirect: null,
  lastStage2Ms: 0,
  convoStartTs: null,
  sessionsDiscovered: 0,
  overlayYou: '',
  overlayHalo: '',
  agents: [],
  metrics: {},
  mic: { access: false, denied: false },
  toolFires: 0,
  speakCount: 0,

  setConnected: (v) => set({ connected: v }),
  setThinking: (v) => set({ thinking: v }),
  setSpeaking: (v) => set({ speaking: v }),
  setMode: (v) => set({ modeDirect: v }),
  setStage2Ms: (v) => set({ lastStage2Ms: v }),
  setConvoStart: (v) => set({ convoStartTs: v }),
  setSessions: (v) => set({ sessionsDiscovered: v }),
  setOverlayYou: (v) => set({ overlayYou: v }),
  setOverlayHalo: (v) => set({ overlayHalo: v }),
  setAgents: (v) => set({ agents: v }),
  setMetrics: (v) => set({ metrics: v }),
  setMic: (v) => set({ mic: v }),
  incTool: () => set((s) => ({ toolFires: s.toolFires + 1 })),
  incSpeak: () => set((s) => ({ speakCount: s.speakCount + 1 })),
}))

// Non-hook accessor for modules (api poller, audio) that live outside React.
export const halo = () => useHaloStore.getState()

// Expose the store on window for quick debugging of visual states from the
// devtools console on the localhost dashboard, e.g.
//   __halo.setState({ speaking: true })
if (typeof window !== 'undefined') {
  ;(window as unknown as { __halo: typeof useHaloStore }).__halo = useHaloStore
}
