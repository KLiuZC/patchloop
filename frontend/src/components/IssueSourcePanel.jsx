import { ArrowUpRight, FolderGit2 } from 'lucide-react'
import { validTaskSource } from '../lib/github'
import { formatDate } from '../lib/task'
import { CopyButton } from './ui'
import GitHubIcon from './GitHubIcon'

export default function IssueSourcePanel({ source, baseSha, repoPath, collapseBody = false, embedded = false }) {
  if (source == null) return null
  if (!validTaskSource(source) || source.base_sha !== baseSha || source.repo_path !== repoPath) {
    return <div className="notice notice-warning" role="alert">问题来源与任务版本不一致，暂不展示来源链接。</div>
  }
  const issue = source.issue
  const Heading = embedded ? 'h3' : 'h2'
  const IssueHeading = embedded ? 'h4' : 'h3'
  return <section className={`${embedded ? 'review-source' : 'panel'} issue-source-panel`}><div className="panel-heading"><Heading>问题来源</Heading>{!embedded && <FolderGit2 size={18} />}</div>
    <a href={issue.url} target="_blank" rel="noreferrer"><GitHubIcon />{issue.repository.full_name} #{issue.issue_number}<ArrowUpRight size={14} /></a>
    <IssueHeading className="issue-source-title">{issue.title}</IssueHeading>{collapseBody
      ? <details className="issue-description"><summary>查看 Issue 正文</summary><p className="issue-body">{issue.body || '此 Issue 没有正文。'}</p></details>
      : <p className="issue-body">{issue.body || '此 Issue 没有正文。'}</p>}
    <details><summary>来源记录</summary><dl><dt>读取时间</dt><dd>{formatDate(issue.fetched_at)}</dd><dt>代码版本</dt><dd>{source.resolved_ref}</dd><dt>来源编号</dt><dd className="mono">{issue.source_id}</dd><dt>准备编号</dt><dd className="mono">{source.preparation_id}</dd><dt>快照 SHA-256</dt><dd className="fact-sha"><code className="fact-value">{issue.snapshot_sha256}</code><CopyButton value={issue.snapshot_sha256} label="复制来源摘要" /></dd></dl></details>
  </section>
}
