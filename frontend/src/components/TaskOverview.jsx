import { ArrowRight } from 'lucide-react'
import { formatDate } from '../lib/task'
import { CopyButton, ReviewBadge } from './ui'
import IssueSourcePanel from './IssueSourcePanel'
import ValidationPanel from './ValidationPanel'
import VerificationCommand from './VerificationCommand'

export default function TaskOverview({ task, source, reviewQuery, onNavigate }) {
  const result = task.result
  const repository = source?.issue.repository.full_name || task.repo_path.split(/[\\/]/).filter(Boolean).pop() || '仓库'
  const stopReason = ({ no_tool_calls: '模型自然结束', finish: '模型主动交卷', max_turns: '达到轮数上限', interrupted: '运行被中断', error: '执行异常' })[result?.stop_reason] || result?.stop_reason || '—'
  const noValidation = task.status === 'queued'
    ? task.execution ? '任务正在排队，尚无验证记录。' : '此任务仅保存，未配置执行与验证。'
    : task.status === 'running' ? '任务执行中，验证结果将在结束后保存。' : '本次尚无独立验证记录。'

  return <div className="overview-grid overview-summary"><div className="overview-main">
    <section className="panel overview-card overview-description"><div className="panel-heading"><h2>任务说明</h2></div>
      <p className="overview-goal">{task.goal}</p>
      <IssueSourcePanel source={task.source} baseSha={task.base_sha} repoPath={task.repo_path} collapseBody embedded />
    </section>
    <section className="panel overview-card overview-result"><div className="panel-heading"><h2>执行结果</h2></div>
      {result?.validation ? <ValidationPanel validation={result.validation} compact embedded showHeading={false} /> : <p className="overview-empty">{noValidation}</p>}
      <details className="overview-command"><summary>查看验证命令</summary><VerificationCommand command={task.verification_command} /></details>
      <div className="overview-result-actions"><button type="button" className="button button-quiet button-small" onClick={() => onNavigate('diff')}>查看代码改动<ArrowRight size={14} /></button><button type="button" className="button button-quiet button-small" onClick={() => onNavigate('validation')}>查看完整报告<ArrowRight size={14} /></button></div>
      <div className="overview-review"><span>人工审查</span><div>{reviewQuery.isError ? <span>暂无法读取</span> : reviewQuery.data ? <ReviewBadge value={reviewQuery.data.status} /> : <span>正在读取…</span>}<button type="button" className="text-action" onClick={() => onNavigate('review')}>查看审查<ArrowRight size={14} /></button></div></div>
    </section>
  </div>
    <aside className="panel overview-card task-facts overview-facts"><h2>基本信息</h2>
      <section className="overview-fact-group"><h3>仓库与版本</h3><dl>
        <dt>仓库</dt><dd>{repository}</dd>
        <dt>工作目录</dt><dd><code>{task.workdir}</code></dd>
        <dt>基线提交</dt><dd className="fact-sha"><code title={task.base_sha}>{task.base_sha.slice(0, 12)}</code><CopyButton value={task.base_sha} label="复制基线提交" /></dd>
      </dl><details className="overview-identifiers"><summary>完整路径与标识</summary><dl>
        <dt>任务编号</dt><dd className="fact-sha"><code className="fact-value">{task.task_id}</code><CopyButton value={task.task_id} label="复制完整任务编号" /></dd>
        <dt>仓库路径</dt><dd className="fact-sha"><code className="fact-value">{task.repo_path}</code><CopyButton value={task.repo_path} label="复制仓库路径" /></dd>
        <dt>完整基线</dt><dd><code>{task.base_sha}</code></dd>
      </dl></details></section>
      <section className="overview-fact-group"><h3>执行配置</h3><dl>
        <dt>模型</dt><dd>{task.execution?.model || '未配置执行'}</dd>
        <dt>镜像</dt><dd><code>{task.execution?.image || '—'}</code></dd>
        <dt>轮数上限</dt><dd>{task.execution?.max_model_calls != null ? `${task.execution.max_model_calls} 轮` : '—'}</dd>
        <dt>停止原因</dt><dd>{stopReason}</dd>
      </dl></section>
      <section className="overview-fact-group"><h3>运行时间</h3><dl>
        <dt>创建</dt><dd>{formatDate(task.created_at)}</dd>
        <dt>开始</dt><dd>{formatDate(task.started_at)}</dd>
        <dt>结束</dt><dd>{formatDate(task.finished_at)}</dd>
      </dl></section>
    </aside>
  </div>
}
