const taskStatuses = new Set(['queued', 'running', 'completed', 'interrupted', 'error'])
const validationStatuses = new Set(['not_run', 'passed', 'failed', 'inconclusive'])
const isObject = (value) => value !== null && typeof value === 'object' && !Array.isArray(value)
const isString = (value) => typeof value === 'string'
const isDate = (value) => isString(value) && Number.isFinite(Date.parse(value))
const isNonnegative = (value) => Number.isFinite(value) && value >= 0
const isCount = (value) => Number.isInteger(value) && value >= 0
const nullable = (check) => (value) => value === null || check(value)
const uncertainSubmission = '提交结果未确认，任务可能已创建。请先检查任务列表，避免自动重发。'

export class ApiError extends Error {
  constructor(message, status = 0) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

function invalidResponse(status, method) {
  return new ApiError(`服务器返回的数据格式无效。${method === 'POST' ? uncertainSubmission : ''}`, status)
}

function detailMessage(detail) {
  if (isString(detail)) return detail.trim()
  if (!Array.isArray(detail)) return ''
  return detail.map((entry) => {
    if (isString(entry)) return entry.trim()
    if (!isObject(entry) || !isString(entry.msg)) return ''
    const location = Array.isArray(entry.loc)
      ? entry.loc.filter((part) => isString(part) || typeof part === 'number').join('.')
      : ''
    return location ? `${location}: ${entry.msg}` : entry.msg
  }).filter(Boolean).join('；')
}

async function request(url, { method = 'GET', signal, body, artifact = false, validate } = {}) {
  let response
  let text
  try {
    response = await fetch(url, {
      method,
      signal,
      ...(body === undefined ? {} : { body, headers: { 'Content-Type': 'application/json' } }),
    })
    if (artifact && response.status === 404) return null
    text = await response.text()
  } catch (error) {
    if (error?.name === 'AbortError' || signal?.aborted) throw error
    if (response && !response.ok) {
      throw new ApiError(`请求失败（HTTP ${response.status}）。`, response.status)
    }
    throw new ApiError(method === 'POST'
      ? `网络连接失败。${uncertainSubmission}`
      : '网络连接失败，请检查网络连接后重试。')
  }

  if (!response.ok) {
    let message = ''
    try {
      message = detailMessage(JSON.parse(text)?.detail)
    } catch { /* A proxy may return HTML instead of FastAPI JSON. */ }
    throw new ApiError(message || `请求失败（HTTP ${response.status}）。`, response.status)
  }
  if (artifact) return text

  let data
  try {
    data = JSON.parse(text)
  } catch {
    throw invalidResponse(response.status, method)
  }
  if (!validate(data)) throw invalidResponse(response.status, method)
  return data
}

// Nested records may be sparse; validate known fields only when they are present.
function validFields(record, fields) {
  return isObject(record) && Object.entries(fields).every(([key, check]) =>
    !Object.hasOwn(record, key) || check(record[key]))
}

function validVerification(record, baseline = false) {
  return validFields(record, {
    status: (value) => validationStatuses.has(value) || (baseline && value === 'error'),
    profile_id: nullable(isString),
    command: nullable(isString),
    base_sha: nullable(isString),
    patch_sha256: nullable(isString),
    output: nullable(isString),
    error: nullable(isString),
    exit_code: nullable(Number.isInteger),
    seconds: nullable(isNonnegative),
  })
}

function validValidation(record) {
  return validVerification(record) && validFields(record, {
    baseline: nullable((value) => validVerification(value, true)),
    candidate: nullable((value) => validVerification(value)),
  })
}

function validResult(record) {
  return validFields(record, {
    cost: nullable(isNonnegative),
    seconds: nullable(isNonnegative),
    turns: nullable(isCount),
    prompt_tokens: nullable(isCount),
    cached_tokens: nullable(isCount),
    completion_tokens: nullable(isCount),
    patch: isString,
    error: isString,
    stop_reason: isString,
    review_status: isString,
    validation: nullable(validValidation),
  })
}

function validExecution(record) {
  return validFields(record, {
    model: nullable(isString),
    image: nullable(isString),
    provider: nullable(isString),
    platform: nullable(isString),
    max_model_calls: Number.isInteger,
    memory_mb: Number.isInteger,
    command_timeout_seconds: Number.isInteger,
    verification_timeout_seconds: Number.isInteger,
    budget_reminders: (value) => typeof value === 'boolean',
    explicit_finish: (value) => typeof value === 'boolean',
  })
}

// TaskSummary / TaskRecord from api/models.py. Never infer validation from completion or model text.
function validTask(task, detail = false) {
  if (!isObject(task) || !isString(task.task_id) || !task.task_id.trim()) return false
  if (!taskStatuses.has(task.status) || !validationStatuses.has(task.validation_status)) return false
  if (!['repo_path', 'workdir', 'goal', 'error'].every((key) => isString(task[key]))) return false
  if (!['created_at', 'updated_at'].every((key) => isDate(task[key]))) return false
  if (!['started_at', 'finished_at'].every((key) => nullable(isDate)(task[key]))) return false
  if (typeof task.cleanup_pending !== 'boolean') return false
  if (!detail) return true
  return ['base_sha', 'verification_command'].every((key) => isString(task[key]))
    && nullable(isString)(task.artifacts_dir)
    && nullable(validExecution)(task.execution)
    && nullable(validResult)(task.result)
}

export function listTasks({ status, cursor, signal } = {}) {
  const query = new URLSearchParams({ limit: '20' })
  if (status) query.set('status', status)
  if (cursor) query.set('cursor', cursor)
  return request(`/api/tasks?${query}`, {
    signal,
    validate: (page) => isObject(page)
      && Array.isArray(page.items)
      && page.items.every((task) => validTask(task))
      && (page.next_cursor === null || (isString(page.next_cursor) && page.next_cursor.length > 0)),
  })
}

export function getTask(id, { signal } = {}) {
  return request(`/api/tasks/${encodeURIComponent(id)}`, {
    signal,
    validate: (task) => validTask(task, true),
  })
}

/** A submission is sent once; retrying can create another task. */
export function createTask(payload) {
  return request('/api/tasks', {
    method: 'POST',
    body: JSON.stringify(payload),
    validate: (task) => validTask(task, true),
  })
}

export function artifactUrl(id, name) {
  return `/api/tasks/${encodeURIComponent(id)}/artifacts/${encodeURIComponent(name)}`
}

export function getArtifact(id, name, { signal } = {}) {
  return request(artifactUrl(id, name), { signal, artifact: true })
}
