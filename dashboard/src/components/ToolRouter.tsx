import { motion } from 'motion/react'
import HologramPanel from './HologramPanel'
import { useHaloStore } from '../store/voiceHarnessStore'
import type { Agent, ToolRow } from '../lib/types'

const STATIC_TOOLS: ToolRow[] = [
  { name: 'calculator', title: 'CALCULATOR', sub: 'system tool', color: 'amber', latency: '—', status: 'dim' },
  { name: 'browser', title: 'WEB BROWSER', sub: 'OS default · webbrowser', color: 'amber', latency: '—', status: 'dim' },
  { name: 'notepad', title: 'NOTEPAD', sub: 'text editor', color: 'amber', latency: '—', status: 'dim' },
  { name: 'explorer', title: 'FILE EXPLORER', sub: 'shell · open', color: 'violet', latency: '—', status: 'dim' },
  { name: 'terminal', title: 'TERMINAL', sub: 'powershell · cmd', color: 'violet', latency: '—', status: 'dim' },
  { name: 'open_file', title: 'OPEN FILE', sub: 'filesystem', color: 'emerald', latency: '—', status: 'dim' },
]

const ICONS: Record<string, JSX.Element> = {
  browser: (<><circle cx="12" cy="12" r="9" /><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18" /></>),
  calculator: (<><rect x="4" y="3" width="16" height="18" rx="2" /><rect x="7" y="6" width="10" height="3" /><circle cx="8.5" cy="14" r="1" /><circle cx="15.5" cy="14" r="1" /><circle cx="12" cy="17" r="1" /></>),
  notepad: (<><path d="M4 4h12l4 4v12H4z" /><path d="M14 4v4h6" /><path d="M8 13h8M8 17h6" /></>),
  explorer: <path d="M3 7l2-3h6l2 3h8v12H3z" />,
  terminal: (<><rect x="3" y="4" width="18" height="16" rx="2" /><path d="M7 9l3 3-3 3M13 15h4" /></>),
  open_file: (<><path d="M14 3H5v18h14V8z" /><path d="M14 3v5h5" /></>),
  claude_code: (<><circle cx="12" cy="12" r="9" /><path d="M9 9h6M9 12h4M9 15h6" /></>),
  codex_cli: (<><polyline points="8 6 3 12 8 18" /><polyline points="16 6 21 12 16 18" /></>),
  default: (<><circle cx="12" cy="12" r="5" /><circle cx="12" cy="12" r="9" opacity="0.4" /></>),
}

const ICON_BG: Record<string, string> = {
  cyan: 'bg-[rgba(0,229,255,0.08)] text-cyan', amber: 'bg-[rgba(255,170,59,0.08)] text-amber',
  violet: 'bg-[rgba(183,148,244,0.08)] text-violet', emerald: 'bg-[rgba(52,211,153,0.08)] text-emerald',
  warn: 'bg-[rgba(255,181,71,0.08)] text-amber', bad: 'bg-[rgba(255,93,108,0.08)] text-rose',
}
const DOT: Record<string, string> = {
  ok: 'bg-emerald shadow-[0_0_6px_#34d399]', warn: 'bg-amber shadow-[0_0_6px_#ffaa3b]',
  bad: 'bg-rose shadow-[0_0_6px_#ff5d6c]', dim: 'bg-faint',
}

function agentRow(a: Agent): ToolRow {
  return {
    name: a.key,
    title: String(a.spoken_name || 'agent').toUpperCase() + (a.session_active ? ` · ${(a.session_name || '').slice(0, 8)}` : ''),
    sub: a.installed ? (a.responsive ? 'CLI · ready' : 'CLI · needs login') : 'not installed',
    color: a.installed ? (a.responsive ? 'cyan' : 'warn') : 'bad',
    latency: a.job_running ? `${Math.round(a.job_elapsed_sec || 0)}s` : a.installed ? (a.responsive ? 'ready' : 'auth') : '—',
    status: !a.installed ? 'bad' : a.responsive ? (a.job_running ? 'warn' : 'ok') : 'warn',
  }
}

export default function ToolRouter() {
  const agents = useHaloStore((s) => s.agents)
  const rows: ToolRow[] = [...agents.map(agentRow), ...STATIC_TOOLS]
  const active = rows.filter((r) => r.status === 'ok').length
  return (
    <HologramPanel
      accent="amber"
      title="Tools"
      sub="MCP Router"
      delay={0.1}
      className="flex min-h-0 flex-col"
      right={<span className="mono text-[9px] tracking-[0.14em] text-faint"><b className="text-amber">{active}</b> ACTIVE</span>}
    >
      <ul className="mt-1.5 flex min-h-0 flex-1 list-none flex-col gap-1.5 overflow-y-auto p-0">
        {rows.map((r, i) => (
          <motion.li
            key={r.name + i}
            initial={{ opacity: 0, x: 8 }}
            animate={{ opacity: 1, x: 0 }}
            transition={{ delay: 0.15 + i * 0.03 }}
            className="grid grid-cols-[26px_1fr_auto_11px] items-center gap-2.5 rounded border border-[#121a29] bg-[rgba(10,14,23,0.55)] px-2.5 py-1.5"
          >
            <span className={`flex h-[23px] w-[23px] items-center justify-center rounded ${ICON_BG[r.color]}`}>
              <svg viewBox="0 0 24 24" className="h-[13px] w-[13px]" fill="none" stroke="currentColor" strokeWidth="2">
                {ICONS[r.name] || ICONS.default}
              </svg>
            </span>
            <span className="min-w-0">
              <span className="mono block truncate text-[11px] font-bold uppercase tracking-[0.06em] text-[#e3eaf6]">{r.title}</span>
              <span className="mono block truncate text-[9px] text-faint">{r.sub}</span>
            </span>
            <span className="mono text-right text-[10px] text-cyan">{r.latency}</span>
            <span className={`h-2 w-2 rounded-full ${DOT[r.status]}`} />
          </motion.li>
        ))}
      </ul>
    </HologramPanel>
  )
}
