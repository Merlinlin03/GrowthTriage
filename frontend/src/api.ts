const readCookie = (name: string) => {
  const item = document.cookie.split('; ').find((entry) => entry.startsWith(`${name}=`))
  return item ? decodeURIComponent(item.split('=').slice(1).join('=')) : ''
}

export class ApiError extends Error {
  code: string
  status: number
  fieldErrors: Array<{ code: string; file: string; row: number | null; field: string; message: string }>
  constructor(code: string, message: string, status: number, fieldErrors: Array<{ code: string; file: string; row: number | null; field: string; message: string }> = []) {
    super(message)
    this.code = code
    this.status = status
    this.fieldErrors = fieldErrors
  }
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const method = (init.method || 'GET').toUpperCase()
  const headers = new Headers(init.headers)
  if (init.body && !(init.body instanceof FormData)) headers.set('Content-Type', 'application/json')
  if (!['GET', 'HEAD', 'OPTIONS'].includes(method)) {
    const csrf = readCookie('gt_csrf')
    if (csrf) headers.set('X-CSRF-Token', csrf)
  }
  const response = await fetch(path, { ...init, headers, credentials: 'same-origin' })
  if (!response.ok) {
    const body = await response.json().catch(() => ({ error: { code: 'NETWORK_ERROR', message: '请求失败' } }))
    throw new ApiError(body.error?.code || 'HTTP_ERROR', body.error?.message || '请求失败', response.status, body.error?.field_errors || [])
  }
  const body = await response.json()
  return body.data as T
}

export const createGuest = () => api<any>('/api/v1/sessions/guest', { method: 'POST' })
export const getSession = () => api<any>('/api/v1/session')
export const listRuns = () => api<Array<{ run_id: string; status: string; created_at: string }>>('/api/v1/runs')
export const startGolden = () => api<any>('/api/v1/runs/golden', { method: 'POST', headers: { 'Idempotency-Key': crypto.randomUUID() } })
export const uploadDataset = (feedback: File, metrics: File) => {
  const body = new FormData()
  body.append('feedback', feedback)
  body.append('ad_metrics', metrics)
  body.append('consent', 'true')
  return api<{ dataset_id: string; feedback_rows: number; metric_rows: number; warnings: Array<{ code: string; file: string; row: number | null; message: string }> }>('/api/v1/datasets', { method: 'POST', body })
}
export const startDataset = (datasetId: string) => api<{ run_id: string }>(`/api/v1/datasets/${datasetId}/runs`, { method: 'POST', headers: { 'Idempotency-Key': crypto.randomUUID() } })
export const getStatus = (runId: string) => api<any>(`/api/v1/runs/${runId}/status`)
export const getResults = (runId: string) => api<any>(`/api/v1/runs/${runId}/results`)
export const getTrace = (runId: string) => api<any>(`/api/v1/runs/${runId}/trace`)
export const getApproval = (id: string) => api<any>(`/api/v1/approvals/${id}`)
export const decideApproval = (id: string, decision: 'approved' | 'rejected', comment: string) => api<any>(`/api/v1/approvals/${id}/decision`, { method: 'POST', body: JSON.stringify({ decision, comment, decision_key: crypto.randomUUID() }) })
export const adminLogin = (password: string) => api<any>('/api/v1/admin/login', { method: 'POST', body: JSON.stringify({ password }) })
export const adminHealth = () => api<any>('/api/v1/admin/health')
export const adminModelProbe = () => api<{ configured: boolean; reachable: boolean; actual_model: string | null; latency_ms: number; error_code: string | null }>('/api/v1/admin/model/probe', { method: 'POST' })
export const adminWorkspaces = () => api<Array<{ workspace_id: string; status: string; expires_at: string }>>('/api/v1/admin/workspaces')
export const adminRuns = () => api<Array<{ run_id: string; status: string; execution_mode: string; error_code: string | null }>>('/api/v1/admin/runs')
export const adminCleanup = () => api<any>('/api/v1/admin/cleanup', { method: 'POST' })
export const adminDeleteWorkspace = (id: string) => api<any>(`/api/v1/admin/workspaces/${id}`, { method: 'DELETE', headers: { 'X-Confirm-Workspace-Id': id } })
export const logout = () => api<any>('/api/v1/session', { method: 'DELETE' })
