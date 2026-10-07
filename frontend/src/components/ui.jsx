import { useState } from 'react'
import { AlertCircle, Check, CheckCircle2, Circle, Clock3, Copy, LoaderCircle, Minus, TriangleAlert, XCircle } from 'lucide-react'
import { executionStates, validationStates } from '../lib/task'

const icons = { queued: Clock3, running: LoaderCircle, completed: Check, interrupted: TriangleAlert, error: XCircle, not_run: Minus, passed: CheckCircle2, failed: XCircle, inconclusive: AlertCircle }

export function StatusBadge({ value, validation = false }) {
  const info = (validation ? validationStates : executionStates)[value] || { label: value || '未知', tone: 'neutral' }
  const Icon = icons[value] || Circle
  return <span className={`badge badge-${info.tone}`}><Icon size={13} className={value === 'running' ? 'spin' : ''} aria-hidden="true" />{info.label}</span>
}

export function ReviewBadge({ value }) {
  const info = {
    pending: { label: '待审查', tone: 'neutral', icon: Clock3 },
    accepted: { label: '已接受', tone: 'green', icon: CheckCircle2 },
    rejected: { label: '已拒绝', tone: 'red', icon: XCircle },
    stale: { label: '审查已过期', tone: 'amber', icon: TriangleAlert },
  }[value]
  if (!info) return <span className="badge badge-neutral">暂无法读取</span>
  const Icon = info.icon
  return <span className={`badge badge-${info.tone}`}><Icon size={13} aria-hidden="true" />{info.label}</span>
}

export function ErrorNotice({ error, onRetry, children }) {
  return <div className="notice notice-error" role="alert">
    <AlertCircle size={18} aria-hidden="true" />
    <div><strong>{error?.message || '暂时无法读取数据'}</strong>{children && <p>{children}</p>}</div>
    {onRetry && <button type="button" className="button button-small button-quiet" onClick={onRetry}>重试</button>}
  </div>
}

export function CopyButton({ value, label = '复制编号' }) {
  const [copied, setCopied] = useState(null)
  const feedback = copied?.value === value ? copied.message : ''
  async function copy() {
    try {
      await navigator.clipboard.writeText(value)
      setCopied({ value, message: '已复制' })
    } catch {
      setCopied({ value, message: '复制失败，请手动选择文本' })
    }
  }
  return <span className="copy-control">
    <button className="icon-button" type="button" title={label} aria-label={label} onClick={copy}>
      {feedback === '已复制' ? <Check size={15} /> : <Copy size={15} />}
    </button>
    <span className="sr-only" role="status">{feedback}</span>
  </span>
}

export function LoadingState({ label = '正在读取任务…' }) {
  return <div className="loading-state" role="status"><LoaderCircle size={20} className="spin" aria-hidden="true" /><span>{label}</span></div>
}

export function EmptyState({ icon: Icon = Circle, title, children, action }) {
  return <div className="empty-state"><div className="empty-icon"><Icon size={28} strokeWidth={1.5} /></div><h2>{title}</h2><p>{children}</p>{action}</div>
}
