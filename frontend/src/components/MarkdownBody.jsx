import { lazy, Suspense, useId, useState } from 'react'
import { CopyButton } from './ui'

const MarkdownRenderer = lazy(() => import('./MarkdownRenderer').catch(() => ({ default: PlainBody })))

function PlainBody({ body }) {
  return <pre className="issue-raw-text">{body}</pre>
}

export default function MarkdownBody({ body, baseUrl, collapse = false, label = '正文', rawLabel = '原文',
  heading = '正文', collapseLabel = '查看正文', emptyText = '暂无正文。' }) {
  return <BodyView key={`${baseUrl}\0${body}`} body={body} baseUrl={baseUrl} collapse={collapse}
    label={label} rawLabel={rawLabel} heading={heading} collapseLabel={collapseLabel} emptyText={emptyText} />
}

function BodyView({ body, baseUrl, collapse, label, rawLabel, heading, collapseLabel, emptyText }) {
  const [raw, setRaw] = useState(false)
  const regionId = useId()
  const prefix = `markdown-${useId().replace(/[^a-zA-Z0-9_-]/g, '')}-`
  const content = body ? <div className="issue-body">
    <div id={regionId} className="issue-body-content" role="region" aria-label={raw ? rawLabel : label} tabIndex={0}>
      {raw ? <PlainBody body={body} /> : <Suspense fallback={<PlainBody body={body} />}>
        <MarkdownRenderer body={body} baseUrl={baseUrl} prefix={prefix} />
      </Suspense>}
    </div>
  </div> : <p className="issue-body-empty">{emptyText}</p>
  return <div className="issue-body-view">
    {collapse ? <details className="issue-description"><summary>{collapseLabel}</summary>{content}</details>
      : <><div className="issue-body-heading"><span className="issue-section-label">{heading}</span></div>{content}</>}
    {body && <div className="issue-body-toolbar"><button type="button" className="text-action" aria-pressed={raw}
      aria-controls={regionId} onClick={() => setRaw(!raw)}>{raw ? '返回排版' : '查看原文'}</button>
      {raw && <CopyButton value={body} label={`复制 ${rawLabel}`} />}
    </div>}
  </div>
}
