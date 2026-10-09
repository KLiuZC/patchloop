import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter } from 'react-router-dom'
import { QueryClientProvider } from '@tanstack/react-query'
import { MotionConfig } from 'motion/react'
import App from '../App'
import { createQueryClient } from '../lib/query-client'
import { newGithubDraft, saveGithubDraft } from '../lib/github-draft'
import { jsonResponse, taskId } from './fixtures'
import { issue, issueTask, issueUrl, preparation, preparationId, sourceId } from './github-fixtures'

const clients = []
function mount(path = '/tasks/github') {
  const client = createQueryClient()
  client.setDefaultOptions({ queries: { retry: false, gcTime: 0 } })
  clients.push(client)
  return render(<QueryClientProvider client={client}><MotionConfig reducedMotion="always"><MemoryRouter initialEntries={[path]}><App /></MemoryRouter></MotionConfig></QueryClientProvider>)
}

beforeEach(() => saveGithubDraft(newGithubDraft()))
afterEach(() => { clients.splice(0).forEach((client) => client.clear()); vi.unstubAllGlobals(); vi.restoreAllMocks() })

function server({ taskPost, preparePost, preparationState, importPost } = {}) {
  let source = issue()
  let prepared = preparation()
  let task
  const fetch = vi.fn(async (url, options = {}) => {
    const body = options.body && JSON.parse(options.body)
    if (options.method === 'POST' && url === '/api/sources/github/issues') {
      if (importPost) {
        const result = await importPost(body)
        if (result.ok) source = (await result.clone().json()).source
        return result
      }
      source = issue({ request_id: body.request_id })
      return jsonResponse({ source, replayed: false }, 201)
    }
    if (url === `/api/sources/github/issues/${sourceId}`) return jsonResponse(source)
    if (options.method === 'POST' && url === '/api/repositories/prepare') {
      if (preparePost) return preparePost(body)
      prepared = preparation({ request_id: body.request_id, requested_ref: body.ref, resolved_ref: body.ref || 'main' })
      return jsonResponse({ preparation: prepared, replayed: false }, 201)
    }
    if (url === `/api/repositories/${preparationId}`) return jsonResponse(preparationState?.(prepared) || { preparation: prepared, checkout_intact: true, reason: '' })
    if (options.method === 'POST' && url === '/api/tasks/from-issue') {
      if (taskPost) {
        const result = await taskPost(body, source, prepared)
        if (result.ok) task = (await result.clone().json()).task
        return result
      }
      task = issueTask(body, source, prepared)
      return jsonResponse({ task, replayed: false }, 201)
    }
    if (url === `/api/tasks/${taskId}`) return jsonResponse(task)
    if (url === `/api/tasks/${taskId}/review`) return jsonResponse({ detail: 'not yet reviewed' }, 404)
    if (url.startsWith('/api/tasks?')) return jsonResponse({ items: [], next_cursor: null })
    throw new Error(`unexpected request ${url}`)
  })
  vi.stubGlobal('fetch', fetch)
  return fetch
}

async function readAndPrepare(user) {
  await user.type(screen.getByLabelText('Issue 链接'), issueUrl)
  await user.click(screen.getByRole('button', { name: '读取 Issue' }))
  expect(await screen.findByRole('heading', { name: '版本号无法处理 v 前缀' })).toBeVisible()
  await user.click(screen.getByRole('button', { name: '准备仓库' }))
  expect(await screen.findByText('代码已就绪')).toBeVisible()
}

async function configure(user) {
  await user.type(screen.getByLabelText('修复要求'), '只修复小写 v 前缀，不改变其他行为。')
  await user.type(screen.getByLabelText('Docker 镜像'), 'patchloop-toy')
}

describe('GitHub Issue 创建流程', () => {
  it('keeps repository progress inside the preparation section', async () => {
    let complete
    server({ preparePost: (body) => new Promise((resolve) => {
      complete = () => resolve(jsonResponse({ preparation: preparation({ request_id: body.request_id }), replayed: false }, 201))
    }) })
    const user = userEvent.setup()
    mount()
    await user.type(screen.getByLabelText('Issue 链接'), issueUrl)
    await user.click(screen.getByRole('button', { name: '读取 Issue' }))
    await screen.findByRole('heading', { name: '版本号无法处理 v 前缀' })
    await user.click(screen.getByRole('button', { name: '准备仓库' }))
    const progress = await screen.findByText('正在准备代码…')
    expect(progress.closest('section')).toBe(screen.getByRole('heading', { name: '准备代码' }).closest('section'))
    expect(screen.getByRole('button', { name: '准备仓库' })).toBeDisabled()
    await act(async () => complete())
    expect(await screen.findByText('代码已就绪')).toBeVisible()
    expect(screen.queryByText('正在准备代码…')).not.toBeInTheDocument()
  })

  it('allows cancelling link editing without losing the prepared version', async () => {
    const fetch = server()
    const user = userEvent.setup()
    mount(); await readAndPrepare(user)
    await user.click(screen.getByRole('button', { name: '修改链接' }))
    expect(screen.getByLabelText('Issue 链接')).toHaveValue(issueUrl)
    expect(screen.getByRole('button', { name: '确认并创建任务' })).toBeDisabled()
    await user.clear(screen.getByLabelText('Issue 链接'))
    await user.type(screen.getByLabelText('Issue 链接'), 'https://github.com/example/repair-demo/issues/43')
    await user.click(screen.getByRole('button', { name: '取消修改' }))
    expect(screen.getByText('代码已就绪')).toBeVisible()
    expect(screen.getByRole('link', { name: 'example/repair-demo #42' })).toHaveAttribute('href', issueUrl)
    expect(screen.getByRole('button', { name: '确认并创建任务' })).not.toBeDisabled()
    expect(fetch.mock.calls.filter(([, opts]) => opts?.method === 'POST')).toHaveLength(2)
  })

  it('rereads a changed link with a new UUID and clears the old preparation', async () => {
    const fetch = server({ importPost: (body) => {
      const number = Number(body.url.split('/').at(-1))
      return jsonResponse({ source: issue({ request_id: body.request_id, issue_number: number,
        title: number === 42 ? '版本号无法处理 v 前缀' : '另一个问题',
        requested_url: body.url, url: body.url, api_url: `https://api.github.com/repos/example/repair-demo/issues/${number}` }), replayed: false }, 201)
    } })
    const user = userEvent.setup()
    mount(); await readAndPrepare(user)
    await user.click(screen.getByRole('button', { name: '修改链接' }))
    await user.clear(screen.getByLabelText('Issue 链接'))
    await user.type(screen.getByLabelText('Issue 链接'), 'https://github.com/example/repair-demo/issues/43')
    await user.click(screen.getByRole('button', { name: '重新读取 Issue' }))
    expect(await screen.findByRole('heading', { name: '另一个问题' })).toBeVisible()
    expect(screen.queryByText('代码已就绪')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '确认并创建任务' })).toBeDisabled()
    const posts = fetch.mock.calls.filter(([url, opts]) => url === '/api/sources/github/issues' && opts?.method === 'POST')
    expect(JSON.parse(posts[0][1].body).request_id).not.toBe(JSON.parse(posts[1][1].body).request_id)
    expect(JSON.parse(sessionStorage.getItem('patchloop:github-task-draft:v1')).preparationId).toBeNull()
  })

  it('imports, prepares, creates and displays the frozen Issue in task detail', async () => {
    const fetch = server()
    const user = userEvent.setup()
    mount()
    expect(screen.getByRole('button', { name: '确认并创建任务' })).toBeDisabled()
    await readAndPrepare(user)
    expect(screen.getByLabelText('Docker 镜像')).toHaveValue('')
    await user.click(screen.getByRole('button', { name: '确认并创建任务' }))
    expect(screen.getByText('请填写Docker 镜像')).toBeVisible()
    await configure(user)
    await user.dblClick(screen.getByRole('button', { name: '确认并创建任务' }))
    await waitFor(() => expect(screen.getByRole('heading', { name: '问题来源' })).toBeVisible())
    expect(screen.getByRole('link', { name: 'example/repair-demo #42' })).toHaveAttribute('href', issueUrl)
    await user.click(screen.getByText('查看 Issue 正文'))
    expect(screen.getByText(/输入 v1.2.3 会报错/)).toBeVisible()
    const posts = fetch.mock.calls.filter(([, options]) => options?.method === 'POST')
    expect(posts).toHaveLength(3)
    const body = JSON.parse(posts[2][1].body)
    expect(body).toMatchObject({ preparation_id: preparationId, workdir: '.', execution: { explicit_finish: false, budget_reminders: true } })
    expect(body).not.toHaveProperty('repo_path')
    expect(body).not.toHaveProperty('base_sha')
  })

  it('retains the exact import request after a network failure and reload', async () => {
    let body
    let attempts = 0
    const fetch = server({ importPost: (payload) => {
      attempts += 1
      if (attempts === 1) { body = payload; throw new TypeError('offline') }
      expect(payload).toEqual(body)
      return jsonResponse({ source: issue({ request_id: payload.request_id }), replayed: true })
    } })
    const user = userEvent.setup()
    const view = mount()
    await user.type(screen.getByLabelText('Issue 链接'), issueUrl)
    await user.click(screen.getByRole('button', { name: '读取 Issue' }))
    expect(await screen.findByRole('button', { name: '用原请求重试' })).toBeVisible()
    view.unmount()
    mount()
    await user.click(screen.getByRole('button', { name: '用原请求重试' }))
    expect(await screen.findByRole('heading', { name: '版本号无法处理 v 前缀' })).toBeVisible()
    expect(fetch.mock.calls.filter(([, options]) => options?.method === 'POST')).toHaveLength(2)
  })

  it('rechecks prepared files on reload and blocks modified code', async () => {
    let intact = true
    server({ preparationState: (prepared) => ({ preparation: prepared, checkout_intact: intact, reason: intact ? '' : 'local changes' }) })
    const user = userEvent.setup()
    const view = mount()
    await readAndPrepare(user)
    await configure(user)
    view.unmount(); intact = false
    mount()
    expect(await screen.findByText('代码需要重新检查')).toBeVisible()
    expect(screen.getByRole('button', { name: '确认并创建任务' })).toBeDisabled()
    expect(screen.getByText('local changes')).toBeVisible()
  })

  it('requires preparing again after editing the selected ref', async () => {
    server()
    const user = userEvent.setup()
    mount()
    await readAndPrepare(user)
    await user.type(screen.getByLabelText('代码版本'), 'feature/fix')
    expect(screen.getByRole('button', { name: '确认并创建任务' })).toBeDisabled()
    await user.click(screen.getByRole('button', { name: '准备另一个版本' }))
    await waitFor(() => expect(screen.getByRole('button', { name: '确认并创建任务' })).not.toBeDisabled())
  })

  it('preserves an uncertain task payload and navigates on idempotent replay', async () => {
    let body
    let attempts = 0
    server({ taskPost: (payload, source, prepared) => {
      attempts += 1
      if (attempts === 1) { body = payload; throw new TypeError('offline') }
      expect(payload).toEqual(body)
      return jsonResponse({ task: issueTask(payload, source, prepared), replayed: true })
    } })
    const user = userEvent.setup()
    const view = mount()
    await readAndPrepare(user); await configure(user)
    await user.click(screen.getByRole('button', { name: '确认并创建任务' }))
    await screen.findByRole('button', { name: '用原请求重试' })
    view.unmount(); mount()
    expect(screen.getByLabelText('修复要求')).toBeDisabled()
    await user.click(screen.getByRole('button', { name: '用原请求重试' }))
    await waitFor(() => expect(screen.getByRole('heading', { name: '问题来源' })).toBeVisible())
    expect(attempts).toBe(2)
  })

  it('shows persisted preparation failures without treating them as ready', async () => {
    const failed = preparation({ status: 'failed', error: 'download failed', error_status: 502 })
    server({ preparePost: (payload) => jsonResponse({ preparation: { ...failed, request_id: payload.request_id }, replayed: false }, 201),
      preparationState: () => ({ preparation: failed, checkout_intact: null, reason: 'download failed' }) })
    const user = userEvent.setup()
    mount(); await user.type(screen.getByLabelText('Issue 链接'), issueUrl)
    await user.click(screen.getByRole('button', { name: '读取 Issue' }))
    await screen.findByRole('heading', { name: '版本号无法处理 v 前缀' })
    await user.click(screen.getByRole('button', { name: '准备仓库' }))
    expect(await screen.findByText('准备失败')).toBeVisible()
    expect(screen.getByRole('button', { name: '确认并创建任务' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '准备另一个版本' })).not.toBeDisabled()
  })

  it('does not let a late import overwrite a newer repository request', async () => {
    let resolveOriginal
    let attempt = 0
    server({ importPost: (body) => {
      attempt += 1
      if (attempt === 1) return new Promise((resolve) => { resolveOriginal = () => resolve(jsonResponse({ source: issue({ request_id: body.request_id }), replayed: false }, 201)) })
      return jsonResponse({ source: issue({ request_id: body.request_id }), replayed: true })
    } })
    const user = userEvent.setup()
    const original = mount()
    await user.type(screen.getByLabelText('Issue 链接'), issueUrl)
    await user.click(screen.getByRole('button', { name: '读取 Issue' }))
    original.unmount(); mount()
    await user.click(screen.getByRole('button', { name: '用原请求重试' }))
    await screen.findByRole('heading', { name: '版本号无法处理 v 前缀' })
    await user.click(screen.getByRole('button', { name: '准备仓库' }))
    await screen.findByText('代码已就绪')
    await act(async () => resolveOriginal())
    expect(JSON.parse(sessionStorage.getItem('patchloop:github-task-draft:v1')).preparationId).toBe(preparationId)
  })

  it('reconciles the mounted retry when the original import succeeds first', async () => {
    const releases = []
    server({ importPost: (body) => new Promise((resolve) => releases.push(() => resolve(jsonResponse({ source: issue({ request_id: body.request_id }), replayed: false }, 201)))) })
    const user = userEvent.setup()
    const first = mount()
    await user.type(screen.getByLabelText('Issue 链接'), issueUrl)
    await user.click(screen.getByRole('button', { name: '读取 Issue' }))
    first.unmount(); mount()
    await user.click(screen.getByRole('button', { name: '用原请求重试' }))
    await act(async () => releases[0]())
    await act(async () => releases[1]())
    expect(await screen.findByRole('heading', { name: '版本号无法处理 v 前缀' })).toBeVisible()
    expect(screen.getByRole('button', { name: '准备仓库' })).not.toBeDisabled()
    expect(screen.queryByRole('button', { name: '用原请求重试' })).not.toBeInTheDocument()
  })

  it('does not send a task if its request UUID cannot be persisted', async () => {
    const fetch = server()
    const user = userEvent.setup()
    mount(); await readAndPrepare(user); await configure(user)
    const original = globalThis.sessionStorage
    vi.stubGlobal('sessionStorage', { getItem: original.getItem.bind(original),
      setItem: () => { throw new DOMException('full', 'QuotaExceededError') } })
    await user.click(screen.getByRole('button', { name: '确认并创建任务' }))
    expect(await screen.findByText(/浏览器无法保存请求编号/)).toBeVisible()
    expect(fetch.mock.calls.filter(([url]) => url === '/api/tasks/from-issue')).toHaveLength(0)
    vi.stubGlobal('sessionStorage', original)
    await user.click(screen.getByRole('button', { name: '用原请求重试' }))
    await waitFor(() => expect(screen.getByRole('heading', { name: '问题来源' })).toBeVisible())
    expect(fetch.mock.calls.filter(([url]) => url === '/api/tasks/from-issue')).toHaveLength(1)
  })

  it('keeps confirmed task completion when the mounted retry later fails', async () => {
    let resolveOriginal, rejectRetry
    let attempts = 0
    server({ taskPost: (body, source, prepared) => {
      attempts += 1
      if (attempts === 1) return new Promise((resolve) => { resolveOriginal = () => resolve(jsonResponse({ task: issueTask(body, source, prepared), replayed: false }, 201)) })
      return new Promise((_, reject) => { rejectRetry = () => reject(new TypeError('late offline failure')) })
    } })
    const user = userEvent.setup()
    const first = mount(); await readAndPrepare(user); await configure(user)
    await user.click(screen.getByRole('button', { name: '确认并创建任务' }))
    first.unmount(); mount()
    await user.click(screen.getByRole('button', { name: '用原请求重试' }))
    await act(async () => resolveOriginal())
    await act(async () => rejectRetry())
    await waitFor(() => expect(screen.getByRole('heading', { name: '问题来源' })).toBeVisible())
    expect(screen.queryByText('上次请求结果尚未确认')).not.toBeInTheDocument()
    expect(JSON.parse(sessionStorage.getItem('patchloop:github-task-draft:v1')).taskId).toBe(taskId)
  })
})
