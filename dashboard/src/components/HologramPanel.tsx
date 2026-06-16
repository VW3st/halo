import { motion } from 'motion/react'
import type { ReactNode } from 'react'

interface Props {
  title?: string
  sub?: string
  accent?: 'cyan' | 'amber' | 'violet'
  right?: ReactNode
  children: ReactNode
  className?: string
  delay?: number
}

const EDGE: Record<string, string> = {
  cyan: 'border-cyan/20',
  amber: 'border-amber/25',
  violet: 'border-violet/25',
}
const TITLE: Record<string, string> = {
  cyan: 'text-cyan',
  amber: 'text-amber',
  violet: 'text-violet',
}
const CORNER: Record<string, string> = {
  cyan: 'border-cyan',
  amber: 'border-amber',
  violet: 'border-violet',
}

export default function HologramPanel({ title, sub, accent = 'cyan', right, children, className = '', delay = 0 }: Props) {
  return (
    <motion.section
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.5, delay, ease: 'easeOut' }}
      className={`halo-panel relative rounded border ${EDGE[accent]} bg-[rgba(11,17,30,0.82)] p-3 overflow-hidden
        shadow-[inset_0_0_30px_rgba(0,229,255,0.03),0_0_22px_rgba(0,229,255,0.035)] ${className}`}
    >
      {/* corner brackets */}
      <span className={`pointer-events-none absolute left-1 top-1 h-3 w-3 border-l border-t ${CORNER[accent]} opacity-60`} />
      <span className={`pointer-events-none absolute bottom-1 right-1 h-3 w-3 border-b border-r ${CORNER[accent]} opacity-60`} />
      {(title || right) && (
        <div className="mb-1 flex items-center justify-between">
          {title && <span className={`mono uppercase ${TITLE[accent]} text-[10px] tracking-[0.22em]`}>{title}</span>}
          {right}
        </div>
      )}
      {sub && <div className="psub mb-1">{sub}</div>}
      {children}
    </motion.section>
  )
}
