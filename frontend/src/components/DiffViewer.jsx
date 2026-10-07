import { useMemo } from 'react'
import { FileCode2, FileDiff } from 'lucide-react'
import { parseDiff } from '../lib/artifacts'
import { EmptyState } from './ui'

export default function DiffViewer({ patch }) {
  const diff = useMemo(() => parseDiff(patch || ''), [patch])
  if (!patch) return <EmptyState icon={FileDiff} title="尚无候选补丁">任务可能还在执行，或本次没有产生代码改动。</EmptyState>
  return <section className="diff-panel" aria-label="代码差异"><div className="code-toolbar"><span><FileCode2 size={16} />patch.diff</span><div className="diff-stats"><span>{diff.files} 个文件</span><span className="added">+{diff.added}</span><span className="removed">−{diff.removed}</span></div></div>
    <div className="diff-scroll" tabIndex={0} aria-label="可横向滚动的补丁内容"><table className="diff-table"><tbody>{diff.rows.map((row, index) => <tr className={`diff-${row.type}`} key={index}><td className="line-number" aria-hidden="true">{row.oldLine ?? ''}</td><td className="line-number" aria-hidden="true">{row.newLine ?? ''}</td><td className="diff-code"><code>{row.text || ' '}</code></td></tr>)}</tbody></table></div>
  </section>
}
