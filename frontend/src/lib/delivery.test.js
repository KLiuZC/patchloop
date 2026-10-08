import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from './api'
import {
  acceptsDeliveryReview, clearPendingDelivery, getDelivery, matchesDelivery, readPendingDelivery,
  sameDeliveryPlan, savePendingDelivery, submitDelivery,
} from './delivery'
import { jsonResponse, secondId, taskId } from '../test/fixtures'

const requestId = '630ac7b8-a3c0-4000-a000-000000000001'
const reviewId = '730ac7b8-a3c0-4000-a000-000000000001'
const evidence = {
  base_sha: 'a'.repeat(40), patch_sha256: 'b'.repeat(64), validation_sha256: 'c'.repeat(64),
  task_sha256: 'd'.repeat(64), summary_sha256: 'e'.repeat(64), record_sha256: 'f'.repeat(64),
}
const payload = { request_id: requestId, review_id: reviewId, review_revision: 1, evidence, target_head: 'a'.repeat(40) }
const storageKey = (id) => `patchloop:pending-delivery:${id}`

function record(overrides = {}) {
  return {
    delivery_id: '830ac7b8-a3c0-4000-a000-000000000001', task_id: taskId, ...payload,
    branch: 'patchloop/task-1', worktree_path: '/repo/worktrees/task-1', status: 'completed',
    commit_sha: '1'.repeat(40), tree_sha: '2'.repeat(40), error: '',
    created_at: '2026-10-07T07:00:00Z', updated_at: '2026-10-07T07:00:01Z', ...overrides,
  }
}

function state(overrides = {}) {
  return {
    task_id: taskId, can_deliver: true, reason: '', review_id: reviewId, review_revision: 1,
    evidence: { ...evidence }, target_head: payload.target_head, branch: 'patchloop/task-1',
    worktree_path: '/repo/worktrees/task-1', dirty: false, changed_paths: ['src/version.py'],
    policy: { allowed_paths: ['src/**'], protected_paths: ['tests/**'] },
    delivery: null, delivery_intact: null, ...overrides,
  }
}

function review(overrides = {}) {
  return {
    task_id: taskId, status: 'accepted', revision: 1, evidence,
    latest_review: { task_id: taskId, review_id: reviewId, revision: 1, decision: 'accepted', evidence },
    ...overrides,
  }
}

const respond = (data, status = 200) => fetch.mockResolvedValueOnce(jsonResponse(data, status))

beforeEach(() => {
  for (const id of [taskId, secondId]) {
    const saved = readPendingDelivery(id)
    if (saved) clearPendingDelivery(id, saved.request_id)
  }
  sessionStorage.clear()
  vi.stubGlobal('fetch', vi.fn(() => { throw new Error('Unexpected network request') }))
})
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals() })

describe('delivery GET DTO', () => {
  it.each([
    state(), state({ dirty: true }), state({ delivery: record(), delivery_intact: true, can_deliver: false }),
    state({ delivery: record({ status: 'preparing', commit_sha: null, tree_sha: null }), can_deliver: false }),
    state({ delivery: record({ status: 'failed', error: 'conflict', commit_sha: null, tree_sha: null }), can_deliver: false }),
    state({ can_deliver: false, review_id: null, review_revision: 0, evidence: null, target_head: null, branch: null, worktree_path: null }),
    state({ review_id: secondId, review_revision: 2, delivery: record(), delivery_intact: false }),
    state({ evidence: { ...evidence, base_sha: 'a'.repeat(64) }, target_head: 'b'.repeat(64) }),
    state({ delivery: record({ commit_sha: null, tree_sha: null }), can_deliver: false }),
  ])('accepts complete states, nullable SHAs and historical deliveries', async (value) => {
    respond(value)
    const signal = new AbortController().signal
    await expect(getDelivery(taskId, { signal })).resolves.toEqual(value)
    expect(fetch).toHaveBeenCalledWith(`/api/tasks/${taskId}/delivery`, expect.objectContaining({ method: 'GET', signal }))
  })

  it.each(Object.keys(state()))('requires state field %s', async (key) => {
    const value = state()
    delete value[key]
    respond(value)
    await expect(getDelivery(taskId)).rejects.toMatchObject({ name: 'ApiError', status: 200 })
  })

  it.each([
    null, [], {}, state({ task_id: secondId }), state({ can_deliver: 'true' }), state({ reason: null }),
    state({ review_id: '' }), state({ review_revision: -1 }), state({ review_revision: 1.5 }),
    state({ review_revision: '1' }), state({ review_revision: Number.MAX_SAFE_INTEGER + 1 }),
    state({ evidence: {} }), state({ evidence: { ...evidence, extra: 'unexpected' } }),
    state({ target_head: 'HEAD' }), state({ branch: 1 }), state({ worktree_path: '' }),
    state({ dirty: 'false' }), state({ changed_paths: {} }), state({ changed_paths: [2] }),
    state({ policy: null }), state({ policy: {} }), state({ policy: { allowed_paths: [], protected_paths: [null] } }),
    state({ policy: { allowed_paths: ['../private'], protected_paths: [] } }),
    state({ policy: { allowed_paths: [], protected_paths: 'tests/**' } }),
    state({ policy: { allowed_paths: [], protected_paths: [], extra: true } }),
    state({ delivery: {} }), state({ delivery: record({ task_id: secondId }) }), state({ delivery_intact: 1 }),
    ...['review_id', 'evidence', 'target_head', 'branch', 'worktree_path'].map((key) => state({ [key]: null })),
    state({ review_revision: 0 }),
  ])('rejects malformed or unsafe plans: %j', async (value) => {
    respond(value)
    await expect(getDelivery(taskId)).rejects.toBeInstanceOf(ApiError)
  })

  it.each(Object.keys(evidence))('checks SHA evidence field %s', async (key) => {
    for (const value of [undefined, null, 0, {}, '', 'z'.repeat(64), 'a'.repeat(41)]) {
      respond(state({ evidence: { ...evidence, [key]: value } }))
      await expect(getDelivery(taskId)).rejects.toBeInstanceOf(ApiError)
    }
  })

  it.each(Object.keys(record()))('requires record field %s', async (key) => {
    const item = record()
    delete item[key]
    respond(state({ delivery: item }))
    await expect(getDelivery(taskId)).rejects.toBeInstanceOf(ApiError)
  })

  it.each([
    { delivery_id: 0 }, { request_id: 'invalid' }, { review_id: 'invalid' }, { review_revision: 0 },
    { status: 'accepted' }, { evidence: null }, { target_head: '' }, { branch: null }, { worktree_path: [] },
    { commit_sha: 'abc' }, { tree_sha: 'HEAD' }, { error: {} }, { created_at: 4 }, { updated_at: 'yesterday' },
  ])('rejects invalid record fields: %j', async (changes) => {
    respond(state({ delivery: record(changes) }))
    await expect(getDelivery(taskId)).rejects.toBeInstanceOf(ApiError)
  })
})

describe('delivery POST contract', () => {
  it.each([201, 200])('sends one exact request and accepts HTTP %s', async (status) => {
    const response = { delivery: record({ request_id: secondId }), replayed: status === 200 }
    respond(response, status)
    await expect(submitDelivery(taskId, payload)).resolves.toEqual(response)
    expect(fetch).toHaveBeenCalledTimes(1)
    expect(fetch).toHaveBeenCalledWith(`/api/tasks/${taskId}/delivery`, expect.objectContaining({
      method: 'POST', body: JSON.stringify(payload), headers: { 'Content-Type': 'application/json' },
    }))
  })

  it('matches the serialized payload if the caller later mutates it', async () => {
    respond({ delivery: record(), replayed: false }, 201)
    const submitted = structuredClone(payload)
    const response = submitDelivery(taskId, submitted)
    submitted.evidence.record_sha256 = '0'.repeat(64)
    submitted.target_head = '0'.repeat(40)
    await expect(response).resolves.toMatchObject({ delivery: record() })
  })

  it.each([
    null, {}, { delivery: record() }, { replayed: false }, { delivery: record(), replayed: 'true' },
    ...[{ task_id: secondId }, { review_id: secondId }, { review_revision: 2 }, { target_head: '0'.repeat(40) }]
      .map((change) => ({ delivery: record(change), replayed: true })),
    ...Object.keys(evidence).map((key) => ({ replayed: false, delivery: record({
      evidence: { ...evidence, [key]: '0'.repeat(key === 'base_sha' ? 40 : 64) },
    }) })),
  ])('rejects malformed or mismatched responses without retrying', async (value) => {
    respond(value, 201)
    await expect(submitDelivery(taskId, payload)).rejects.toMatchObject({
      name: 'ApiError', status: 201, message: expect.stringContaining('交付结果未确认'),
    })
    expect(fetch).toHaveBeenCalledTimes(1)
  })

  it.each([
    { request_id: '' }, { review_id: null }, { review_revision: 0 }, { target_head: 'HEAD' },
    { evidence: {} }, { branch: 'user-provided' }, { worktree_path: '/user/path' },
  ])('rejects invalid submissions locally: %j', async (changes) => {
    await expect(submitDelivery(taskId, { ...payload, ...changes })).rejects.toMatchObject({ status: 422 })
    expect(fetch).not.toHaveBeenCalled()
  })

  it.each(['network', 'body', 'json'])('retains delivery-specific uncertainty on %s failures', async (failure) => {
    if (failure === 'network') fetch.mockRejectedValueOnce(new TypeError('Failed to fetch'))
    if (failure === 'body') fetch.mockResolvedValueOnce({ ok: true, status: 201, text: () => Promise.reject(new Error('closed')) })
    if (failure === 'json') fetch.mockResolvedValueOnce(new Response('broken', { status: 201 }))
    await expect(submitDelivery(taskId, payload)).rejects.toMatchObject({ message: expect.stringContaining('交付结果未确认') })
    expect(fetch).toHaveBeenCalledTimes(1)
  })

  it.each([409, 422, 500, 502])('preserves HTTP %i without retrying', async (status) => {
    respond({ detail: 'delivery unavailable' }, status)
    await expect(submitDelivery(taskId, payload)).rejects.toMatchObject({ status, message: 'delivery unavailable' })
    expect(fetch).toHaveBeenCalledTimes(1)
  })

  it('encodes the URL and preserves GET cancellation', async () => {
    const signal = new AbortController().signal
    const error = new DOMException('Aborted', 'AbortError')
    fetch.mockRejectedValueOnce(error)
    await expect(getDelivery('id/with ?#中文', { signal })).rejects.toBe(error)
    expect(fetch.mock.calls[0][0]).toBe('/api/tasks/id%2Fwith%20%3F%23%E4%B8%AD%E6%96%87/delivery')
  })
})

describe('frozen delivery identity and storage', () => {
  it('compares all displayed plan fields without depending on request_id', () => {
    expect(sameDeliveryPlan(state(), structuredClone(state()))).toBe(true)
    for (const change of [
      { task_id: secondId }, { review_id: secondId }, { review_revision: 2 }, { target_head: '0'.repeat(40) },
      { evidence: { ...evidence, record_sha256: '0'.repeat(64) } }, { branch: 'another' }, { worktree_path: '/another' },
      { dirty: true }, { changed_paths: ['other.py'] }, { policy: { allowed_paths: [], protected_paths: [] } },
    ]) expect(sameDeliveryPlan(state(), state(change))).toBe(false)
    expect(matchesDelivery(record({ request_id: secondId }), payload, taskId)).toBe(true)
    expect(matchesDelivery(record(), payload, secondId)).toBe(false)
  })

  it('requires the latest accepted review with matching evidence', () => {
    expect(acceptsDeliveryReview(review(), state())).toBe(true)
    for (const value of [
      undefined, null, review({ status: 'stale' }), review({ status: 'rejected' }), review({ revision: 2 }),
      review({ task_id: secondId }), review({ latest_review: null }), review({ evidence: null }),
      review({ latest_review: { ...review().latest_review, decision: 'rejected' } }),
      review({ latest_review: { ...review().latest_review, evidence: { ...evidence, patch_sha256: '0'.repeat(64) } } }),
    ]) expect(acceptsDeliveryReview(value, state())).toBe(false)
  })

  it('keeps immutable, task-scoped requests in a separate delivery key', () => {
    const submitted = structuredClone(payload)
    sessionStorage.setItem(`patchloop:pending-review:${taskId}`, 'review draft')
    savePendingDelivery(taskId, submitted)
    submitted.evidence.record_sha256 = '0'.repeat(64)
    expect(readPendingDelivery(taskId)).toEqual(payload)
    expect(JSON.parse(sessionStorage.getItem(storageKey(taskId)))).toEqual(payload)
    expect(readPendingDelivery(secondId)).toBeNull()
    clearPendingDelivery(taskId, secondId)
    expect(readPendingDelivery(taskId)).toEqual(payload)
    clearPendingDelivery(taskId, requestId)
    expect(readPendingDelivery(taskId)).toBeNull()
    expect(sessionStorage.getItem(`patchloop:pending-review:${taskId}`)).toBe('review draft')
  })

  it('can restore a request directly from session storage', () => {
    sessionStorage.setItem(storageKey(taskId), JSON.stringify(payload))
    expect(readPendingDelivery(taskId)).toEqual(payload)
  })

  it.each(['invalid json', 'null', '{}', JSON.stringify({ ...payload, review_revision: '1' })])('ignores invalid stored requests', (value) => {
    sessionStorage.setItem(storageKey(taskId), value)
    expect(readPendingDelivery(taskId)).toBeNull()
  })

  it('retains a frozen request when session storage is unavailable', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('unavailable') })
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('unavailable') })
    vi.spyOn(Storage.prototype, 'removeItem').mockImplementation(() => { throw new Error('unavailable') })
    savePendingDelivery(taskId, payload)
    const copy = readPendingDelivery(taskId)
    copy.evidence.patch_sha256 = '0'.repeat(64)
    expect(readPendingDelivery(taskId)).toEqual(payload)
    clearPendingDelivery(taskId, requestId)
    expect(readPendingDelivery(taskId)).toBeNull()
  })
})
