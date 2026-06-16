import { motion } from 'motion/react'
import HologramPanel from './HologramPanel'
import { useHaloStore } from '../store/voiceHarnessStore'
import type { Agent } from '../lib/types'

const ACCENTS = ['#00e5ff', '#b794f4', '#ffaa3b', '#34d399', '#e070d0']

const GLYPH: Record<string, JSX.Element> = {
  claude_code: (<><circle cx="12" cy="12" r="9" /><path d="M9 9h6M9 12h4M9 15h6" /></>),
  codex_cli: (<><polyline points="8 6 3 12 8 18" /><polyline points="16 6 21 12 16 18" /></>),
  default: (<><circle cx="12" cy="12" r="5" /><circle cx="12" cy="12" r="9" opacity="0.4" /></>),
}

function cardClass(a: Agent) {
  if (!a.installed) return 'border-rose/30 opacity-65'
  if (!a.responsive) return 'border-amber/35'
  if (a.job_running) return 'border-cyan bg-[rgba(0,229,255,0.06)] shadow-[0_0_12px_rgba(0,229,255,0.18)]'
  if (a.session_active) return 'border-emerald/35'
  return 'border-[#121a29]'
}

export default function AgentCards() {
  const agents = useHaloStore((s) => s.agents)
  const running = agents.filter((a) => a.job_running).length
  const slots = [...agents]
  const placeholders = Math.max(0, 5 - slots.length)

  return (
    <HologramPanel
      accent="violet"
      title="Active Agents"
      delay={0.15}
      className="flex min-h-0 flex-col"
      right={<span className="mono text-[9px] tracking-[0.14em] text-faint"><b className="text-violet">{running}</b> RUNNING</span>}
    >
      <div className="mt-2 grid grid-cols-5 gap-2">
        {slots.map((a, i) => {
          const acc = ACCENTS[i % 5]
          const busy = a.job_running
          return (
            <motion.div
              key={a.key}
              initial={{ opacity: 0, scale: 0.85 }}
              animate={busy ? { opacity: 1, scale: 1, boxShadow: ['0 0 8px rgba(0,229,255,0.18)', '0 0 18px rgba(0,229,255,0.32)', '0 0 8px rgba(0,229,255,0.18)'] } : { opacity: 1, scale: 1 }}
              transition={busy ? { duration: 1.6, repeat: Infinity } : { duration: 0.4, delay: 0.2 + i * 0.05 }}
              title={a.job_prompt || a.install_hint || a.session_name || a.spoken_name}
              className={`flex aspect-square max-h-[92px] flex-col items-center justify-center gap-1.5 rounded-md border bg-[rgba(10,14,23,0.5)] p-1.5 ${cardClass(a)}`}
            >
              <span
                className="flex h-[30px] w-[30px] items-center justify-center rounded-full"
                style={{ color: acc, border: `1px solid ${acc}55`, boxShadow: `0 0 10px ${acc}33` }}
              >
                <svg viewBox="0 0 24 24" className="h-[17px] w-[17px]" fill="none" stroke="currentColor" strokeWidth="1.5">
                  {GLYPH[a.key] || GLYPH.default}
                </svg>
              </span>
              <span className={`mono max-w-full truncate text-[8px] uppercase tracking-[0.08em] ${busy ? 'text-cyan' : 'text-dim'}`}>
                {(a.session_name || a.spoken_name || '?').slice(0, 8)}
              </span>
            </motion.div>
          )
        })}
        {Array.from({ length: placeholders }).map((_, i) => {
          const acc = ACCENTS[(slots.length + i) % 5]
          return (
            <div key={`ph${i}`} className="flex aspect-square max-h-[92px] flex-col items-center justify-center gap-1.5 rounded-md border border-[#121a29] bg-[rgba(10,14,23,0.5)] p-1.5 opacity-40">
              <span className="flex h-[30px] w-[30px] items-center justify-center rounded-full" style={{ color: `${acc}99`, border: `1px solid ${acc}33` }}>
                <svg viewBox="0 0 24 24" className="h-[17px] w-[17px]" fill="none" stroke="currentColor" strokeWidth="1.5">{GLYPH.default}</svg>
              </span>
              <span className="mono text-[8px] text-dim">—</span>
            </div>
          )
        })}
      </div>
    </HologramPanel>
  )
}
