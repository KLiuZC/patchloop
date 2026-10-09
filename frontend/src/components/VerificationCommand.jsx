import { lazy, Suspense, useId, useMemo, useState } from 'react'
import { Check, ChevronDown, ChevronUp, Copy, Terminal } from 'lucide-react'
import { describeVerificationCommand } from '../lib/verification-command'

function PlainCode({ code, label }) {
  return <pre tabIndex={0} aria-label={label} className="verification-code-content"><code>{code}</code></pre>
}

const HighlightedCode = lazy(() => import('./VerificationHighlight').catch(() => ({ default: PlainCode })))

export default function VerificationCommand({ command, label = '验证命令' }) {
  if (typeof command !== 'string' || !command) return null
  return <CommandCard key={command} command={command} label={label} />
}

function CommandCard({ command, label }) {
  const preview = useMemo(() => describeVerificationCommand(command), [command])
  const [showOriginal, setShowOriginal] = useState(false)
  const [expanded, setExpanded] = useState(false)
  const [copyStatus, setCopyStatus] = useState('')
  const regionId = useId()
  const titleId = useId()
  const code = showOriginal ? command : preview.code
  const language = showOriginal ? 'bash' : preview.language
  const codeLabel = preview.extracted && !showOriginal ? 'Python 脚本预览' : '原始验证命令'
  const longCode = code.length > 480 || code.split('\n').length > 12

  async function copyOriginal() {
    try {
      await navigator.clipboard.writeText(command)
      setCopyStatus('已复制原始命令')
    } catch {
      setCopyStatus('复制失败，请打开原始命令后手动选择复制。')
    }
  }

  return <section className="verification-command-card" aria-labelledby={titleId}>
    <div className="verification-code-toolbar"><div className="verification-code-title"><Terminal size={15} aria-hidden="true" /><span id={titleId}>{label}</span><span className="verification-code-language">{language === 'python' ? 'Python' : 'Shell'}</span></div>
      <button type="button" className="verification-code-action" onClick={copyOriginal}>
        {copyStatus === '已复制原始命令' ? <Check size={14} aria-hidden="true" /> : <Copy size={14} aria-hidden="true" />}复制原始命令
      </button>
    </div>
    {preview.extracted && <div className="verification-code-options" role="group" aria-label="验证命令显示方式">
      <button type="button" aria-pressed={!showOriginal} onClick={() => { setShowOriginal(false); setExpanded(false) }}>脚本预览</button>
      <button type="button" aria-pressed={showOriginal} onClick={() => { setShowOriginal(true); setExpanded(false) }}>原始命令</button>
    </div>}
    <div id={regionId} className={`verification-code-scroll${expanded ? ' is-expanded' : ''}`}>
      {code.length <= 20000 ? <Suspense fallback={<PlainCode code={code} label={codeLabel} />}>
        <HighlightedCode code={code} language={language} label={codeLabel} />
      </Suspense> : <PlainCode code={code} label={codeLabel} />}
    </div>
    {(longCode || copyStatus) && <div className="verification-code-footer"><span role="status">{copyStatus}</span>
      {longCode && <button type="button" className="verification-code-action" aria-expanded={expanded} aria-controls={regionId} onClick={() => setExpanded(!expanded)}>
        {expanded ? <ChevronUp size={14} aria-hidden="true" /> : <ChevronDown size={14} aria-hidden="true" />}{expanded ? '收起代码' : '展开全部'}
      </button>}
    </div>}
  </section>
}
