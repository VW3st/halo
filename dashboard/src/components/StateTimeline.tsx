import { useEffect, useRef } from 'react'
import HologramPanel from './HologramPanel'
import { LANES, TL_N, TL_WIN, bufferFor, toolTicks } from '../lib/timeline'

const TAU = Math.PI * 2
const LANE_META = [
  { label: 'Listening', cls: 'text-cyan' },
  { label: 'Thinking', cls: 'text-violet' },
  { label: 'Tools', cls: 'text-amber' },
  { label: 'Memory', cls: 'text-emerald' },
  { label: 'Speaking', cls: 'text-cyan' },
]
const GLYPH = [
  <path key="l" d="M3 12h4l3-8 4 16 3-8h4" />,
  <g key="t"><circle cx="12" cy="12" r="3" /><path d="M3 12h3M18 12h3M12 3v3M12 18v3" /></g>,
  <g key="o"><circle cx="6" cy="6" r="2" /><circle cx="18" cy="18" r="2" /><path d="M6 8l12 8" /></g>,
  <path key="m" d="M3 7c0-1.5 4-3 9-3s9 1.5 9 3v10c0 1.5-4 3-9 3s-9-1.5-9-3z" />,
  <path key="s" d="M11 5L6 9H2v6h4l5 4z" />,
]

function Canvas() {
  const ref = useRef<HTMLCanvasElement>(null)
  useEffect(() => {
    const c = ref.current!
    const ctx = c.getContext('2d')!
    let raf = 0
    const loop = () => {
      const dpr = window.devicePixelRatio || 1
      const w = Math.round(c.clientWidth), h = Math.round(c.clientHeight)
      if (w && h && (c.width !== w * dpr || c.height !== h * dpr)) { c.width = w * dpr; c.height = h * dpr; ctx.setTransform(dpr, 0, 0, dpr, 0, 0) }
      if (w && h) draw(ctx, w, h)
      raf = requestAnimationFrame(loop)
    }
    loop()
    return () => cancelAnimationFrame(raf)
  }, [])
  return <canvas ref={ref} className="absolute inset-0 h-full w-full" />
}

function draw(ctx: CanvasRenderingContext2D, w: number, h: number) {
  ctx.clearRect(0, 0, w, h)
  const laneH = h / 5
  LANES.forEach((l, li) => { if (li % 2 === 0) { ctx.fillStyle = `rgba(${l.color},0.025)`; ctx.fillRect(0, li * laneH, w, laneH) } })
  ctx.strokeStyle = 'rgba(60,74,100,0.22)'; ctx.lineWidth = 1; ctx.beginPath()
  for (let i = 0; i <= 4; i++) { const x = (i / 4) * w; ctx.moveTo(x + 0.5, 0); ctx.lineTo(x + 0.5, h) }
  ctx.stroke()
  ctx.strokeStyle = 'rgba(36,48,68,0.5)'; ctx.beginPath()
  for (let i = 1; i < 5; i++) { const y = i * laneH; ctx.moveTo(0, y + 0.5); ctx.lineTo(w, y + 0.5) }
  ctx.stroke()
  ctx.globalCompositeOperation = 'lighter'
  LANES.forEach((l, li) => {
    const buf = bufferFor(l.key), mid = li * laneH + laneH * 0.5, amp = laneH * 0.38
    const grad = ctx.createLinearGradient(0, mid - amp, 0, mid + 2)
    grad.addColorStop(0, `rgba(${l.color},0.28)`); grad.addColorStop(1, `rgba(${l.color},0)`)
    ctx.fillStyle = grad; ctx.beginPath(); ctx.moveTo(0, mid)
    for (let i = 0; i < TL_N; i++) ctx.lineTo((i / (TL_N - 1)) * w, mid - buf[i] * amp)
    ctx.lineTo(w, mid); ctx.closePath(); ctx.fill()
    ctx.strokeStyle = `rgba(${l.color},0.95)`; ctx.lineWidth = 1.3; ctx.shadowColor = `rgba(${l.color},0.9)`; ctx.shadowBlur = 4
    ctx.beginPath()
    for (let i = 0; i < TL_N; i++) { const x = (i / (TL_N - 1)) * w, y = mid - buf[i] * amp; i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y) }
    ctx.stroke(); ctx.shadowBlur = 0
  })
  const toolMid = 2 * laneH + laneH * 0.5, nowS = Date.now() / 1000
  for (const tk of toolTicks) {
    const dt = nowS - tk.ts; if (dt > TL_WIN) continue
    const x = w - (dt / TL_WIN) * w
    ctx.save(); ctx.translate(x, toolMid); ctx.rotate(Math.PI / 4)
    ctx.fillStyle = 'rgba(255,190,90,0.9)'; ctx.shadowColor = '#ffaa3b'; ctx.shadowBlur = 5
    ctx.fillRect(-3, -3, 6, 6); ctx.restore(); ctx.shadowBlur = 0
  }
  ctx.globalCompositeOperation = 'source-over'
  ctx.strokeStyle = 'rgba(0,229,255,0.85)'; ctx.lineWidth = 1.4
  ctx.beginPath(); ctx.moveTo(w - 1.5, 0); ctx.lineTo(w - 1.5, h); ctx.stroke()
  ctx.fillStyle = '#00e5ff'; ctx.shadowColor = '#00e5ff'; ctx.shadowBlur = 8
  ctx.beginPath(); ctx.arc(w - 1.5, 3, 3, 0, TAU); ctx.fill(); ctx.shadowBlur = 0
}

export default function StateTimeline() {
  return (
    <HologramPanel
      title="State Timeline"
      delay={0.1}
      className="flex min-h-0 flex-col"
      right={<div className="mono flex gap-6 text-[9px] tracking-[0.08em] text-faint"><span>-60s</span><span>-30s</span><span>now</span></div>}
    >
      <div className="mt-1.5 grid min-h-0 flex-1 grid-cols-[88px_1fr] gap-1">
        <div className="flex flex-col justify-around">
          {LANE_META.map((m, i) => (
            <div key={m.label} className={`mono flex h-[18px] items-center gap-1.5 text-[9px] uppercase tracking-[0.14em] ${m.cls}`}>
              <span className="flex h-3.5 w-3.5 items-center justify-center rounded-sm border border-current">
                <svg viewBox="0 0 24 24" className="h-2 w-2" fill="none" stroke="currentColor" strokeWidth="2">{GLYPH[i]}</svg>
              </span>
              <span className="text-faint">{m.label}</span>
            </div>
          ))}
        </div>
        <div className="relative min-h-0"><Canvas /></div>
      </div>
    </HologramPanel>
  )
}
