import { useEffect, useRef } from 'react'
import type { MutableRefObject } from 'react'
import { motion, AnimatePresence } from 'motion/react'
import HologramPanel from './HologramPanel'
import VoiceCore3D from './VoiceCore3D'
import { useHaloStore } from '../store/voiceHarnessStore'

interface Lobe {
  key: string
  title: string
  sub: string
  tags: string[]
  color: string
  rgb: [number, number, number]
  pos: string // tailwind corner classes
  anchor: [number, number] // fraction of the stage the bolt reaches
  align: 'left' | 'right'
}

const LOBES: Lobe[] = [
  { key: 'think', title: 'THINKING', sub: 'REASONING LATTICE', tags: ['ANALYZE', 'REASON', 'PLAN', 'DECIDE'], color: 'text-violet', rgb: [183, 148, 244], pos: 'left-2 top-2', anchor: [0.13, 0.2], align: 'left' },
  { key: 'tools', title: 'TOOLS', sub: 'ORCHESTRATION BUS', tags: ['SELECT', 'INVOKE', 'EXECUTE', 'RETURN'], color: 'text-amber', rgb: [255, 170, 59], pos: 'right-2 top-2', anchor: [0.87, 0.2], align: 'right' },
  { key: 'memory', title: 'MEMORY', sub: 'CONTEXTUAL STREAMS', tags: ['SHORT TERM', 'LONG TERM', 'EPISODIC', 'SEMANTIC'], color: 'text-emerald', rgb: [52, 211, 153], pos: 'left-2 bottom-2', anchor: [0.13, 0.82], align: 'left' },
  { key: 'speak', title: 'SPEAKING', sub: 'SYNTHESIS ENGINE', tags: ['GENERATE', 'PROSODY', 'RENDER', 'EMIT'], color: 'text-cyan', rgb: [0, 229, 255], pos: 'right-2 bottom-2', anchor: [0.87, 0.82], align: 'right' },
]

type Pt = [number, number]

function makeFlowBolt(x0: number, y0: number, x1: number, y1: number, amp: number, time: number, seed: number): Pt[] {
  const pts: Pt[] = []
  const dx = x1 - x0, dy = y1 - y0, len = Math.hypot(dx, dy) || 1
  const nx = -dy / len, ny = dx / len
  const steps = 28
  for (let i = 0; i < steps; i++) {
    const u = i / (steps - 1)
    const taper = Math.sin(u * Math.PI)
    const drift =
      Math.sin(u * 9.0 + time * 1.5 + seed) * 0.58 +
      Math.sin(u * 17.0 - time * 0.95 + seed * 1.7) * 0.28 +
      Math.sin(u * 31.0 + time * 0.42 + seed * 0.6) * 0.14
    const bend = (u - 0.5) * len * 0.018
    pts.push([x0 + dx * u + nx * (drift * amp * taper + bend), y0 + dy * u + ny * (drift * amp * taper + bend)])
  }
  return pts
}

function strokePts(ctx: CanvasRenderingContext2D, pts: Pt[]) {
  ctx.beginPath()
  ctx.moveTo(pts[0][0], pts[0][1])
  for (let i = 1; i < pts.length; i++) ctx.lineTo(pts[i][0], pts[i][1])
  ctx.stroke()
}

function Lightning({
  thinking,
  speaking,
  iconRefs,
}: {
  thinking: boolean
  speaking: boolean
  iconRefs: MutableRefObject<Record<string, HTMLElement | null>>
}) {
  const ref = useRef<HTMLCanvasElement>(null)
  const tRef = useRef(thinking), sRef = useRef(speaking)
  tRef.current = thinking; sRef.current = speaking
  useEffect(() => {
    const c = ref.current!
    const ctx = c.getContext('2d')!
    let raf = 0, frame = 0
    const loop = () => {
      frame++
      const dpr = window.devicePixelRatio || 1
      const w = Math.round(c.clientWidth), h = Math.round(c.clientHeight)
      if (w && h && (c.width !== w * dpr || c.height !== h * dpr)) { c.width = w * dpr; c.height = h * dpr; ctx.setTransform(dpr, 0, 0, dpr, 0, 0) }
      if (!w || !h) { raf = requestAnimationFrame(loop); return }
      ctx.clearRect(0, 0, w, h)
      ctx.globalCompositeOperation = 'lighter'
      ctx.lineJoin = 'round'; ctx.lineCap = 'round'
      const cx = w / 2, cy = h / 2, min = Math.min(w, h)
      const crect = c.getBoundingClientRect()
      LOBES.forEach((l, idx) => {
        const active = (l.key === 'think' && tRef.current) || (l.key === 'speak' && sRef.current)
        let ax: number, ay: number
        ax = l.anchor[0] * w; ay = l.anchor[1] * h
        const el = iconRefs.current[l.key]
        if (el) {
          const r = el.getBoundingClientRect()
          ax = r.left + r.width / 2 - crect.left
          ay = r.top + r.height / 2 - crect.top
        }
        const dx = ax - cx, dy = ay - cy, len = Math.hypot(dx, dy) || 1
        const sx = cx + (dx / len) * min * 0.16, sy = cy + (dy / len) * min * 0.16
        const time = frame / 60
        const main = makeFlowBolt(sx, sy, ax, ay, len * (active ? 0.07 : 0.032), time, idx + 1.7)
        const branchSeed = idx + 5.2
        const branchOn = active || Math.sin(time * 0.7 + branchSeed) > 0.45
        const bp = main[Math.floor(main.length * 0.62)]
        const branch = branchOn
          ? makeFlowBolt(bp[0], bp[1], bp[0] + (idx % 2 ? -1 : 1) * min * 0.055, bp[1] + (idx < 2 ? 1 : -1) * min * 0.04, len * 0.024, time * 1.25, branchSeed)
          : null
        // Smooth sine shimmer (NOT per-frame random) -> calm on the eyes.
        const shimmer = active
          ? 0.7 + 0.24 * Math.sin(frame * 0.11 + idx)
          : 0.42 + 0.1 * Math.sin(frame * 0.03 + idx * 1.7)
        const [r, g, b] = l.rgb
        // outer colored glow
        ctx.strokeStyle = `rgba(${r},${g},${b},${shimmer * 0.3})`
        ctx.lineWidth = active ? 4.5 : 2.4
        ctx.shadowColor = `rgb(${r},${g},${b})`; ctx.shadowBlur = active ? 12 : 6
        strokePts(ctx, main)
        if (branch) strokePts(ctx, branch)
        // bright white-hot core
        ctx.strokeStyle = `rgba(255,255,255,${shimmer * (active ? 0.78 : 0.45)})`
        ctx.lineWidth = active ? 1.5 : 0.85; ctx.shadowBlur = 5
        strokePts(ctx, main)
        ctx.shadowBlur = 0
        // node where the bolt meets the lobe icon
        const pulse = 0.55 + 0.35 * Math.sin(frame * 0.08 + idx)
        ctx.fillStyle = `rgba(${r},${g},${b},${(active ? 0.85 : 0.45) * pulse})`
        ctx.beginPath(); ctx.arc(ax, ay, active ? 3.5 : 2.3, 0, Math.PI * 2); ctx.fill()
      })
      ctx.globalCompositeOperation = 'source-over'
      raf = requestAnimationFrame(loop)
    }
    loop()
    return () => cancelAnimationFrame(raf)
  }, [])
  return <canvas ref={ref} className="pointer-events-none absolute inset-0 z-[3] h-full w-full" />
}

export default function CenterStage() {
  const thinking = useHaloStore((s) => s.thinking)
  const speaking = useHaloStore((s) => s.speaking)
  const overlayYou = useHaloStore((s) => s.overlayYou)
  const overlayHalo = useHaloStore((s) => s.overlayHalo)
  const sessions = useHaloStore((s) => s.sessionsDiscovered)
  const toolFires = useHaloStore((s) => s.toolFires)

  // Live DOM handles to each lobe's icon box so the lightning can terminate
  // exactly on them (positions shift with panel size).
  const iconRefs = useRef<Record<string, HTMLElement | null>>({})

  const status = speaking ? 'SPEAKING' : thinking ? 'THINKING' : 'OPTIMAL'
  const statusColor = speaking ? 'text-amber' : thinking ? 'text-violet' : 'text-emerald'
  const dot = speaking ? 'bg-amber shadow-[0_0_8px_#ffaa3b]' : thinking ? 'bg-violet shadow-[0_0_8px_#b794f4]' : 'bg-emerald shadow-[0_0_6px_#34d399]'

  const counts: Record<string, string> = { think: '', tools: String(toolFires), memory: String(sessions), speak: '' }

  return (
    <HologramPanel className="flex min-h-0 flex-col" delay={0}>
      <div className="text-center">
        <div className="mono text-[13px] font-bold tracking-[0.4em] text-cyan drop-shadow-[0_0_12px_rgba(0,229,255,0.5)]">VOICE CORE</div>
        <div className="mono text-[9px] tracking-[0.3em] text-faint">SENTIENT ORCHESTRATION ENGINE</div>
      </div>

      <div className="relative mt-1.5 min-h-0 flex-1">
        <VoiceCore3D />

        {/* lightning-bolt energy connectors (canvas) */}
        <Lightning thinking={thinking} speaking={speaking} iconRefs={iconRefs} />

        {/* corner lobe labels */}
        {LOBES.map((l, i) => {
          const active = (l.key === 'think' && thinking) || (l.key === 'speak' && speaking)
          return (
            <motion.div
              key={l.key}
              initial={{ opacity: 0 }}
              animate={{ opacity: active ? 1 : 0.85 }}
              transition={{ duration: 0.4, delay: 0.2 + i * 0.05 }}
              className={`pointer-events-none absolute z-[4] ${l.pos} flex max-w-[42%] gap-1.5 ${l.align === 'right' ? 'flex-row-reverse text-right' : ''}`}
              style={{ filter: 'drop-shadow(0 0 5px rgba(6,8,13,0.95))' }}
            >
              <span ref={(el) => { iconRefs.current[l.key] = el }} className={`flex h-[22px] w-[22px] shrink-0 items-center justify-center rounded border border-current ${l.color} bg-[rgba(6,8,13,0.55)]`} style={{ boxShadow: '0 0 8px currentColor' }}>
                <svg viewBox="0 0 24 24" className="h-3 w-3" fill="none" stroke="currentColor" strokeWidth="2"><circle cx="12" cy="12" r="6" /></svg>
              </span>
              <div className={`flex flex-col leading-[1.2] ${l.align === 'right' ? 'items-end' : ''}`}>
                <span className={`mono text-[10px] font-bold tracking-[0.16em] ${l.color}`} style={{ textShadow: '0 0 8px currentColor,0 1px 3px #000' }}>{l.title}</span>
                <span className="mono text-[8px] tracking-[0.14em] text-faint" style={{ textShadow: '0 1px 3px #000' }}>{l.sub}</span>
                <div className="mt-0.5">
                  {l.tags.map((tg) => (
                    <span key={tg} className="mono block text-[8px] leading-[1.55] tracking-[0.14em] text-dim" style={{ textShadow: '0 1px 3px #000' }}>
                      {l.align === 'right' ? `${tg} ∵` : `∴ ${tg}`} {counts[l.key] && tg === l.tags[0] ? <b className="text-[#e3eaf6]">{counts[l.key]}</b> : null}
                    </span>
                  ))}
                </div>
              </div>
            </motion.div>
          )
        })}

        {/* core status pill */}
        <div className="absolute bottom-1 left-1/2 z-[4] flex -translate-x-1/2 items-center gap-2 whitespace-nowrap rounded-full border border-edge bg-[rgba(6,8,13,0.82)] px-3 py-1 mono text-[9px] uppercase tracking-[0.2em] text-dim">
          <span className={`h-1.5 w-1.5 rounded-full ${dot}`} /><span>Core Status</span><span className={statusColor}>{status}</span>
        </div>

        {/* transcript captions */}
        <AnimatePresence>
          {overlayYou && (
            <motion.div
              initial={{ opacity: 0, y: -6 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }}
              className="absolute left-1/2 top-8 z-[6] max-w-[80%] -translate-x-1/2 rounded border border-edge bg-[rgba(6,8,13,0.85)] px-4 py-2 text-center shadow-[0_4px_22px_rgba(0,229,255,0.18)]"
            >
              <span className="mono block text-[8px] tracking-[0.3em] text-cyan">YOU</span>
              <span className="mono text-[13px] text-[#e3eaf6]">{overlayYou}</span>
            </motion.div>
          )}
        </AnimatePresence>
        <AnimatePresence>
          {overlayHalo && (
            <motion.div
              initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }}
              className="absolute bottom-8 left-1/2 z-[6] max-w-[80%] -translate-x-1/2 rounded border border-edge bg-[rgba(6,8,13,0.85)] px-4 py-2 text-center shadow-[0_4px_22px_rgba(255,170,59,0.18)]"
            >
              <span className="mono block text-[8px] tracking-[0.3em] text-amber">HALO</span>
              <span className="mono text-[13px] text-[#e3eaf6]">{overlayHalo}</span>
            </motion.div>
          )}
        </AnimatePresence>
      </div>
    </HologramPanel>
  )
}
