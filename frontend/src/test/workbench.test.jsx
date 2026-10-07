import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter } from 'react-router-dom'
import { onlineManager, QueryClientProvider } from '@tanstack/react-query'
import { MotionConfig } from 'motion/react'
import App from '../App'
import { buildTaskPayload, defaultForm, validateTaskForm } from '../lib/task'
import { jsonResponse, secondId, taskId, taskRecord } from './fixtures'
import { createQueryClient } from '../lib/query-client'

const clients = []
function mount(path = '/tasks') {
  const client = createQueryClient()
  client.setDefaultOptions({ ...client.getDefaultOptions(), queries: { ...client.getDefaultOptions().queries, retry: false, gcTime: 0 } })
  clients.push(client)
  const view = render(<QueryClientProvider client={client}><MotionConfig reducedMotion="always"><MemoryRouter initialEntries={[path]}><App /></MemoryRouter></MotionConfig></QueryClientProvider>)
  return { ...view, client }
}

afterEach(() => {
  clients.splice(0).forEach((client) => client.clear())
  onlineManager.setOnline(true)
  vi.unstubAllGlobals()
})

describe('任务工作台', () => {
  it('keeps execution completion separate from failed validation', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({ items: [taskRecord({ status: 'completed', validation_status: 'failed' })], next_cursor: null })))
    mount()
    expect(await screen.findByText('验证未通过')).toBeVisible()
    expect(within(screen.getByRole('list')).getByText('执行完成')).toBeVisible()
    expect(screen.queryByText('验证通过')).not.toBeInTheDocument()
    expect(screen.getByRole('link', { name: /修复版本号前缀/ })).toHaveAttribute('href', `/tasks/${taskId}`)
  })

  it('loads another page and starts a fresh cursor when filters change', async () => {
    const fetch = vi.fn(async (url) => {
      const params = new URL(url, 'http://localhost').searchParams
      if (params.get('status') === 'error') return jsonResponse({ items: [taskRecord({ task_id: secondId, goal: '失败任务', status: 'error' })], next_cursor: null })
      if (params.get('cursor')) return jsonResponse({ items: [taskRecord({ task_id: secondId, goal: '第二页任务' })], next_cursor: null })
      return jsonResponse({ items: [taskRecord()], next_cursor: 'next-page' })
    })
    vi.stubGlobal('fetch', fetch)
    const user = userEvent.setup()
    mount()
    await user.click(await screen.findByRole('button', { name: '加载更多' }))
    expect(await screen.findByText('第二页任务')).toBeVisible()
    await user.click(screen.getByRole('combobox', { name: '执行状态筛选' }))
    await user.click(await screen.findByRole('option', { name: '执行出错' }))
    expect(await screen.findByText('失败任务')).toBeVisible()
    expect(screen.queryByText('第二页任务')).not.toBeInTheDocument()
    const filtered = fetch.mock.calls.filter(([url]) => url.includes('status=error'))
    expect(filtered.length).toBeGreaterThan(0)
    expect(filtered.every(([url]) => !url.includes('cursor='))).toBe(true)
    await user.click(screen.getByRole('combobox', { name: '执行状态筛选' }))
    await user.click(await screen.findByRole('option', { name: '所有执行状态' }))
    expect(await screen.findByText('修复版本号前缀')).toBeVisible()
    expect(screen.getByRole('combobox', { name: '执行状态筛选' })).toHaveTextContent('所有执行状态')
  })

  it('changes provider defaults and preserves automatic architecture in submitted data', async () => {
    const fetch = vi.fn(async () => jsonResponse({ detail: '模拟的提交失败' }, 422))
    vi.stubGlobal('fetch', fetch)
    const user = userEvent.setup()
    mount('/tasks/new')
    await user.click(screen.getByRole('button', { name: '填入示例' }))
    const provider = screen.getByRole('combobox', { name: '模型提供方' })
    await user.click(provider)
    await user.click(await screen.findByRole('option', { name: '智谱 GLM' }))
    expect(screen.getByLabelText('模型名称')).toHaveValue('glm-5.3-flash')
    expect(provider).toHaveFocus()
    await user.click(screen.getByText('高级设置'))
    const platform = screen.getByRole('combobox', { name: '镜像架构' })
    await user.click(platform)
    await user.click(await screen.findByRole('option', { name: 'linux/amd64' }))
    expect(platform).toHaveTextContent('linux/amd64')
    await user.click(platform)
    await user.click(await screen.findByRole('option', { name: '按本地镜像自动选择' }))
    expect(fetch).not.toHaveBeenCalled()
    await user.click(screen.getByRole('button', { name: '提交任务' }))
    await screen.findByRole('alert')
    const submitted = JSON.parse(fetch.mock.calls[0][1].body)
    expect(submitted.execution).toMatchObject({ provider: 'glm', model: 'glm-5.3-flash', platform: null })
    expect(fetch).toHaveBeenCalledTimes(1)
  })

  it('shows a useful empty state without inventing tasks', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({ items: [], next_cursor: null })))
    mount()
    expect(await screen.findByRole('heading', { name: '开始你的第一次修复' })).toBeVisible()
    expect(screen.queryByRole('list')).not.toBeInTheDocument()
  })

  it('validates required fields and sends a double-clicked submission once', async () => {
    let resolvePost
    let submitted
    const fetch = vi.fn((url, options) => {
      if (options.method === 'POST') {
        submitted = JSON.parse(options.body)
        return new Promise((resolve) => { resolvePost = resolve })
      }
      if (url.startsWith(`/api/tasks/${taskId}`)) return Promise.resolve(jsonResponse(taskRecord({ goal: submitted.goal })))
      return Promise.resolve(jsonResponse({ items: [], next_cursor: null }))
    })
    vi.stubGlobal('fetch', fetch)
    const user = userEvent.setup()
    mount('/tasks/new')
    await user.click(screen.getByRole('button', { name: '提交任务' }))
    expect(screen.getByText('请填写仓库目录')).toBeVisible()
    expect(screen.getByLabelText('仓库目录')).toHaveFocus()
    expect(fetch).not.toHaveBeenCalled()
    await user.click(screen.getByRole('button', { name: '填入示例' }))
    await user.dblClick(screen.getByRole('button', { name: '提交任务' }))
    expect(fetch.mock.calls.filter(([, options]) => options.method === 'POST')).toHaveLength(1)
    expect(screen.getByRole('combobox', { name: '模型提供方' })).toBeDisabled()
    expect(submitted.execution.max_model_calls).toBe(10)
    await act(async () => resolvePost(jsonResponse(taskRecord({ goal: submitted.goal }), 201)))
    expect(await screen.findByRole('heading', { level: 1, name: submitted.goal })).toBeVisible()
    expect(screen.getByText('已进入队列，等待 worker 领取')).toBeVisible()
  })

  it('preserves form values on failed submission and does not retry', async () => {
    const fetch = vi.fn(async () => jsonResponse({ detail: '仓库不可用' }, 422))
    vi.stubGlobal('fetch', fetch)
    const user = userEvent.setup()
    mount('/tasks/new')
    await user.click(screen.getByRole('button', { name: '填入示例' }))
    await user.click(screen.getByRole('button', { name: '提交任务' }))
    expect(await screen.findByText('仓库不可用')).toBeVisible()
    expect(screen.getByLabelText('仓库目录')).toHaveValue('examples/toy_repo')
    expect(screen.getByRole('button', { name: '提交任务' })).toBeEnabled()
    expect(fetch).toHaveBeenCalledTimes(1)
  })

  it('shows connection errors instead of waiting forever when the browser is offline', async () => {
    onlineManager.setOnline(false)
    vi.stubGlobal('fetch', vi.fn(async () => { throw new TypeError('network offline') }))
    mount()
    expect(await screen.findByRole('alert')).toHaveTextContent('网络连接失败')
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })

  it('does not defer an offline submission and send it after reconnecting', async () => {
    onlineManager.setOnline(false)
    const fetch = vi.fn(async () => { throw new TypeError('network offline') })
    vi.stubGlobal('fetch', fetch)
    const user = userEvent.setup()
    mount('/tasks/new')
    await user.click(screen.getByRole('button', { name: '填入示例' }))
    await user.click(screen.getByRole('button', { name: '提交任务' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('提交结果未确认')
    expect(screen.getByRole('button', { name: '提交任务' })).toBeEnabled()
    act(() => onlineManager.setOnline(true))
    expect(fetch).toHaveBeenCalledTimes(1)
  })

  it('retains command and path whitespace while rejecting invalid budgets', () => {
    const form = { ...defaultForm, repo_path: '/repo with space ', goal: 'fix', verification_command: 'test "foo " = foo\\ ' }
    expect(validateTaskForm(form)).toEqual({})
    expect(buildTaskPayload(form).repo_path).toBe(form.repo_path)
    expect(buildTaskPayload(form).verification_command).toBe(form.verification_command)
    expect(validateTaskForm({ ...form, max_model_calls: '1.5' })).toHaveProperty('max_model_calls')
    expect(validateTaskForm({ ...form, memory_mb: '0' })).toHaveProperty('memory_mb')
  })

  it('freezes visible path rules and preserves meaningful pattern whitespace', () => {
    const form = { ...defaultForm, repo_path: '/repo', goal: 'fix', allowed_paths: 'src/**\n spaced name.py ', protected_paths: 'tests/**\n**/tests/**' }
    expect(validateTaskForm(form)).toEqual({})
    expect(buildTaskPayload(form).path_policy).toEqual({ allowed_paths: ['src/**', ' spaced name.py '], protected_paths: ['tests/**', '**/tests/**'] })
    expect(buildTaskPayload({ ...form, protected_paths: undefined }).path_policy.protected_paths).toContain('**/test_*.py')
    expect(buildTaskPayload({ ...form, protected_paths: '' }).path_policy.protected_paths).toEqual([])
    expect(validateTaskForm({ ...form, protected_paths: '../tests/**' })).toHaveProperty('protected_paths')
    expect(validateTaskForm({ ...form, allowed_paths: '/src/**' })).toHaveProperty('allowed_paths')
  })

  it('handles a missing task without crashing the whole page', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({ detail: 'Task not found' }, 404)))
    mount(`/tasks/${taskId}`)
    expect(await screen.findByRole('alert')).toHaveTextContent('Task not found')
    expect(screen.getByRole('link', { name: '返回任务列表' })).toBeVisible()
  })

  it('supports keyboard tabs and renders model/tool output as text', async () => {
    const record = taskRecord({ status: 'completed', artifacts_dir: '/artifacts', result: { turns: 1, seconds: 1, cost: null, patch: '' } })
    const output = '<img src=x onerror=alert(1)>'
    const trace = JSON.stringify({ type: 'turn', turn: 1, seconds: 1, assistant: { content: '检查结束', reasoning_content: 'private reasoning' }, tools: [{ id: 'tool', name: 'bash', arguments: '{"cmd":"echo ok"}', output }] }) + '\n'
    vi.stubGlobal('fetch', vi.fn(async (url) => url.includes('/artifacts/') ? new Response(trace) : jsonResponse(record)))
    const user = userEvent.setup()
    mount(`/tasks/${taskId}`)
    const overview = await screen.findByRole('tab', { name: '概览' })
    overview.focus()
    await user.keyboard('{ArrowRight}')
    expect(screen.getByRole('tab', { name: '代码改动' })).toHaveFocus()
    expect(screen.getByRole('tab', { name: '代码改动' })).toHaveAttribute('aria-selected', 'true')
    await user.keyboard('{ArrowRight}')
    await waitFor(() => expect(screen.getByText(output)).toBeVisible())
    expect(screen.queryByText('private reasoning')).not.toBeInTheDocument()
    expect(document.querySelector('img[src="x"]')).toBeNull()
    expect(screen.getByRole('tabpanel', { name: '执行日志' })).toBeInTheDocument()
  })

  it('fetches final logs when a running task becomes terminal', async () => {
    const running = taskRecord({ status: 'running', artifacts_dir: '/artifacts' })
    let trace = JSON.stringify({ type: 'turn', turn: 1, assistant: { content: '执行中' }, tools: [] }) + '\n'
    vi.stubGlobal('fetch', vi.fn(async (url) => url.includes('/artifacts/') ? new Response(trace) : jsonResponse(running)))
    const user = userEvent.setup()
    const { client } = mount(`/tasks/${taskId}`)
    await user.click(await screen.findByRole('tab', { name: '执行日志' }))
    await waitFor(() => expect(screen.getByText('执行中')).toBeVisible())
    trace += JSON.stringify({ type: 'turn', turn: 2, assistant: { content: '最终结果已保存' }, tools: [] }) + '\n'
    act(() => client.setQueryData(['task', taskId], { ...running, status: 'completed', updated_at: '2026-10-07T07:10:00Z' }))
    expect(await screen.findByText('最终结果已保存')).toBeVisible()
  })

  it('labels a missing execution configuration as storage-only', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(taskRecord({ execution: null }))))
    mount(`/tasks/${taskId}`)
    expect(await screen.findByText('这是一条仅保存的任务记录')).toBeVisible()
    expect(screen.queryByText('已进入队列，等待 worker 领取')).not.toBeInTheDocument()
  })

  it('keeps the full task id available when clipboard access fails', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(taskRecord())))
    const user = userEvent.setup()
    vi.spyOn(navigator.clipboard, 'writeText').mockRejectedValue(new Error('permission denied'))
    mount(`/tasks/${taskId}`)
    await user.click(await screen.findByRole('button', { name: '复制编号', exact: true }))
    expect(await screen.findByText('复制失败，请手动选择文本')).toHaveAttribute('role', 'status')
    expect(screen.getByText(taskId)).toBeVisible()
  })

  it.each(['broken\n', '{"type":"turn"'])('keeps malformed or partial logs downloadable even with zero turns: %s', async (trace) => {
    const record = taskRecord({ status: 'interrupted', artifacts_dir: '/artifacts' })
    vi.stubGlobal('fetch', vi.fn(async (url) => url.includes('/artifacts/') ? new Response(trace) : jsonResponse(record)))
    const user = userEvent.setup()
    mount(`/tasks/${taskId}`)
    await user.click(await screen.findByRole('tab', { name: '执行日志' }))
    await waitFor(() => expect(screen.getByRole('link', { name: '原始日志' })).toBeVisible())
    expect(screen.getByText('尚无完整的模型轮次')).toBeVisible()
    expect(screen.queryByText('执行日志尚未生成')).not.toBeInTheDocument()
    if (trace.endsWith('\n')) expect(screen.getByText(/有 1 条日志无法解析/)).toBeVisible()
    else expect(screen.getByText(/最后一条日志不完整/)).toBeVisible()
  })

  it('rejects malformed result metrics with an error message rather than crashing', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(taskRecord({ status: 'completed', result: { cost: '0.01' } }))))
    mount(`/tasks/${taskId}`)
    expect(await screen.findByRole('alert')).toHaveTextContent('数据格式无效')
    expect(screen.getByRole('link', { name: '返回任务列表' })).toBeVisible()
  })

  it('does not pull the user back into details after leaving a pending submission', async () => {
    let resolvePost
    vi.stubGlobal('fetch', vi.fn((url, options) => options.method === 'POST'
      ? new Promise((resolve) => { resolvePost = resolve })
      : Promise.resolve(jsonResponse({ items: [], next_cursor: null }))))
    const user = userEvent.setup()
    mount('/tasks/new')
    await user.click(screen.getByRole('button', { name: '填入示例' }))
    await user.click(screen.getByRole('button', { name: '提交任务' }))
    await user.click(screen.getByRole('link', { name: '返回任务列表' }))
    await act(async () => resolvePost(jsonResponse(taskRecord(), 201)))
    expect(await screen.findByRole('heading', { name: '修复任务' })).toBeVisible()
    expect(screen.queryByRole('tablist')).not.toBeInTheDocument()
  })

  it('presents independent baseline and candidate validation', async () => {
    const record = taskRecord({ status: 'completed', validation_status: 'inconclusive', result: {
      patch: '', cost: 0, turns: 2, seconds: 1,
      validation: { status: 'inconclusive', command: 'pytest -q', error: 'baseline verification already passed',
        baseline: { status: 'passed', exit_code: 0, output: 'baseline output' },
        candidate: { status: 'passed', exit_code: 0, output: 'candidate output' } },
    } })
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(record)))
    mount(`/tasks/${taskId}`)
    const statuses = await screen.findAllByText('暂无法判定')
    expect(statuses.length).toBeGreaterThan(0)
    expect(screen.getAllByText('验证通过')).toHaveLength(2)
    const before = screen.getByText('修复前 · 干净基线').closest('.verification-card')
    fireEvent.click(within(before).getByText('查看测试输出'))
    await waitFor(() => expect(screen.getByText('baseline output')).toBeVisible())
  })
})
