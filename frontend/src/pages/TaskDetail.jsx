import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link, useParams } from 'react-router-dom'
import { motion as Motion } from 'motion/react'
import { ArrowLeft, CheckCircle2, ChevronDown, Clock3, Code2, Download, FileDiff, FolderGit2, GitCommitHorizontal, RefreshCw, ShieldCheck, Terminal, TriangleAlert } from 'lucide-react'
import { artifactUrl, getTask } from '../lib/api'
import { formatDate, formatDuration, isActive, shortPath } from '../lib/task'
import { CopyButton, EmptyState, ErrorNotice, LoadingState, StatusBadge } from '../components/ui'
import DiffViewer from '../components/DiffViewer'
import TaskLogs from '../components/TaskLogs'

const tabs = [{ id: 'overview', label: '概览', icon: FolderGit2 }, { id: 'diff', label: '代码改动', icon: FileDiff }, { id: 'logs', label: '执行日志', icon: Terminal }, { id: 'validation', label: '验证报告', icon: ShieldCheck }]

function VerificationCard({ label, result }) {
  return <div className="verification-card"><div className="verification-card-heading"><span>{label}</span><StatusBadge value={result?.status || 'not_run'} validation /></div>
    <p>{result?.exit_code != null ? `退出码 ${result.exit_code}` : '尚无执行结果'}{result?.seconds != null && <span> · {formatDuration(result.seconds)}</span>}</p>
    {result?.error && <p className="verification-error">{result.error}</p>}
    {result?.output && <details className="verification-output"><summary>查看测试输出<ChevronDown size={14} /></summary><pre tabIndex={0} aria-label={`${label}测试输出`}>{result.output}</pre></details>}
  </div>
}

function ValidationPanel({ task, compact = false }) {
  const validation = task.result?.validation
  if (!validation) return <EmptyState icon={ShieldCheck} title="独立验证尚未完成">验证结果将在修复结束后保存。执行完成与测试通过会分别记录。</EmptyState>
  return <section className="validation-panel"><div className="panel-heading"><div><span className="eyebrow">INDEPENDENT VERIFICATION</span><h2>修复过程</h2></div><ShieldCheck size={22} /></div>
    <div className="verification-comparison"><VerificationCard label="修复前 · 干净基线" result={validation.baseline} /><VerificationCard label="修复后 · 候选补丁" result={validation.candidate} /></div>
    {validation.error && <div className="notice notice-warning"><TriangleAlert size={16} /><span>{validation.error}</span></div>}
    {!compact && <div className="verification-meta"><label>固定验证命令</label><code>{validation.command}</code><label>补丁 SHA-256</label><code>{validation.patch_sha256 || '尚无补丁'}</code></div>}
  </section>
}

function Overview({ task }) {
  const result = task.result
  const review = { pending: '待人工审查', accepted: '已接受', rejected: '已拒绝', stale: '审查已过期' }[result?.review_status] || '尚未进入审查'
  return <div className="overview-grid"><div className="overview-main"><section className="panel goal-panel"><div className="panel-heading"><h2>修复要求</h2><Code2 size={18} /></div><p>{task.goal}</p><div className="verification-command"><span>验证命令</span><code>{task.verification_command}</code></div></section><ValidationPanel task={task} compact /></div>
    <aside className="task-facts panel"><h2>任务信息</h2><dl><dt>任务编号</dt><dd className="mono">{task.task_id}</dd><dt>仓库</dt><dd title={task.repo_path}>{task.repo_path}</dd><dt>工作目录</dt><dd>{task.workdir}</dd><dt>基线提交</dt><dd className="mono fact-sha"><code className="fact-value">{task.base_sha}</code><CopyButton value={task.base_sha} label="复制基线提交" /></dd><dt>模型</dt><dd>{task.execution?.model || '未配置执行'}</dd><dt>执行镜像</dt><dd>{task.execution?.image || '—'}</dd><dt>创建时间</dt><dd>{formatDate(task.created_at)}</dd><dt>开始 / 结束</dt><dd>{formatDate(task.started_at)} / {formatDate(task.finished_at)}</dd><dt>人工审查</dt><dd>{review}</dd><dt>停止原因</dt><dd>{({ no_tool_calls: '模型自然结束', finish: '模型主动交卷', max_turns: '达到轮数上限', interrupted: '运行被中断', error: '执行异常' })[result?.stop_reason] || result?.stop_reason || '—'}</dd></dl></aside>
  </div>
}

export default function TaskDetail() {
  const { taskId } = useParams()
  const [tab, setTab] = useState('overview')
  const query = useQuery({ queryKey: ['task', taskId], queryFn: ({ signal }) => getTask(taskId, { signal }),
    refetchInterval: (query) => isActive(query.state.data) || query.state.data?.cleanup_pending ? 2000 : false })
  const task = query.data

  function tabKey(event) {
    const current = tabs.findIndex((item) => item.id === tab)
    const next = event.key === 'ArrowRight' ? (current + 1) % tabs.length : event.key === 'ArrowLeft' ? (current + tabs.length - 1) % tabs.length : event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : null
    if (next == null) return
    event.preventDefault()
    setTab(tabs[next].id)
    document.getElementById(`tab-${tabs[next].id}`)?.focus()
  }

  return <div className="page detail-page"><Link className="back-link" to="/tasks"><ArrowLeft size={15} />返回任务列表</Link>
    <div className="detail-heading"><div><span className="eyebrow">REPAIR DETAILS</span><h1 id="page-title" tabIndex={-1}>{task?.goal || '任务详情'}</h1>{task && <div className="task-subtitle"><span><FolderGit2 size={14} />{shortPath(task)}</span><span><GitCommitHorizontal size={15} />{task.base_sha.slice(0, 8)}</span><span className="task-id">{task.task_id.slice(0, 8)}<CopyButton value={task.task_id} /></span></div>}</div><div className="detail-actions"><button className="icon-button" title="刷新任务详情" aria-label="刷新任务详情" disabled={query.isFetching} onClick={() => query.refetch()}><RefreshCw size={16} className={query.isFetching ? 'spin' : ''} /></button>{task?.result?.patch && <a href={artifactUrl(taskId, 'patch.diff')} download className="button button-quiet"><Download size={15} />下载补丁</a>}</div></div>
    {query.isPending && <LoadingState />}
    {query.isError && <ErrorNotice error={query.error} onRetry={() => query.refetch()}>{task ? '以下为上次成功读取的任务记录。' : '请检查任务编号，或返回列表选择任务。'}</ErrorNotice>}
    {task && <>
      <div className="status-overview"><div className="status-fact"><span>执行状态</span><StatusBadge value={task.status} /></div><div className="status-fact"><span>验证结论</span><StatusBadge value={task.validation_status} validation /></div><div className="status-fact"><span>模型轮数</span><strong>{task.result?.turns ?? '—'}<small>{task.execution ? ` / ${task.execution.max_model_calls}` : ''}</small></strong></div><div className="status-fact"><span>修复耗时</span><strong>{formatDuration(task.result?.seconds)}</strong></div><div className="status-fact"><span>估算费用</span><strong>{task.result?.cost == null ? '—' : `¥${task.result.cost.toFixed(4)}`}</strong></div></div>
      {task.status === 'queued' && <div className="notice notice-info"><Clock3 size={18} /><div><strong>{task.execution ? '已进入队列，等待 worker 领取' : '这是一条仅保存的任务记录'}</strong><p>{task.execution ? '确认独立 worker 已启动后，任务会按顺序执行。你可以关闭网页，稍后再来查看。' : '此任务没有执行配置。需要运行时，请新建一条配置了模型和镜像的任务。'}</p></div></div>}
      {task.error && <div className="notice notice-error" role="alert"><TriangleAlert size={18} /><div><strong>执行记录</strong><p>{task.error}</p></div></div>}
      {task.cleanup_pending && !isActive(task) && <div className="notice notice-warning"><TriangleAlert size={17} /><span>容器清理尚未完成，worker 暂停领取下一题。</span></div>}
      <div className="detail-tabs" role="tablist" aria-label="任务详情内容" onKeyDown={tabKey}>{tabs.map(({ id, label, icon: Icon }) => <button id={`tab-${id}`} type="button" key={id} role="tab" aria-selected={tab === id} aria-controls={`panel-${id}`} tabIndex={tab === id ? 0 : -1} className={tab === id ? 'active' : ''} onClick={() => setTab(id)}><Icon size={16} />{label}</button>)}</div>
      <Motion.div key={`${taskId}-${tab}`} role="tabpanel" id={`panel-${tab}`} aria-labelledby={`tab-${tab}`} tabIndex={0} initial={{ opacity: 0 }} animate={{ opacity: 1 }} transition={{ duration: 0.16 }}>
        {tab === 'overview' && <Overview task={task} />}{tab === 'diff' && <DiffViewer patch={task.result?.patch} />}{tab === 'logs' && <TaskLogs task={task} />}{tab === 'validation' && <ValidationPanel task={task} />}
      </Motion.div>
      {task.result && <div className="result-footer"><span><CheckCircle2 size={14} />结果已保存</span><span>输入 {task.result.prompt_tokens?.toLocaleString() ?? '—'} tokens · 缓存 {task.result.cached_tokens?.toLocaleString() ?? '—'} · 输出 {task.result.completion_tokens?.toLocaleString() ?? '—'}</span></div>}
    </>}
  </div>
}
