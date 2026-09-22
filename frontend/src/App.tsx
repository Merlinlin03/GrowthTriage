import { api } from './api'
import { useCallback, useEffect, useState } from 'react'
import { ArrowRight, BadgeCheck, Ban, BarChart3, Bot, Braces, Check, CheckCircle2, ChevronRight, Clock3, Download, FileJson, FileText, FlaskConical, GitBranch, Info, LayoutDashboard, LoaderCircle, LockKeyhole, MessageSquareText, PauseCircle, Play, RefreshCw, ShieldAlert, ShieldCheck, Sparkles, TicketCheck, Upload, X } from './icons'
import { ApiError, adminCleanup, adminDeleteWorkspace, adminHealth, adminLogin, adminModelProbe, adminRuns, adminWorkspaces, createGuest, decideApproval, getApproval, getResults, getSession, getStatus, getTrace, listRuns, logout, startDataset, uploadDataset } from './api'
import type { RunResults, RunStatus, SessionInfo } from './types'

const steps = [
  ['validator', '数据校验', '规则计算'],
  ['snapshot', '事实快照', '规则计算'],
  ['feedback', '用户反馈分析 Agent', '模型分析'],
  ['performance', '广告指标诊断 Agent', '规则计算'],
  ['correlation', '联合证据分析 Agent', '证据 Join'],
  ['planner', '优化方案规划 Agent', '实验与行动'],
  ['auditor', '诊断审核 Agent', '规则校验'],
  ['policy_gate', 'Harness Policy Gate', 'R0–R3'],
  ['report_builder', '诊断报告 Agent', '脱敏导出'],
]

function useLocation() {
  const [location, setLocation] = useState({ pathname: window.location.pathname, search: window.location.search })
  useEffect(() => {
    const update = () => setLocation({ pathname: window.location.pathname, search: window.location.search })
    window.addEventListener('popstate', update)
    return () => window.removeEventListener('popstate', update)
  }, [])
  return location
}

function useNavigate() {
  return useCallback((to: string) => {
    window.history.pushState({}, '', to)
    window.dispatchEvent(new PopStateEvent('popstate'))
  }, [])
}

function useParams() {
  const { pathname } = useLocation()
  const run = pathname.match(/^\/runs\/([^/]+)/)
  const approval = pathname.match(/^\/approvals\/([^/]+)/)
  return { runId: run?.[1], approvalId: approval?.[1] }
}

function statusLabel(value: string) {
  return ({ queued: '排队中', running: '分析中', waiting_approval: '待审批', succeeded: '已完成', failed: '未完成', expired: '已过期', pending: '待审批', approved: '已批准', rejected: '已拒绝', allowed: '已授权', executed: '已执行', suggest_only: '仅提供建议', require_approval: '需要人工审批' } as Record<string, string>)[value] || value
}

function businessText(value: string) {
  return value.replaceAll('沙箱工单', '内部工单').replaceAll('沙箱', '工作区').replaceAll('Synthetic Demo Data', '合成数据').replaceAll('DeepSeek', '模型服务')
}

function Chip({ children, tone = 'neutral', square = false }: { children: React.ReactNode; tone?: string; square?: boolean }) {
  return <span className={`chip chip-${tone} ${square ? 'chip-square' : ''}`}>{children}</span>
}

function Button({ children, variant = 'primary', icon, disabled, onClick, type = 'button' }: any) {
  return <button type={type} className={`button button-${variant}`} disabled={disabled} onClick={onClick}>{icon}{children}</button>
}

function Shell({ children }: { children: React.ReactNode }) {
  const location = useLocation()
  const navigate = useNavigate()
  const section = new URLSearchParams(location.search).get('section')
  const active = section === 'admin' ? 'admin' : location.pathname.startsWith('/approvals') || section === 'approvals' ? 'approval' : section === 'reports' ? 'report' : section === 'upload' ? 'upload' : location.pathname.startsWith('/runs') || section === 'runs' ? 'run' : 'overview'
  return <div className="app-shell">
    <aside className="sidebar">
      <button className="brand" onClick={() => navigate('/')}><span className="brand-mark"><Sparkles size={19}/></span><span><strong>GrowthTriage</strong><small>Agent Decision System</small></span></button>
      <nav>
        <button className={active === 'overview' ? 'active' : ''} onClick={() => navigate('/workspace')}><LayoutDashboard size={17}/>工作台</button>
        <button className={active === 'upload' ? 'active' : ''} onClick={() => navigate('/workspace?section=upload')}><Upload size={17}/>上传数据</button>
        <button className={active === 'run' ? 'active' : ''} onClick={() => navigate('/workspace?section=runs')}><GitBranch size={17}/>联合诊断</button>
        <button className={active === 'approval' ? 'active' : ''} onClick={() => navigate('/workspace?section=approvals')}><ShieldCheck size={17}/>审批中心</button>
        <button className={active === 'report' ? 'active' : ''} onClick={() => navigate('/workspace?section=reports')}><FileText size={17}/>成果包</button>
        <button className={active === 'admin' ? 'active' : ''} onClick={() => navigate('/workspace?section=admin')}><LockKeyhole size={17}/>管理员</button>
      </nav>
      <div className="sidebar-status"><span><i/>增长运营</span><small>反馈洞察 · 投放诊断</small></div>
    </aside>
    <main className="main">{children}</main>
  </div>
}

function Topbar({ section, session, mode, source }: { section: string; session?: SessionInfo | null; mode?: string; source?: string }) {
  return <div className="topbar">
    <div className="breadcrumb"><span>{session?.role === 'admin' ? '管理工作区' : '访客工作区'}</span><ChevronRight size={13}/><strong>{section}</strong></div>
    <div className="top-actions"><Chip tone="brand"><BadgeCheck size={13}/>{source === 'golden' ? '合成数据' : source === 'upload' ? '上传数据' : '业务工作区'}</Chip>{mode && <Chip tone={mode === 'live' ? 'brand' : mode === 'mixed' ? 'warning' : mode === 'failed' ? 'danger' : 'neutral'}>{mode === 'live' ? '模型分析' : mode === 'mixed' ? '混合分析' : mode === 'failed' ? '分析失败' : '规则分析'}</Chip>}<Chip><Clock3 size={13}/>{session ? session.role === 'admin' ? '管理员工作区' : '访客工作区' : '访客模式'}</Chip></div>
  </div>
}

function UploadPage() {
  const navigate = useNavigate()
  const [session, setSession] = useState<SessionInfo | null>(null)
  const [feedbackFile, setFeedbackFile] = useState<File | null>(null)
  const [metricsFile, setMetricsFile] = useState<File | null>(null)
  const [consent, setConsent] = useState(false)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  const [issues, setIssues] = useState<ApiError['fieldErrors']>([])
  const [dataset, setDataset] = useState<Awaited<ReturnType<typeof uploadDataset>> | null>(null)
  useEffect(() => { getSession().then(setSession).catch(async () => { await createGuest(); setSession(await getSession()) }) }, [])
  const validate = async () => {
    if (!feedbackFile || !metricsFile || !consent) return
    setBusy(true); setMessage(''); setIssues([]); setDataset(null)
    try { setDataset(await uploadDataset(feedbackFile, metricsFile)) }
    catch (error) { const issue = error as ApiError; setMessage(issue.message); setIssues(issue.fieldErrors) }
    finally { setBusy(false) }
  }
  const run = async () => {
    if (!dataset) return
    setBusy(true); setMessage('')
    try { const created = await startDataset(dataset.dataset_id); navigate(`/runs/${created.run_id}`) }
    catch (error) { setMessage((error as Error).message) }
    finally { setBusy(false) }
  }
  return <Shell>
    <Topbar section="上传数据" session={session}/>
    <div className="page-heading"><div><span className="eyebrow">CUSTOM DATA · TWO SIGNALS</span><h1>上传分析数据</h1><p>两份 CSV 先独立质检，再冻结数据集并运行同一套 Multi-Agent 流程。</p></div></div>
    {session?.active_run_id && <div className="network-banner"><Info size={15}/>当前工作区已有活动诊断。你可以先质检数据，待当前 Run 结束后再启动。<Button variant="secondary" onClick={() => navigate(`/runs/${session.active_run_id}`)}>查看当前 Run</Button></div>}
    <section className="upload-grid">
      <div className="panel upload-card"><span className="icon-box evidence"><MessageSquareText size={18}/></span><h3>01 · 用户反馈</h3><p>一行一条用户反馈，含 UTC 时间、来源、国家与可选素材关联键。</p><a href="/api/v1/templates/feedback.csv" download>下载 feedback.csv 模板 <Download size={14}/></a><label className="file-drop"><Upload size={18}/><strong>{feedbackFile ? feedbackFile.name : '点击选择反馈 CSV'}</strong><small>{feedbackFile ? `${(feedbackFile.size / 1024).toFixed(1)} KB · 点击更换` : 'UTF-8 · ≤ 1 MB · ≤ 500 行'}</small><input aria-label="选择反馈 CSV" type="file" accept=".csv,text/csv" onChange={event => { setFeedbackFile(event.target.files?.[0] || null); setDataset(null) }}/></label></div>
      <div className="panel upload-card"><span className="icon-box danger"><BarChart3 size={18}/></span><h3>02 · 广告指标</h3><p>每个 Creative × 国家提供相邻的 baseline 和 current 七日窗口。</p><a href="/api/v1/templates/ad_metrics.csv" download>下载 ad_metrics.csv 模板 <Download size={14}/></a><label className="file-drop"><Upload size={18}/><strong>{metricsFile ? metricsFile.name : '点击选择广告指标 CSV'}</strong><small>{metricsFile ? `${(metricsFile.size / 1024).toFixed(1)} KB · 点击更换` : 'UTF-8 · ≤ 2 MB · ≤ 5000 行'}</small><input aria-label="选择广告指标 CSV" type="file" accept=".csv,text/csv" onChange={event => { setMetricsFile(event.target.files?.[0] || null); setDataset(null) }}/></label></div>
    </section>
    <section className="panel upload-review"><label><input type="checkbox" checked={consent} onChange={event => setConsent(event.target.checked)}/><span>我确认数据已脱敏，并同意将反馈文本、指标及分析结果发送至已配置的模型服务进行联合诊断。</span></label><div><Button icon={<ShieldCheck size={16}/>} disabled={busy || !feedbackFile || !metricsFile || !consent} onClick={validate}>{busy ? '正在校验…' : '校验双 CSV'}</Button>{dataset && <Button variant="secondary" icon={<Play size={16}/>} disabled={busy || !!session?.active_run_id || (session ? session.run_count >= session.run_limit : false)} onClick={run}>开始分析</Button>}</div></section>
    {message && <div className="inline-error" role="alert"><ShieldAlert size={16}/>{message}</div>}
    {issues.length > 0 && <section className="panel quality-list"><h3>阻断问题 · {issues.length}</h3>{issues.map((issue, index) => <div key={index}><Chip tone="danger">{issue.code}</Chip><span>{issue.file}{issue.row ? ` 第 ${issue.row} 行` : ''} · {issue.field}</span><strong>{issue.message}</strong></div>)}</section>}
    {dataset && <section className="panel quality-list"><div className="panel-title"><CheckCircle2 className="green"/><h3>质检通过 · 数据集已就绪</h3><Chip tone="success">READY</Chip></div><p>{dataset.feedback_rows} 条反馈 · {dataset.metric_rows} 行指标。点击“开始分析”后，结论和报告将使用这组数据。</p>{dataset.warnings.map((warning, index) => <div key={index}><Chip tone="warning">{warning.code}</Chip><span>{warning.file}{warning.row ? ` 第 ${warning.row} 行` : ''}</span><strong>{warning.message}</strong></div>)}</section>}
  </Shell>
}

function AdminPage() {
  const navigate = useNavigate()
  const [session, setSession] = useState<SessionInfo | null>(null)
  const [password, setPassword] = useState('')
  const [health, setHealth] = useState<any>(null)
  const [modelProbe, setModelProbe] = useState<Awaited<ReturnType<typeof adminModelProbe>> | null>(null)
  const [workspaces, setWorkspaces] = useState<Array<{ workspace_id: string; status: string; expires_at: string }>>([])
  const [runs, setRuns] = useState<Array<{ run_id: string; status: string; execution_mode: string; error_code: string | null }>>([])
  const [confirmId, setConfirmId] = useState('')
  const [confirmTarget, setConfirmTarget] = useState('')
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  const refresh = async () => {
    const current = await getSession()
    setSession(current)
    if (current.role === 'admin') {
      const [nextHealth, nextWorkspaces, nextRuns] = await Promise.all([adminHealth(), adminWorkspaces(), adminRuns()])
      setHealth(nextHealth); setWorkspaces(nextWorkspaces); setRuns(nextRuns)
    }
  }
  useEffect(() => { refresh().catch(() => {}) }, [])
  const login = async () => {
    setBusy(true); setMessage('')
    try { await adminLogin(password); setPassword(''); await refresh() }
    catch (error) { setMessage((error as Error).message) }
    finally { setBusy(false) }
  }
  const clean = async () => {
    setBusy(true); setMessage('')
    try { const result = await adminCleanup(); setMessage(`清理完成：${result.soft_expired} 个软到期，${result.hard_deleted} 个硬删除。`); await refresh() }
    catch (error) { setMessage((error as Error).message) }
    finally { setBusy(false) }
  }
  const probe = async () => {
    setBusy(true); setMessage('')
    try { setModelProbe(await adminModelProbe()) }
    catch (error) { setMessage((error as Error).message) }
    finally { setBusy(false) }
  }
  const remove = async (id: string) => {
    if (confirmId !== id) return
    setBusy(true); setMessage('')
    try { await adminDeleteWorkspace(id); setConfirmId(''); setConfirmTarget(''); await refresh(); setMessage('访客工作区已删除。') }
    catch (error) { setMessage((error as Error).message) }
    finally { setBusy(false) }
  }
  return <Shell>
    <Topbar section="管理员" session={session}/>
    <div className="page-heading"><div><span className="eyebrow">OWNER CONSOLE</span><h1>系统管理</h1><p>管理工作区、诊断任务和模型服务。</p></div>{session?.role === 'admin' && <Button variant="secondary" onClick={async () => { await logout(); navigate('/') }}>退出登录</Button>}</div>
    {message && <div className={message.includes('完成') || message.includes('删除') ? 'network-banner' : 'inline-error'} role="status">{message}</div>}
    {session?.role !== 'admin' ? <section className="panel admin-login"><span className="icon-box warning"><LockKeyhole/></span><h2>管理员登录</h2><p>使用管理员凭据登录，查看系统状态并管理工作区。</p><label>管理员密码<input aria-label="管理员密码" type="password" value={password} onChange={event => setPassword(event.target.value)} onKeyDown={event => { if (event.key === 'Enter') login() }}/></label><Button disabled={busy || !password} onClick={login}>{busy ? '正在验证…' : '登录'}</Button></section> : <>
      <section className="admin-stats">{[['数据库',health?.database || '—'],['运行总数',health?.runs ?? '—'],['活动诊断',health?.active_runs ?? '—'],['访客工作区',health?.guest_workspaces ?? '—'],['模型服务',health?.model_configured ? '已配置' : '未配置'],['模型请求日配额',`${health?.live_quota_used ?? 0} / ${health?.live_quota_limit ?? 30}`]].map(([label, value]) => <div className="metric-card" key={label}><span>{label}</span><strong>{value}</strong></div>)}</section>
      <section className="panel admin-table"><div className="panel-title spread"><h3>模型连通性</h3><Button variant="secondary" disabled={busy} onClick={probe}>检测模型</Button></div><p>检测模型服务的可用性；检测请求计入每日额度。</p>{modelProbe && <p role="status">{modelProbe.reachable ? `已连通 · ${modelProbe.latency_ms} ms` : `未连通 · ${modelProbe.error_code || 'LLM_PROVIDER_ERROR'}`}</p>}</section>
      <section className="panel admin-table"><div className="panel-title spread"><h3>访客工作区</h3><Button variant="secondary" disabled={busy} onClick={clean}>清理已到期</Button></div><p>手动删除需要在对应行输入完整 工作区 ID，且只允许删除访客工作区。</p>{workspaces.length ? workspaces.map(item => <div className="admin-row" key={item.workspace_id}><span><strong>{item.workspace_id}</strong><small>{statusLabel(item.status)} · 到期 {new Date(item.expires_at).toLocaleString()}</small></span><input aria-label={`确认删除 ${item.workspace_id}`} placeholder="输入完整 ID 确认删除" value={confirmTarget === item.workspace_id ? confirmId : ''} onFocus={() => { setConfirmTarget(item.workspace_id); setConfirmId('') }} onChange={event => { setConfirmTarget(item.workspace_id); setConfirmId(event.target.value) }}/><Button variant="danger" disabled={busy || confirmTarget !== item.workspace_id || confirmId !== item.workspace_id} onClick={() => remove(item.workspace_id)}>删除</Button></div>) : <p>目前没有访客工作区。</p>}</section>
      <section className="panel admin-table"><div className="panel-title"><GitBranch size={18}/><h3>最近诊断记录</h3></div>{runs.length ? runs.slice(0, 20).map(item => <div className="admin-row" key={item.run_id}><span><strong style={{overflowWrap: 'anywhere'}}>{item.run_id}</strong><small>{item.execution_mode} · {item.error_code || '无错误码'}</small></span><Chip tone={item.status === 'succeeded' ? 'success' : item.status === 'failed' ? 'danger' : 'neutral'}>{statusLabel(item.status)}</Chip></div>) : <p>暂无诊断记录。</p>}</section>
    </>}
  </Shell>
}

const metricLabels: Array<[string, string]> = [['ctr', 'CTR'], ['cvr', 'CVR'], ['cpi', 'CPI'], ['roas_d7', 'ROAS D7'], ['frequency', 'Frequency']]
function metricText(name: string, value: number | null | undefined) {
  if (value == null) return 'N/A'
  return name === 'ctr' || name === 'cvr' ? `${(value * 100).toFixed(2)}%` : value.toFixed(name === 'cpi' ? 4 : 2)
}
function runTitle(results: RunResults | null) {
  const scope = results?.scope
  return scope ? `${scope.market} 市场 ${scope.creative_id} 联合诊断` : '联合诊断'
}

function OverviewPage() {
  const navigate = useNavigate()
  const [session, setSession] = useState<SessionInfo | null>(null)
  const [runs, setRuns] = useState<Array<{ run_id: string; status: string; created_at: string }>>([])
  const [loading, setLoading] = useState(true)
  const [message, setMessage] = useState('')
  useEffect(() => {
    let cancelled = false
    const load = async () => {
      try {
        let current: SessionInfo
        try { current = await getSession() } catch { await createGuest(); current = await getSession() }
        const items = await listRuns()
        if (!cancelled) { setSession(current); setRuns(items) }
      } catch (error) { if (!cancelled) setMessage((error as Error).message) }
      finally { if (!cancelled) setLoading(false) }
    }
    load()
    return () => { cancelled = true }
  }, [])
  return <Shell>
    <Topbar section="工作台" session={session}/>
    <div className="page-heading"><div><span className="eyebrow">GROWTH INTELLIGENCE</span><h1>增长诊断工作台</h1><p>汇集用户反馈与广告指标，追踪诊断、审批和执行结果。</p></div><Button icon={<Upload size={16}/>} onClick={() => navigate('/workspace?section=upload')}>新建诊断</Button></div>
    {message && <div className="inline-error" role="alert">{message}</div>}
    <section className="outcome-grid">{[['诊断总数', runs.length], ['进行中', runs.filter(r => ['queued','running'].includes(r.status)).length], ['待审批', runs.filter(r => r.status === 'waiting_approval').length], ['已完成', runs.filter(r => r.status === 'succeeded').length]].map(([label, value]) => <div className="metric-card" key={label}><span>{label}</span><strong>{loading || message ? '—' : value}</strong><em>当前工作区</em></div>)}</section>
    <section className="panel"><div className="panel-title spread"><h3>最近诊断</h3><Button variant="secondary" onClick={() => navigate('/workspace?section=runs')}>查看全部</Button></div>
      {loading ? <p>正在加载诊断记录…</p> : message ? <p>记录加载失败，请刷新页面重试。</p> : runs.length ? runs.slice(0, 8).map(run => <div className="workspace-item" key={run.run_id}><div><strong>联合诊断 · {new Date(run.created_at).toLocaleString('zh-CN', {hour12: false})}</strong><p>{statusLabel(run.status)}</p></div><Button variant="secondary" onClick={() => navigate(`/runs/${run.run_id}`)}>查看详情</Button></div>) : <div className="workspace-empty"><FileText/><h2>开始第一次联合诊断</h2><p>上传用户反馈和广告指标，通过数据校验后开始分析。</p><Button onClick={() => navigate('/workspace?section=upload')}>上传数据</Button></div>}
    </section>
  </Shell>
}

function WorkspaceSection({ section }: { section: 'runs' | 'approvals' | 'reports' }) {
  const navigate = useNavigate()
  const [session, setSession] = useState<SessionInfo | null>(null)
  const [runs, setRuns] = useState<Array<{ run_id: string; status: string; created_at: string }>>([])
  const [results, setResults] = useState<RunResults[]>([])
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  const labels = { runs: '联合诊断', approvals: '审批中心', reports: '成果包' }
  useEffect(() => {
    let cancelled = false
    const load = async () => {
      setLoading(true); setMessage('')
      try {
        let current: SessionInfo
        try { current = await getSession() } catch { await createGuest(); current = await getSession() }
        const items = await listRuns()
        const runResults = await Promise.all(items.map(item => getResults(item.run_id)))
        if (!cancelled) { setSession(current); setRuns(items); setResults(runResults) }
      } catch (error) { if (!cancelled) setMessage((error as Error).message || '加载失败，请刷新页面。') }
      finally { if (!cancelled) setLoading(false) }
    }
    load()
    return () => { cancelled = true }
  }, [section])
  const start = async () => {
    if (session?.active_run_id) { navigate(`/runs/${session.active_run_id}`); return }
    setBusy(true); setMessage('')
    try {
      if (session && session.run_count >= session.run_limit) {
        if (runs[0]) navigate(`/runs/${runs[0].run_id}`)
        else setMessage('当前工作区的运行次数已用完。')
      } else {
        navigate('/workspace?section=upload')
      }
    } catch (error) { setMessage((error as Error).message || '创建失败，请重试。') }
    finally { setBusy(false) }
  }
  const approvals = results.filter(result => result.approval)
  const reports = runs.filter(run => run.status === 'succeeded')
  const emptyTitle = section === 'runs' ? '还没有诊断记录' : section === 'approvals' ? '还没有审批请求' : '还没有可下载的成果包'
  const emptyDescription = section === 'runs' ? '上传反馈和广告指标后，系统将开始联合分析。' : section === 'approvals' ? '诊断产生需要人工确认的工单操作时，会在这里显示审批请求。' : '诊断完成后可查看报告；如有待审批操作，请先处理审批。'
  const isEmpty = section === 'runs' ? runs.length === 0 : section === 'approvals' ? approvals.length === 0 : reports.length === 0
  return <Shell>
    <Topbar section={labels[section]} session={session}/>
    <div className="page-heading"><div><span className="eyebrow">WORKSPACE</span><h1>{labels[section]}</h1><p>{section === 'runs' ? '查看当前工作区中的诊断过程。' : section === 'approvals' ? '审核诊断提出的工单申请，确认后创建内部工单并继续生成报告。' : '查看已经完成的诊断结果和可下载报告。'}</p></div><Button icon={<Play size={16}/>} disabled={busy || loading} onClick={start}>{session?.active_run_id ? '继续当前诊断' : session && session.run_count >= session.run_limit ? '查看最近诊断' : '新建诊断'}</Button></div>
    {message && <div className="inline-error" role="alert"><ShieldAlert size={16}/>{message}</div>}
    {loading ? <div className="center-state"><LoaderCircle className="spin"/><p>正在读取当前工作区…</p></div> : isEmpty ? <section className="panel workspace-empty"><span className="icon-box warning">{section === 'approvals' ? <ShieldCheck/> : section === 'reports' ? <FileText/> : <GitBranch/>}</span><h2>{emptyTitle}</h2><p>{emptyDescription}</p>{runs[0] && section !== 'runs' ? <Button variant="secondary" icon={<ArrowRight size={16}/>} onClick={() => navigate(`/runs/${runs[0].run_id}`)}>查看当前诊断</Button> : <Button icon={<Play size={16}/>} disabled={busy} onClick={start}>新建诊断</Button>}</section> : <div className="workspace-list">
      {section === 'runs' && runs.map(run => { const result = results.find(item => item.run_id === run.run_id); return <section className="panel workspace-item" key={run.run_id}><div><Chip tone={run.status === 'succeeded' ? 'success' : run.status === 'waiting_approval' ? 'warning' : 'brand'}>{statusLabel(run.status)}</Chip><h3>{runTitle(result || null)}</h3><p>创建时间：{new Date(run.created_at).toLocaleString('zh-CN', {hour12: false})}</p><p>{result?.scope ? `${result.scope.market} / ${result.scope.creative_id}` : '正在生成事实快照'} · {result?.source === 'upload' ? '自定义 CSV' : '合成数据'}</p></div><Button variant="secondary" icon={<ArrowRight size={16}/>} onClick={() => navigate(`/runs/${run.run_id}`)}>查看诊断</Button></section> })}
      {section === 'approvals' && approvals.map(result => <section className="panel workspace-item" key={result.approval.id}><div><Chip tone={result.approval.status === 'pending' ? 'warning' : 'success'}>{statusLabel(result.approval.status)}</Chip><h3>创建素材诊断内部工单</h3><p>来源：{runTitle(result)} · 创建后可在系统内查看和跟进</p></div><Button variant="secondary" icon={<ArrowRight size={16}/>} onClick={() => navigate(`/approvals/${result.approval.id}`)}>{result.approval.status === 'pending' ? '处理审批' : '查看决定'}</Button></section>)}
      {section === 'reports' && reports.map(run => { const result = results.find(item => item.run_id === run.run_id); return <section className="panel workspace-item" key={run.run_id}><div><Chip tone="success">已生成</Chip><h3>{runTitle(result || null)}成果包</h3><p>联合诊断 · {new Date(run.created_at).toLocaleString('zh-CN', {hour12: false})} · 报告、证据与实验卡</p></div><Button variant="secondary" icon={<ArrowRight size={16}/>} onClick={() => navigate(`/runs/${run.run_id}`)}>打开成果包</Button></section> })}
    </div>}
  </Shell>
}

function ProcessingView({ status, trace, results }: { status: RunStatus; trace: any; results: RunResults | null }) {
  const stepMap = new Map((trace?.steps || []).map((s: any) => [s.step_key, s]))
  return <>
    <section className="panel dag-panel"><div className="panel-title spread"><div><span className="eyebrow">WORKFLOW</span><h3>诊断流程 · {status.current_phase}</h3></div><Chip><RefreshCw size={12}/>进度自动更新</Chip></div><div className="dag-grid">{steps.map(([key, label, mode], index) => { const s: any = stepMap.get(key); const state = s?.status || (status.active_step_keys.includes(key) ? 'running' : 'pending'); return <div className={`dag-node ${state}`} key={key}><span className="step-no">{String(index + 1).padStart(2,'0')}</span>{state === 'succeeded' ? <CheckCircle2/> : state === 'running' ? <LoaderCircle className="spin"/> : <Clock3/>}<strong>{label}</strong><small>{s?.execution_mode === 'live' ? '模型分析' : s?.execution_mode === 'fallback' ? '规则分析' : mode}</small></div>})}</div></section>
    {results?.metrics && <section className="metric-cards">{metricLabels.map(([key, label]) => { const value = results.metrics?.[key]; return <div className="metric-card" key={key}><span>{label}<Info size={12}/></span><strong>{metricText(key, value?.current)}</strong><em>Baseline {metricText(key, value?.baseline)} · {value?.delta_pct == null ? 'N/A' : `${value.delta_pct > 0 ? '+' : ''}${value.delta_pct.toFixed(2)}%`}</em></div> })}</section>}
    <section className="dual-grid lower"><div className="panel"><div className="panel-title"><Bot className="purple"/><h3>Agent Monitor · 6 个业务 Agent</h3><Chip>确定性编排 · 无主 Agent</Chip></div>{['用户反馈分析 Agent','广告指标诊断 Agent','联合证据分析 Agent','优化方案规划 Agent','诊断审核 Agent','诊断报告 Agent'].map(name => { const step: any = trace?.steps?.find((entry: any) => entry.label === name); return <div className="agent-row" key={name}><span><Bot size={14}/>{name}</span><strong>{step?.status === 'succeeded' ? step.execution_mode === 'live' ? '已完成 · 模型分析' : '已完成 · 规则计算' : step?.status === 'running' ? '执行中' : '等待上游'}</strong></div> })}</div><div className="panel formula"><div className="panel-title"><Braces className="teal"/><h3>可验证 Trace</h3><Chip>原始 JSON 默认折叠</Chip></div><div className="formula-box"><small>CTR = destination_clicks / impressions</small><strong>{results?.raw_metrics ? `${results.raw_metrics.current.destination_clicks?.toLocaleString()} / ${results.raw_metrics.current.impressions?.toLocaleString()} = ${metricText('ctr', results.metrics?.ctr?.current)}` : '等待事实快照…'}</strong><span>指标快照 · metrics-1.0</span></div></div></section>
    {trace?.artifacts?.length > 0 && <section className="panel trace-list"><div className="panel-title"><Braces className="teal"/><h3>节点 Artifact</h3><Chip>{trace.artifacts.length} 份</Chip></div>{trace.artifacts.map((artifact: any) => <details key={artifact.id}><summary>{artifact.step_key} · {artifact.artifact_type}</summary><pre>{JSON.stringify(artifact.payload, null, 2)}</pre></details>)}</section>}
  </>
}

function WaitingView({ results, onApproval }: { results: RunResults; onApproval: () => void }) {
  return <>
    <section className="approval-banner"><span className="icon-box warning"><ShieldAlert/></span><div><h3>需要你的决定</h3><p>有一项内部工单申请等待确认。批准或拒绝后，系统将继续生成报告。</p><small>所有证据和中间结果均已保存</small></div><Button icon={<ArrowRight size={16}/>} onClick={onApproval}>查看审批请求</Button></section>
    <section className="dual-grid waiting-grid"><div className="panel"><div className="panel-title spread"><div><h3>{results.finding?.title}</h3><small>置信度 {Math.round((results.finding?.confidence || 0) * 100)}% · 诊断审核 Agent 已通过</small></div><div><Chip tone="danger" square>{results.finding?.incident_priority} 高影响</Chip><Chip tone="success">{results.finding?.link_strength} · 证据关联</Chip><Chip tone="warning">因果未验证</Chip></div></div><div className="claim">{results.finding?.claim}</div><div className="evidence-grid">{results.evidence.map(e => <div key={e.label}><small>{e.label}</small><strong>{e.value}</strong></div>)}</div></div><div className="panel"><div className="panel-title"><FlaskConical className="purple"/><h3>候选假设</h3><Chip tone="warning">待实验验证</Chip></div>{(results.correlation?.hypotheses || []).map((hypothesis: string, index: number) => <div className="hypothesis" key={hypothesis}><Chip tone="brand" square>H{index + 1}</Chip><strong>{hypothesis}</strong><span>依据见左侧 Evidence</span></div>)}<div className="caution"><Info size={14}/>候选假设不是因果结论。</div></div></section>
    <section className="risk-grid">{results.actions.map(a => <div className={`risk-card risk-${a.risk_level.toLowerCase()}`} key={a.risk_level}><div><ShieldCheck/><h3>{a.risk_level === 'R1' ? '内容生成' : a.risk_level === 'R2' ? '内部工单写入' : '外部动作'}</h3><Chip tone={a.risk_level === 'R1' ? 'success' : a.risk_level === 'R2' ? 'warning' : 'danger'}>{a.risk_level} {statusLabel(a.status)}</Chip></div><strong>{businessText(a.title)}</strong><small>{a.risk_level === 'R3' ? '投放调整需在广告平台操作' : a.risk_level === 'R2' ? '创建内部工单' : '已获授权'}</small></div>)}</section>
  </>
}

function CompleteView({ results, runId }: { results: RunResults; runId: string }) {
  return <>
    <section className="complete-hero"><span className="icon-box success"><CheckCircle2/></span><div><span className="eyebrow light">DIAGNOSIS COMPLETE</span><h2>{runTitle(results)}完成</h2><p>结果基于已上传数据与可复核指标，包含诊断结论和后续行动建议。</p></div><Chip tone="success">已完成</Chip></section>
    <section className="outcome-grid">{[['Finding',results.finding ? '1':'0',results.finding?.link_strength ? `${results.finding.link_strength} · 证据关联` : '未达到异常阈值'],['A/B 实验卡',String(results.experiments.length),'待受控验证'],['内部工单',results.ticket ? '1':'0',results.ticket ? '仅当前工作区':results.approval ? '审批已拒绝':'不需要审批'],['广告平台连接','未接入','投放建议需在广告平台操作']].map(([l,v,f]) => <div className="metric-card" key={l}><span>{l}</span><strong>{v}</strong><em>{f}</em></div>)}</section>
    <section className="panel executive"><div className="panel-title"><Sparkles className="purple"/><h3>Executive Summary</h3><Chip tone="success">Evidence-backed</Chip></div><p>{results.finding?.claim || '当前数据未达到异常阈值；仅保留可复核的事实快照。'}</p></section>
    {results.finding && <section className="panel"><div className="panel-title spread"><div><h3>{results.finding.title}</h3><small>置信度 {Math.round(results.finding.confidence * 100)}% · 因果未验证</small></div><Chip tone="danger" square>{results.finding.incident_priority}</Chip></div><p className="claim plain">{results.finding.claim}</p><div className="evidence-grid six">{results.evidence.map(e => <div key={e.label}><small>{e.label}</small><strong>{e.value}</strong></div>)}</div></section>}
    <section className="artifact-grid"><div className="panel ticket"><div className="panel-title"><TicketCheck className="orange"/><h3>内部工单</h3><Chip tone={results.ticket ? 'success':'neutral'}>{results.ticket ? '待处理':'未创建'}</Chip></div>{results.ticket ? <><strong className="ticket-no">{results.ticket.ticket_no}</strong><h3>{businessText(results.ticket.title)}</h3><p>当前工作区 · 未同步至任何外部系统</p></> : <p>{results.approval ? 'R2 已拒绝；Run 仍合法完成并生成报告。' : '本次结论未请求 R2 内部工单写入。'}</p>}</div>{results.experiments.map((e, i) => <div className="panel experiment" key={e.title}><div className="panel-title"><Chip tone="brand" square>实验 {i ? 'B':'A'}</Chip><Chip>观察 {e.duration_days} 天</Chip></div><h3>{e.title}</h3><div className="experiment-metrics"><span><small>主指标</small>{e.primary_metric}</span><span><small>护栏</small>{e.guardrails.join('、')}</span></div><p>{e.validation_goal}</p><div className="caution"><ShieldAlert size={13}/>{e.stop_condition}</div></div>)}</section>
    <section className="download-bar"><div><strong><ShieldCheck size={16}/>{results.report_ready ? '脱敏检查已通过' : '报告脱敏检查未通过，下载已禁用'}</strong><small>{results.source === 'golden' ? '合成数据' : '上传数据'} · 指标口径 metrics-1.0 · 审批策略 policy-1.0</small></div>{results.report_ready && <div><a className="button button-secondary" href={`/api/v1/runs/${runId}/report.json`}><FileJson size={15}/>下载 JSON</a><a className="button button-primary" href={`/api/v1/runs/${runId}/report.md`}><Download size={15}/>下载 Markdown</a></div>}</section>
  </>
}

function TracePanel({ trace }: { trace: any }) {
  return <section className="panel trace-list"><div className="panel-title"><GitBranch className="purple"/><h3>Agent Trace 与 Harness 审计</h3><Chip>{trace?.steps?.length || 0} Step</Chip></div><p>展开节点查看执行模式、耗时与结构化 Artifact；数值来自事实快照；模型只可通过 Harness 读取授权证据，工单写入仍需人工审批。</p>{(trace?.steps || []).map((step: any) => { const artifact = [...(trace?.artifacts || [])].reverse().find((item: any) => item.step_key === step.step_key); return <details key={step.step_key}><summary>{step.label} · {statusLabel(step.status)} · {step.execution_mode}{step.error_code ? ` · ${step.error_code}` : ''}</summary><div className="trace-meta">尝试 {step.attempt_count} 次 · {step.latency_ms ?? '—'} ms · {step.execution_mode === 'live' ? '模型分析' : step.status === 'failed' ? '模型调用未完成' : '规则执行'}</div>{artifact && <pre>{JSON.stringify(artifact.payload, null, 2)}</pre>}</details> })}<details><summary>ToolCall 与 AuditEvent · {(trace?.tools || []).length} / {(trace?.events || []).length}</summary><pre>{JSON.stringify({ tools: trace?.tools, events: trace?.events }, null, 2)}</pre></details></section>
}

function RunPage() {
  const { runId = '' } = useParams()
  const navigate = useNavigate()
  const [session, setSession] = useState<SessionInfo | null>(null)
  const [status, setStatus] = useState<RunStatus | null>(null)
  const [trace, setTrace] = useState<any>(null)
  const [results, setResults] = useState<RunResults | null>(null)
  const [network, setNetwork] = useState<'online' | 'reconnecting'>('online')
  const [retryBusy, setRetryBusy] = useState(false)
  const [retryMessage, setRetryMessage] = useState('')
  const retryReport = async () => {
    setRetryBusy(true); setRetryMessage('')
    try { await api(`/api/v1/runs/${runId}/retry-report`, { method: 'POST' }); window.location.reload() }
    catch (e) { setRetryMessage((e as Error).message); setRetryBusy(false) }
  }
  useEffect(() => { getSession().then(setSession).catch(() => navigate('/')) }, [navigate])
  useEffect(() => {
    let stopped = false
    let timer: number
    const poll = async () => {
      try {
        const next = await getStatus(runId)
        if (stopped) return
        setStatus(next); setNetwork('online')
        const [nextTrace, nextResults] = await Promise.all([getTrace(runId), next.completed_steps >= 2 ? getResults(runId) : Promise.resolve(null)])
        if (!stopped) { setTrace(nextTrace); if (nextResults) setResults(nextResults) }
        if (!['succeeded','failed','expired'].includes(next.status)) timer = window.setTimeout(poll, next.poll_after_ms || 1500)
      } catch {
        if (!stopped) { setNetwork('reconnecting'); timer = window.setTimeout(poll, 3000) }
      }
    }
    poll()
    return () => { stopped = true; window.clearTimeout(timer) }
  }, [runId])
  if (!status) return <Shell><div className="center-state"><LoaderCircle className="spin"/><h2>正在恢复 Run…</h2><p>刷新页面不会中断分析。</p></div></Shell>
  return <Shell>
    <Topbar section="诊断详情" session={session} mode={status.execution_mode} source={results?.source}/>
    {network === 'reconnecting' && <div className="network-banner"><RefreshCw className="spin"/>连接中断，正在重连… 最后结果保持只读。</div>}
    <div className="page-heading run-heading"><div><span className="eyebrow">{results?.scope ? `${results.scope.platform} · ${results.scope.campaign_id} · ${results.scope.market} · ${results.scope.creative_id}` : 'GROWTHTRIAGE · DATASET ANALYSIS'}</span><h1>{status.status === 'succeeded' ? `${runTitle(results)}完成` : status.status === 'waiting_approval' ? '联合诊断已完成，等待工单审批' : runTitle(results)}</h1><p>{results?.source === 'golden' ? '合成数据 · ' : results?.source === 'upload' ? '上传数据 · ' : ''}刷新页面不会中断 Run。</p></div><div><Chip tone={status.status === 'waiting_approval' ? 'warning' : status.status === 'succeeded' ? 'success' : status.status === 'failed' ? 'danger' : 'brand'}>{status.status === 'waiting_approval' ? <PauseCircle size={13}/> : status.status === 'succeeded' ? <Check size={13}/> : status.status === 'failed' ? <ShieldAlert size={13}/> : <LoaderCircle className="spin" size={13}/>} {statusLabel(status.status)}</Chip><Chip>{status.completed_steps} / {status.total_steps} Step</Chip></div></div>
    {status.status === 'failed' || status.status === 'expired' ? <section className="panel workspace-empty"><ShieldAlert/><h2>{status.status === 'failed' ? '本次诊断未完成' : '访客工作区已过期'}</h2><p>{status.error_code === 'LLM_TIMEOUT' ? '模型未在等待时限内返回，本次未生成报告。请稍后重新创建诊断；重复发生时检查网络或联系管理员调整模型等待时间。' : status.error_code ? `安全错误码：${status.error_code}。本次未生成诊断报告，请检查模型配置或稍后重试。` : '已完成的 Trace 保留在当前页面；你可以返回入口创建新的访客工作区。'}</p>{status.status === 'failed' && ['report_builder','final_review'].includes(status.current_phase) && ['LLM_PROVIDER_ERROR','LLM_TRANSPORT_ERROR','LLM_RESPONSE_INVALID','LLM_TIMEOUT','LLM_HTTP_429','LLM_HTTP_500','LLM_HTTP_502','LLM_HTTP_503','LLM_HTTP_504'].includes(status.error_code || '') && <><p>{status.current_phase === 'final_review' ? '报告草稿已生成，终稿复核调用未完成。' : '报告阶段调用未完成。'}可保留已有分析和审批结果，仅重试报告阶段；模型请求仍计入配额，不重复创建工单。</p><Button disabled={retryBusy} onClick={retryReport}>{retryBusy ? '正在恢复…' : '仅重试报告阶段'}</Button>{retryMessage && <p role="alert">{retryMessage}</p>}</>}<Button onClick={() => navigate('/')}>返回入口</Button></section> : status.status === 'waiting_approval' && results ? <WaitingView results={results} onApproval={() => navigate(`/approvals/${status.pending_approval_id}`)}/> : status.status === 'succeeded' && results ? <CompleteView results={results} runId={runId}/> : <ProcessingView status={status} trace={trace} results={results}/>} 
    {(status.status === 'succeeded' || status.status === 'failed') && trace && <TracePanel trace={trace}/>}
  </Shell>
}

function ApprovalPage() {
  const { approvalId = '' } = useParams()
  const navigate = useNavigate()
  const [session, setSession] = useState<SessionInfo | null>(null)
  const [approval, setApproval] = useState<any>(null)
  const [results, setResults] = useState<RunResults | null>(null)
  const [comment, setComment] = useState('')
  const [modal, setModal] = useState<'approved' | 'rejected' | null>(null)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  useEffect(() => { getSession().then(setSession); getApproval(approvalId).then(value => { setApproval(value); getResults(value.run_id).then(setResults) }).catch(() => navigate('/workspace')) }, [approvalId, navigate])
  const submit = async () => {
    if (!modal) return
    setBusy(true); setMessage('')
    try { const result = await decideApproval(approvalId, modal, comment); navigate(`/runs/${result.run_id}`) }
    catch (e) { const err = e as ApiError; setMessage(err.message); setModal(null); getApproval(approvalId).then(setApproval) }
    finally { setBusy(false) }
  }
  if (!approval) return <Shell><div className="center-state"><LoaderCircle className="spin"/><h2>正在读取审批…</h2></div></Shell>
  return <Shell>
    <Topbar section="审批详情" session={session}/>
    <div className="page-heading"><div><span className="eyebrow">工单申请</span><h1>审批创建内部工单</h1><p>来源：{runTitle(results)} · 有效至 {new Date(approval.expires_at).toLocaleString()}</p></div><Chip tone="warning">{statusLabel(approval.status)}</Chip></div>
    {message && <div className="inline-error"><ShieldAlert/>{message}</div>}
    <section className="approval-grid"><div className="panel approval-detail"><div className="panel-title spread"><div className="title-with-icon"><span className="icon-box warning"><TicketCheck/></span><div><h3>{businessText(approval.title)}</h3><small>创建内部诊断工单</small></div></div><Chip tone="warning">需人工审批</Chip></div><div className="expected"><small>预计结果</small><strong>在当前工作区内创建 1 张工单草稿</strong></div><div className="field-grid">{[['负责人','Growth Operator'],['事件优先级',`${approval.incident_priority} · 待复核`],['写入范围','当前工作区'],['工单保存位置','当前系统']].map(([l,v]) => <div key={l}><small>{l}</small><strong>{v}</strong></div>)}</div><h4>支持证据</h4>{(results?.evidence || []).map(item => <div className="check-line" key={item.label}><CheckCircle2 size={15}/>{item.label} · {item.value}</div>)}</div><div className="panel policy-card"><div className="panel-title"><ShieldCheck className="purple"/><h3>Harness Policy</h3><Chip>policy-1.0</Chip></div><div className="policy-decision"><small>策略判断</small><strong>{statusLabel(approval.policy.decision)}</strong><span>命中规则 · {approval.policy.matched_rule}</span></div><h4>安全边界</h4>{approval.safety.map((x: string) => <div className="safety-line" key={x}><LockKeyhole size={14}/>{businessText(x)}</div>)}<div className="r3-notice"><Ban/><strong>预算与停投建议</strong><span>当前未接入广告平台执行功能，请在对应平台确认并操作。</span></div></div></section>
    <label className="comment-field"><span>审批说明（可选）<small>{comment.length} / 500</small></span><textarea value={comment} maxLength={500} onChange={e => setComment(e.target.value)} placeholder="补充批准或拒绝的理由，最多 500 字"/></label>
    <div className="decision-bar"><p><Info size={14}/>请核对申请内容后批准或拒绝，提交后将继续处理诊断。</p><div><Button variant="danger" icon={<X size={16}/>} disabled={busy || approval.status !== 'pending'} onClick={() => setModal('rejected')}>拒绝</Button><Button icon={<Check size={16}/>} disabled={busy || approval.status !== 'pending'} onClick={() => setModal('approved')}>批准并继续</Button></div></div>
    {modal && <div className="modal-overlay" role="dialog" aria-modal="true" aria-labelledby="confirm-title"><div className="modal"><div className="modal-title"><span className="icon-box warning">{modal === 'approved' ? <ShieldCheck/> : <ShieldAlert/>}</span><div><h2 id="confirm-title">{modal === 'approved' ? '批准创建内部工单？' : '拒绝创建内部工单？'}</h2><p>{modal === 'approved' ? '批准后，Harness 将在当前工作区内创建 1 张内部工单，并继续生成诊断报告。不会写入 Meta 或任何外部系统。' : '拒绝后不会创建工单，Run 仍会完成并生成报告。'}</p></div></div><div className="modal-summary"><TicketCheck/><span>预计生成</span><strong>{modal === 'approved' ? '1 张内部工单' : '0 张工单'}</strong></div><div className="modal-actions"><Button variant="secondary" disabled={busy} onClick={() => setModal(null)}>返回检查</Button><Button variant={modal === 'approved' ? 'primary' : 'danger'} disabled={busy} onClick={submit}>{busy ? '正在提交决定…' : modal === 'approved' ? '批准并继续' : '确认拒绝'}</Button></div></div></div>}
  </Shell>
}

export default function App() {
  const { pathname, search } = useLocation()
  if (pathname === '/') return <OverviewPage/>
  if (pathname === '/workspace' || pathname === '/demo') {
    const section = new URLSearchParams(search).get('section')
    if (section === 'upload') return <UploadPage/>
    if (section === 'admin') return <AdminPage/>
    return section === 'runs' || section === 'approvals' || section === 'reports' ? <WorkspaceSection key={section} section={section}/> : <OverviewPage/>
  }
  if (pathname.startsWith('/runs/')) return <RunPage/>
  if (pathname.startsWith('/approvals/')) return <ApprovalPage/>
  window.history.replaceState({}, '', '/')
  return <OverviewPage/>
}
