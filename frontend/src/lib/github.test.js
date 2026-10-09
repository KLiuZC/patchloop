import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from './api.js'
import {
  createIssueTask, getIssue, getPreparation, importIssue, prepareRepository,
  validIssue, validPreparation, validTaskSource,
} from './github.js'
import { jsonResponse, secondId, taskRecord } from '../test/fixtures.js'

const requestId = '630ac7b8-a3c0-4000-a000-000000000001'
const sourceId = '730ac7b8-a3c0-4000-a000-000000000001'
const preparationId = '830ac7b8-a3c0-4000-a000-000000000001'
const date = '2026-10-09T07:00:00.123456Z'
const url = 'https://github.com/acme/widget/issues/12'
const repository = { id: 123, full_name: 'Acme/Widget', html_url: 'https://github.com/Acme/Widget', default_branch: 'main', archived: false, visibility: 'public' }
const execution = {
  image: 'patchloop-toy', provider: 'deepseek', model: 'deepseek-flash', platform: null,
  memory_mb: 2048, max_model_calls: 30, command_timeout_seconds: 60,
  verification_timeout_seconds: 180, budget_reminders: true, explicit_finish: false,
}
const pathPolicy = { allowed_paths: ['src/**'], protected_paths: ['tests/**'] }
const importPayload = { request_id: requestId, url }
const preparePayload = { request_id: requestId, source_id: sourceId, ref: 'main' }
const taskPayload = {
  request_id: requestId, preparation_id: preparationId, goal: 'Fix the widget',
  verification_command: 'python -m pytest -q', workdir: 'packages/widget', execution, path_policy: pathPolicy,
}

function issue(overrides = {}) {
  return {
    source_id: sourceId, request_id: requestId, kind: 'github_issue', requested_url: url, url,
    api_url: 'https://api.github.com/repos/acme/widget/issues/12', repository: { ...repository },
    issue_id: 456, issue_number: 12, title: 'Widget regression', body: '<script>untrusted text</script>', state: 'open',
    issue_created_at: date, issue_updated_at: date, fetched_at: date, api_version: '2026-03-10', snapshot_sha256: 'b'.repeat(64),
    ...overrides,
  }
}

function preparation(overrides = {}) {
  return {
    preparation_id: preparationId, request_id: requestId, source_id: sourceId, repository: { ...repository },
    requested_ref: 'main', resolved_ref: 'main', base_sha: 'a'.repeat(40), resolved_at: date,
    clone_url: 'https://github.com/acme/widget.git', repo_path: '/repo/patchloop', status: 'ready',
    created_at: date, updated_at: date, finished_at: date, error: '', error_status: null, ...overrides,
  }
}

function taskSource(overrides = {}) {
  return { kind: 'github_issue', preparation_id: preparationId, issue: issue(), repo_path: '/repo/patchloop', base_sha: 'a'.repeat(40), resolved_ref: 'main', resolved_at: date, ...overrides }
}

function issueTask(overrides = {}) {
  return taskRecord({ goal: taskPayload.goal, workdir: taskPayload.workdir, execution: { ...execution }, path_policy: structuredClone(pathPolicy), source: taskSource(), ...overrides })
}

const respond = (value, status = 200) => fetch.mockResolvedValueOnce(jsonResponse(value, status))

beforeEach(() => vi.stubGlobal('fetch', vi.fn(() => { throw new Error('Unexpected network request') })))
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals() })

describe('frozen GitHub contracts', () => {
  it('accepts backend Issue, preparation and task source records', () => {
    expect(validIssue(issue())).toBe(true)
    expect(validIssue(issue({ body: '', state: 'closed', repository: { ...repository, default_branch: null, archived: true } }))).toBe(true)
    expect(validIssue(issue({ fetched_at: '2026-10-09T15:00:00+08:00' }))).toBe(true)
    expect(validPreparation(preparation())).toBe(true)
    expect(validTaskSource(taskSource())).toBe(true)
    expect(fetch).not.toHaveBeenCalled()
  })

  it.each(Object.keys(issue()))('requires Issue field %s', (key) => {
    const value = issue(); delete value[key]
    expect(validIssue(value)).toBe(false)
  })

  it.each([
    { source_id: '' }, { request_id: 1 }, { kind: 'url' }, { issue_id: '456' }, { issue_number: 0 },
    { issue_id: Number.MAX_SAFE_INTEGER + 1 }, { title: null }, { title: '' }, { body: null }, { state: 'pending' },
    { fetched_at: 1 }, { fetched_at: 'today' }, { fetched_at: '2026-10-09T07:00:00' },
    { fetched_at: '2026-02-30T07:00:00Z' }, { fetched_at: '2026-10-09T24:00:00Z' },
    { snapshot_sha256: 'A'.repeat(64) }, { snapshot_sha256: 'b'.repeat(40) }, { api_version: null },
    { requested_url: 'https://github.com/acme/widget/issues/13' }, { url: 'https://github.com/acme/other/issues/12' },
    { api_url: 'https://api.github.com/repos/acme/widget/issues/13' }, { repository: null },
    ...[{ id: 0 }, { id: '123' }, { full_name: 'acme/other' }, { full_name: 'acme/..' }, { archived: 'false' }, { visibility: 'private' }, { default_branch: '' }]
      .map((changes) => ({ repository: { ...repository, ...changes } })),
  ])('rejects invalid Issue data: %j', (changes) => expect(validIssue(issue(changes))).toBe(false))

  it.each([
    'http://github.com/acme/widget', 'javascript:alert(1)', '//github.com/acme/widget',
    'https://github.com.evil.test/acme/widget', 'https://user@github.com/acme/widget',
    'https://github.com:443/acme/widget', 'https://github.com/acme/widget?next=evil',
    'https://github.com/acme/widget#anchor', 'https://github.com/acme\\widget',
    'https://github.com/acme/%2e%2e', 'https://github.com/acme/widget\n',
  ])('rejects unsafe repository links %s', (html_url) => {
    expect(validIssue(issue({ repository: { ...repository, html_url } }))).toBe(false)
  })

  it.each(Object.keys(preparation()))('requires preparation field %s', (key) => {
    const value = preparation(); delete value[key]
    expect(validPreparation(value)).toBe(false)
  })

  it('accepts unresolved and pinned preparing/failed states', () => {
    const unresolved = { resolved_ref: null, base_sha: null, resolved_at: null }
    for (const pin of [unresolved, {}]) {
      expect(validPreparation(preparation({ ...pin, status: 'preparing', finished_at: null }))).toBe(true)
      expect(validPreparation(preparation({ ...pin, status: 'failed', error: 'fetch failed', error_status: 502 }))).toBe(true)
    }
  })

  it.each([
    { preparation_id: '' }, { source_id: 1 }, { request_id: null }, { requested_ref: '../main' },
    { resolved_ref: 'other' }, { base_sha: 'a'.repeat(64) }, { base_sha: 'A'.repeat(40) },
    { resolved_at: null }, { finished_at: null }, { status: 'completed' }, { repo_path: '' },
    { clone_url: 'https://evil.test/acme/widget.git' }, { clone_url: 'https://github.com/acme/other.git' },
    { error: null }, { error_status: '502' }, { error_status: 600 }, { error: 'failed' },
    { status: 'failed', error: '' }, { status: 'preparing' }, { created_at: 'yesterday' },
  ])('rejects invalid preparation data: %j', (changes) => expect(validPreparation(preparation(changes))).toBe(false))

  it.each(Object.keys(taskSource()))('requires present source field %s', (key) => {
    const value = taskSource(); delete value[key]
    expect(validTaskSource(value)).toBe(false)
  })

  it.each([null, undefined, [], {}, taskSource({ issue: {} }), taskSource({ base_sha: 'HEAD' }), taskSource({ resolved_ref: '-main' })])(
    'rejects malformed present task sources', (value) => expect(validTaskSource(value)).toBe(false),
  )
})

describe('GET identity and cancellation', () => {
  it('returns a source directly and the full preparation recovery state', async () => {
    const signal = new AbortController().signal
    respond(issue()); await expect(getIssue(sourceId, { signal })).resolves.toEqual(issue())
    expect(fetch).toHaveBeenLastCalledWith(`/api/sources/github/issues/${sourceId}`, expect.objectContaining({ method: 'GET', signal }))
    const state = { preparation: preparation(), checkout_intact: false, reason: 'Checkout changed' }
    respond(state); await expect(getPreparation(preparationId, { signal })).resolves.toEqual(state)
    expect(fetch).toHaveBeenLastCalledWith(`/api/repositories/${preparationId}`, expect.objectContaining({ method: 'GET', signal }))
  })

  it.each([
    { preparation: preparation(), checkout_intact: true, reason: '' },
    { preparation: preparation({ status: 'preparing', finished_at: null }), checkout_intact: null, reason: '' },
    { preparation: preparation({ status: 'failed', error: 'failed', error_status: 502 }), checkout_intact: null, reason: 'failed' },
  ])('accepts typed recovery states', async (state) => {
    respond(state); await expect(getPreparation(preparationId)).resolves.toEqual(state)
  })

  it.each([
    {}, { preparation: preparation(), reason: '' }, { preparation: preparation(), checkout_intact: true },
    { preparation: preparation(), checkout_intact: null, reason: '' },
    { preparation: preparation(), checkout_intact: 'true', reason: '' },
    { preparation: preparation({ preparation_id: secondId }), checkout_intact: true, reason: '' },
  ])('rejects invalid/mismatched recovery states', async (state) => {
    respond(state); await expect(getPreparation(preparationId)).rejects.toBeInstanceOf(ApiError)
  })

  it('rejects another source returned for the requested ID', async () => {
    respond(issue({ source_id: secondId })); await expect(getIssue(sourceId)).rejects.toMatchObject({ status: 200 })
  })

  it.each([
    [getIssue, '/api/sources/github/issues/'], [getPreparation, '/api/repositories/'],
  ])('encodes IDs and forwards aborts unchanged', async (get, prefix) => {
    const signal = new AbortController().signal
    const error = new DOMException('Stopped', 'AbortError')
    fetch.mockRejectedValueOnce(error)
    await expect(get('id/with ?#中文', { signal })).rejects.toBe(error)
    expect(fetch).toHaveBeenCalledWith(`${prefix}id%2Fwith%20%3F%23%E4%B8%AD%E6%96%87`, expect.objectContaining({ signal }))
  })
})

const posts = [
  [importIssue, importPayload, '/api/sources/github/issues', 'source', issue, '导入结果未确认'],
  [prepareRepository, preparePayload, '/api/repositories/prepare', 'preparation', preparation, '仓库准备结果未确认'],
  [createIssueTask, taskPayload, '/api/tasks/from-issue', 'task', issueTask, '提交结果未确认'],
]

describe.each(posts)('%s POST', (post, payload, endpoint, key, record, uncertainty) => {
  it.each([201, 200])('sends one exact request and accepts replay at HTTP %i', async (status) => {
    const value = { [key]: record(), replayed: status === 200 }
    respond(value, status); await expect(post(payload)).resolves.toEqual(value)
    expect(fetch).toHaveBeenCalledTimes(1)
    expect(fetch).toHaveBeenCalledWith(endpoint, expect.objectContaining({ method: 'POST', body: JSON.stringify(payload), headers: { 'Content-Type': 'application/json' } }))
  })

  it('matches a frozen copy despite caller mutation after submission', async () => {
    const copy = structuredClone(payload)
    respond({ [key]: record(), replayed: false }, 201)
    const result = post(copy)
    copy.request_id = secondId
    if (key === 'source') copy.url = url.replace('/12', '/13')
    if (key === 'preparation') { copy.ref = 'other'; copy.source_id = secondId }
    if (key === 'task') { copy.goal = 'Other'; copy.execution.image = 'other'; copy.path_policy.allowed_paths.push('other/**') }
    await expect(result).resolves.toMatchObject({ replayed: false })
  })

  it.each(['network', 'body', 'json', 'shape', 'replayed'])('preserves operation-specific uncertainty on %s failure', async (failure) => {
    if (failure === 'network') fetch.mockRejectedValueOnce(new TypeError('closed'))
    if (failure === 'body') fetch.mockResolvedValueOnce({ ok: true, status: 201, text: () => Promise.reject(new Error('closed')) })
    if (failure === 'json') fetch.mockResolvedValueOnce(new Response('invalid JSON', { status: 201 }))
    if (failure === 'shape') respond({ [key]: {}, replayed: false }, 201)
    if (failure === 'replayed') respond({ [key]: record(), replayed: 'false' }, 201)
    await expect(post(payload)).rejects.toMatchObject({ message: expect.stringContaining(uncertainty) })
    expect(fetch).toHaveBeenCalledTimes(1)
  })

  it.each([404, 409, 422, 429, 502, 503])('preserves HTTP %i without retry', async (status) => {
    respond({ detail: 'unavailable' }, status)
    await expect(post(payload)).rejects.toMatchObject({ status, message: 'unavailable' })
    expect(fetch).toHaveBeenCalledTimes(1)
  })

  it('rejects malformed UUID and unexpected payload fields locally', async () => {
    await expect(post({ ...payload, request_id: '' })).rejects.toMatchObject({ status: 422 })
    await expect(post({ ...payload, unexpected: true })).rejects.toMatchObject({ status: 422 })
    expect(fetch).not.toHaveBeenCalled()
  })
})

describe('POST identity and frozen configuration', () => {
  it('normalizes equivalent GitHub Issue URLs as the backend does', async () => {
    respond({ source: issue(), replayed: true })
    await expect(importIssue({ ...importPayload, url: ' HTTPS://GITHUB.COM/Acme/%57idget/issues/0012/?x=1#comment ' })).resolves.toMatchObject({ replayed: true })
  })

  it.each([
    'javascript:alert(1)', 'http://github.com/acme/widget/issues/12', 'https://github.com:443/acme/widget/issues/12',
    'https://evil.test/acme/widget/issues/12', 'https://user@github.com/acme/widget/issues/12',
    'https://github.com/acme/widget/pull/12', 'https://github.com/acme/../issues/12',
    'https://github.com/acme/widget/issues/0', 'https://github.com/acme/widget/issues/9007199254740992',
    'https://github.com/acme/widget/issues/12\n', 'https://github.com/acme\\widget/issues/12',
  ])('rejects unsafe/unsupported import URL locally', async (url) => {
    await expect(importIssue({ ...importPayload, url })).rejects.toMatchObject({ status: 422 })
    expect(fetch).not.toHaveBeenCalled()
  })

  it.each([{ request_id: secondId }, { requested_url: url.replace('/12', '/13'), url: url.replace('/12', '/13'), issue_number: 13, api_url: 'https://api.github.com/repos/acme/widget/issues/13' }])(
    'rejects valid but mismatched import response', async (change) => {
      respond({ source: issue(change), replayed: true }, 200)
      await expect(importIssue(importPayload)).rejects.toMatchObject({ status: 200, message: expect.stringContaining('导入结果未确认') })
    },
  )

  it.each([{ request_id: secondId }, { source_id: secondId }, { requested_ref: 'other', resolved_ref: 'other' }])(
    'rejects valid but mismatched preparation response', async (change) => {
      respond({ preparation: preparation(change), replayed: true })
      await expect(prepareRepository(preparePayload)).rejects.toBeInstanceOf(ApiError)
    },
  )

  it('accepts omitted ref and default workdir/config/policy using backend defaults', async () => {
    respond({ preparation: preparation({ requested_ref: null }), replayed: false }, 201)
    await expect(prepareRepository({ request_id: requestId, source_id: sourceId })).resolves.toMatchObject({ replayed: false })
    const payload = { ...taskPayload, execution: { image: execution.image } }
    delete payload.workdir; delete payload.path_policy
    const policy = { allowed_paths: [], protected_paths: ['tests/**', '**/tests/**', 'testing/**', '**/testing/**', 'test_*.py', '**/test_*.py', '*_test.py', '**/*_test.py', 'conftest.py', '**/conftest.py'] }
    respond({ task: issueTask({ workdir: '.', path_policy: policy }), replayed: false }, 201)
    await expect(createIssueTask(payload)).resolves.toMatchObject({ replayed: false })
  })

  it('matches backend path normalization and permits current task state on replay', async () => {
    respond({ task: issueTask({ status: 'running' }), replayed: true })
    await expect(createIssueTask({ ...taskPayload, path_policy: { allowed_paths: ['src/', 'src/**'], protected_paths: ['tests/**'] } })).resolves.toMatchObject({ replayed: true })
  })

  it.each([
    { goal: 'Other' }, { workdir: '.' }, { verification_command: 'other' }, { source: null }, { source: {} },
    { source: taskSource({ preparation_id: secondId }) }, { source: taskSource({ repo_path: '/other' }) },
    { source: taskSource({ base_sha: 'c'.repeat(40) }) }, { source: taskSource({ issue: issue({ source_id: '' }) }) },
    ...Object.keys(execution).map((key) => ({ execution: { ...execution, [key]: typeof execution[key] === 'boolean' ? !execution[key] : typeof execution[key] === 'number' ? execution[key] + 1 : 'other' } })),
    { path_policy: null }, { path_policy: { allowed_paths: [], protected_paths: ['tests/**'] } },
    { path_policy: { allowed_paths: ['src/**'], protected_paths: [] } },
  ])('rejects task identity/config mismatches without retry', async (changes) => {
    respond({ task: issueTask(changes), replayed: true }, 200)
    await expect(createIssueTask(taskPayload)).rejects.toMatchObject({ status: 200, message: expect.stringContaining('提交结果未确认') })
    expect(fetch).toHaveBeenCalledTimes(1)
  })

  it.each([
    { workdir: null }, { workdir: '../x' }, { workdir: '/tmp' }, { workdir: '.git' },
    { goal: '' }, { execution: null }, { execution: { ...execution, memory_mb: '2048' } },
    { path_policy: { allowed_paths: ['../secret'], protected_paths: [] } }, { preparation_id: '' },
  ])('rejects invalid task submissions locally', async (changes) => {
    await expect(createIssueTask({ ...taskPayload, ...changes })).rejects.toMatchObject({ status: 422 })
    expect(fetch).not.toHaveBeenCalled()
  })
})
