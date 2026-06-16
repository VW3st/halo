import { useEffect, useRef, useState } from 'react'
import HologramPanel from './HologramPanel'
import { useHaloStore } from '../store/voiceHarnessStore'

const TAU = Math.PI * 2

function Bloom() {
  const ref = useRef<HTMLCanvasElement>(null)
  const outRing = useRef(new Float32Array(720))
  const ptr = useRef(0)
  useEffect(() => {
    const c = ref.current!
    const ctx = c.getContext('2d')!
    let raf = 0
    const loop = () => {
      const t = performance.now()
      const speaking = useHaloStore.getState().speaking
      // push synth sample
      let v: number
      if (speaking) v = (Math.sin(t * 0.018) * 0.4 + Math.sin(t * 0.06 + 1.4) * 0.3 + Math.sin(t * 0.011 + 2.1) * 0.2) * (0.6 + Math.sin(t * 0.003) * 0.4)
      else v = 0.1 * Math.sin(t * 0.01) + 0.05 * Math.sin(t * 0.024 + 1) + 0.025 * Math.sin(t * 0.05 + 2)
      outRing.current[ptr.current] = v; ptr.current = (ptr.current + 1) % outRing.current.length
      const dpr = window.devicePixelRatio || 1
      const w = Math.round(c.clientWidth), h = Math.round(c.clientHeight)
      if (w && h && (c.width !== w * dpr || c.height !== h * dpr)) { c.width = w * dpr; c.height = h * dpr; ctx.setTransform(dpr, 0, 0, dpr, 0, 0) }
      if (w && h) draw(ctx, w, h, t, speaking)
      raf = requestAnimationFrame(loop)
    }
    const rings: { r: number; life: number }[] = []
    let lastRing = 0
    const draw = (ctx: CanvasRenderingContext2D, w: number, h: number, t: number, speaking: boolean) => {
      ctx.clearRect(0, 0, w, h)
      ctx.globalCompositeOperation = 'lighter'
      const cy = h * 0.5, N = outRing.current.length
      const col = speaking ? '255,170,59' : '0,185,255'
      const amp = h * 0.42
      // soft horizon glow band behind the wave
      const bg = ctx.createLinearGradient(0, 0, 0, h)
      bg.addColorStop(0, 'rgba(0,0,0,0)'); bg.addColorStop(0.5, `rgba(${col},${speaking ? 0.07 : 0.04})`); bg.addColorStop(1, 'rgba(0,0,0,0)')
      ctx.fillStyle = bg; ctx.fillRect(0, 0, w, h)
      // expanding pulse rings emanating from centre (life, not a static line)
      if (t - lastRing > (speaking ? 420 : 1400)) { lastRing = t; rings.push({ r: 6, life: 1 }) }
      for (let i = rings.length - 1; i >= 0; i--) {
        const rg = rings[i]; rg.r += speaking ? 2.6 : 1.4; rg.life -= 0.012
        if (rg.life <= 0) { rings.splice(i, 1); continue }
        ctx.strokeStyle = `rgba(${col},${rg.life * 0.18})`; ctx.lineWidth = 1
        ctx.beginPath(); ctx.ellipse(w / 2, cy, rg.r, rg.r * 0.42, 0, 0, TAU); ctx.stroke()
      }
      // smoothed sample helper (mild moving average for an organic envelope)
      const sm = (i: number) => {
        const a = outRing.current[(ptr.current + i) % N]
        const b = outRing.current[(ptr.current + i + 1) % N]
        const c = outRing.current[(ptr.current + Math.max(0, i - 1)) % N]
        return (a * 2 + b + c) / 4
      }
      // filled mirror envelope
      const grad = ctx.createLinearGradient(0, cy - amp, 0, cy + amp)
      grad.addColorStop(0, `rgba(${col},0)`); grad.addColorStop(0.5, `rgba(${col},0.32)`); grad.addColorStop(1, `rgba(${col},0)`)
      ctx.fillStyle = grad
      ctx.beginPath(); ctx.moveTo(0, cy)
      for (let i = 0; i < N; i++) ctx.lineTo((i / (N - 1)) * w, cy - sm(i) * amp)
      for (let i = N - 1; i >= 0; i--) ctx.lineTo((i / (N - 1)) * w, cy + sm(i) * amp)
      ctx.closePath(); ctx.fill()
      // bright glowing outline (top + bottom)
      for (const sign of [-1, 1]) {
        ctx.strokeStyle = `rgba(${col},0.9)`; ctx.lineWidth = 1.4; ctx.shadowColor = `rgb(${col})`; ctx.shadowBlur = 6
        ctx.beginPath()
        for (let i = 0; i < N; i++) { const x = (i / (N - 1)) * w, y = cy + sign * sm(i) * amp; i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y) }
        ctx.stroke()
      }
      ctx.shadowBlur = 0
      ctx.globalCompositeOperation = 'source-over'
    }
    loop()
    return () => cancelAnimationFrame(raf)
  }, [])
  return <canvas ref={ref} className="absolute inset-0 h-full w-full" />
}

function Spectrogram() {
  const ref = useRef<HTMLCanvasElement>(null)
  useEffect(() => {
    const BINS = 56, HIST = 220
    const off = document.createElement('canvas'); off.width = HIST; off.height = BINS
    const octx = off.getContext('2d')!
    const c = ref.current!
    const ctx = c.getContext('2d')!
    let raf = 0, last = 0
    const push = () => {
      octx.globalCompositeOperation = 'copy'; octx.drawImage(off, -1, 0); octx.globalCompositeOperation = 'source-over'
      const speaking = useHaloStore.getState().speaking, x = HIST - 1
      for (let yi = 0; yi < BINS; yi++) {
        let v: number
        if (speaking) {
          // two formant-ish bands for a realistic voiced spectrogram
          const b1 = Math.exp(-Math.pow((yi - BINS * 0.28) / 7, 2))
          const b2 = Math.exp(-Math.pow((yi - BINS * 0.55) / 10, 2)) * 0.7
          v = (b1 + b2) * (0.55 + Math.random() * 0.45)
        } else {
          v = 0.04 + Math.random() * 0.05
        }
        // heat ramp: dark blue (low) -> cyan -> yellow -> red (high) = real spectrogram
        const hue = 250 - v * 230
        const light = 12 + v * 55
        octx.fillStyle = `hsl(${hue},95%,${light}%)`
        octx.fillRect(x, BINS - 1 - yi, 1, 1)
      }
    }
    const loop = (t: number) => {
      if (t - last > 80) { push(); last = t }
      const dpr = window.devicePixelRatio || 1
      const w = Math.round(c.clientWidth), h = Math.round(c.clientHeight)
      if (w && h && (c.width !== w * dpr || c.height !== h * dpr)) { c.width = w * dpr; c.height = h * dpr; ctx.setTransform(dpr, 0, 0, dpr, 0, 0) }
      if (w && h) { ctx.clearRect(0, 0, w, h); ctx.imageSmoothingEnabled = true; ctx.drawImage(off, 0, 0, w, h) }
      raf = requestAnimationFrame(loop)
    }
    raf = requestAnimationFrame(loop)
    return () => cancelAnimationFrame(raf)
  }, [])
  return <canvas ref={ref} className="absolute inset-0 h-full w-full" />
}

function VolRing({ pct }: { pct: number }) {
  const ref = useRef<HTMLCanvasElement>(null)
  useEffect(() => {
    const c = ref.current!, ctx = c.getContext('2d')!
    const dpr = window.devicePixelRatio || 1
    const w = Math.round(c.clientWidth), h = Math.round(c.clientHeight)
    c.width = w * dpr; c.height = h * dpr; ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
    ctx.clearRect(0, 0, w, h)
    const cx = w / 2, cy = h / 2, R = Math.min(w, h) * 0.38
    ctx.strokeStyle = 'rgba(26,38,56,0.7)'; ctx.lineWidth = 2; ctx.beginPath(); ctx.arc(cx, cy, R, 0, TAU); ctx.stroke()
    ctx.strokeStyle = 'rgba(0,229,255,0.85)'; ctx.lineWidth = 2; ctx.shadowColor = '#00e5ff'; ctx.shadowBlur = 6
    ctx.beginPath(); ctx.arc(cx, cy, R, -Math.PI / 2, -Math.PI / 2 + pct * TAU); ctx.stroke(); ctx.shadowBlur = 0
  }, [pct])
  return <canvas ref={ref} className="h-full w-full" />
}

export default function OutputPanel() {
  const speaking = useHaloStore((s) => s.speaking)
  const [vol, setVol] = useState(0)
  useEffect(() => {
    const id = setInterval(() => setVol(useHaloStore.getState().speaking ? 0.6 + Math.random() * 0.3 : 0), 200)
    return () => clearInterval(id)
  }, [])
  return (
    <HologramPanel
      accent="amber"
      delay={0.15}
      className="flex min-h-0 flex-col"
      title="Output"
      right={
        <div className="mono text-right text-[11px] font-bold text-amber">
          {speaking ? `${-14 - Math.round((1 - vol) * 12)}` : '-∞'}
          <span className="block text-[8px] text-faint">LUFS</span>
        </div>
      }
    >
      <div className={`mono mt-1 flex items-center gap-1.5 text-[10px] uppercase tracking-[0.16em] ${speaking ? 'text-amber' : 'text-faint'}`}>
        <span className={`h-1.5 w-1.5 rounded-full ${speaking ? 'animate-blip bg-amber shadow-[0_0_6px_#ffaa3b]' : 'bg-faint'}`} />
        {speaking ? 'SPEAKING' : 'SILENT'}
      </div>
      <div className="relative mt-1.5 min-h-0 flex-1"><Bloom /></div>
      <div className="mt-1.5 grid grid-cols-[1fr_64px] items-center gap-2">
        <div className="relative h-[38px] overflow-hidden rounded-sm border border-[#121a29]">
          <span className="mono absolute left-1.5 top-1 z-10 text-[8px] tracking-[0.14em] text-faint">SPECTROGRAM</span>
          <span className="mono absolute right-1.5 top-1 z-10 text-[8px] tracking-[0.13em] text-emerald">LIVE</span>
          <Spectrogram />
        </div>
        <div className="relative h-[44px]">
          <VolRing pct={vol} />
          <div className="mono absolute inset-0 flex items-center justify-center text-[11px] font-bold text-cyan">{Math.round(vol * 100)}%</div>
        </div>
      </div>
    </HologramPanel>
  )
}
