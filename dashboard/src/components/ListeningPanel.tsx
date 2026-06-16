import { useEffect, useState } from 'react'
import HologramPanel from './HologramPanel'
import { MicDisc, TimeDomain, Fft } from './AudioWaveform'
import { audio } from '../lib/audioAnalyser'
import { useHaloStore } from '../store/voiceHarnessStore'

function useAudioScalars() {
  const [s, setS] = useState({ db: -Infinity, va: 0 })
  useEffect(() => {
    const id = setInterval(() => setS({ db: audio.micDb, va: audio.voiceActivity }), 100)
    return () => clearInterval(id)
  }, [])
  return s
}

export function ListeningPanel() {
  const mic = useHaloStore((s) => s.mic)
  const { db, va } = useAudioScalars()
  return (
    <HologramPanel className="flex min-h-0 flex-col" delay={0.05}>
      <div className="flex items-start justify-between">
        <div>
          <div className="ptitle">Listening</div>
          <div className="psub mt-0.5 flex items-center gap-1.5">
            <span className="h-1 w-1 rounded-full bg-cyan shadow-[0_0_6px_#00e5ff]" />MIC INPUT STREAM
          </div>
        </div>
        <div className="mono text-right text-[11px] text-cyan">
          <span className="block text-[8px] tracking-[0.15em] text-faint">LEVEL</span>
          {isFinite(db) ? `${db.toFixed(0)} dB` : '-∞ dB'}
        </div>
      </div>

      <div className={`relative flex min-h-0 flex-1 items-center justify-center ${!mic.access ? 'is-blocked' : ''}`}>
        <MicDisc />
        <svg
          viewBox="0 0 24 24"
          className={`relative z-[2] h-[34px] w-[34px] drop-shadow-[0_0_12px_rgba(0,229,255,0.55)] ${mic.access ? 'text-cyan' : 'text-amber'}`}
          fill="none" stroke="currentColor" strokeWidth="1.5"
        >
          <rect x="9" y="2" width="6" height="12" rx="3" /><path d="M5 11a7 7 0 0 0 14 0" /><line x1="12" y1="18" x2="12" y2="22" /><line x1="8" y1="22" x2="16" y2="22" />
        </svg>
        {!mic.access && (
          <button
            onClick={() => audio.enable(true)}
            className="absolute inset-1.5 z-[5] flex cursor-pointer flex-col items-center justify-center gap-2 rounded border border-dashed border-amber bg-[rgba(6,8,13,0.86)] p-3.5 text-center transition-colors hover:border-cyan"
          >
            <span className="mono text-[10px] uppercase tracking-[0.2em] text-amber">
              {mic.denied ? 'MIC ACCESS DENIED' : 'CLICK TO ENABLE MIC'}
            </span>
            <span className="mono max-w-[230px] text-[9px] leading-snug text-dim">
              {mic.denied ? 'Allow the mic in your browser site settings, then reload.' : 'Browser needs permission to read your microphone.'}
            </span>
          </button>
        )}
      </div>

      <div className="mt-2 flex flex-col gap-[3px]">
        <div className="mono flex justify-between text-[9px] uppercase tracking-[0.13em] text-faint">
          <span>Voice Activity</span><span className="text-cyan">{(va * 100).toFixed(0)}%</span>
        </div>
        <div className="h-1 overflow-hidden rounded-sm bg-[rgba(0,229,255,0.08)]">
          <div
            className="h-full rounded-sm bg-gradient-to-r from-[rgba(0,229,255,0.3)] to-cyan shadow-[0_0_8px_rgba(0,229,255,0.7)] transition-[width] duration-75"
            style={{ width: `${va * 100}%` }}
          />
        </div>
      </div>
    </HologramPanel>
  )
}

export function TimeDomainPanel() {
  return (
    <HologramPanel
      className="flex min-h-0 flex-col"
      delay={0.1}
      title="Time Domain"
      right={<span className="mono text-[9px] tracking-[0.15em] text-emerald">LIVE</span>}
    >
      <div className="mono flex justify-between text-[8px] tracking-[0.08em] text-faint"><span>1</span><span>-1</span></div>
      <TimeDomain />
      <div className="mono flex justify-between text-[8px] tracking-[0.08em] text-faint"><span>-6s</span><span>-4s</span><span>-2s</span><span>now</span></div>
    </HologramPanel>
  )
}

export function FftPanel() {
  return (
    <HologramPanel
      className="flex min-h-0 flex-col"
      delay={0.15}
      title="Frequency Analysis (FFT)"
      right={<span className="mono text-[9px] tracking-[0.15em] text-emerald">LIVE</span>}
    >
      <Fft />
      <div className="mono flex justify-between text-[8px] tracking-[0.08em] text-faint"><span>20</span><span>200</span><span>2k</span><span>20k</span></div>
      <div className="mono mt-1 flex justify-between text-[9px] uppercase tracking-[0.14em] text-faint">
        <span>NOISE GATE <span className="font-bold text-emerald">ON</span></span>
        <span>AGC <span className="font-bold text-emerald">ON</span></span>
        <span>VAD <span className="font-bold text-emerald">ON</span></span>
      </div>
    </HologramPanel>
  )
}
