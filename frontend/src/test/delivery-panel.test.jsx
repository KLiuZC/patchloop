import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import DeliveryPanel from '../components/DeliveryPanel'
import { ApiError } from '../lib/api'
import { clearPendingDelivery, getDelivery, readPendingDelivery, savePendingDelivery, submitDelivery } from '../lib/delivery'
import { secondId, taskId } from './fixtures'

vi.mock('../lib/delivery', async (importOriginal) => ({
  ...await importOriginal(), getDelivery: vi.fn(), submitDelivery: vi.fn(),
}))

const reviewId = '730ac7b8-a3c0-4000-a000-000000000001'
const requestId = '630ac7b8-a3c0-4000-a000-000000000001'
const evidence = {
  base_sha: 'a'.repeat(40), patch_sha256: 'b'.repeat(64), validation_sha256: 'c'.repeat(64),
  task_sha256: 'd'.repeat(64), summary_sha256: 'e'.repeat(64), record_sha256: 'f'.repeat(64),
}
let server

function review(overrides = {}) {
  return {
    task_id: taskId, status: 'accepted', revision: 1, evidence: { ...evidence }, can_accept: true, can_reject: true, reason: '',
    latest_review: { task_id: taskId, review_id: reviewId, revision: 1, decision: 'accepted', evidence: { ...evidence } },
    ...overrides,
  }
}

function plan(overrides = {}) {
  return {
    task_id: taskId, can_deliver: true, reason: '', review_id: reviewId, review_revision: 1,
    evidence: { ...evidence }, target_head: 'a'.repeat(40), branch: 'patchloop/task-1',
    worktree_path: '/repo/worktrees/task-1', dirty: false, changed_paths: ['src/version.py'],
    policy: { allowed_paths: ['src/**'], protected_paths: ['tests/**', '**/conftest.py'] },
    delivery: null, delivery_intact: null, ...overrides,
  }
}

function body(overrides = {}) {
  return { request_id: requestId, review_id: reviewId, review_revision: 1, evidence: { ...evidence }, target_head: 'a'.repeat(40), ...overrides }
}

function record(overrides = {}) {
  return {
    delivery_id: '830ac7b8-a3c0-4000-a000-000000000001', task_id: taskId, ...body(),
    branch: 'patchloop/task-1', worktree_path: '/repo/worktrees/task-1', status: 'completed',
    commit_sha: '1'.repeat(40), tree_sha: '2'.repeat(40), error: '',
    created_at: '2026-10-07T07:00:00Z', updated_at: '2026-10-07T07:00:01Z', ...overrides,
  }
}

function deferred() {
  let resolve
  const promise = new Promise((done) => { resolve = done })
  return { promise, resolve }
}

function mount(initialReview = review(), id = taskId) {
  const view = render(<DeliveryPanel taskId={id} reviewState={initialReview} />)
  return { ...view, update: (nextReview, nextId = id) => view.rerender(<DeliveryPanel taskId={nextId} reviewState={nextReview} />) }
}

const generate = () => screen.getByRole('button', { name: '生成修改分支' })
const reload = () => screen.getByRole('button', { name: '刷新交付计划' })
const retry = () => screen.getByRole('button', { name: '重试原交付' })
const reconcile = () => screen.getByRole('button', { name: '核对交付结果' })
const recover = () => screen.getByRole('button', { name: '恢复交付' })
const ready = () => waitFor(() => expect(reload()).toBeEnabled())
const finish = (id, submission, overrides = {}) => {
  const delivery = record({ task_id: id, ...submission, ...overrides })
  server[id] = { ...server[id], can_deliver: false, delivery, delivery_intact: true }
  return { delivery, replayed: false }
}

beforeEach(() => {
  for (const id of [taskId, secondId]) {
    const saved = readPendingDelivery(id)
    if (saved) clearPendingDelivery(id, saved.request_id)
  }
  sessionStorage.clear()
  vi.resetAllMocks()
  server = { [taskId]: plan(), [secondId]: plan({ task_id: secondId }) }
  getDelivery.mockImplementation(async (id) => structuredClone(server[id]))
  submitDelivery.mockImplementation(async (id, submission) => finish(id, submission))
  vi.stubGlobal('fetch', vi.fn(() => { throw new Error('Unexpected real API request') }))
})

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  for (const id of [taskId, secondId]) {
    const saved = readPendingDelivery(id)
    if (saved) clearPendingDelivery(id, saved.request_id)
  }
  sessionStorage.clear()
  expect(fetch).not.toHaveBeenCalled()
  vi.unstubAllGlobals()
})

describe('local delivery display and explicit submission', () => {
  it('shows the server-generated plan, dirty handling, policy and changed files without delivering on acceptance', async () => {
    server[taskId].dirty = true
    mount()
    await ready()
    expect(screen.getByRole('region', { name: '本地交付' })).toBeVisible()
    expect(screen.getByText('patchloop/task-1')).toBeVisible()
    expect(screen.getByText('/repo/worktrees/task-1')).toBeVisible()
    expect(screen.getByText('tests/**、**/conftest.py')).toBeVisible()
    expect(screen.getByText('src/version.py')).toBeVisible()
    expect(screen.getByText(/保留原改动/)).toBeVisible()
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /push|merge|推送|合并/i })).not.toBeInTheDocument()
    expect(generate()).toBeEnabled()
    expect(submitDelivery).not.toHaveBeenCalled()
  })

  it('blocks while the initial plan is loading', async () => {
    const read = deferred()
    getDelivery.mockReturnValueOnce(read.promise)
    mount()
    expect(generate()).toBeDisabled()
    fireEvent.click(generate())
    expect(submitDelivery).not.toHaveBeenCalled()
    await act(async () => read.resolve(plan()))
    expect(generate()).toBeEnabled()
  })

  it.each([
    null, review({ status: 'pending' }), review({ status: 'rejected' }), review({ status: 'stale' }),
    review({ revision: 2 }), review({ task_id: secondId }), review({ latest_review: null }),
    review({ latest_review: { ...review().latest_review, decision: 'rejected' } }),
    review({ evidence: { ...evidence, record_sha256: '0'.repeat(64) } }),
  ])('requires the latest matching accepted review', async (value) => {
    mount(value)
    await ready()
    expect(generate()).toBeDisabled()
    fireEvent.click(generate())
    expect(submitDelivery).not.toHaveBeenCalled()
  })

  it('blocks disabled server plans and displays the reason', async () => {
    server[taskId] = plan({ can_deliver: false, reason: '目标分支已变化' })
    mount()
    await ready()
    expect(generate()).toBeDisabled()
    expect(screen.getByText('目标分支已变化')).toBeVisible()
  })

  it('fails closed on GET errors and only recovers after a successful refresh', async () => {
    getDelivery.mockRejectedValueOnce(new ApiError('读取失败', 503))
    const user = userEvent.setup()
    mount()
    await screen.findByText('读取失败')
    expect(generate()).toBeDisabled()
    expect(getDelivery).toHaveBeenCalledTimes(1)
    await user.click(reload())
    await ready()
    expect(generate()).toBeEnabled()
    expect(submitDelivery).not.toHaveBeenCalled()
  })

  it('checks the plan before POST, copies completed output and never repeats a successful delivery', async () => {
    const user = userEvent.setup()
    mount()
    await ready()
    await user.click(generate())
    await screen.findByText('本地交付已完成。')
    expect(getDelivery).toHaveBeenCalledTimes(2)
    expect(submitDelivery).toHaveBeenCalledTimes(1)
    expect(submitDelivery.mock.calls[0]).toEqual([taskId, body({ request_id: expect.any(String) })])
    expect(readPendingDelivery(taskId)).toBeNull()
    expect(generate()).toBeDisabled()
    await user.click(screen.getByRole('button', { name: '复制提交 SHA' }))
    expect(await navigator.clipboard.readText()).toBe('1'.repeat(40))
    await user.click(screen.getByRole('button', { name: '复制工作区路径' }))
    expect(await navigator.clipboard.readText()).toBe('/repo/worktrees/task-1')
    await user.click(generate())
    await user.click(reload())
    await ready()
    expect(generate()).toBeDisabled()
    expect(submitDelivery).toHaveBeenCalledTimes(1)
  })

  it('guards rapid repeated clicks during preflight and POST', async () => {
    const check = deferred()
    const post = deferred()
    mount()
    await ready()
    getDelivery.mockReturnValueOnce(check.promise)
    submitDelivery.mockReturnValueOnce(post.promise)
    fireEvent.click(generate())
    fireEvent.click(generate())
    expect(getDelivery).toHaveBeenCalledTimes(2)
    expect(submitDelivery).not.toHaveBeenCalled()
    await act(async () => check.resolve(plan()))
    fireEvent.click(generate())
    expect(submitDelivery).toHaveBeenCalledTimes(1)
    await act(async () => post.resolve(finish(taskId, submitDelivery.mock.calls[0][1])))
    expect(generate()).toBeDisabled()
  })

  it('does not submit if the parent review changes during the preflight read', async () => {
    const check = deferred()
    const view = mount()
    await ready()
    getDelivery.mockReturnValueOnce(check.promise)
    fireEvent.click(generate())
    view.update(review({ status: 'stale' }))
    await act(async () => check.resolve(plan()))
    expect(submitDelivery).not.toHaveBeenCalled()
    expect(screen.getByText(/审查、依据或交付计划已变化/)).toBeVisible()
  })

  it('aborts initial reads and ignores old preflight results when switching tasks', async () => {
    const check = deferred()
    const view = mount()
    await ready()
    const initialSignal = getDelivery.mock.calls[0][1].signal
    getDelivery.mockReturnValueOnce(check.promise)
    fireEvent.click(generate())
    view.update(review({ task_id: secondId, latest_review: { ...review().latest_review, task_id: secondId } }), secondId)
    await ready()
    expect(initialSignal.aborted).toBe(true)
    await act(async () => check.resolve(plan()))
    expect(submitDelivery).not.toHaveBeenCalled()
    expect(generate()).toBeEnabled()
  })
})

describe('preflight conflicts and uncertain requests', () => {
  it.each([
    { review_id: secondId }, { review_revision: 2 }, { target_head: '0'.repeat(40) },
    ...Object.keys(evidence).map((key) => ({ evidence: { ...evidence, [key]: '0'.repeat(key === 'base_sha' ? 40 : 64) } })),
    { branch: 'other' }, { worktree_path: '/other' }, { dirty: true }, { changed_paths: ['other.py'] },
    { policy: { allowed_paths: [], protected_paths: ['extra/**'] } }, { can_deliver: false },
  ])('blocks a changed plan without automatically replacing the displayed snapshot: %j', async (change) => {
    const user = userEvent.setup()
    mount()
    await ready()
    server[taskId] = plan(change)
    await user.click(generate())
    await screen.findByText(/审查、依据或交付计划已变化/)
    expect(screen.getByText('patchloop/task-1')).toBeVisible()
    expect(generate()).toBeDisabled()
    expect(submitDelivery).not.toHaveBeenCalled()
    expect(readPendingDelivery(taskId)).toBeNull()
    await user.click(reload())
    await ready()
    expect(submitDelivery).not.toHaveBeenCalled()
  })

  it('a failed preflight read disables further submission until refreshed', async () => {
    const user = userEvent.setup()
    mount()
    await ready()
    getDelivery.mockRejectedValueOnce(new ApiError('核对读取失败', 503))
    await user.click(generate())
    await screen.findByText('核对读取失败')
    expect(generate()).toBeDisabled()
    expect(submitDelivery).not.toHaveBeenCalled()
    expect(readPendingDelivery(taskId)).toBeNull()
    await user.click(reload())
    await ready()
    expect(generate()).toBeEnabled()
  })

  it('retains a 409 request, checks its status once and requires explicit refresh before retry', async () => {
    submitDelivery.mockRejectedValueOnce(new ApiError('already delivering', 409))
    const user = userEvent.setup()
    mount()
    await ready()
    await user.click(generate())
    await screen.findByText(/交付未确认，可能有其他操作/)
    await ready()
    expect(getDelivery).toHaveBeenCalledTimes(3)
    expect(generate()).toBeDisabled()
    expect(retry()).toBeDisabled()
    expect(readPendingDelivery(taskId)).toEqual(submitDelivery.mock.calls[0][1])
    expect(submitDelivery).toHaveBeenCalledTimes(1)
    await user.click(reload())
    await ready()
    expect(retry()).toBeEnabled()
    expect(submitDelivery).toHaveBeenCalledTimes(1)
  })

  it.each([0, 201, 500])('preserves an uncertain POST (%i) across refresh and remount, and retries the same body', async (status) => {
    submitDelivery.mockRejectedValueOnce(new ApiError('交付结果未确认', status))
    const user = userEvent.setup()
    const first = mount()
    await ready()
    await user.click(generate())
    await screen.findByText('交付结果未确认')
    const original = structuredClone(submitDelivery.mock.calls[0][1])
    expect(readPendingDelivery(taskId)).toEqual(original)
    expect(generate()).toBeDisabled()
    await user.click(reload())
    await ready()
    expect(submitDelivery).toHaveBeenCalledTimes(1)
    first.unmount()
    mount()
    await ready()
    expect(submitDelivery).toHaveBeenCalledTimes(1)
    await user.click(retry())
    await screen.findByText('本地交付已完成。')
    expect(submitDelivery.mock.calls[1]).toEqual([taskId, original])
    expect(readPendingDelivery(taskId)).toBeNull()
    expect(generate()).toBeDisabled()
  })

  it('reconciles a completed record with another request_id without resending', async () => {
    submitDelivery.mockRejectedValueOnce(new ApiError('交付结果未确认'))
    const user = userEvent.setup()
    mount()
    await ready()
    await user.click(generate())
    await screen.findByText('交付结果未确认')
    finish(taskId, submitDelivery.mock.calls[0][1], { request_id: secondId })
    await user.click(reconcile())
    await screen.findByText('本地交付已完成。')
    expect(readPendingDelivery(taskId)).toBeNull()
    expect(screen.queryByRole('button', { name: '重试原交付' })).not.toBeInTheDocument()
    expect(submitDelivery).toHaveBeenCalledTimes(1)
  })

  it('does not reconcile a record with mismatched evidence or target', async () => {
    savePendingDelivery(taskId, body())
    server[taskId] = plan({ delivery: record({ target_head: '0'.repeat(40) }), can_deliver: false })
    const user = userEvent.setup()
    mount()
    await ready()
    await user.click(reconcile())
    await screen.findByText(/尚未确认这次交付/)
    expect(readPendingDelivery(taskId)).toEqual(body())
    expect(retry()).toBeDisabled()
    expect(submitDelivery).not.toHaveBeenCalled()
  })

  it('discovers a concurrent completed delivery during preflight and does not POST', async () => {
    const user = userEvent.setup()
    mount()
    await ready()
    finish(taskId, body({ request_id: secondId }))
    await user.click(generate())
    await screen.findByText('本地交付已完成。')
    expect(submitDelivery).not.toHaveBeenCalled()
    expect(generate()).toBeDisabled()
  })

  it('checks before retrying and stops if the original delivery has completed', async () => {
    savePendingDelivery(taskId, body())
    const user = userEvent.setup()
    mount()
    await ready()
    finish(taskId, body({ request_id: secondId }))
    await user.click(retry())
    await screen.findByText('本地交付已完成。')
    expect(submitDelivery).not.toHaveBeenCalled()
    expect(readPendingDelivery(taskId)).toBeNull()
  })

  it('will not retry a preserved request against a changed HEAD', async () => {
    savePendingDelivery(taskId, body())
    const user = userEvent.setup()
    mount()
    await ready()
    server[taskId] = plan({ target_head: '0'.repeat(40) })
    await user.click(retry())
    await screen.findByText(/审查、依据或交付计划已变化/)
    expect(submitDelivery).not.toHaveBeenCalled()
    expect(readPendingDelivery(taskId)).toEqual(body())
    await user.click(reload())
    await ready()
    expect(retry()).toBeDisabled()
  })

  it('clears a matching completed request even if the panel unmounts during POST', async () => {
    const post = deferred()
    submitDelivery.mockReturnValueOnce(post.promise)
    const user = userEvent.setup()
    const view = mount()
    await ready()
    await user.click(generate())
    await waitFor(() => expect(submitDelivery).toHaveBeenCalledTimes(1))
    const original = submitDelivery.mock.calls[0][1]
    view.unmount()
    await act(async () => post.resolve(finish(taskId, original, { request_id: secondId })))
    expect(readPendingDelivery(taskId)).toBeNull()
    mount()
    await ready()
    expect(generate()).toBeDisabled()
    expect(submitDelivery).toHaveBeenCalledTimes(1)
  })
})

describe('preparing, terminal states and delivery history', () => {
  it('adopts a preparing record after a restart and only resumes it on an explicit retry', async () => {
    server[taskId] = plan({ delivery: record({ request_id: secondId, status: 'preparing', commit_sha: null, tree_sha: null }) })
    const user = userEvent.setup()
    mount()
    await ready()
    expect(screen.getByText(/本地交付进行中/)).toBeVisible()
    expect(readPendingDelivery(taskId)).toEqual(body({ request_id: secondId }))
    expect(submitDelivery).not.toHaveBeenCalled()
    expect(generate()).toBeDisabled()
    expect(recover()).toBeEnabled()
    await user.click(recover())
    await screen.findByText('本地交付已完成。')
    expect(submitDelivery).toHaveBeenCalledExactlyOnceWith(taskId, body({ request_id: secondId }))
    expect(readPendingDelivery(taskId)).toBeNull()
    expect(generate()).toBeDisabled()
  })

  it('uses the recorded request_id for explicit recovery even when a different local request was pending', async () => {
    savePendingDelivery(taskId, body())
    server[taskId] = plan({ delivery: record({ request_id: secondId, status: 'preparing', commit_sha: null, tree_sha: null }) })
    const user = userEvent.setup()
    mount()
    await ready()
    await user.click(recover())
    await screen.findByText('本地交付已完成。')
    expect(submitDelivery).toHaveBeenCalledExactlyOnceWith(taskId, body({ request_id: secondId }))
  })

  it('can explicitly recover after approval changes, and a 409 keeps the same record available for recovery', async () => {
    server[taskId] = plan({ can_deliver: false, review_id: secondId, review_revision: 2, evidence: null,
      target_head: null, branch: null, worktree_path: null, reason: 'a current accepted review with passing validation is required',
      delivery: record({ status: 'preparing', commit_sha: '1'.repeat(40), tree_sha: '2'.repeat(40) }) })
    submitDelivery.mockRejectedValueOnce(new ApiError('another delivery is running', 409))
    const user = userEvent.setup()
    mount(review({ status: 'rejected', revision: 2, latest_review: { ...review().latest_review, review_id: secondId, revision: 2, decision: 'rejected' } }))
    await ready()
    expect(generate()).toBeDisabled()
    expect(recover()).toBeEnabled()
    expect(screen.getByText('需要最新且依据一致的已接受审查。')).toBeVisible()
    expect(submitDelivery).not.toHaveBeenCalled()
    await user.click(recover())
    await ready()
    expect(screen.getByText(/本地交付进行中/)).toBeVisible()
    expect(submitDelivery).toHaveBeenCalledExactlyOnceWith(taskId, body())
    expect(recover()).toBeEnabled()
    await user.click(recover())
    await screen.findByText('本地交付已完成。')
    expect(submitDelivery.mock.calls).toEqual([[taskId, body()], [taskId, body()]])
    expect(readPendingDelivery(taskId)).toBeNull()
    expect(generate()).toBeDisabled()
  })

  it('never replaces completed recovery output with an older preparing response', async () => {
    const previous = plan({ delivery: record({ status: 'preparing', commit_sha: null, tree_sha: null }) })
    server[taskId] = structuredClone(previous)
    const user = userEvent.setup()
    mount()
    await ready()
    await user.click(recover())
    await screen.findByText('本地交付已完成。')
    getDelivery.mockResolvedValueOnce(previous)
    await user.click(reload())
    await ready()
    expect(screen.getByText('本地交付已完成。')).toBeVisible()
    expect(screen.getByRole('button', { name: '复制提交 SHA' })).toBeEnabled()
    expect(screen.queryByRole('button', { name: '恢复交付' })).not.toBeInTheDocument()
    expect(screen.queryByText(/本地交付进行中/)).not.toBeInTheDocument()
    expect(readPendingDelivery(taskId)).toBeNull()
    expect(submitDelivery).toHaveBeenCalledTimes(1)
  })

  it.each(['post', 'conflict', 'reload'])('polls a preparing record discovered via %s and stops on completion', async (source) => {
    vi.useFakeTimers()
    const preparing = record({ status: 'preparing', commit_sha: null, tree_sha: null })
    if (source === 'reload') {
      savePendingDelivery(taskId, body())
      server[taskId] = plan({ can_deliver: false, delivery: preparing })
    } else if (source === 'post') {
      submitDelivery.mockImplementationOnce(async (id, submission) => finish(id, submission, { status: 'preparing', commit_sha: null, tree_sha: null }))
    } else {
      submitDelivery.mockImplementationOnce(async (id, submission) => {
        finish(id, submission, { status: 'preparing', commit_sha: null, tree_sha: null })
        throw new ApiError('preparing', 409)
      })
    }
    mount()
    await act(async () => {})
    if (source !== 'reload') await act(async () => fireEvent.click(generate()))
    expect(screen.getByText(/本地交付进行中/)).toBeVisible()
    expect(generate()).toBeDisabled()
    const reads = getDelivery.mock.calls.length
    await act(async () => { await vi.advanceTimersByTimeAsync(2000) })
    expect(getDelivery).toHaveBeenCalledTimes(reads + 2)
    finish(taskId, body({ request_id: secondId }))
    await act(async () => { await vi.advanceTimersByTimeAsync(1000) })
    expect(screen.getByText('本地交付已完成。')).toBeVisible()
    expect(readPendingDelivery(taskId)).toBeNull()
    const completedReads = getDelivery.mock.calls.length
    await act(async () => { await vi.advanceTimersByTimeAsync(5000) })
    expect(getDelivery).toHaveBeenCalledTimes(completedReads)
    expect(submitDelivery).toHaveBeenCalledTimes(source === 'reload' ? 0 : 1)
  })

  it('keeps polling through read failures and stops on failed status without resubmitting', async () => {
    vi.useFakeTimers()
    savePendingDelivery(taskId, body())
    server[taskId] = plan({ can_deliver: false, delivery: record({ status: 'preparing', commit_sha: null, tree_sha: null }) })
    mount()
    await act(async () => {})
    const error = new ApiError('暂时无法读取', 503)
    getDelivery.mockRejectedValueOnce(error).mockRejectedValueOnce(error)
    await act(async () => { await vi.advanceTimersByTimeAsync(2000) })
    expect(generate()).toBeDisabled()
    expect(screen.getByText('暂时无法读取')).toBeVisible()
    finish(taskId, body(), { status: 'failed', error: '无法应用补丁', commit_sha: null, tree_sha: null })
    await act(async () => { await vi.advanceTimersByTimeAsync(1000) })
    expect(screen.getByText('本地交付失败。')).toBeVisible()
    expect(screen.getByText('无法应用补丁')).toBeVisible()
    expect(readPendingDelivery(taskId)).toBeNull()
    expect(submitDelivery).not.toHaveBeenCalled()
    const reads = getDelivery.mock.calls.length
    await act(async () => { await vi.advanceTimersByTimeAsync(5000) })
    expect(getDelivery).toHaveBeenCalledTimes(reads)
  })

  it('warns when completed output has changed and never regenerates the same review', async () => {
    server[taskId] = plan({ can_deliver: false, delivery: record(), delivery_intact: false })
    mount()
    await ready()
    expect(screen.getByText(/交付分支或工作区已变化/)).toBeVisible()
    expect(generate()).toBeDisabled()
    expect(submitDelivery).not.toHaveBeenCalled()
  })

  it('accepts completed server states without a current plan and shows the recorded destination', async () => {
    server[taskId] = plan({ can_deliver: false, target_head: null, branch: null, worktree_path: null,
      delivery: record(), delivery_intact: true, changed_paths: [] })
    mount()
    await ready()
    expect(screen.getByText('本地交付已完成。')).toBeVisible()
    expect(screen.getByRole('button', { name: '复制工作区路径' })).toBeEnabled()
    expect(screen.queryByText(/原工作区干净/)).not.toBeInTheDocument()
    expect(generate()).toBeDisabled()
  })

  it('shows old delivery history while allowing a newer accepted review to deliver explicitly', async () => {
    server[taskId] = plan({ review_id: secondId, review_revision: 2, delivery: record(), delivery_intact: true })
    mount(review({ revision: 2, latest_review: { ...review().latest_review, review_id: secondId, revision: 2 } }))
    await ready()
    expect(screen.getByText(/第 1 次审查（历史交付）/)).toBeVisible()
    expect(generate()).toBeEnabled()
    expect(submitDelivery).not.toHaveBeenCalled()
  })
})
