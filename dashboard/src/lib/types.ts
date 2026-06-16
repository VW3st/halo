// ---- Backend event bus shapes (from halo/bus.py via /api/events) ----
export interface BusEvent {
  seq: number
  ts: number
  kind: string
  [k: string]: unknown
}

// ---- /api/state ----
export interface Agent {
  key: string
  spoken_name: string
  session_name: string
  session_active: boolean
  job_running: boolean
  job_elapsed_sec: number | null
  job_prompt: string | null
  installed: boolean
  responsive: boolean
  install_hint: string
}

// ---- /api/system-metrics ----
export interface Metrics {
  cpu_pct: number
  mem_used_gb: number
  mem_total_gb: number
  mem_pct: number
  gpu_pct: number
  gpu_temp_c: number
  gpu_mem_pct: number
  gpu_name: string
  tokens_per_sec: number
  context_tokens: number
  active_jobs: number
  session_count: number
  uptime_sec: number
}

// ---- Derived UI tool row ----
export interface ToolRow {
  name: string
  title: string
  sub: string
  color: 'cyan' | 'amber' | 'violet' | 'emerald' | 'warn' | 'bad'
  latency: string
  status: 'ok' | 'warn' | 'bad' | 'dim'
}

// ---- The five reasoning lobes ----
export type Lane = 'listen' | 'think' | 'tools' | 'memory' | 'speak'

export interface ToolTick {
  name: string
  ts: number
}
