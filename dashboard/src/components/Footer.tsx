import { useEffect, useState } from 'react'
import { useHaloStore } from '../store/voiceHarnessStore'

function clock(start: number | null) {
  if (!start) return '00:00:00'
  const e = Math.floor((Date.now() - start) / 1000)
  return [Math.floor(e / 3600), Math.floor((e % 3600) / 60), e % 60].map((n) => String(n).padStart(2, '0')).join(':')
}

export default function Footer() {
  const connected = useHaloStore((s) => s.connected)
  const convoStartTs = useHaloStore((s) => s.convoStartTs)
  const [now, setNow] = useState('00:00:00')
  useEffect(() => {
    const id = setInterval(() => setNow(clock(useHaloStore.getState().convoStartTs)), 1000)
    return () => clearInterval(id)
  }, [convoStartTs])

  const dot = 'h-[5px] w-[5px] rounded-full'
  return (
    <footer className="mono flex items-center justify-between px-4 text-[9px] uppercase tracking-[0.2em] text-faint">
      <div className="flex items-center gap-6">
        <div className="flex items-center gap-1.5"><span className={`${dot} bg-emerald shadow-[0_0_4px_#34d399]`} /><span className="text-dim">Secure Session</span></div>
        <div className="flex items-center gap-1.5"><span className={`${dot} bg-cyan shadow-[0_0_4px_#00e5ff]`} /><span className="text-dim">AES-256</span></div>
        <div className="flex items-center gap-1.5"><span className={`${dot} ${connected ? 'bg-emerald shadow-[0_0_4px_#34d399]' : 'bg-rose shadow-[0_0_4px_#ff5d6c]'}`} /><span className="text-dim">{connected ? 'Dashboard Live' : 'Disconnected'}</span></div>
      </div>
      <div className="flex items-center gap-3.5">
        <span className="text-dim">Session</span><span className="text-cyan">{now}</span>
        <button title="stop" className="flex h-[18px] w-[18px] items-center justify-center rounded-full border border-[#1a2638] text-dim transition-colors hover:border-cyan hover:text-cyan"><svg viewBox="0 0 24 24" className="h-2 w-2" fill="currentColor"><rect x="6" y="6" width="12" height="12" rx="1" /></svg></button>
        <button title="pause" className="flex h-[18px] w-[18px] items-center justify-center rounded-full border border-[#1a2638] text-dim transition-colors hover:border-cyan hover:text-cyan"><svg viewBox="0 0 24 24" className="h-2 w-2" fill="currentColor"><rect x="6" y="5" width="4" height="14" /><rect x="14" y="5" width="4" height="14" /></svg></button>
        <button title="record" className="flex h-[18px] w-[18px] items-center justify-center rounded-full border border-amber/45"><span className="h-[7px] w-[7px] animate-blip rounded-full bg-amber shadow-[0_0_6px_#ffaa3b]" /></button>
      </div>
      <div className="flex items-center gap-6">
        <div className="flex items-center gap-1.5"><span className={`${dot} bg-cyan shadow-[0_0_4px_#00e5ff]`} /><span className="text-dim">Mode Realtime</span></div>
        <div className="flex items-center gap-1.5"><span className={`${dot} bg-amber shadow-[0_0_4px_#ffaa3b]`} /><span className="text-dim">Built for agents</span></div>
      </div>
    </footer>
  )
}
