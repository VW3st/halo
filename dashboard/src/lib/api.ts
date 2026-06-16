// Polls the Flask backend and translates the raw bus events into store
// updates + timeline bumps. This is the single bridge between Halo's Python
// event bus and the React UI. Lives outside React (module singleton).
import { halo } from '../store/voiceHarnessStore'
import { bump, addToolTick, startTimeline } from './timeline'
import type { BusEvent } from './types'

let lastSeq = 0
let overlayYouTimer: ReturnType<typeof setTimeout> | null = null
let overlayHaloTimer: ReturnType<typeof setTimeout> | null = null

function showYou(text: string) {
  halo().setOverlayYou(text)
  if (overlayYouTimer) clearTimeout(overlayYouTimer)
  overlayYouTimer = setTimeout(() => halo().setOverlayYou(''), 4500)
}
function showHalo(text: string) {
  halo().setOverlayHalo(text)
}
function hideHalo() {
  if (overlayHaloTimer) clearTimeout(overlayHaloTimer)
  overlayHaloTimer = setTimeout(() => halo().setOverlayHalo(''), 700)
}

function applyEvent(evt: BusEvent) {
  const s = halo()
  switch (evt.kind) {
    case 'wake.fired':
      bump('listen', 0.9); break
    case 'record.opened':
      bump('listen', 1.0); break
    case 'record.silence':
      bump('listen', 0.3); break
    case 'stt.done':
      bump('listen', 0.8); showYou(String(evt.text || '')); break
    case 'thinking.start':
      s.setThinking(true); bump('think', 0.6); break
    case 'thinking.end':
      s.setThinking(false)
      if (typeof evt.ms === 'number') s.setStage2Ms(evt.ms)
      bump('think', 0.9); break
    case 'tools.invoke':
      s.incTool(); addToolTick(String(evt.name || '?')); bump('tools', 1.0); break
    case 'route.matched':
      if (['status_query', 'vocative', 'verbal', 'tool', 'direct'].includes(String(evt.handler)))
        bump('memory', 0.4)
      break
    case 'speaking.start':
      s.setSpeaking(true); s.incSpeak(); bump('speak', 0.9); showHalo(String(evt.text || '')); break
    case 'speaking.end':
      s.setSpeaking(false); hideHalo(); break
    case 'convo.entered':
      s.setConvoStart(Date.now()); break
    case 'convo.exited':
      s.setConvoStart(null); break
    case 'mode.changed':
      s.setMode((evt.direct as string) || null); break
    case 'discovery.changed':
      s.setSessions(Number(evt.count) || 0); break
    case 'side_convo.ignored':
      bump('listen', 0.3); break
    case 'agent.dispatched':
      bump('memory', 0.5); break
    case 'agent.streaming':
      bump('speak', 0.3); break
    default:
      break
  }
}

async function pollEvents() {
  try {
    const r = await fetch('/api/events?since=' + lastSeq)
    if (!r.ok) throw new Error(String(r.status))
    const data = await r.json()
    halo().setConnected(true)
    // server restart -> seq reset; refetch from 0
    if (typeof data.seq === 'number' && data.seq < lastSeq) {
      lastSeq = 0
      const r2 = await fetch('/api/events?since=0')
      if (r2.ok) {
        const d2 = await r2.json()
        ;(d2.events || []).forEach(applyEvent)
        lastSeq = d2.seq || 0
      }
      return
    }
    ;(data.events || []).forEach(applyEvent)
    if (data.seq) lastSeq = data.seq
  } catch {
    halo().setConnected(false)
  }
}

async function pollState() {
  try {
    const r = await fetch('/api/state')
    if (!r.ok) return
    const data = await r.json()
    halo().setAgents(data.agents || [])
  } catch { /* ignore */ }
}

async function pollMetrics() {
  try {
    const r = await fetch('/api/system-metrics')
    if (!r.ok) return
    halo().setMetrics(await r.json())
  } catch { /* ignore */ }
}

export async function resetSessions() {
  try { await fetch('/api/control/reset-sessions', { method: 'POST' }) } catch { /* ignore */ }
}

let polling = false
export function startPolling() {
  if (polling) return
  polling = true
  startTimeline()
  pollEvents(); pollState(); pollMetrics()
  setInterval(pollEvents, 250)
  setInterval(pollState, 1000)
  setInterval(pollMetrics, 1000)
}
