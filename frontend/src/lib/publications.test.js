import { webcrypto } from 'node:crypto'
import { TextEncoder } from 'node:util'
import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest'
import {
  checkPublication, getPublications, keepLatestPublications, planHash, previewPublication,
  publicationMatchesApproval, submitPublication, validPreviewInput,
} from './publications'
import { readPendingPublication, savePendingPublication, clearPendingPublication } from './publication-draft'
import { jsonResponse, taskId, secondId } from '../test/fixtures'
import { delivery, plan, previewValue, publication, remoteValue, review, source, submission } from '../test/publication-fixtures'

beforeEach(() => { vi.stubGlobal('crypto', webcrypto); vi.stubGlobal('TextEncoder', TextEncoder); sessionStorage.clear() })
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks() })

describe('publication API boundaries', () => {
  it('verifies the Python-compatible plan digest including Unicode and precise command text', async () => {
    const value = { z: '中文 🐛', a: { title: 'line\n```shell\nprintf "\\n"\n```', draft: false }, paths: ['src/v.py', 'docs/修改.md'] }
    // Reference generated independently with Python json.dumps and hashlib.sha256.
    expect(await planHash(value)).toBe('8982e5f9fcde0386cd0aa881382134cfc2fcefe904b377fa7f88fd077838d1ff')
  })

  it('keeps offline preview and remote check separate and preserves the input', async () => {
    const value = await previewValue()
    const fetch = vi.fn().mockResolvedValueOnce(jsonResponse(value)).mockResolvedValueOnce(jsonResponse(remoteValue(value)))
    vi.stubGlobal('fetch', fetch)
    expect(await previewPublication(taskId, { base_branch: 'main' }, { source })).toEqual(value)
    expect((await checkPublication(taskId, { base_branch: 'main' }, { source })).can_publish).toBe(true)
    expect(fetch.mock.calls.map(([url]) => url)).toEqual([
      `/api/tasks/${taskId}/github-delivery/preview`, `/api/tasks/${taskId}/github-delivery/check`,
    ])
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({ base_branch: 'main' })
  })

  it.each(['task', 'source', 'repository', 'hash', 'content', 'input', 'branch'])('rejects a mismatched preview: %s', async (change) => {
    const value = await previewValue()
    if (change === 'task') value.plan.task_id = secondId
    if (change === 'source') value.plan.source_id = secondId
    if (change === 'repository') value.plan.repository.html_url = 'https://github.com.evil.invalid/repo'
    if (change === 'hash') value.plan_sha256 = '0'.repeat(64)
    if (change === 'content') value.plan.body += ' extra'
    if (change === 'input') value.plan.base_branch = 'other'
    if (change === 'branch') value.plan.head_branch = 'main'
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(value)))
    await expect(previewPublication(taskId, { base_branch: 'main' }, { source })).rejects.toBeInstanceOf(Error)
  })

  it.each(['url', 'head', 'confirmation', 'evidence', 'completion'])('rejects inconsistent publication history: %s', async (change) => {
    const value = publication(await previewValue())
    if (change === 'url') value.pull_request.url = 'javascript:alert(1)'
    if (change === 'head') value.pull_request.head_sha = '0'.repeat(40)
    if (change === 'confirmation') value.submission.confirmed = false
    if (change === 'evidence') value.plan.evidence.base_sha = '0'.repeat(40)
    if (change === 'completion') value.pr_attempted = false
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({ task_id: taskId, configured: true, items: [value] })))
    await expect(getPublications(taskId, { source })).rejects.toBeInstanceOf(Error)
  })

  it('accepts a canonical request id on same-plan replay, but rejects a different plan', async () => {
    const preview = await previewValue()
    const record = publication(preview)
    const fetch = vi.fn(async () => jsonResponse({ publication: record, replayed: true }))
    vi.stubGlobal('fetch', fetch)
    const body = submission(preview, { request_id: secondId })
    expect((await submitPublication(taskId, body, { source })).publication.request_id).toBe(record.request_id)
    record.plan_sha256 = '1'.repeat(64)
    await expect(submitPublication(taskId, body, { source })).rejects.toBeInstanceOf(Error)
    expect(fetch).toHaveBeenCalledTimes(2)
  })

  it('never automatically retries a failed write or accepts an unconfirmed request', async () => {
    const value = await previewValue()
    const fetch = vi.fn(async () => { throw new TypeError('offline') })
    vi.stubGlobal('fetch', fetch)
    await expect(submitPublication(taskId, submission(value, { confirmed: false }))).rejects.toMatchObject({ status: 422 })
    expect(fetch).not.toHaveBeenCalled()
    await expect(submitPublication(taskId, submission(value))).rejects.toThrow('发布结果未确认')
    expect(fetch).toHaveBeenCalledTimes(1)
  })

  it('treats an unverified successful write response as uncertain rather than a definitive rejection', async () => {
    const value = await previewValue()
    const record = publication(value)
    record.plan.changed_paths.push('unreviewed.py')
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({ publication: record, replayed: false }, 201)))
    await expect(submitPublication(taskId, submission(value))).rejects.toMatchObject({ status: 0 })
  })

  it('does not report a remote check as publishable if the branch is already present', async () => {
    const value = remoteValue(await previewValue(), { remote_head_sha: '1'.repeat(40) })
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(value)))
    await expect(checkPublication(taskId, { base_branch: 'main' })).rejects.toBeInstanceOf(Error)
  })

  it.each([{ base_branch: '-main' }, { base_branch: 'main', title: '' }, { base_branch: 'main', body: '\0' },
    { base_branch: 'main', draft: 'true' }, { base_branch: 'main', token: 'not-a-token' }])('rejects invalid editable input %j', (value) => {
    expect(validPreviewInput(value)).toBe(false)
  })
})

describe('publication recovery', () => {
  it('preserves exact requests by task and clears only the matching request id', async () => {
    const body = submission(await previewValue())
    savePendingPublication(taskId, body)
    body.preview.body = 'changed after saving'
    const saved = readPendingPublication(taskId)
    expect(saved.request.preview.body).not.toBe(body.preview.body)
    expect(readPendingPublication(secondId).request).toBeNull()
    clearPendingPublication(taskId, secondId)
    expect(readPendingPublication(taskId)).toEqual(saved)
    clearPendingPublication(taskId, saved.request.request_id)
    expect(readPendingPublication(taskId).request).toBeNull()
  })

  it('fails before publication when request persistence is unavailable', async () => {
    const body = submission(await previewValue())
    const original = sessionStorage
    vi.stubGlobal('sessionStorage', { getItem: original.getItem.bind(original), setItem: () => { throw new Error('storage full') } })
    expect(() => savePendingPublication(taskId, body)).toThrow('本次没有发送')
    expect(readPendingPublication(taskId).request).toBeNull()
  })

  it('does not silently discard corrupted pending requests', () => {
    sessionStorage.setItem(`patchloop:pending-publication:${taskId}`, '{broken')
    expect(readPendingPublication(taskId).error).not.toBe('')
  })

  it('keeps completed records and newer microsecond checkpoints when an old response arrives', async () => {
    const record = publication(await previewValue())
    const state = { task_id: taskId, configured: true, items: [record] }
    const older = { ...record, status: 'pending', phase: 'push', pull_request: null, updated_at: record.created_at }
    expect(keepLatestPublications(state, { ...state, items: [older] }).items).toEqual([record])
    expect(keepLatestPublications(state, { ...state, items: [] }).items).toEqual([record])
    const newer = { ...older, status: 'uncertain', updated_at: '2026-10-09T09:00:00.000002Z' }
    expect(keepLatestPublications({ ...state, items: [newer] }, { ...state, items: [older] }).items).toEqual([newer])
  })

  it('requires matching approval and exact local delivery before enabling publication', () => {
    const value = plan()
    expect(publicationMatchesApproval(value, review(), delivery())).toBe(true)
    expect(publicationMatchesApproval(value, review({ status: 'stale' }), delivery())).toBe(false)
    expect(publicationMatchesApproval(value, review(), delivery({ delivery_intact: false }))).toBe(false)
    expect(publicationMatchesApproval({ ...value, head_sha: '3'.repeat(40) }, review(), delivery())).toBe(false)
  })
})
