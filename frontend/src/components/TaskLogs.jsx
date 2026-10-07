import { useQuery } from '@tanstack/react-query'
import { useMemo } from 'react'
import { Download, MessageSquareText, Terminal, Timer } from 'lucide-react'
import { artifactUrl, getArtifact } from '../lib/api'
import { parseTrajectory } from '../lib/artifacts'
import { formatDuration, isActive } from '../lib/task'
import { EmptyState, ErrorNotice, LoadingState } from './ui'

function toolCommand(tool) {
  try {
    const args = JSON.parse(tool.arguments)
    return args.cmd || args.command || args.summary || tool.arguments
  } catch {
    return tool.arguments
  }
}

export default function TaskLogs({ task }) {
  const query = useQuery({
    queryKey: ['artifact', task.task_id, 'trajectory.jsonl', task.status, task.updated_at],
    queryFn: ({ signal }) => getArtifact(task.task_id, 'trajectory.jsonl', { signal }),
    enabled: Boolean(task.artifacts_dir), refetchInterval: isActive(task) ? 2500 : false,
    placeholderData: (previous) => previous,
  })
  const log = useMemo(() => parseTrajectory(query.data || ''), [query.data])
  if (query.isLoading) return <LoadingState label="正在读取执行日志…" />
  if (query.isError) return <ErrorNotice error={query.error} onRetry={() => query.refetch()} />
  if (query.data == null) return <EmptyState icon={Terminal} title="执行日志尚未生成">任务开始执行后，这里会显示每轮工具调用与返回结果。</EmptyState>
  return <section className="log-panel"><div className="code-toolbar"><span><Terminal size={16} />执行轨迹 <span className="count-chip">{log.turns.length} 轮</span></span><a className="text-action" href={artifactUrl(task.task_id, 'trajectory.jsonl')} download><Download size={14} />原始日志</a></div>
    {log.malformedLines > 0 && <div className="notice notice-warning">有 {log.malformedLines} 条日志无法解析，可下载原始文件查看。</div>}
    {!log.turns.length && <EmptyState icon={Terminal} title="尚无完整的模型轮次">{isActive(task) ? '等待模型返回。已经保存的内容可以通过原始日志查看。' : '本次运行没有可展示的完整轮次，请下载原始日志核对。'}</EmptyState>}
    <div className="turn-list">{log.turns.map((turn, index) => <article className="turn" key={`${turn.turn}-${index}`}><div className="turn-marker">{turn.turn}</div><div className="turn-body"><div className="turn-heading"><strong>第 {turn.turn} 轮</strong><span><Timer size={12} />{formatDuration(turn.seconds)}</span></div>
      {turn.content && <p className="model-message"><MessageSquareText size={14} />{turn.content}</p>}
      {turn.tools.map((tool, toolIndex) => <details className="tool-output" key={`${tool.id}-${toolIndex}`} open={index === log.turns.length - 1}><summary><Terminal size={14} /><code>{String(toolCommand(tool))}</code><span className="tool-name">{tool.name}</span></summary><pre>{tool.output || '未记录输出'}</pre></details>)}
      {!turn.tools.length && !turn.content && <p className="muted">本轮没有工具调用。</p>}
    </div></article>)}</div>
    {log.hasPartialLine && <p className="log-footnote">{isActive(task) ? '日志仍在写入，稍后会自动补全。' : '最后一条日志不完整，已显示可解析内容。可下载原始文件核对。'}</p>}
  </section>
}
