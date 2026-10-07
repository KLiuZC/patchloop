import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { onlineManager, QueryClientProvider } from '@tanstack/react-query'
import { MotionConfig } from 'motion/react'
import ReviewPanel from '../components/ReviewPanel'
import { ApiError } from '../lib/api'
import { createQueryClient } from '../lib/query-client'

// Review behavior is isolated here; delivery has its own interaction suite.
vi.mock('../components/DeliveryPanel', () => ({ default: () => null }))
import { clearPendingReview, readPendingReview } from '../lib/review-draft'
import { getReview, listReviews, loadReviewSnapshot, submitReview } from '../lib/reviews'
import { secondId, taskId } from './fixtures'

vi.mock('../lib/reviews', async (importOriginal) => ({
  ...await importOriginal(),
  getReview: vi.fn(),
  listReviews: vi.fn(),
  loadReviewSnapshot: vi.fn(),
  submitReview: vi.fn(),
}))

const evidence = Object.freeze({
  base_sha: 'a'.repeat(40), patch_sha256: 'b'.repeat(64), validation_sha256: 'c'.repeat(64),
  task_sha256: 'd'.repeat(64), summary_sha256: 'e'.repeat(64), record_sha256: 'f'.repeat(64),
})
const patch = 'diff --git a/version.py b/version.py\n--- a/version.py\n+++ b/version.py\n@@ -1 +1 @@\n-return version\n+return version.removeprefix("v")\n'
const requestId = '630ac7b8-a3c0-4000-a000-000000000001'
const otherRequestId = '630ac7b8-a3c0-4000-a000-000000000002'
const storageKey = (id) => `patchloop:pending-review:${id}`
const clients = []
let server

function reviewRecord(overrides = {}) {
  return {
    task_id: taskId, review_id: '730ac7b8-a3c0-4000-a000-000000000001', request_id: requestId,
    revision: 1, decision: 'accepted', comment: '已核对回归测试', evidence: { ...evidence },
    created_at: '2026-10-07T07:00:00Z', ...overrides,
  }
}

function reviewState(overrides = {}) {
  return {
    task_id: taskId, status: 'pending', revision: 0, latest_review: null,
    evidence: { ...evidence }, can_accept: true, can_reject: true, reason: '', ...overrides,
  }
}

function requestBody(overrides = {}) {
  return { request_id: requestId, expected_revision: 0, decision: 'accepted', comment: '保留这条意见', evidence: { ...evidence }, ...overrides }
}

function checkedSnapshot(state = reviewState()) {
  return {
    state: structuredClone(state), patch,
    task: {
      task_id: state.task_id, base_sha: state.evidence?.base_sha, repo_path: '/repo', workdir: '.',
      goal: '仅修复小写 v 前缀', verification: { id: 'pytest', command: 'python -m pytest -q' },
    },
    validation: {
      status: 'passed', command: 'python -m pytest -q', patch_sha256: state.evidence?.patch_sha256, error: '',
      baseline: { status: 'failed', exit_code: 1, seconds: 1, output: 'baseline: 1 failed' },
      candidate: { status: 'passed', exit_code: 0, seconds: 2, output: 'candidate: 4 passed' },
    },
  }
}

function taskServer(id = taskId) {
  const state = reviewState({ task_id: id })
  return { state, snapshot: checkedSnapshot(state), history: { task_id: id, items: [] } }
}

function setState(state, id = taskId) {
  server[id].state = state
  server[id].snapshot = checkedSnapshot(state)
}

function recordSubmission(id, body) {
  const record = reviewRecord({
    task_id: id, review_id: crypto.randomUUID(), request_id: body.request_id, revision: body.expected_revision + 1,
    decision: body.decision, comment: body.comment, evidence: structuredClone(body.evidence),
  })
  const current = reviewState({ task_id: id, status: record.decision, revision: record.revision, latest_review: record, evidence: record.evidence })
  setState(current, id)
  server[id].history.items.push(record)
  return { review: record, current, replayed: false }
}

function deferred() {
  let resolve
  const promise = new Promise((done) => { resolve = done })
  return { promise, resolve }
}

function mount(id = taskId, client = createQueryClient()) {
  client.setDefaultOptions({ ...client.getDefaultOptions(), queries: { ...client.getDefaultOptions().queries, retry: false, gcTime: 0 } })
  if (!clients.includes(client)) clients.push(client)
  const tree = (nextId) => <QueryClientProvider client={client}><MotionConfig reducedMotion="always"><ReviewPanel taskId={nextId} /></MotionConfig></QueryClientProvider>
  const view = render(tree(id))
  return { ...view, client, switchTask: (nextId) => view.rerender(tree(nextId)) }
}

const accept = () => screen.getByRole('button', { name: '接受补丁' })
const reject = () => screen.getByRole('button', { name: '拒绝补丁' })
const commentField = () => screen.getByRole('textbox', { name: '意见（选填）' })
const reload = () => screen.getByRole('button', { name: '重新加载审查资料' })
const currentStatus = () => within(screen.getByText('当前状态').parentElement)

async function ready() {
  await screen.findByText('仅修复小写 v 前缀')
  await waitFor(() => expect(reload()).toBeEnabled())
}

async function uncertainSubmission(user, comment = '保留这条意见') {
  submitReview.mockRejectedValueOnce(new ApiError('网络连接失败。审查结果未确认。'))
  await user.type(commentField(), comment)
  await user.click(accept())
  await screen.findByText('网络连接失败。审查结果未确认。')
  return structuredClone(submitReview.mock.calls.at(-1)[1])
}

beforeEach(() => {
  for (const id of [taskId, secondId]) {
    const pending = readPendingReview(id)
    if (pending) clearPendingReview(id, pending.request_id)
  }
  sessionStorage.clear()
  vi.resetAllMocks()
  server = { [taskId]: taskServer(), [secondId]: taskServer(secondId) }
  getReview.mockImplementation(async (id) => structuredClone(server[id].state))
  listReviews.mockImplementation(async (id) => structuredClone(server[id].history))
  loadReviewSnapshot.mockImplementation(async (id) => structuredClone(server[id].snapshot))
  submitReview.mockImplementation(async (id, body) => recordSubmission(id, body))
  // Every review boundary is mocked; any accidental real network request fails the test.
  vi.stubGlobal('fetch', vi.fn(async () => { throw new Error('Unexpected real API request') }))
})

afterEach(() => {
  cleanup()
  clients.splice(0).forEach((client) => client.clear())
  for (const id of [taskId, secondId]) {
    const pending = readPendingReview(id)
    if (pending) clearPendingReview(id, pending.request_id)
  }
  sessionStorage.clear()
  onlineManager.setOnline(true)
  expect(fetch).not.toHaveBeenCalled()
  vi.unstubAllGlobals()
})

describe('人工审查交互', () => {
  it('shows the checked patch, evidence, independent validation and newest history first', async () => {
    const first = reviewRecord({ comment: '第一次：核对通过' })
    const latest = reviewRecord({ review_id: otherRequestId, request_id: otherRequestId, revision: 2, decision: 'rejected', comment: '第二次：修改超出范围' })
    setState(reviewState({ status: 'rejected', revision: 2, latest_review: latest }))
    server[taskId].history.items = [first, latest]
    const user = userEvent.setup()
    mount()
    await ready()
    expect(screen.getByRole('region', { name: '代码差异' })).toHaveTextContent('+return version.removeprefix("v")')
    const requirements = screen.getByRole('heading', { name: '本次审查要求' }).closest('section')
    expect(requirements).toHaveTextContent('python -m pytest -q')
    await user.click(within(requirements).getByText('审查依据'))
    expect(within(requirements).getByText(evidence.base_sha)).toBeVisible()
    expect(within(requirements).getByText(evidence.patch_sha256)).toBeVisible()
    expect(within(requirements).getByText(evidence.validation_sha256)).toBeVisible()
    const before = screen.getByText('修复前 · 干净基线').closest('.verification-card')
    const after = screen.getByText('修复后 · 候选补丁').closest('.verification-card')
    expect(within(before).getByText('验证未通过')).toBeVisible()
    expect(within(after).getByText('验证通过')).toBeVisible()
    await user.click(within(before).getByText('查看测试输出'))
    await user.click(within(after).getByText('查看测试输出'))
    expect(screen.getByText('baseline: 1 failed')).toBeVisible()
    expect(screen.getByText('candidate: 4 passed')).toBeVisible()
    const items = within(screen.getByRole('list', { name: '审查历史' })).getAllByRole('listitem')
    expect(items[0]).toHaveTextContent('第二次：修改超出范围')
    expect(items[1]).toHaveTextContent('第一次：核对通过')
    expect(currentStatus().getByText('已拒绝')).toBeVisible()
    expect(reject()).toBeDisabled()
    expect(accept()).toBeEnabled()
  })

  it('keeps both decisions disabled until the checked snapshot has loaded', async () => {
    const snapshot = deferred()
    loadReviewSnapshot.mockReturnValueOnce(snapshot.promise)
    const user = userEvent.setup()
    mount()
    await screen.findByText('待审查')
    expect(accept()).toBeDisabled()
    expect(reject()).toBeDisabled()
    await user.click(accept())
    expect(submitReview).not.toHaveBeenCalled()
    await act(async () => snapshot.resolve(server[taskId].snapshot))
    await ready()
    expect(accept()).toBeEnabled()
  })

  it('does not treat completed execution as passed validation and still permits an allowed rejection', async () => {
    setState(reviewState({ can_accept: false }))
    server[taskId].snapshot.task.status = 'completed'
    server[taskId].snapshot.validation.status = 'failed'
    server[taskId].snapshot.validation.candidate = { status: 'failed', exit_code: 1, output: 'candidate failed' }
    const user = userEvent.setup()
    mount()
    await ready()
    expect(screen.queryByText('验证通过')).not.toBeInTheDocument()
    expect(screen.getByText('现有测试证据不足以接受补丁，仍可提交拒绝意见。')).toBeVisible()
    expect(accept()).toBeDisabled()
    expect(reject()).toBeEnabled()
    await user.type(commentField(), '执行已完成，但测试仍然失败')
    await user.click(reject())
    await screen.findByText('审查已保存。')
    expect(submitReview).toHaveBeenCalledWith(taskId, expect.objectContaining({ decision: 'rejected', comment: '执行已完成，但测试仍然失败' }))
  })

  it('honors both server permission flags even when the candidate validation passed', async () => {
    setState(reviewState({ can_accept: false, can_reject: false, reason: 'cleanup pending' }))
    const user = userEvent.setup()
    mount()
    await ready()
    expect(screen.getByText('验证通过')).toBeVisible()
    expect(screen.getByText('任务环境仍在清理，完成后才能审查。')).toBeVisible()
    expect(accept()).toBeDisabled()
    expect(reject()).toBeDisabled()
    await user.click(accept())
    await user.click(reject())
    expect(submitReview).not.toHaveBeenCalled()
  })

  it('rechecks permission immediately before posting even if revision and evidence are unchanged', async () => {
    const user = userEvent.setup()
    mount()
    await ready()
    await user.type(commentField(), '等待权限核对')
    server[taskId].state.can_accept = false
    await user.click(accept())
    await screen.findByText(/你的意见已保留/)
    expect(submitReview).not.toHaveBeenCalled()
    expect(commentField()).toHaveValue('等待权限核对')
    expect(accept()).toBeDisabled()
  })

  it('submits the exact comment with the frozen revision and all six evidence fields', async () => {
    const previous = reviewRecord({ revision: 3, decision: 'rejected' })
    setState(reviewState({ status: 'rejected', revision: 3, latest_review: previous }))
    const user = userEvent.setup()
    mount()
    await ready()
    const comment = '  已核对补丁\n保留尾部空格  '
    await user.type(commentField(), comment)
    await user.click(accept())
    await screen.findByText('审查已保存。')
    expect(submitReview).toHaveBeenCalledExactlyOnceWith(taskId, {
      request_id: expect.stringMatching(/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i),
      expected_revision: 3, decision: 'accepted', comment, evidence,
    })
    expect(getReview.mock.invocationCallOrder.at(-1)).toBeLessThan(submitReview.mock.invocationCallOrder[0])
    expect(commentField()).toHaveValue('')
    expect(accept()).toBeDisabled()
    expect(reject()).toBeEnabled()
    expect(sessionStorage.getItem(storageKey(taskId))).toBeNull()
  })

  it('posts only once across rapid repeated clicks during preflight and the pending POST', async () => {
    const preflight = deferred()
    const post = deferred()
    const user = userEvent.setup()
    mount()
    await ready()
    getReview.mockReturnValueOnce(preflight.promise)
    submitReview.mockReturnValueOnce(post.promise)
    await user.dblClick(accept())
    expect(submitReview).not.toHaveBeenCalled()
    expect(commentField()).toBeDisabled()
    expect(reject()).toBeDisabled()
    await act(async () => preflight.resolve(server[taskId].state))
    await waitFor(() => expect(submitReview).toHaveBeenCalledTimes(1))
    await user.dblClick(accept())
    await user.click(reject())
    expect(submitReview).toHaveBeenCalledTimes(1)
    const body = submitReview.mock.calls[0][1]
    await act(async () => post.resolve(recordSubmission(taskId, body)))
    await screen.findByText('审查已保存。')
  })

  it.each(['revision', 'hash'])('blocks a preflight %s change, preserves the comment and requires explicit reload', async (change) => {
    const user = userEvent.setup()
    mount()
    await ready()
    await user.type(commentField(), '需要重新核对，但不要丢失意见')
    const latest = change === 'revision'
      ? reviewState({ status: 'rejected', revision: 1, latest_review: reviewRecord({ decision: 'rejected' }) })
      : reviewState({ evidence: { ...evidence, patch_sha256: '1'.repeat(64) } })
    setState(latest)
    server[taskId].snapshot.patch = patch.replace('removeprefix("v")', 'lstrip("v")')
    await user.click(accept())
    await screen.findByText(/你的意见已保留/)
    expect(submitReview).not.toHaveBeenCalled()
    expect(commentField()).toHaveValue('需要重新核对，但不要丢失意见')
    expect(accept()).toBeDisabled()
    expect(reject()).toBeDisabled()
    expect(loadReviewSnapshot).toHaveBeenCalledTimes(1)
    expect(screen.getByRole('region', { name: '代码差异' })).toHaveTextContent('removeprefix')
    await user.click(accept())
    expect(submitReview).not.toHaveBeenCalled()
    await user.click(reload())
    await waitFor(() => expect(accept()).toBeEnabled())
    expect(screen.getByRole('region', { name: '代码差异' })).toHaveTextContent('lstrip')
    expect(commentField()).toHaveValue('需要重新核对，但不要丢失意见')
    await user.click(accept())
    await screen.findByText('审查已保存。')
    expect(submitReview).toHaveBeenCalledExactlyOnceWith(taskId, expect.objectContaining({ expected_revision: latest.revision, evidence: latest.evidence, comment: '需要重新核对，但不要丢失意见' }))
  })

  it('does not silently resend an HTTP 409 and requires reload before a new request', async () => {
    submitReview.mockRejectedValueOnce(new ApiError('revision conflict', 409))
    const user = userEvent.setup()
    const { client } = mount()
    await ready()
    await user.type(commentField(), '并发审查中的意见')
    await user.click(accept())
    await screen.findByText(/你的意见已保留/)
    const original = structuredClone(submitReview.mock.calls[0][1])
    await act(async () => client.invalidateQueries({ queryKey: ['review', taskId] }))
    expect(submitReview).toHaveBeenCalledTimes(1)
    expect(commentField()).toHaveValue('并发审查中的意见')
    expect(accept()).toBeDisabled()
    expect(reject()).toBeDisabled()
    expect(screen.queryByRole('button', { name: '重试原提交' })).not.toBeInTheDocument()
    expect(sessionStorage.getItem(storageKey(taskId))).toBeNull()
    await user.click(reload())
    await waitFor(() => expect(accept()).toBeEnabled())
    expect(submitReview).toHaveBeenCalledTimes(1)
    await user.click(accept())
    await screen.findByText('审查已保存。')
    expect(submitReview).toHaveBeenCalledTimes(2)
    expect(submitReview.mock.calls[1][1].request_id).not.toBe(original.request_id)
  })

  it('preserves an uncertain request and only manually retries the identical body and request_id', async () => {
    const user = userEvent.setup()
    const { client } = mount()
    await ready()
    const original = await uncertainSubmission(user, '  网络断开时的原始意见  ')
    expect(JSON.parse(sessionStorage.getItem(storageKey(taskId)))).toEqual(original)
    expect(commentField()).toHaveValue(original.comment)
    expect(commentField()).toBeDisabled()
    expect(accept()).toBeDisabled()
    expect(reject()).toBeDisabled()
    act(() => onlineManager.setOnline(false))
    act(() => onlineManager.setOnline(true))
    await act(async () => client.invalidateQueries({ queryKey: ['review', taskId] }))
    await user.click(reload())
    await waitFor(() => expect(reload()).toBeEnabled())
    expect(submitReview).toHaveBeenCalledTimes(1)
    expect(JSON.parse(sessionStorage.getItem(storageKey(taskId)))).toEqual(original)
    await user.click(screen.getByRole('button', { name: '重试原提交' }))
    await screen.findByText('审查已保存。')
    expect(submitReview).toHaveBeenCalledTimes(2)
    expect(submitReview.mock.calls[1]).toEqual([taskId, original])
    expect(sessionStorage.getItem(storageKey(taskId))).toBeNull()
  })

  it('uses the newer rejected current state when a replay returns an older accepted review', async () => {
    const user = userEvent.setup()
    mount()
    await ready()
    const body = await uncertainSubmission(user)
    const old = reviewRecord({ request_id: body.request_id, comment: body.comment })
    const latest = reviewRecord({ review_id: otherRequestId, request_id: otherRequestId, revision: 2, decision: 'rejected', comment: '后续审查已拒绝' })
    const current = reviewState({ status: 'rejected', revision: 2, latest_review: latest })
    setState(current)
    server[taskId].history.items = [old, latest]
    submitReview.mockResolvedValueOnce({ review: old, current, replayed: true })
    await user.click(screen.getByRole('button', { name: '重试原提交' }))
    await screen.findByText('审查已保存。')
    expect(currentStatus().getByText('已拒绝')).toBeVisible()
    expect(currentStatus().queryByText('已接受')).not.toBeInTheDocument()
    expect(screen.getByText('已记录 2 次审查')).toBeVisible()
    expect(reject()).toBeDisabled()
    expect(accept()).toBeEnabled()
    await user.click(accept())
    await waitFor(() => expect(submitReview).toHaveBeenCalledTimes(3))
    expect(submitReview.mock.calls[2][1].expected_revision).toBe(2)
    await waitFor(() => expect(currentStatus().getByText('已接受')).toBeVisible())
  })

  it('clears an uncertain request after history confirms it without another POST', async () => {
    const user = userEvent.setup()
    mount()
    await ready()
    const body = await uncertainSubmission(user)
    recordSubmission(taskId, body)
    await user.click(screen.getByRole('button', { name: '核对提交结果' }))
    await screen.findByText('审查已保存。')
    expect(submitReview).toHaveBeenCalledTimes(1)
    expect(screen.queryByText('有一条审查提交尚未确认')).not.toBeInTheDocument()
    expect(commentField()).toHaveValue('')
    expect(commentField()).toBeEnabled()
    expect(sessionStorage.getItem(storageKey(taskId))).toBeNull()
    expect(currentStatus().getByText('已接受')).toBeVisible()
    expect(reject()).toBeEnabled()
  })

  it('keeps pending when history is absent or the same request_id has different evidence', async () => {
    const user = userEvent.setup()
    mount()
    await ready()
    const body = await uncertainSubmission(user)
    await user.click(screen.getByRole('button', { name: '核对提交结果' }))
    await screen.findByText('尚未确认这次提交，可使用原请求重试。')
    server[taskId].history.items = [reviewRecord({ request_id: body.request_id, comment: body.comment, evidence: { ...evidence, record_sha256: '1'.repeat(64) } })]
    await user.click(screen.getByRole('button', { name: '核对提交结果' }))
    await screen.findByText('尚未确认这次提交，可使用原请求重试。')
    expect(commentField()).toHaveValue(body.comment)
    expect(commentField()).toBeDisabled()
    expect(JSON.parse(sessionStorage.getItem(storageKey(taskId)))).toEqual(body)
    expect(submitReview).toHaveBeenCalledTimes(1)
    expect(accept()).toBeDisabled()
    expect(reject()).toBeDisabled()
  })

  it('does not carry another task pending request into a taskId prop change', async () => {
    const user = userEvent.setup()
    const view = mount()
    await ready()
    const first = await uncertainSubmission(user, '只属于任务 A')
    view.switchTask(secondId)
    await waitFor(() => expect(loadReviewSnapshot).toHaveBeenCalledWith(secondId, expect.anything()))
    await ready()
    expect(commentField()).toHaveValue('')
    expect(commentField()).toBeEnabled()
    expect(screen.queryByRole('button', { name: '重试原提交' })).not.toBeInTheDocument()
    expect(accept()).toBeEnabled()
    expect(JSON.parse(sessionStorage.getItem(storageKey(taskId)))).toEqual(first)
    await user.type(commentField(), '只属于任务 B')
    await user.click(reject())
    await screen.findByText('审查已保存。')
    expect(submitReview.mock.calls[1]).toEqual([secondId, expect.objectContaining({ decision: 'rejected', comment: '只属于任务 B' })])
    expect(submitReview.mock.calls[1][1].request_id).not.toBe(first.request_id)
  })

  it('restores only its own pending request when unmounted and mounted again', async () => {
    const user = userEvent.setup()
    const first = mount()
    await ready()
    const original = await uncertainSubmission(user, '任务 A 的未确认意见')
    first.unmount()
    const second = mount(secondId)
    await ready()
    expect(commentField()).toHaveValue('')
    expect(accept()).toBeEnabled()
    expect(screen.queryByRole('button', { name: '重试原提交' })).not.toBeInTheDocument()
    second.unmount()
    mount()
    await ready()
    expect(commentField()).toHaveValue(original.comment)
    expect(commentField()).toBeDisabled()
    expect(screen.getByRole('button', { name: '重试原提交' })).toBeEnabled()
    expect(submitReview).toHaveBeenCalledTimes(1)
    await user.click(screen.getByRole('button', { name: '重试原提交' }))
    await screen.findByText('审查已保存。')
    expect(submitReview.mock.calls[1]).toEqual([taskId, original])
  })

  it('restores an unsent comment on remount with the same QueryClient and clears it after saving', async () => {
    const user = userEvent.setup()
    const first = mount()
    // Keep the normal cache lifetime for unobserved drafts instead of the test helper's gcTime: 0.
    first.client.setQueryDefaults(['review-comment'], { gcTime: 300_000 })
    await ready()
    const comment = '切换详情页签前，还没有提交的意见'
    await user.type(commentField(), comment)
    first.unmount()
    const other = mount(secondId, first.client)
    await ready()
    expect(commentField()).toHaveValue('')
    other.unmount()
    const restored = mount(taskId, first.client)
    await ready()
    expect(commentField()).toHaveValue(comment)
    expect(commentField()).toBeEnabled()
    expect(submitReview).not.toHaveBeenCalled()
    expect(sessionStorage.getItem(storageKey(taskId))).toBeNull()
    await user.click(accept())
    await screen.findByText('审查已保存。')
    expect(submitReview).toHaveBeenCalledExactlyOnceWith(taskId, expect.objectContaining({ comment }))
    restored.unmount()
    mount(taskId, first.client)
    await ready()
    expect(commentField()).toHaveValue('')
    expect(submitReview).toHaveBeenCalledTimes(1)
  })

  it('does not let a late response for the previous task clear the new task pending request', async () => {
    const post = deferred()
    submitReview.mockReturnValueOnce(post.promise)
    const secondBody = requestBody({ request_id: otherRequestId, decision: 'rejected', comment: '任务 B 的未确认意见' })
    sessionStorage.setItem(storageKey(secondId), JSON.stringify(secondBody))
    const user = userEvent.setup()
    const view = mount()
    await ready()
    await user.type(commentField(), '任务 A 尚在提交')
    await user.click(accept())
    await waitFor(() => expect(submitReview).toHaveBeenCalledTimes(1))
    const firstBody = structuredClone(submitReview.mock.calls[0][1])
    view.switchTask(secondId)
    await act(async () => post.resolve(recordSubmission(taskId, firstBody)))
    await ready()
    expect(commentField()).toHaveValue(secondBody.comment)
    expect(commentField()).toBeDisabled()
    expect(JSON.parse(sessionStorage.getItem(storageKey(secondId)))).toEqual(secondBody)
    expect(currentStatus().getByText('待审查')).toBeVisible()
    expect(accept()).toBeDisabled()
    await user.click(screen.getByRole('button', { name: '重试原提交' }))
    await screen.findByText('审查已保存。')
    expect(submitReview.mock.calls[1]).toEqual([secondId, secondBody])
  })

  it('recovers a request from sessionStorage without automatically posting it', async () => {
    const body = requestBody({ decision: 'rejected', comment: '页面重载前的意见' })
    sessionStorage.setItem(storageKey(taskId), JSON.stringify(body))
    const user = userEvent.setup()
    mount()
    await ready()
    expect(commentField()).toHaveValue(body.comment)
    expect(commentField()).toBeDisabled()
    expect(accept()).toBeDisabled()
    expect(reject()).toBeDisabled()
    expect(submitReview).not.toHaveBeenCalled()
    await user.click(reload())
    await waitFor(() => expect(reload()).toBeEnabled())
    expect(submitReview).not.toHaveBeenCalled()
    await user.click(screen.getByRole('button', { name: '重试原提交' }))
    await screen.findByText('审查已保存。')
    expect(submitReview).toHaveBeenCalledExactlyOnceWith(taskId, body)
    expect(sessionStorage.getItem(storageKey(taskId))).toBeNull()
  })

  it('renders history and submitted HTML comments as literal text', async () => {
    const html = '<img src=x onerror=alert(1)><script>alert("review")</script>'
    const historical = reviewRecord({ decision: 'rejected', comment: html })
    setState(reviewState({ status: 'rejected', revision: 1, latest_review: historical }))
    server[taskId].history.items = [historical]
    const user = userEvent.setup()
    const { container } = mount()
    await ready()
    expect(within(screen.getByRole('list', { name: '审查历史' })).getByText(html)).toBeVisible()
    await user.type(commentField(), html)
    expect(commentField()).toHaveValue(html)
    await user.click(accept())
    await screen.findByText('审查已保存。')
    expect(submitReview).toHaveBeenCalledWith(taskId, expect.objectContaining({ comment: html }))
    await waitFor(() => expect(screen.getAllByText(html)).toHaveLength(2))
    expect(container.querySelector('img, script')).toBeNull()
  })

  it('does not enable decisions when the current review endpoint is unavailable despite a loaded snapshot', async () => {
    getReview.mockRejectedValue(new ApiError('Not found', 404))
    const user = userEvent.setup()
    mount()
    await ready()
    expect(await screen.findByRole('alert')).toHaveTextContent('无法读取审查接口，请确认任务存在且后端已更新。')
    expect(currentStatus().getByText('暂无法读取')).toBeVisible()
    expect(accept()).toBeDisabled()
    expect(reject()).toBeDisabled()
    await user.click(accept())
    expect(submitReview).not.toHaveBeenCalled()
  })

  it('preserves the comment and never POSTs if the preflight request fails', async () => {
    const user = userEvent.setup()
    mount()
    await ready()
    await user.type(commentField(), '服务恢复后继续核对')
    getReview.mockRejectedValueOnce(new ApiError('审查服务暂不可用', 503))
    await user.click(accept())
    expect(await screen.findByRole('alert')).toHaveTextContent('审查服务暂不可用')
    expect(submitReview).not.toHaveBeenCalled()
    expect(commentField()).toHaveValue('服务恢复后继续核对')
    expect(sessionStorage.getItem(storageKey(taskId))).toBeNull()
    expect(screen.queryByRole('button', { name: '重试原提交' })).not.toBeInTheDocument()
  })

  it('keeps decisions disabled when the snapshot could not verify the artifact hashes', async () => {
    loadReviewSnapshot.mockRejectedValue(new ApiError('审查证据哈希不一致', 409))
    const user = userEvent.setup()
    mount()
    expect(await screen.findByRole('alert')).toHaveTextContent('审查证据哈希不一致')
    expect(accept()).toBeDisabled()
    expect(reject()).toBeDisabled()
    expect(screen.queryByRole('region', { name: '代码差异' })).not.toBeInTheDocument()
    await user.click(accept())
    await user.click(reject())
    expect(submitReview).not.toHaveBeenCalled()
  })

  it('does not permit either decision when no saved repair evidence exists', async () => {
    const state = reviewState({ evidence: null, can_accept: false, can_reject: false, reason: 'no saved repair evidence' })
    server[taskId].state = state
    server[taskId].snapshot = { state, task: null, patch: null, validation: null }
    mount()
    expect(await screen.findByText('这次任务尚无完整的修复记录。')).toBeVisible()
    expect(await screen.findByText('暂无审查记录。')).toBeVisible()
    expect(accept()).toBeDisabled()
    expect(reject()).toBeDisabled()
    expect(submitReview).not.toHaveBeenCalled()
  })

  it('keeps the displayed patch frozen when background review data changes until explicit reload', async () => {
    const user = userEvent.setup()
    const { client } = mount()
    await ready()
    fireEvent.change(commentField(), { target: { value: '正在核对旧补丁' } })
    const next = reviewState({ evidence: { ...evidence, patch_sha256: '2'.repeat(64) } })
    setState(next)
    server[taskId].snapshot.patch = patch.replace('removeprefix("v")', 'lstrip("v")')
    await act(async () => client.invalidateQueries({ queryKey: ['review', taskId] }))
    expect(await screen.findByText(/你的意见已保留/)).toBeVisible()
    expect(accept()).toBeDisabled()
    expect(reject()).toBeDisabled()
    expect(screen.getByRole('region', { name: '代码差异' })).toHaveTextContent('removeprefix')
    expect(loadReviewSnapshot).toHaveBeenCalledTimes(1)
    expect(commentField()).toHaveValue('正在核对旧补丁')
    await user.click(reload())
    await waitFor(() => expect(accept()).toBeEnabled())
    expect(screen.getByRole('region', { name: '代码差异' })).toHaveTextContent('lstrip')
    expect(commentField()).toHaveValue('正在核对旧补丁')
    expect(submitReview).not.toHaveBeenCalled()
  })

  it('keeps revision 2 rejected when a pending POST later returns revision 1 accepted', async () => {
    const post = deferred()
    submitReview.mockReturnValueOnce(post.promise)
    const user = userEvent.setup()
    const { client } = mount()
    await ready()
    await user.type(commentField(), '这次接受请求的响应被延迟')
    await user.click(accept())
    await waitFor(() => expect(submitReview).toHaveBeenCalledTimes(1))
    const oldResponse = recordSubmission(taskId, submitReview.mock.calls[0][1])
    const latest = reviewRecord({ review_id: otherRequestId, request_id: otherRequestId, revision: 2, decision: 'rejected', comment: '另一位审查者已拒绝' })
    const current = reviewState({ status: 'rejected', revision: 2, latest_review: latest })
    setState(current)
    server[taskId].history.items.push(latest)
    await act(async () => client.invalidateQueries({ queryKey: ['review', taskId], exact: true }))
    expect(await currentStatus().findByText('已拒绝')).toBeVisible()
    expect(screen.getByText('已记录 2 次审查')).toBeVisible()
    expect(commentField()).toBeDisabled()

    await act(async () => post.resolve(oldResponse))
    await screen.findByText('审查已保存。')
    expect(client.getQueryData(['review', taskId])).toEqual(current)
    expect(client.getQueryData(['review-snapshot', taskId]).state).toEqual(current)
    expect(currentStatus().getByText('已拒绝')).toBeVisible()
    expect(currentStatus().queryByText('已接受')).not.toBeInTheDocument()
    expect(screen.getByText('已记录 2 次审查')).toBeVisible()
    expect(reject()).toBeDisabled()
    expect(accept()).toBeEnabled()
    expect(commentField()).toHaveValue('')
    expect(commentField()).toBeEnabled()
    expect(sessionStorage.getItem(storageKey(taskId))).toBeNull()

    await user.type(commentField(), '基于第二次审查重新决定')
    await user.click(accept())
    await waitFor(() => expect(submitReview).toHaveBeenCalledTimes(2))
    expect(submitReview.mock.calls[1][1]).toMatchObject({ expected_revision: 2, comment: '基于第二次审查重新决定' })
    await screen.findByText('已记录 3 次审查')
  })

  it('keeps the saved revision 1 when an earlier background GET returns revision 0 late', async () => {
    const post = deferred()
    const staleRead = deferred()
    submitReview.mockReturnValueOnce(post.promise)
    const user = userEvent.setup()
    const { client } = mount()
    await ready()
    const oldState = structuredClone(server[taskId].state)
    await user.click(accept())
    await waitFor(() => expect(submitReview).toHaveBeenCalledTimes(1))

    // Start the read after preflight, while the POST is still pending.
    const readsBefore = getReview.mock.calls.length
    getReview.mockReturnValueOnce(staleRead.promise)
    let refetch
    act(() => { refetch = client.invalidateQueries({ queryKey: ['review', taskId], exact: true }) })
    await waitFor(() => expect(getReview).toHaveBeenCalledTimes(readsBefore + 1))
    expect(client.isFetching({ queryKey: ['review', taskId], exact: true })).toBe(1)
    const response = recordSubmission(taskId, submitReview.mock.calls[0][1])
    await act(async () => post.resolve(response))
    await screen.findByText('审查已保存。')
    expect(client.getQueryData(['review', taskId])).toEqual(response.current)
    expect(currentStatus().getByText('已接受')).toBeVisible()
    await user.type(commentField(), '下一轮尚未提交的意见')

    // The mock still resolves even if its AbortSignal was cancelled.
    await act(async () => {
      staleRead.resolve(oldState)
      await staleRead.promise
      await refetch
    })
    expect(client.getQueryData(['review', taskId])).toEqual(response.current)
    expect(client.getQueryData(['review-snapshot', taskId]).state).toEqual(response.current)
    expect(currentStatus().getByText('已接受')).toBeVisible()
    expect(currentStatus().queryByText('待审查')).not.toBeInTheDocument()
    expect(screen.getByText('已记录 1 次审查')).toBeVisible()
    expect(accept()).toBeDisabled()
    expect(reject()).toBeEnabled()
    expect(commentField()).toHaveValue('下一轮尚未提交的意见')
    expect(screen.queryByText(/你的意见已保留/)).not.toBeInTheDocument()
    expect(submitReview).toHaveBeenCalledTimes(1)
    expect(sessionStorage.getItem(storageKey(taskId))).toBeNull()
  })

  it('keeps an observed stale revision with null evidence until the user explicitly reloads', async () => {
    const accepted = reviewState({ status: 'accepted', revision: 1, latest_review: reviewRecord() })
    setState(accepted)
    const lateRead = deferred()
    const user = userEvent.setup()
    const { client } = mount()
    await ready()
    await user.type(commentField(), '资料恢复后仍需重新核对')
    getReview.mockReturnValueOnce(lateRead.promise)
    let refetch
    act(() => { refetch = client.invalidateQueries({ queryKey: ['review', taskId], exact: true }) })
    expect(client.isFetching({ queryKey: ['review', taskId], exact: true })).toBe(1)
    const stale = { ...accepted, status: 'stale', evidence: null, can_accept: false, can_reject: false }
    act(() => client.setQueryData(['review', taskId], stale))
    expect(await currentStatus().findByText('审查已过期')).toBeVisible()
    expect(accept()).toBeDisabled()
    expect(reject()).toBeDisabled()

    await act(async () => {
      lateRead.resolve(accepted)
      await refetch
    })
    expect(client.getQueryData(['review', taskId])).toEqual(stale)
    expect(currentStatus().getByText('审查已过期')).toBeVisible()
    expect(currentStatus().queryByText('已接受')).not.toBeInTheDocument()
    expect(reject()).toBeDisabled()
    expect(commentField()).toHaveValue('资料恢复后仍需重新核对')
    expect(loadReviewSnapshot).toHaveBeenCalledTimes(1)

    await user.click(reload())
    await waitFor(() => expect(reject()).toBeEnabled())
    expect(currentStatus().getByText('已接受')).toBeVisible()
    expect(client.getQueryData(['review', taskId])).toEqual(accepted)
    expect(loadReviewSnapshot).toHaveBeenCalledTimes(2)
    expect(accept()).toBeDisabled()
    expect(commentField()).toHaveValue('资料恢复后仍需重新核对')
    expect(submitReview).not.toHaveBeenCalled()
  })

  it('does not POST after unmounting while the preflight GET is still pending', async () => {
    const preflight = deferred()
    const user = userEvent.setup()
    const view = mount()
    await ready()
    const readsBefore = getReview.mock.calls.length
    getReview.mockReturnValueOnce(preflight.promise)
    await user.type(commentField(), '离开页面前尚在核对')
    await user.click(accept())
    await waitFor(() => expect(getReview).toHaveBeenCalledTimes(readsBefore + 1))
    expect(commentField()).toBeDisabled()
    expect(submitReview).not.toHaveBeenCalled()
    view.unmount()
    await act(async () => preflight.resolve(structuredClone(server[taskId].state)))
    expect(submitReview).not.toHaveBeenCalled()
    expect(readPendingReview(taskId)).toBeNull()
    expect(sessionStorage.getItem(storageKey(taskId))).toBeNull()
    mount()
    await ready()
    expect(accept()).toBeEnabled()
    expect(screen.queryByRole('button', { name: '重试原提交' })).not.toBeInTheDocument()
    expect(submitReview).not.toHaveBeenCalled()
  })

  it('preserves a new draft when an old POST resolves after a remounted panel has reconciled it', async () => {
    const post = deferred()
    submitReview.mockReturnValueOnce(post.promise)
    const user = userEvent.setup()
    const first = mount()
    first.client.setQueryDefaults(['review-comment'], { gcTime: 300_000 })
    await ready()
    await user.type(commentField(), '已经提交的旧意见')
    await user.click(accept())
    await waitFor(() => expect(submitReview).toHaveBeenCalledTimes(1))
    const body = structuredClone(submitReview.mock.calls[0][1])
    const oldResponse = recordSubmission(taskId, body)
    first.unmount()
    const next = mount(taskId, first.client)
    await ready()
    expect(commentField()).toHaveValue(body.comment)
    await user.click(screen.getByRole('button', { name: '核对提交结果' }))
    await screen.findByText('审查已保存。')
    expect(readPendingReview(taskId)).toBeNull()
    expect(sessionStorage.getItem(storageKey(taskId))).toBeNull()

    const newComment = '核对完成后新写的下一轮意见'
    await user.type(commentField(), newComment)
    expect(first.client.getQueryData(['review-comment', taskId])).toBe(newComment)
    await act(async () => post.resolve(oldResponse))
    expect(commentField()).toHaveValue(newComment)
    expect(commentField()).toBeEnabled()
    expect(first.client.getQueryData(['review-comment', taskId])).toBe(newComment)
    expect(readPendingReview(taskId)).toBeNull()
    expect(submitReview).toHaveBeenCalledTimes(1)
    next.unmount()
    mount(taskId, first.client)
    await ready()
    expect(commentField()).toHaveValue(newComment)
    expect(screen.queryByRole('button', { name: '重试原提交' })).not.toBeInTheDocument()
  })

  it('keeps pending when a matching history request has the wrong resulting revision', async () => {
    const user = userEvent.setup()
    mount()
    await ready()
    const body = await uncertainSubmission(user)
    const found = reviewRecord({ request_id: body.request_id, comment: body.comment, revision: body.expected_revision + 2 })
    server[taskId].history.items = [found]
    setState(reviewState({ status: 'accepted', revision: found.revision, latest_review: found }))
    await user.click(screen.getByRole('button', { name: '核对提交结果' }))
    expect(await screen.findByText('历史记录与原提交不一致，请保留原请求并核查。')).toBeVisible()
    expect(readPendingReview(taskId)).toEqual(body)
    expect(JSON.parse(sessionStorage.getItem(storageKey(taskId)))).toEqual(body)
    expect(commentField()).toHaveValue(body.comment)
    expect(commentField()).toBeDisabled()
    expect(accept()).toBeDisabled()
    expect(reject()).toBeDisabled()
    expect(screen.getByRole('button', { name: '重试原提交' })).toBeEnabled()
    expect(submitReview).toHaveBeenCalledTimes(1)
  })

  it('rereads a current state behind history and keeps pending until that state catches up', async () => {
    const user = userEvent.setup()
    mount()
    await ready()
    const body = await uncertainSubmission(user)
    const found = reviewRecord({ request_id: body.request_id, comment: body.comment, revision: body.expected_revision + 1 })
    server[taskId].history.items = [found]
    const readsBefore = getReview.mock.calls.length
    await user.click(screen.getByRole('button', { name: '核对提交结果' }))
    expect(await screen.findByText('审查状态尚未同步，请稍后再次核对。')).toBeVisible()
    expect(getReview).toHaveBeenCalledTimes(readsBefore + 2)
    expect(readPendingReview(taskId)).toEqual(body)
    expect(JSON.parse(sessionStorage.getItem(storageKey(taskId)))).toEqual(body)
    expect(commentField()).toHaveValue(body.comment)
    expect(commentField()).toBeDisabled()
    expect(accept()).toBeDisabled()
    expect(reject()).toBeDisabled()
    expect(screen.getByRole('button', { name: '重试原提交' })).toBeEnabled()
    expect(submitReview).toHaveBeenCalledTimes(1)

    setState(reviewState({ status: 'accepted', revision: found.revision, latest_review: found }))
    await user.click(screen.getByRole('button', { name: '核对提交结果' }))
    await screen.findByText('审查已保存。')
    expect(getReview).toHaveBeenCalledTimes(readsBefore + 3)
    expect(currentStatus().getByText('已接受')).toBeVisible()
    expect(readPendingReview(taskId)).toBeNull()
    expect(sessionStorage.getItem(storageKey(taskId))).toBeNull()
    expect(commentField()).toHaveValue('')
    expect(commentField()).toBeEnabled()
    expect(reject()).toBeEnabled()
    expect(submitReview).toHaveBeenCalledTimes(1)
  })
})
