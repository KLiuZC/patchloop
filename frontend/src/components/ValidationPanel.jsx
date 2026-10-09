import { ChevronDown, ShieldCheck, TriangleAlert } from 'lucide-react'
import { formatDuration } from '../lib/task'
import { EmptyState, StatusBadge } from './ui'
import VerificationCommand from './VerificationCommand'

function VerificationCard({ label, result }) {
  return <div className="verification-card"><div className="verification-card-heading"><span>{label}</span><StatusBadge value={result?.status || 'not_run'} validation /></div>
    <p>{result?.exit_code != null ? `退出码 ${result.exit_code}` : '尚无执行结果'}{result?.seconds != null && <span> · {formatDuration(result.seconds)}</span>}</p>
    {result?.error && <p className="verification-error">{result.error}</p>}
    {result?.output && <details className="verification-output"><summary>查看测试输出<ChevronDown size={14} /></summary><pre tabIndex={0} aria-label={`${label}测试输出`}>{result.output}</pre></details>}
  </div>
}

export default function ValidationPanel({ validation, compact = false, showCommand = true, embedded = false, showHeading = true }) {
  if (!validation) return <EmptyState icon={ShieldCheck} title="独立验证尚未完成">验证结果将在修复结束后保存。执行完成与测试通过会分别记录。</EmptyState>
  return <section className={`validation-panel${embedded ? ' review-validation' : ''}`}>{showHeading && <div className="panel-heading">{embedded ? <h3>测试结果</h3> : <h2>修复过程</h2>}</div>}
    <div className="verification-comparison"><VerificationCard label="修复前 · 干净基线" result={validation.baseline} /><VerificationCard label="修复后 · 候选补丁" result={validation.candidate} /></div>
    {validation.error && <div className="notice notice-warning"><TriangleAlert size={16} /><span>{validation.error}</span></div>}
    {!compact && <>{showCommand && <VerificationCommand command={validation.command} label="固定验证命令" />}<div className="verification-meta"><span>补丁 SHA-256</span><code>{validation.patch_sha256 || '尚无补丁'}</code></div></>}
  </section>
}
