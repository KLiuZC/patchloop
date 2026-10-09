import Markdown, { defaultUrlTransform } from 'react-markdown'
import remarkGfm from 'remark-gfm'

function resolveLink(value, baseUrl) {
  const safe = defaultUrlTransform(value)
  if (!safe) return ''
  if (safe.startsWith('#')) return safe
  try {
    const url = new URL(safe, baseUrl)
    return ['https:', 'http:', 'mailto:'].includes(url.protocol) && !url.username && !url.password ? url.href : ''
  } catch { return '' }
}

function MarkdownLink({ href, title, children, id, ...props }) {
  if (!href) return <span>{children}</span>
  return <a href={href} title={title} id={id} target={href.startsWith('#') ? undefined : '_blank'}
    rel={href.startsWith('#') ? undefined : 'noreferrer noopener'}
    aria-label={props['aria-label']} aria-describedby={props['aria-describedby']}
    data-footnote-ref={props['data-footnote-ref']} data-footnote-backref={props['data-footnote-backref']}>{children}</a>
}

function MarkdownImage({ src, alt }) {
  // Keep attachments accessible without fetching external images on page load.
  return <MarkdownLink href={src}>图片：{alt || '查看附件'}</MarkdownLink>
}

function MarkdownTable({ children }) {
  return <div className="issue-markdown-table" tabIndex={0} role="region" aria-label="可横向滚动的表格"><table>{children}</table></div>
}

function MarkdownCodeBlock({ children }) {
  return <pre tabIndex={0} aria-label="Markdown 代码块">{children}</pre>
}

function MarkdownHeading({ node, children, id }) {
  const level = Number(node.tagName.slice(1))
  const Tag = `h${Math.min(6, level + 3)}`
  return <Tag className={`issue-markdown-heading issue-markdown-h${level}`} id={id}>{children}</Tag>
}

const components = {
  a: MarkdownLink, img: MarkdownImage, table: MarkdownTable, pre: MarkdownCodeBlock,
  h1: MarkdownHeading, h2: MarkdownHeading, h3: MarkdownHeading,
  h4: MarkdownHeading, h5: MarkdownHeading, h6: MarkdownHeading,
}

export default function MarkdownRenderer({ body, baseUrl, prefix }) {
  return <div className="issue-markdown"><Markdown remarkPlugins={[remarkGfm]} skipHtml
    remarkRehypeOptions={{ clobberPrefix: prefix, footnoteLabel: '注释', footnoteBackLabel: '返回正文' }}
    urlTransform={(value) => resolveLink(value, baseUrl)} components={components}>{body}</Markdown></div>
}
