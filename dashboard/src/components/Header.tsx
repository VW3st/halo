import { useEffect, useRef } from 'react'
import { motion } from 'motion/react'
import { useHaloStore } from '../store/voiceHarnessStore'
import { resetSessions } from '../lib/api'

function Ecg() {
  const ref = useRef<HTMLCanvasElement>(null)
  useEffect(() => {
    const c = ref.current!
    const ctx = c.getContext('2d')!
    let raf = 0
    const draw = () => {
      const dpr = window.devicePixelRatio || 1
      const w = c.clientWidth, h = c.clientHeight
      if (c.width !== w * dpr) { c.width = w * dpr; c.height = h * dpr; ctx.setTransform(dpr, 0, 0, dpr, 0, 0) }
      ctx.clearRect(0, 0, w, h)
      const mid = h * 0.5, period = w / 2, shift = performance.now() * 0.09
      ctx.strokeStyle = 'rgba(0,229,255,0.85)'; ctx.lineWidth = 1.3; ctx.shadowColor = '#00e5ff'; ctx.shadowBlur = 5
      ctx.beginPath()
      for (let x = 0; x <= w; x++) {
        const u = ((x + shift) % period) / period
        let y = mid
        y -= Math.exp(-Math.pow((u - 0.2) / 0.03, 2)) * h * 0.12
        y += Math.exp(-Math.pow((u - 0.45) / 0.012, 2)) * h * 0.06
        y -= Math.exp(-Math.pow((u - 0.5) / 0.012, 2)) * h * 0.44
        y += Math.exp(-Math.pow((u - 0.55) / 0.015, 2)) * h * 0.14
        y -= Math.exp(-Math.pow((u - 0.72) / 0.045, 2)) * h * 0.15
        x === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y)
      }
      ctx.stroke(); ctx.shadowBlur = 0
      raf = requestAnimationFrame(draw)
    }
    draw()
    return () => cancelAnimationFrame(raf)
  }, [])
  return <canvas ref={ref} className="h-[18px] w-[90px]" />
}

export default function Header() {
  const modeDirect = useHaloStore((s) => s.modeDirect)
  const metrics = useHaloStore((s) => s.metrics)
  const lastStage2Ms = useHaloStore((s) => s.lastStage2Ms)

  const health = Math.max(
    30,
    Math.min(100, 100 - (metrics.cpu_pct || 0) * 0.5 - ((metrics.gpu_temp_c || 0) > 60 ? (metrics.gpu_temp_c || 0) - 60 : 0)),
  )
  const healthClass = health < 60 ? 'text-rose' : health < 80 ? 'text-amber' : 'text-cyan'

  return (
    <motion.header
      initial={{ opacity: 0, y: -8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.5 }}
      className="grid grid-cols-[1fr_auto_1fr] items-center rounded border border-edge bg-gradient-to-b from-[rgba(11,17,30,0.85)] to-[rgba(11,17,30,0.4)] px-4"
    >
      <div className="flex items-center gap-3.5">
        <svg viewBox="0 0 32 32" className="h-7 w-7 drop-shadow-[0_0_10px_rgba(0,229,255,0.6)]">
          <circle cx="16" cy="16" r="13" stroke="#00e5ff" strokeWidth="1.5" opacity="0.4" fill="none" />
          <circle cx="16" cy="16" r="8" stroke="#00e5ff" strokeWidth="1.5" fill="none" />
          <path d="M8 16H24M16 8V24" stroke="#00e5ff" strokeWidth="1" opacity="0.5" />
          <circle cx="16" cy="16" r="3" fill="#00e5ff" />
        </svg>
        <div className="leading-[1.05]">
          <div className="mono text-[15px] font-bold tracking-[0.3em] text-[#e3eaf6] drop-shadow-[0_0_12px_rgba(0,229,255,0.4)]">HALO</div>
          <div className="mono text-[8px] tracking-[0.28em] text-faint">VOICE HARNESS · AI ORCHESTRATOR</div>
        </div>
        <div className="mono rounded border border-edge px-2 py-1 text-[10px] tracking-[0.16em] text-dim">
          <span className="mr-1.5 text-cyan">v2.0</span>NEBULA
        </div>
      </div>

      <div className="flex items-center justify-center gap-5">
        <motion.div
          animate={{ opacity: [0.6, 1, 0.6] }}
          transition={{ duration: 2, repeat: Infinity }}
          className={`mono flex items-center gap-2 text-[12px] uppercase tracking-[0.3em] ${
            modeDirect ? 'text-amber' : 'text-dim'
          }`}
        >
          <span
            className="h-2 w-2 rounded-full"
            style={{ background: 'currentColor', boxShadow: '0 0 8px currentColor' }}
          />
          {modeDirect ? `DIRECT · ${modeDirect.replace('_', ' ').toUpperCase()}` : 'COMMAND MODE'}
        </motion.div>
        <Ecg />
      </div>

      <div className="flex items-center justify-end gap-4">
        <div className="mono flex items-center gap-2 text-[10px] uppercase tracking-[0.12em] text-dim">
          <span className="text-faint">System</span>
          <span className={`text-[12px] font-bold ${healthClass}`}>{health.toFixed(0)}%</span>
          <span className="h-1.5 w-1.5 rounded-full bg-emerald shadow-[0_0_6px_#34d399]" />
        </div>
        <div className="mono flex items-center gap-2 text-[10px] uppercase tracking-[0.12em] text-dim">
          <span className="text-faint">Latency</span>
          <span className="text-[12px] font-bold text-cyan">{lastStage2Ms > 0 ? `${lastStage2Ms}ms` : '—'}</span>
        </div>
        <button
          onClick={resetSessions}
          title="Reset agent sessions"
          className="flex h-7 w-7 items-center justify-center rounded border border-[#1a2638] text-dim transition-colors hover:border-cyan hover:text-cyan"
        >
          <svg viewBox="0 0 24 24" className="h-3.5 w-3.5" fill="none" stroke="currentColor" strokeWidth="2">
            <path d="M3 12a9 9 0 1 0 3-6.7" /><polyline points="3 4 3 10 9 10" />
          </svg>
        </button>
        <button
          onClick={() => (document.fullscreenElement ? document.exitFullscreen() : document.documentElement.requestFullscreen())}
          title="Fullscreen"
          className="flex h-7 w-7 items-center justify-center rounded border border-[#1a2638] text-dim transition-colors hover:border-cyan hover:text-cyan"
        >
          <svg viewBox="0 0 24 24" className="h-3.5 w-3.5" fill="none" stroke="currentColor" strokeWidth="2">
            <polyline points="4 9 4 4 9 4" /><polyline points="20 9 20 4 15 4" /><polyline points="4 15 4 20 9 20" /><polyline points="20 15 20 20 15 20" />
          </svg>
        </button>
      </div>
    </motion.header>
  )
}
