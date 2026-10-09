import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import IssueBody from './IssueBody'
import IssueSourcePanel from './IssueSourcePanel'
import { issueTask } from '../test/github-fixtures'

const issueUrl = 'https://github.com/example/repair-demo/issues/42'
const markdown = `## 问题

调用 \`parse_version("v1.2")\` 时 **应正常解析**。

## 期望行为

- 保留普通版本号
- 拒绝重复前缀

> 只修改源码。

| 输入 | 结果 |
| --- | --- |
| v1.2 | 通过 |

- [x] 已复现
- [ ] 待修复

~~旧方案~~

\`\`\`python
if version.startswith("v"):
    version = version[1:]
\`\`\`
`

afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals() })

describe('Issue Markdown presentation', () => {
  it('renders headings, lists, inline code, quotes, tables, tasks and code blocks', async () => {
    render(<IssueBody body={markdown} issueUrl={issueUrl} />)
    const body = screen.getByRole('region', { name: 'Issue 正文', exact: true })
    expect(await within(body).findByRole('heading', { name: '问题' })).toBeVisible()
    expect(within(body).getByText('应正常解析').tagName).toBe('STRONG')
    expect(within(body).getByText('parse_version("v1.2")').tagName).toBe('CODE')
    expect(within(body).getByText('保留普通版本号').tagName).toBe('LI')
    expect(within(body).getByText('只修改源码。').closest('blockquote')).not.toBeNull()
    expect(within(body).getByRole('table')).toBeVisible()
    const boxes = within(body).getAllByRole('checkbox')
    expect(boxes).toHaveLength(2)
    expect(boxes[0]).toBeChecked(); expect(boxes[1]).not.toBeChecked()
    boxes.forEach((box) => expect(box).toBeDisabled())
    expect(within(body).getByText('旧方案').tagName).toBe('DEL')
    expect(within(body).getByLabelText('Markdown 代码块').textContent).toBe('if version.startswith("v"):\n    version = version[1:]\n')
  })

  it('switches to and copies the exact original without altering whitespace or markup', async () => {
    const user = userEvent.setup()
    const copy = vi.spyOn(navigator.clipboard, 'writeText').mockResolvedValue()
    const original = '## 标题\r\n\r\n  `value`\t\n\n尾部空格  \n'
    render(<IssueBody body={original} issueUrl={issueUrl} />)
    await screen.findByRole('heading', { name: '标题' })
    await user.click(screen.getByRole('button', { name: '查看原文' }))
    expect(screen.getByRole('region', { name: 'Issue 原文' }).textContent).toBe(original)
    await user.click(screen.getByRole('button', { name: '复制 Issue 原文' }))
    expect(copy).toHaveBeenCalledWith(original)
    await user.click(screen.getByRole('button', { name: '返回排版' }))
    expect(await screen.findByRole('heading', { name: '标题' })).toBeVisible()
  })

  it.each(['javascript:alert(1)', 'data:text/html,bad', 'vbscript:msgbox(1)', 'file:///tmp/private', 'https://user:pass@example.com/'])('does not activate unsafe link %s', async (url) => {
    render(<IssueBody body={`[链接](${url})`} issueUrl={issueUrl} />)
    await waitFor(() => expect(document.querySelector('.issue-markdown')).not.toBeNull())
    expect(screen.queryByRole('link')).not.toBeInTheDocument()
    expect(screen.getByText('链接')).toBeVisible()
  })

  it('resolves relative links against GitHub and isolates external navigation', async () => {
    render(<IssueBody issueUrl={issueUrl} body={'[相关问题](43) [仓库](/example/repair-demo) [文档](https://example.com/docs)'} />)
    const link = await screen.findByRole('link', { name: '相关问题' })
    expect(link).toHaveAttribute('href', 'https://github.com/example/repair-demo/issues/43')
    expect(screen.getByRole('link', { name: '仓库' })).toHaveAttribute('href', 'https://github.com/example/repair-demo')
    expect(screen.getByRole('link', { name: '文档' })).toHaveAttribute('target', '_blank')
    expect(link).toHaveAttribute('rel', 'noreferrer noopener')
  })

  it('ignores raw HTML and renders code literally without executing or fetching it', async () => {
    const original = '<script>window.hacked = true</script>\n\n<img src="/api/private" onerror="alert(1)">\n\n```html\n<button onclick="alert(1)">example</button>\n```'
    const fetch = vi.fn(() => { throw new Error('unexpected request') })
    vi.stubGlobal('fetch', fetch)
    const { container } = render(<IssueBody body={original} issueUrl={issueUrl} />)
    const code = await screen.findByLabelText('Markdown 代码块')
    expect(code).toHaveTextContent('<button onclick="alert(1)">example</button>')
    expect(container.querySelector('script, img, iframe, [onclick]')).toBeNull()
    expect(fetch).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: '查看原文' }))
    expect(screen.getByRole('region', { name: 'Issue 原文' }).textContent).toBe(original)
    expect(container.querySelector('script, img')).toBeNull()
  })

  it('shows Markdown images as attachment links without automatic image loads', async () => {
    const { container } = render(<IssueBody body={'![错误截图](https://example.com/screenshot.png)'} issueUrl={issueUrl} />)
    expect(await screen.findByRole('link', { name: '图片：错误截图' })).toHaveAttribute('href', 'https://example.com/screenshot.png')
    expect(container.querySelector('img')).toBeNull()
  })

  it('gives footnote anchors independent identities in multiple visible bodies', async () => {
    const body = '正文[^1]\n\n[^1]: 补充说明'
    const { container } = render(<><IssueBody body={body} issueUrl={issueUrl} /><IssueBody body={body} issueUrl={issueUrl} /></>)
    await screen.findAllByText('补充说明')
    const links = [...container.querySelectorAll('[data-footnote-ref]')]
    expect(links).toHaveLength(2)
    expect(links[0].getAttribute('href')).not.toBe(links[1].getAttribute('href'))
    links.forEach((link) => {
      expect(document.getElementById(link.getAttribute('href').slice(1))).not.toBeNull()
      expect(link).not.toHaveAttribute('target')
    })
  })

  it('keeps the source collapse and original evidence intact in the shared source panel', async () => {
    const { source } = issueTask({ goal: '修复问题' })
    source.issue.body = markdown
    const original = structuredClone(source)
    const user = userEvent.setup()
    render(<IssueSourcePanel source={source} baseSha={source.base_sha} repoPath={source.repo_path} embedded collapseBody />)
    const heading = await screen.findByRole('heading', { name: '问题', hidden: true })
    expect(heading).not.toBeVisible()
    await user.click(screen.getByText('查看 Issue 正文'))
    expect(heading).toBeVisible()
    expect(source).toEqual(original)
  })

  it('resets raw view when changing Issue and displays an empty-body message', async () => {
    const { rerender } = render(<IssueBody body={'## 原问题'} issueUrl={issueUrl} />)
    await screen.findByRole('heading', { name: '原问题' })
    fireEvent.click(screen.getByRole('button', { name: '查看原文' }))
    rerender(<IssueBody body={'## 新问题'} issueUrl={issueUrl.replace('42', '43')} />)
    expect(await screen.findByRole('heading', { name: '新问题' })).toBeVisible()
    expect(screen.queryByRole('region', { name: 'Issue 原文' })).not.toBeInTheDocument()
    rerender(<IssueBody body="" issueUrl={issueUrl} />)
    expect(screen.getByText('此 Issue 没有正文。')).toBeVisible()
  })
})
