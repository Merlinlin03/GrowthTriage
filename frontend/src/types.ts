export type RunStatus = {
  run_id: string
  status: 'queued' | 'running' | 'waiting_approval' | 'succeeded' | 'failed' | 'expired'
  execution_mode: 'live' | 'mixed' | 'fallback' | 'failed'
  error_code: string | null
  current_phase: string
  active_step_keys: string[]
  completed_steps: number
  total_steps: number
  pending_approval_id: string | null
  revision: number
  updated_at: string
  poll_after_ms: number
}

export type SessionInfo = {
  role: string
  workspace_id: string
  status: string
  expires_at: string
  hard_expires_at: string
  run_count: number
  run_limit: number
  active_run_id: string | null
  csrf_token?: string
}

export type RunResults = {
  run_id: string
  status: string
  execution_mode: string
  fixture: any
  source: 'golden' | 'upload'
  scope: { market: string; platform: string; campaign_id: string; creative_id: string } | null
  metrics: Record<string, { baseline: number | null; current: number | null; delta_pct: number | null }> | null
  raw_metrics: { baseline: Record<string, number | null>; current: Record<string, number | null> } | null
  feedback: any | null
  correlation: any | null
  finding: any | null
  evidence: Array<{ type: string; label: string; value: string }>
  actions: Array<{ title: string; risk_level: string; status: string; executed: boolean; details: any }>
  experiments: Array<{ title: string; primary_metric: string; guardrails: string[]; duration_days: number; validation_goal: string; stop_condition: string }>
  approval: any | null
  ticket: any | null
  report_ready: boolean
  external_writes: number
}
