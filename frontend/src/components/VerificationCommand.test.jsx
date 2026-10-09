import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import VerificationCommand from './VerificationCommand'

const command = readFileSync(resolve('../evals/profiles/github-flask-6146/verification_command.txt'), 'utf8').trimEnd()
const shellScript = readFileSync(resolve('../evals/profiles/github-flask-6146/verify.sh'), 'utf8')
const script = shellScript.slice("python - <<'PY'\n".length, -'PY\n'.length)

afterEach(() => vi.restoreAllMocks())

async function highlighted(label) {
  await waitFor(() => expect(screen.getByLabelText(label).querySelector('.token')).not.toBeNull())
  return screen.getByLabelText(label)
}

function sourceText(element) {
  const clone = element.cloneNode(true)
  clone.querySelectorAll('.linenumber').forEach((number) => number.remove())
  return clone.textContent
}

describe('verification command display', () => {
  it('shows readable Python with highlighting, line numbers and expandable height', async () => {
    const user = userEvent.setup()
    render(<VerificationCommand command={command} />)
    const code = await highlighted('Python 脚本预览')
    expect(sourceText(code)).toBe(script)
    expect(code.querySelector('.linenumber')).not.toBeNull()
    expect(screen.getByRole('button', { name: '脚本预览' })).toHaveAttribute('aria-pressed', 'true')
    const expand = screen.getByRole('button', { name: '展开全部' })
    expect(expand).toHaveAttribute('aria-expanded', 'false')
    await user.click(expand)
    expect(screen.getByRole('button', { name: '收起代码' })).toHaveAttribute('aria-expanded', 'true')
    expect(code.parentElement).toHaveClass('is-expanded')
    await user.click(screen.getByRole('button', { name: '收起代码' }))
    expect(code.parentElement).not.toHaveClass('is-expanded')
  })

  it('switches views without losing any command bytes and always copies the original', async () => {
    const user = userEvent.setup()
    const copy = vi.spyOn(navigator.clipboard, 'writeText').mockResolvedValue()
    render(<VerificationCommand command={command} />)
    await highlighted('Python 脚本预览')
    await user.click(screen.getByRole('button', { name: '复制原始命令' }))
    expect(copy).toHaveBeenLastCalledWith(command)
    expect(await screen.findByRole('status')).toHaveTextContent('已复制原始命令')
    await user.click(screen.getByRole('button', { name: '原始命令', exact: true }))
    expect(sourceText(await highlighted('原始验证命令'))).toBe(command)
    await user.click(screen.getByRole('button', { name: '复制原始命令' }))
    expect(copy).toHaveBeenLastCalledWith(command)
    await user.click(screen.getByRole('button', { name: '脚本预览' }))
    expect(sourceText(await highlighted('Python 脚本预览'))).toBe(script)
  })

  it('keeps simple shell commands and whitespace intact', async () => {
    const text = '  python -m pytest -q  '
    render(<VerificationCommand command={text} label="固定验证命令" />)
    expect(sourceText(await highlighted('原始验证命令'))).toBe(text)
    expect(screen.getByRole('region', { name: '固定验证命令' })).toBeVisible()
    expect(screen.queryByRole('button', { name: '脚本预览' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '展开全部' })).not.toBeInTheDocument()
  })

  it('shows clipboard failure and resets view state when the command changes', async () => {
    const user = userEvent.setup()
    vi.spyOn(navigator.clipboard, 'writeText').mockRejectedValue(new Error('denied'))
    const view = render(<VerificationCommand command={command} />)
    await user.click(screen.getByRole('button', { name: '复制原始命令' }))
    expect(await screen.findByRole('status')).toHaveTextContent('复制失败')
    await user.click(screen.getByRole('button', { name: '原始命令', exact: true }))
    view.rerender(<VerificationCommand command="pytest -q" />)
    expect(sourceText(await highlighted('原始验证命令'))).toBe('pytest -q')
    expect(screen.queryByText('复制失败', { exact: false })).not.toBeInTheDocument()
  })

  it('renders source containing HTML as text and makes independent cards accessible', async () => {
    const html = 'echo "<img src=x onerror=alert(1)>"'
    render(<><VerificationCommand command={html} /><VerificationCommand command="pytest -q" label="固定验证命令" /></>)
    const region = screen.getByRole('region', { name: '验证命令', exact: true })
    await waitFor(() => expect(within(region).getByLabelText('原始验证命令').querySelector('.token')).not.toBeNull())
    expect(sourceText(within(region).getByLabelText('原始验证命令'))).toBe(html)
    expect(region.querySelector('img')).toBeNull()
    expect(screen.getByRole('region', { name: '固定验证命令' })).toBeVisible()
  })
})
