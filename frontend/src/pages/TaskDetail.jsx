import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link, useParams } from 'react-router-dom'
import { motion as Motion } from 'motion/react'
import { ArrowLeft, CheckCircle2, ClipboardCheck, Clock3, Download, FileDiff, FolderGit2, GitCommitHorizontal, RefreshCw, ShieldCheck, Terminal, TriangleAlert } from 'lucide-react'
import { artifactUrl, getTask } from '../lib/api'
import { formatDuration, isActive, shortPath } from '../lib/task'
import { CopyButton, ErrorNotice, LoadingState, StatusBadge } from '../components/ui'
import DiffViewer from '../components/DiffViewer'
import TaskLogs from '../components/TaskLogs'
import ValidationPanel from '../components/ValidationPanel'
import TaskOverview from '../components/TaskOverview'
import { validTaskSource } from '../lib/github'
import ReviewPanel from '../components/ReviewPanel'
import { getReview } from '../lib/reviews'
import { keepLatestReview } from '../lib/review-draft'

const tabs = [{ id: 'overview', label: '概览', icon: FolderGit2 }, { id: 'diff', label: '代码改动', icon: FileDiff }, { id: 'logs', label: '执行日志', icon: Terminal }, { id: 'validation', label: '验证报告', icon: ShieldCheck }, { id: 'review', label: '人工审查', icon: ClipboardCheck }]

export default function TaskDetail() {
  const { taskId } = useParams()
  const [tab, setTab] = useState('overview')
  const query = useQuery({ queryKey: ['task', taskId], queryFn: ({ signal }) => getTask(taskId, { signal }),
    refetchInterval: (query) => isActive(query.state.data) || query.state.data?.cleanup_pending ? 2000 : false })
  const task = query.data
  const source = task?.source && validTaskSource(task.source) && task.source.base_sha === task.base_sha && task.source.repo_path === task.repo_path ? task.source : null
  const goalTitle = Array.from(task?.goal.trim().split(/\r?\n/, 1)[0] || '任务详情')
  const title = source ? `${source.issue.repository.full_name} #${source.issue.issue_number}`
    : goalTitle.length > 60 ? goalTitle.slice(0, 60).join('') + '…' : goalTitle.join('')
  const reviewQuery = useQuery({ queryKey: ['review', taskId], queryFn: ({ signal }) => getReview(taskId, { signal }),
    enabled: Boolean(task), refetchInterval: 5000, structuralSharing: keepLatestReview })

  function tabKey(event) {
    const current = tabs.findIndex((item) => item.id === tab)
    const next = event.key === 'ArrowRight' ? (current + 1) % tabs.length : event.key === 'ArrowLeft' ? (current + tabs.length - 1) % tabs.length : event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : null
    if (next == null) return
    event.preventDefault()
    setTab(tabs[next].id)
    document.getElementById(`tab-${tabs[next].id}`)?.focus()
  }

  return <div className="page detail-page"><Link className="back-link" to="/tasks"><ArrowLeft size={15} />返回任务列表</Link>
    <div className="detail-heading"><div><h1 id="page-title" tabIndex={-1}>{title}</h1>{task && <div className="task-subtitle"><span><FolderGit2 size={14} />{source?.issue.repository.full_name || shortPath(task)}</span><span><GitCommitHorizontal size={15} />{task.base_sha.slice(0, 8)}</span><span className="task-id">{task.task_id.slice(0, 8)}<CopyButton value={task.task_id} /></span></div>}</div><div className="detail-actions"><button className="icon-button" title="刷新任务详情" aria-label="刷新任务详情" disabled={query.isFetching || reviewQuery.isFetching} onClick={() => { query.refetch(); reviewQuery.refetch() }}><RefreshCw size={16} className={query.isFetching ? 'spin' : ''} /></button>{task?.result?.patch && <a href={artifactUrl(taskId, 'patch.diff')} download className="button button-quiet"><Download size={15} />下载补丁</a>}</div></div>
    {query.isPending && <LoadingState />}
    {query.isError && <ErrorNotice error={query.error} onRetry={() => query.refetch()}>{task ? '以下为上次成功读取的任务记录。' : '请检查任务编号，或返回列表选择任务。'}</ErrorNotice>}
    {task && <>
      <div className="status-overview"><div className="status-fact"><span>执行状态</span><StatusBadge value={task.status} /></div><div className="status-fact"><span>验证结论</span><StatusBadge value={task.validation_status} validation /></div><div className="status-fact"><span>模型轮数</span><strong>{task.result?.turns ?? '—'}<small>{task.execution ? ` / ${task.execution.max_model_calls}` : ''}</small></strong></div><div className="status-fact"><span>修复耗时</span><strong>{formatDuration(task.result?.seconds)}</strong></div><div className="status-fact"><span>估算费用</span><strong>{task.result?.cost == null ? '—' : `¥${task.result.cost.toFixed(4)}`}</strong></div></div>
      {task.status === 'queued' && <div className="notice notice-info"><Clock3 size={18} /><div><strong>{task.execution ? '已进入队列，等待 worker 领取' : '这是一条仅保存的任务记录'}</strong><p>{task.execution ? '确认独立 worker 已启动后，任务会按顺序执行。你可以关闭网页，稍后再来查看。' : '此任务没有执行配置。需要运行时，请新建一条配置了模型和镜像的任务。'}</p></div></div>}
      {task.error && <div className="notice notice-error" role="alert"><TriangleAlert size={18} /><div><strong>执行记录</strong><p>{task.error}</p></div></div>}
      {task.cleanup_pending && !isActive(task) && <div className="notice notice-warning"><TriangleAlert size={17} /><span>容器清理尚未完成，worker 暂停领取下一题。</span></div>}
      <div className="detail-tabs" role="tablist" aria-label="任务详情内容" onKeyDown={tabKey}>{tabs.map(({ id, label, icon: Icon }) => <button id={`tab-${id}`} type="button" key={id} role="tab" aria-selected={tab === id} aria-controls={`panel-${id}`} tabIndex={tab === id ? 0 : -1} className={tab === id ? 'active' : ''} onClick={() => setTab(id)}><Icon size={16} />{label}</button>)}</div>
      <Motion.div key={`${taskId}-${tab}`} role="tabpanel" id={`panel-${tab}`} aria-labelledby={`tab-${tab}`} tabIndex={0} initial={{ opacity: 0 }} animate={{ opacity: 1 }} transition={{ duration: 0.16 }}>
        {tab === 'overview' && <TaskOverview key={taskId} task={task} source={source} reviewQuery={reviewQuery} onNavigate={(id) => { setTab(id); document.getElementById(`tab-${id}`)?.focus() }} />}{tab === 'diff' && <DiffViewer patch={task.result?.patch} />}{tab === 'logs' && <TaskLogs task={task} />}{tab === 'validation' && <ValidationPanel validation={task.result?.validation} />}{tab === 'review' && <ReviewPanel key={taskId} taskId={taskId} source={source} />}
      </Motion.div>
      {task.result && <div className="result-footer"><span><CheckCircle2 size={14} />结果已保存</span><span>输入 {task.result.prompt_tokens?.toLocaleString() ?? '—'} tokens · 缓存 {task.result.cached_tokens?.toLocaleString() ?? '—'} · 输出 {task.result.completion_tokens?.toLocaleString() ?? '—'}</span></div>}
    </>}
  </div>
}
