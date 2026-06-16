import { useEffect, useRef } from 'react'
import { audio } from '../lib/audioAnalyser'

const TAU = Math.PI * 2

// rAF canvas hook with HiDPI handling; draw(ctx,w,h,t)
function useCanvas(draw: (ctx: CanvasRenderingContext2D, w: number, h: number, t: number) => void) {
  const ref = useRef<HTMLCanvasElement>(null)
  useEffect(() => {
    const c = ref.current!
    const ctx = c.getContext('2d')!
    let raf = 0
    const loop = () => {
      const dpr = window.devicePixelRatio || 1
      const w = Math.round(c.clientWidth), h = Math.round(c.clientHeight)
      if (w && h && (c.width !== w * dpr || c.height !== h * dpr)) {
        c.width = w * dpr; c.height = h * dpr; ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
      }
      if (w && h) draw(ctx, w, h, performance.now())
      raf = requestAnimationFrame(loop)
    }
    loop()
    return () => cancelAnimationFrame(raf)
  }, [draw])
  return ref
}

export function MicDisc() {
  const ref = useCanvas((ctx, w, h, t) => {
    ctx.clearRect(0, 0, w, h)
    const cx = w / 2, cy = h / 2, R0 = Math.min(w, h) * 0.30
    const acc = audio.access ? '0,229,255' : '255,170,59'
    const glow = audio.access ? '#00e5ff' : '#ffaa3b'
    ctx.globalCompositeOperation = 'lighter'
    ctx.strokeStyle = `rgba(${acc},0.10)`; ctx.lineWidth = 1
    ctx.beginPath(); ctx.arc(cx, cy, R0 * 1.32, 0, TAU); ctx.stroke()
    ctx.strokeStyle = `rgba(${acc},0.16)`; ctx.beginPath(); ctx.arc(cx, cy, R0, 0, TAU); ctx.stroke()
    const ticks = 48, tR = R0 * 1.32
    for (let i = 0; i < ticks; i++) {
      const ang = (i / ticks) * TAU, lg = i % 4 === 0, r1 = tR, r2 = tR + (lg ? 6 : 3)
      ctx.strokeStyle = `rgba(${acc},${lg ? 0.5 : 0.22})`; ctx.lineWidth = 1
      ctx.beginPath(); ctx.moveTo(cx + Math.cos(ang) * r1, cy + Math.sin(ang) * r1); ctx.lineTo(cx + Math.cos(ang) * r2, cy + Math.sin(ang) * r2); ctx.stroke()
    }
    const sweep = (t * 0.0012) % TAU
    ctx.strokeStyle = `rgba(${acc},0.5)`; ctx.lineWidth = 2; ctx.shadowColor = glow; ctx.shadowBlur = 6
    ctx.beginPath(); ctx.arc(cx, cy, R0 * 1.16, sweep, sweep + 0.7); ctx.stroke(); ctx.shadowBlur = 0
    // smooth frequency ring (neighbour-averaged so it reads as a soft pulse,
    // not a spiky mess); filled toward the centre for a glassy aperture look
    const N = 128, amp = Math.min(w, h) * 0.12
    const rad = (i: number): number => {
      let v: number
      if (audio.freqData) {
        const idx = Math.floor(2 + (i / N) * 170)
        const a = audio.freqData[idx] || 0, b = audio.freqData[idx + 1] || 0, c = audio.freqData[Math.max(0, idx - 1)] || 0
        v = (a * 2 + b + c) / 4 / 255
      } else {
        v = 0.12 + 0.07 * Math.sin(t * 0.002 + i * 0.2) + 0.03 * Math.sin(t * 0.005 + i * 0.5)
      }
      return R0 + v * amp
    }
    ctx.fillStyle = `rgba(${acc},0.06)`
    ctx.beginPath()
    for (let i = 0; i <= N; i++) { const ang = (i / N) * TAU - Math.PI / 2, r = rad(i); const x = cx + Math.cos(ang) * r, y = cy + Math.sin(ang) * r; i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y) }
    ctx.closePath(); ctx.fill()
    ctx.strokeStyle = `rgba(${acc},${audio.access ? 0.85 : 0.5})`; ctx.lineWidth = 1.5; ctx.shadowColor = glow; ctx.shadowBlur = 7
    ctx.beginPath()
    for (let i = 0; i <= N; i++) { const ang = (i / N) * TAU - Math.PI / 2, r = rad(i); const x = cx + Math.cos(ang) * r, y = cy + Math.sin(ang) * r; i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y) }
    ctx.closePath(); ctx.stroke(); ctx.shadowBlur = 0
    // clean horizontal waveform across the disc (tapered, glowing)
    const span = R0 * 1.6, M = 110
    ctx.strokeStyle = `rgba(${acc},0.9)`; ctx.lineWidth = 1.4; ctx.shadowColor = glow; ctx.shadowBlur = 6
    ctx.beginPath()
    for (let i = 0; i <= M; i++) {
      const u = i / M, x = cx - span / 2 + u * span
      let v: number
      if (audio.timeData) v = (audio.timeData[Math.floor(u * (audio.timeData.length - 1))] - 128) / 128
      else v = 0.26 * Math.sin(u * Math.PI * 7 + t * 0.005) + 0.13 * Math.sin(u * Math.PI * 15 + t * 0.011)
      const y = cy + v * R0 * 0.7 * Math.sin(u * Math.PI)
      i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y)
    }
    ctx.stroke(); ctx.shadowBlur = 0
    ctx.globalCompositeOperation = 'source-over'
  })
  return <canvas ref={ref} className="absolute inset-0 h-full w-full" />
}

export function TimeDomain() {
  const ring = useRef(new Float32Array(900))
  const ptr = useRef(0)
  const ref = useCanvas((ctx, w, h, t) => {
    // push sample
    let peak = 0
    if (audio.timeData) {
      for (let i = 0; i < audio.timeData.length; i++) { const v = (audio.timeData[i] - 128) / 128; if (Math.abs(v) > Math.abs(peak)) peak = v }
    } else {
      peak = 0.22 * Math.sin(t * 0.004) + 0.11 * Math.sin(t * 0.011 + 1.2) + 0.05 * Math.sin(t * 0.027 + 0.5) + 0.03 * (Math.random() - 0.5)
    }
    ring.current[ptr.current] = peak; ptr.current = (ptr.current + 1) % ring.current.length
    ctx.clearRect(0, 0, w, h)
    const mid = h / 2, N = ring.current.length
    ctx.strokeStyle = 'rgba(0,180,255,0.12)'; ctx.lineWidth = 1; ctx.setLineDash([3, 5])
    ctx.beginPath(); ctx.moveTo(0, mid); ctx.lineTo(w, mid); ctx.stroke(); ctx.setLineDash([])
    ctx.globalCompositeOperation = 'lighter'
    const grad = ctx.createLinearGradient(0, 0, 0, h)
    grad.addColorStop(0, 'rgba(0,229,255,0)'); grad.addColorStop(0.5, 'rgba(0,229,255,0.22)'); grad.addColorStop(1, 'rgba(0,229,255,0)')
    ctx.fillStyle = grad; ctx.beginPath(); ctx.moveTo(0, mid)
    for (let i = 0; i < N; i++) { const v = ring.current[(ptr.current + i) % N]; ctx.lineTo((i / (N - 1)) * w, mid - v * (mid - 3)) }
    for (let i = N - 1; i >= 0; i--) { const v = ring.current[(ptr.current + i) % N]; ctx.lineTo((i / (N - 1)) * w, mid + v * (mid - 3)) }
    ctx.closePath(); ctx.fill()
    ctx.strokeStyle = 'rgba(130,238,255,0.95)'; ctx.lineWidth = 1.4; ctx.shadowColor = '#00e5ff'; ctx.shadowBlur = 5
    ctx.beginPath()
    for (let i = 0; i < N; i++) { const v = ring.current[(ptr.current + i) % N]; const x = (i / (N - 1)) * w, y = mid - v * (mid - 3); i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y) }
    ctx.stroke(); ctx.shadowBlur = 0
    ctx.globalCompositeOperation = 'source-over'
  })
  return <canvas ref={ref} className="min-h-0 w-full flex-1" />
}

export function Fft() {
  const ref = useCanvas((ctx, w, h, t) => {
    ctx.clearRect(0, 0, w, h)
    ctx.globalCompositeOperation = 'lighter'
    const BAR = Math.min(96, Math.floor(w / 5)), bw = w / BAR
    for (let i = 0; i < BAR; i++) {
      let v: number
      if (audio.freqData) {
        const N = audio.freqData.length
        const a = Math.pow(i / BAR, 2.2) * N * 0.45, b = Math.pow((i + 1) / BAR, 2.2) * N * 0.45 + 1
        let mx = 0; for (let j = Math.floor(a); j < Math.min(b, N); j++) if (audio.freqData[j] > mx) mx = audio.freqData[j]
        v = mx / 255
      } else {
        const env = 0.45 + 0.55 * Math.exp(-Math.pow((i - BAR * 0.22) / (BAR * 0.5), 2))
        v = Math.max(0.02, env * (0.3 + 0.16 * Math.sin(t * 0.0018 + i * 0.45) + 0.1 * Math.sin(t * 0.0045 + i * 1.2) + 0.06 * Math.sin(t * 0.009 + i * 2.1)))
      }
      const bh = v * (h - 3), x = i * bw, hue = 140 + (i / BAR) * 140
      const g = ctx.createLinearGradient(0, h, 0, h - bh)
      g.addColorStop(0, `hsla(${hue},85%,42%,0.25)`); g.addColorStop(1, `hsla(${hue},100%,64%,0.95)`)
      ctx.fillStyle = g
      const x0 = Math.round(x) + 0.5, bwr = Math.max(1.5, bw - 2.2)
      ctx.fillRect(x0, h - bh, bwr, bh)
      ctx.fillStyle = `hsla(${hue},100%,78%,0.95)`; ctx.fillRect(x0, h - bh - 1.5, bwr, 1.8)
    }
    ctx.globalCompositeOperation = 'source-over'
  })
  return <canvas ref={ref} className="min-h-0 w-full flex-1" />
}
