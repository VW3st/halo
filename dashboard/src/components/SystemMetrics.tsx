import { useEffect, useRef } from 'react'
import HologramPanel from './HologramPanel'
import { useHaloStore } from '../store/voiceHarnessStore'

const TAU = Math.PI * 2

function Torus() {
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

  function draw(ctx: CanvasRenderingContext2D, w: number, h: number) {
    ctx.clearRect(0, 0, w, h)
    ctx.globalCompositeOperation = 'lighter'
    const cpu = (useHaloStore.getState().metrics.cpu_pct || 0) / 100
    const t = performance.now()
    const cx = w / 2, cy = h / 2
    const R = Math.min(w, h) * 0.34 * (1 + cpu * 0.08 + 0.03 * Math.sin(t * 0.002))
    const rt = R * 0.42
    const ry = t * 0.00035 * (1 + cpu * 1.5)
    const tilt = 1.0 + 0.18 * Math.sin(t * 0.0004) // gentle wobble
    const cosY = Math.cos(ry), sinY = Math.sin(ry), cosT = Math.cos(tilt), sinT = Math.sin(tilt)
    const NR = 96, NT = 20, persp = R * 3.4
    const pts: { x: number; y: number; z: number; v: number }[] = []
    for (let i = 0; i < NR; i++) {
      const u = (i / NR) * TAU
      for (let j = 0; j < NT; j++) {
        const v = (j / NT) * TAU
        const x = (R + rt * Math.cos(v)) * Math.cos(u), y = rt * Math.sin(v), z = (R + rt * Math.cos(v)) * Math.sin(u)
        const x2 = x * cosY - z * sinY, z2 = x * sinY + z * cosY
        const y2 = y * cosT - z2 * sinT, z3 = y * sinT + z2 * cosT
        const s = persp / (persp - z3)
        pts.push({ x: cx + x2 * s, y: cy + y2 * s, z: z3, v: j / NT })
      }
    }
    pts.sort((a, b) => a.z - b.z)
    for (const p of pts) {
      const depth = Math.max(0, Math.min(1, (p.z + R * 1.4) / (R * 2.8)))
      const a = (0.1 + depth * 0.75) * (0.55 + cpu * 0.5)
      const hue = 188 + p.v * 120 // cyan -> violet -> magenta around the tube
      const light = 45 + depth * 35
      ctx.fillStyle = `hsla(${hue},90%,${light}%,${Math.min(1, a)})`
      ctx.beginPath(); ctx.arc(p.x, p.y, 0.5 + depth * 1.6, 0, TAU); ctx.fill()
    }
    // bright core glow
    const g = ctx.createRadialGradient(cx, cy, 0, cx, cy, R * 0.8)
    g.addColorStop(0, `rgba(0,200,255,${0.06 + cpu * 0.1})`); g.addColorStop(1, 'rgba(0,0,0,0)')
    ctx.fillStyle = g; ctx.beginPath(); ctx.arc(cx, cy, R * 0.8, 0, TAU); ctx.fill()
    ctx.globalCompositeOperation = 'source-over'
  }
  return <canvas ref={ref} className="absolute inset-0 h-full w-full" />
}

const GLOW: Record<string, string> = { cyan: '#00e5ff', violet: '#b794f4', amber: '#ffaa3b' }
function Row({ label, value, pct, color }: { label: string; value: string; pct: number; color: 'cyan' | 'violet' | 'amber' }) {
  const fill = { cyan: 'from-cyan-dim to-cyan', violet: 'from-violet-dim to-violet', amber: 'from-[#b06c1a] to-amber' }[color]
  const txt = { cyan: 'text-cyan', violet: 'text-violet', amber: 'text-amber' }[color]
  const w = Math.max(0, Math.min(100, pct))
  return (
    <div className="mono grid grid-cols-[58px_1fr_auto] items-center gap-2.5">
      <span className="text-[9px] uppercase tracking-[0.12em] text-faint">{label}</span>
      <span className="relative h-[5px] overflow-hidden rounded-full bg-[rgba(120,140,170,0.1)]">
        {/* segment ticks */}
        <span className="absolute inset-0 opacity-40" style={{ backgroundImage: 'repeating-linear-gradient(90deg,transparent 0,transparent 7px,rgba(6,8,13,0.9) 7px,rgba(6,8,13,0.9) 8px)' }} />
        <span
          className={`relative block h-full rounded-full bg-gradient-to-r ${fill} transition-[width] duration-500 ease-out`}
          style={{ width: `${w}%`, boxShadow: `0 0 8px ${GLOW[color]}, inset 0 0 4px rgba(255,255,255,0.3)` }}
        />
      </span>
      <span className={`min-w-[48px] text-right text-[11px] font-bold ${txt}`} style={{ textShadow: `0 0 8px ${GLOW[color]}66` }}>{value}</span>
    </div>
  )
}

export default function SystemMetrics() {
  const m = useHaloStore((s) => s.metrics)
  const gpu = (m.gpu_pct || 0) > 0 || (m.gpu_temp_c || 0) > 0
  const ctxK = Math.round((m.context_tokens || 0) / 1000)
  return (
    <HologramPanel className="grid min-h-0 grid-cols-[120px_1fr] gap-3" delay={0.05}>
      <div className="flex min-h-0 flex-col">
        <div className="ptitle">Metrics</div>
        <div className="relative min-h-[70px] flex-1"><Torus /></div>
        <div className="mono truncate text-center text-[8px] tracking-[0.08em] text-faint">{m.gpu_name ? m.gpu_name.slice(0, 22) : 'WebGPU · halo-voice'}</div>
      </div>
      <div className="flex flex-col justify-around gap-1">
        <Row label="CPU" value={`${(m.cpu_pct || 0).toFixed(0)}%`} pct={m.cpu_pct || 0} color="cyan" />
        <Row label="Memory" value={`${(m.mem_used_gb || 0).toFixed(1)} GB`} pct={m.mem_pct || 0} color="violet" />
        <Row label="GPU" value={gpu ? `${(m.gpu_pct || 0).toFixed(0)}%` : '—'} pct={gpu ? m.gpu_pct || 0 : 0} color="amber" />
        <Row label="Tokens/s" value={`${(m.tokens_per_sec || 0).toFixed(1)}`} pct={((m.tokens_per_sec || 0) / 50) * 100} color="cyan" />
        <Row label="Context" value={`${ctxK}K`} pct={(ctxK / 200) * 100} color="violet" />
        <Row label="Temp" value={gpu ? `${(m.gpu_temp_c || 0).toFixed(0)}°C` : '—'} pct={gpu ? m.gpu_temp_c || 0 : 0} color="amber" />
      </div>
    </HologramPanel>
  )
}
