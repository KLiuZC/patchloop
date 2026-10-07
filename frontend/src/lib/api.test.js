import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError, artifactUrl, createTask, getArtifact, getTask, listTasks } from './api.js'

const taskId = '01326ef0-b1df-446a-a119-0e39a7aba89b'
const summary = {
  task_id: taskId,
  repo_path: '/workspace/project',
  workdir: '.',
  goal: '修复边界情况',
  status: 'completed',
  validation_status: 'failed',
  created_at: '2026-10-07T00:00:00.000000Z',
  updated_at: '2026-10-07T00:01:00.000000Z',
  started_at: null,
  finished_at: null,
  error: '',
  cleanup_pending: false,
}
const task = {
  ...summary,
  base_sha: 'a'.repeat(40),
  verification_command: 'python -m pytest -q',
  execution: null,
  artifacts_dir: null,
  result: { final_message: 'All tests passed!', validation_status: 'failed' },
}
const payload = {
  repo_path: '/workspace/project',
  goal: '修复边界情况',
  verification_command: 'python -m pytest -q',
}

function respond(data, status = 200) {
  fetch.mockResolvedValueOnce(new Response(JSON.stringify(data), {
    status, headers: { 'Content-Type': 'application/json' },
  }))
}

beforeEach(() => vi.stubGlobal('fetch', vi.fn()))
afterEach(() => vi.unstubAllGlobals())

describe('task API contract', () => {
  it('uses the fixed page size, forwards filters and cursor, and preserves summary fields', async () => {
    const signal = new AbortController().signal
    const cursor = '含空格 + /?=&'
    const page = { items: [summary], next_cursor: cursor }
    respond(page)
    expect(await listTasks({ status: 'completed', cursor, signal })).toEqual(page)
    const [url, options] = fetch.mock.calls[0]
    const parsed = new URL(url, 'http://localhost')
    expect(parsed.pathname).toBe('/api/tasks')
    expect([...parsed.searchParams.entries()]).toEqual([
      ['limit', '20'], ['status', 'completed'], ['cursor', cursor],
    ])
    expect(options).toMatchObject({ method: 'GET', signal })
  })

  it('omits absent filters and accepts an empty final page', async () => {
    respond({ items: [], next_cursor: null })
    expect(await listTasks()).toEqual({ items: [], next_cursor: null })
    expect(fetch).toHaveBeenCalledWith('/api/tasks?limit=20', expect.objectContaining({ method: 'GET' }))
  })

  it('encodes the task id and keeps execution and verification statuses independent', async () => {
    const signal = new AbortController().signal
    respond(task)
    const result = await getTask('id/with ?#中文', { signal })
    expect(fetch).toHaveBeenCalledWith('/api/tasks/id%2Fwith%20%3F%23%E4%B8%AD%E6%96%87',
      expect.objectContaining({ method: 'GET', signal }))
    expect(result).toEqual(task)
    expect(result.status).toBe('completed')
    expect(result.validation_status).toBe('failed')
  })

  it('posts the exact payload once and returns the validated created record', async () => {
    respond({ ...task, status: 'queued', validation_status: 'not_run', result: null }, 201)
    expect(await createTask(payload)).toMatchObject({ status: 'queued', validation_status: 'not_run' })
    expect(fetch).toHaveBeenCalledTimes(1)
    expect(fetch).toHaveBeenCalledWith('/api/tasks', expect.objectContaining({
      method: 'POST', body: JSON.stringify(payload), headers: { 'Content-Type': 'application/json' },
    }))
  })

  it.each([
    null,
    [],
    { items: null, next_cursor: null },
    { items: [], next_cursor: {} },
    { items: [], next_cursor: '' },
    { items: [] },
    { items: [null], next_cursor: null },
    { items: [{ ...summary, task_id: 123 }], next_cursor: null },
    { items: [{ ...summary, status: 'passed' }], next_cursor: null },
    { items: [{ ...summary, validation_status: 'completed' }], next_cursor: null },
    { items: [{ ...summary, goal: {} }], next_cursor: null },
    { items: [{ ...summary, created_at: 'yesterday' }], next_cursor: null },
    { items: [{ ...summary, finished_at: [] }], next_cursor: null },
    { items: [{ ...summary, cleanup_pending: 'false' }], next_cursor: null },
  ])('rejects an invalid page: %j', async (page) => {
    respond(page)
    await expect(listTasks()).rejects.toMatchObject({ name: 'ApiError', status: 200, message: expect.stringContaining('格式') })
  })

  it.each([
    { ...task, validation_status: undefined },
    { ...task, verification_command: {} },
    { ...task, base_sha: null },
    { ...task, result: [] },
    { ...task, execution: 'docker' },
    { ...task, artifacts_dir: {} },
  ])('rejects malformed task details: %j', async (record) => {
    respond(record)
    await expect(getTask(taskId)).rejects.toBeInstanceOf(ApiError)
  })

  it('handles non-JSON successful responses as contract failures', async () => {
    fetch.mockResolvedValueOnce(new Response('<html>proxy page</html>'))
    await expect(listTasks()).rejects.toMatchObject({ status: 200, message: expect.stringContaining('格式') })
  })

  it('warns against resubmission if a successful POST response is malformed', async () => {
    respond({ task_id: taskId }, 201)
    await expect(createTask(payload)).rejects.toMatchObject({
      status: 201, message: expect.stringContaining('提交结果未确认'),
    })
    expect(fetch).toHaveBeenCalledTimes(1)
  })
})

describe('nested task response fields', () => {
  it.each([
    { execution: {}, result: {} },
    { execution: null, result: null },
    { execution: { model: null, image: null }, result: { validation: {} } },
    { execution: {}, result: { validation: { baseline: {}, candidate: {} } } },
    { execution: {}, result: { validation: { baseline: null, candidate: null } } },
    { execution: {}, result: { cost: null, seconds: null, turns: null, prompt_tokens: null, cached_tokens: null, completion_tokens: null, validation: null } },
  ])('preserves sparse records and nullable fields: %j', async (fields) => {
    const record = { ...task, ...fields }
    respond(record)
    await expect(getTask(taskId)).resolves.toEqual(record)
  })

  it('accepts a full result with a baseline error and preserves independent statuses', async () => {
    const record = {
      ...task,
      execution: {
        model: 'test-model', image: 'test-image', provider: 'deepseek', platform: null,
        max_model_calls: 30, memory_mb: 2048, command_timeout_seconds: 60,
        verification_timeout_seconds: 180, budget_reminders: true, explicit_finish: false,
      },
      result: {
        cost: 0.01, seconds: 0, turns: 2, prompt_tokens: 10, cached_tokens: 0, completion_tokens: 5,
        patch: '+change', error: '', stop_reason: 'finish', review_status: 'pending',
        validation: {
          status: 'inconclusive', profile_id: 'test', command: 'test command', base_sha: task.base_sha,
          patch_sha256: null, output: '', error: '', exit_code: null, seconds: 0.5,
          baseline: { status: 'error', exit_code: -9, seconds: 0, output: null, error: 'terminated' },
          candidate: { status: 'passed', exit_code: 0, seconds: 1.5, command: null, output: 'ok', error: null },
        },
      },
    }
    respond(record)
    await expect(getTask(taskId)).resolves.toEqual(record)
  })

  it.each([
    { cost: '0.01' }, { cost: -1 }, { seconds: '1.0' }, { seconds: -1 },
    { turns: {} }, { turns: 1.5 }, { turns: -1 },
    { prompt_tokens: '10' }, { cached_tokens: -1 }, { completion_tokens: 0.5 },
    { patch: {} }, { patch: null }, { error: [] }, { stop_reason: {} }, { review_status: {} },
    { validation: [] }, { validation: 'passed' },
  ])('rejects invalid result values before rendering: %j', async (result) => {
    respond({ ...task, result })
    await expect(getTask(taskId)).rejects.toBeInstanceOf(ApiError)
  })

  it.each([
    { output: {} }, { error: {} }, { command: [] }, { profile_id: {} },
    { base_sha: {} }, { patch_sha256: {} }, { exit_code: 0.5 }, { exit_code: '0' },
    { seconds: -1 }, { seconds: '1.0' }, { status: {} }, { status: 'completed' },
    { status: 'error' }, { baseline: [] }, { candidate: false },
    { baseline: { output: {} } }, { baseline: { error: [] } }, { baseline: { exit_code: '1' } },
    { candidate: { output: {} } }, { candidate: { seconds: -1 } }, { candidate: { status: 'error' } },
  ])('rejects malformed validation and nested verification fields: %j', async (validation) => {
    respond({ ...task, result: { validation } })
    await expect(getTask(taskId)).rejects.toBeInstanceOf(ApiError)
  })

  it.each([
    { model: {} }, { image: [] }, { provider: {} }, { platform: {} },
    { max_model_calls: '30' }, { max_model_calls: 1.5 }, { memory_mb: {} },
    { command_timeout_seconds: '60' }, { verification_timeout_seconds: [] },
    { budget_reminders: 'true' }, { explicit_finish: {} },
  ])('rejects malformed execution fields: %j', async (execution) => {
    respond({ ...task, execution })
    await expect(getTask(taskId)).rejects.toBeInstanceOf(ApiError)
  })

  it.each([
    '{"cost":1e400}', '{"seconds":1e400}', '{"turns":1e400}',
    '{"validation":{"seconds":1e400}}', '{"validation":{"candidate":{"seconds":1e400}}}',
  ])('rejects non-finite numbers parsed from valid JSON: %s', async (result) => {
    // JSON.stringify(Infinity) produces null, so supply the actual overflow literal.
    const body = JSON.stringify({ ...task, result: null }).replace('"result":null', `"result":${result}`)
    fetch.mockResolvedValueOnce(new Response(body))
    await expect(getTask(taskId)).rejects.toBeInstanceOf(ApiError)
  })

  it('validates POST responses too and keeps malformed submissions unconfirmed without retrying', async () => {
    respond({ ...task, result: { cost: '0.01' } }, 201)
    await expect(createTask(payload)).rejects.toMatchObject({
      name: 'ApiError', status: 201, message: expect.stringContaining('提交结果未确认'),
    })
    expect(fetch).toHaveBeenCalledTimes(1)
  })
})

describe('errors and cancellation', () => {
  it('exposes Error semantics and HTTP status', () => {
    const error = new ApiError('任务不存在', 404)
    expect(error).toBeInstanceOf(Error)
    expect(error).toMatchObject({ name: 'ApiError', message: '任务不存在', status: 404 })
  })

  it('uses a FastAPI string detail', async () => {
    respond({ detail: 'Task not found' }, 404)
    await expect(getTask(taskId)).rejects.toMatchObject({ status: 404, message: 'Task not found' })
  })

  it('formats validation details without rendering objects or echoed request inputs', async () => {
    respond({ detail: [
      { loc: ['body', 'execution', 'max_model_calls'], msg: 'Input should be greater than 0', input: 'do not echo' },
      { loc: ['body', 'goal'], msg: 'Field required' },
      'Additional error', null, { unexpected: true },
    ] }, 422)
    await expect(createTask(payload)).rejects.toMatchObject({
      status: 422,
      message: 'body.execution.max_model_calls: Input should be greater than 0；body.goal: Field required；Additional error',
    })
    expect(fetch).toHaveBeenCalledTimes(1)
  })

  it.each(['<html>Bad gateway</html>', '{"detail":{"unexpected":true}}', 'null', ''])(
    'has a readable HTTP fallback for %j', async (body) => {
      fetch.mockResolvedValueOnce(new Response(body, { status: 502 }))
      await expect(listTasks()).rejects.toMatchObject({ status: 502, message: '请求失败（HTTP 502）。' })
    },
  )

  it('makes GET network failures readable in Chinese', async () => {
    fetch.mockRejectedValueOnce(new TypeError('Failed to fetch'))
    await expect(listTasks()).rejects.toMatchObject({ status: 0, message: expect.stringContaining('网络连接失败') })
  })

  it('never retries or claims a failed POST was not created', async () => {
    fetch.mockRejectedValueOnce(new TypeError('Failed to fetch'))
    await expect(createTask(payload)).rejects.toMatchObject({
      status: 0,
      message: '网络连接失败。提交结果未确认，任务可能已创建。请先检查任务列表，避免自动重发。',
    })
    expect(fetch).toHaveBeenCalledTimes(1)
  })

  it('also treats a connection lost while reading a POST response as unconfirmed', async () => {
    fetch.mockResolvedValueOnce({ ok: true, status: 201, text: vi.fn().mockRejectedValue(new TypeError('terminated')) })
    await expect(createTask(payload)).rejects.toMatchObject({ message: expect.stringContaining('提交结果未确认') })
    expect(fetch).toHaveBeenCalledTimes(1)
  })

  it.each([
    (signal) => listTasks({ signal }),
    (signal) => getTask(taskId, { signal }),
    (signal) => getArtifact(taskId, 'patch.diff', { signal }),
  ])('propagates aborts unchanged for every cancellable GET', async (request) => {
    const controller = new AbortController()
    const error = new DOMException('Stopped', 'AbortError')
    fetch.mockRejectedValueOnce(error)
    await expect(request(controller.signal)).rejects.toBe(error)
    expect(fetch.mock.calls[0][1].signal).toBe(controller.signal)
  })

  it('preserves custom cancellation reasons', async () => {
    const controller = new AbortController()
    const reason = new Error('Route changed')
    controller.abort(reason)
    fetch.mockRejectedValueOnce(reason)
    await expect(getTask(taskId, { signal: controller.signal })).rejects.toBe(reason)
  })

  it('propagates abort while reading the response body', async () => {
    const error = new DOMException('Stopped', 'AbortError')
    fetch.mockResolvedValueOnce({ ok: true, status: 200, text: vi.fn().mockRejectedValue(error) })
    await expect(listTasks()).rejects.toBe(error)
  })
})

describe('artifacts', () => {
  it('uses one encoded same-origin path for downloads and text requests', async () => {
    const url = artifactUrl('id/with ?', '目录/patch #?.diff')
    expect(url).toBe('/api/tasks/id%2Fwith%20%3F/artifacts/%E7%9B%AE%E5%BD%95%2Fpatch%20%23%3F.diff')
    expect(new URL(url, 'https://app.example').origin).toBe('https://app.example')
    const text = '+<script>alert(1)</script>\n'
    const signal = new AbortController().signal
    fetch.mockResolvedValueOnce(new Response(text))
    expect(await getArtifact('id/with ?', '目录/patch #?.diff', { signal })).toBe(text)
    expect(fetch).toHaveBeenCalledWith(url, expect.objectContaining({ method: 'GET', signal }))
  })

  it('returns null on artifact 404 but preserves an empty artifact as empty text', async () => {
    respond({ detail: 'Artifact not available' }, 404)
    expect(await getArtifact(taskId, 'patch.diff')).toBeNull()
    fetch.mockResolvedValueOnce(new Response(''))
    expect(await getArtifact(taskId, 'patch.diff')).toBe('')
  })

  it('does not hide non-404 artifact failures', async () => {
    respond({ detail: 'Storage unavailable' }, 503)
    await expect(getArtifact(taskId, 'trajectory.jsonl')).rejects.toMatchObject({ status: 503, message: 'Storage unavailable' })
  })
})
