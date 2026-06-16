// Per-lane rolling signal buffers for the State Timeline. Kept outside React
// (module singleton) so the canvas can draw at 60fps and events can bump
// lanes without re-rendering. A steady interval scrolls the buffers left.
import type { Lane, ToolTick } from './types'

export const LANES: { key: Lane; color: string }[] = [
  { key: 'listen', color: '0,229,255' },
  { key: 'think', color: '183,148,244' },
  { key: 'tools', color: '255,170,59' },
  { key: 'memory', color: '52,211,153' },
  { key: 'speak', color: '0,229,255' },
]

export const TL_N = 300
export const TL_WIN = 60 // seconds shown

const buffers: Record<Lane, Float32Array> = {
  listen: new Float32Array(TL_N),
  think: new Float32Array(TL_N),
  tools: new Float32Array(TL_N),
  memory: new Float32Array(TL_N),
  speak: new Float32Array(TL_N),
}
const level: Record<Lane, number> = { listen: 0, think: 0, tools: 0, memory: 0, speak: 0 }
export const toolTicks: ToolTick[] = []

let clock = 0
let started = false

export function bump(lane: Lane, amt = 0.8): void {
  if (level[lane] === undefined) return
  level[lane] = Math.min(1, level[lane] + amt)
}

export function addToolTick(name: string): void {
  toolTicks.push({ name, ts: Date.now() / 1000 })
  if (toolTicks.length > 48) toolTicks.shift()
}

export function bufferFor(lane: Lane): Float32Array {
  return buffers[lane]
}

function step(): void {
  clock++
  for (let li = 0; li < LANES.length; li++) {
    const k = LANES[li].key
    const buf = buffers[k]
    for (let i = 0; i < TL_N - 1; i++) buf[i] = buf[i + 1]
    const idleAmp = k === 'listen' ? 0.09 : 0.06
    const amb = idleAmp + idleAmp * 0.6 * Math.abs(Math.sin(clock * 0.09 + li * 1.1)) + 0.01 * Math.random()
    buf[TL_N - 1] = Math.max(amb, level[k])
    level[k] *= 0.6
  }
}

export function startTimeline(): void {
  if (started) return
  started = true
  setInterval(step, (TL_WIN * 1000) / TL_N) // ~200ms
}
