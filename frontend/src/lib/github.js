import { ApiError, request, validTask } from './api.js'

const isObject = (value) => value !== null && typeof value === 'object' && !Array.isArray(value)
const isString = (value) => typeof value === 'string'
const isText = (value) => isString(value) && value.trim().length > 0 && !value.includes('\0')
const isBoolean = (value) => typeof value === 'boolean'
const hasControl = (value) => [...value].some((char) => char.charCodeAt(0) < 32 || char.charCodeAt(0) === 127)
const isId = (value) => Number.isSafeInteger(value) && value > 0
const isUuid = (value) => isString(value) && /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i.test(value)
const sameUuid = (a, b) => isUuid(a) && isUuid(b) && a.toLowerCase() === b.toLowerCase()
const isSha = (value) => isString(value) && /^[0-9a-f]{40}$/.test(value)
const isSha256 = (value) => isString(value) && /^[0-9a-f]{64}$/.test(value)
const nullable = (check) => (value) => value === null || check(value)
const ownerPattern = '[A-Za-z0-9][A-Za-z0-9-]{0,99}'
const repoPattern = '[A-Za-z0-9_.-]{1,100}'
const repositoryPattern = new RegExp(`^(${ownerPattern})/(${repoPattern})$`)
const issuePattern = new RegExp(`^/(${ownerPattern})/(${repoPattern})/issues/([0-9]{1,19})/?$`)
const uncertainImport = '导入结果未确认，Issue 可能已保存。请保留相同 request_id 和 URL，核对结果后再显式重试。'
const uncertainPreparation = '仓库准备结果未确认。请先恢复或查询准备状态；如需显式重试，请保留相同 request_id 和请求内容。'
const uncertainTask = '提交结果未确认，任务可能已创建。请先检查任务列表；如需显式重试，请保留相同 request_id 和请求内容。'

function isDate(value) {
  if (!isString(value)) return false
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(?:Z|[+-](\d{2}):(\d{2}))$/.exec(value)
  if (!match || !Number.isFinite(Date.parse(value))) return false
  const [, year, month, day, hour, minute, second, offsetHour = '0', offsetMinute = '0'] = match
  const days = new Date(`${year}-${month}-01T00:00:00Z`)
  days.setUTCMonth(days.getUTCMonth() + 1)
  days.setUTCDate(0)
  return +day >= 1 && +day <= days.getUTCDate() && +hour < 24 && +minute < 60 && +second < 60
    && +offsetHour < 24 && +offsetMinute < 60
}

// Check the original authority/path before URL parsing can normalize unsafe input.
function safeUrl(value, host) {
  if (!isString(value) || value.length > 4096 || hasControl(value) || /[\s\\]/u.test(value)) return null
  const match = /^https:\/\/([^/?#]+)(\/[^?#]*)(\?[^#]*)?(#.*)?$/i.exec(value)
  if (!match || match[1].toLowerCase() !== host) return null
  try { return { path: decodeURIComponent(match[2]), suffix: (match[3] ?? '') + (match[4] ?? '') } }
  catch { return null }
}

function issueUrl(value) {
  if (!isString(value) || value.length > 4096 || hasControl(value) || value.includes('\\')) return null
  const link = safeUrl(isString(value) ? value.trim() : value, 'github.com')
  const match = link && issuePattern.exec(link.path)
  if (!match || ['.', '..'].includes(match[2])) return null
  const number = Number(match[3])
  if (!isId(number)) return null
  return `https://github.com/${match[1].toLowerCase()}/${match[2].toLowerCase()}/issues/${number}`
}

function sameLink(value, host, path) {
  const link = safeUrl(value, host)
  return link !== null && link.suffix === '' && link.path.replace(/\/$/, '').toLowerCase() === path.toLowerCase()
}

function validRepository(value) {
  if (!isObject(value) || !isId(value.id) || !isString(value.full_name)) return false
  const match = repositoryPattern.exec(value.full_name)
  return match !== null && !['.', '..'].includes(match[2])
    && sameLink(value.html_url, 'github.com', `/${value.full_name}`)
    && nullable((branch) => isText(branch) && branch.length <= 1024)(value.default_branch)
    && isBoolean(value.archived) && value.visibility === 'public'
}

function validRef(value) {
  return isText(value) && value.length <= 200 && !hasControl(value) && !/^[-/]|[/.]$/.test(value)
    && !/\.\.|\/\/|\s|[~^:?*[\\]|@\{/u.test(value) && value !== '@'
    && value.split('/').every((part) => !part.startsWith('.') && !part.endsWith('.lock'))
}

/** Frozen IssueSource, including safe links and consistent repository/issue identity. */
export function validIssue(value) {
  if (!isObject(value) || !isUuid(value.source_id) || !isUuid(value.request_id)
    || value.kind !== 'github_issue' || !validRepository(value.repository)
    || !isId(value.issue_id) || !isId(value.issue_number)) return false
  const name = value.repository.full_name.toLowerCase()
  const expected = `https://github.com/${name}/issues/${value.issue_number}`
  return value.requested_url === expected && value.url === expected
    && sameLink(value.api_url, 'api.github.com', `/repos/${name}/issues/${value.issue_number}`)
    && isString(value.title) && value.title.length > 0 && value.title.length <= 1024
    && isString(value.body) && value.body.length <= 200000 && ['open', 'closed'].includes(value.state)
    && ['issue_created_at', 'issue_updated_at', 'fetched_at'].every((key) => isDate(value[key]))
    && isText(value.api_version) && isSha256(value.snapshot_sha256)
}

/** RepositoryPreparation record; GET's checkout state is validated separately. */
export function validPreparation(value) {
  if (!isObject(value) || !['preparation_id', 'request_id', 'source_id'].every((key) => isUuid(value[key]))
    || !validRepository(value.repository) || !nullable(validRef)(value.requested_ref)
    || !nullable(validRef)(value.resolved_ref) || !nullable(isSha)(value.base_sha)
    || !nullable(isDate)(value.resolved_at) || !nullable(isDate)(value.finished_at)
    || !['created_at', 'updated_at'].every((key) => isDate(value[key]))
    || !sameLink(value.clone_url, 'github.com', `/${value.repository.full_name}.git`)
    || !isText(value.repo_path) || !isString(value.error)
    || !nullable((status) => Number.isInteger(status) && status >= 400 && status <= 599)(value.error_status)) return false
  const resolved = ['resolved_ref', 'base_sha', 'resolved_at'].every((key) => value[key] !== null)
  const unresolved = ['resolved_ref', 'base_sha', 'resolved_at'].every((key) => value[key] === null)
  if ((!resolved && !unresolved) || (resolved && value.requested_ref !== null && value.resolved_ref !== value.requested_ref)) return false
  if (value.status === 'ready') return resolved && isDate(value.finished_at) && value.error === '' && value.error_status === null
  if (value.status === 'failed') return isText(value.error) && isDate(value.finished_at)
  return value.status === 'preparing' && value.finished_at === null && value.error === '' && value.error_status === null
}

/** Validate a present TaskSource. Legacy tasks may omit source or have source:null. */
export function validTaskSource(source) {
  return isObject(source) && source.kind === 'github_issue' && isUuid(source.preparation_id)
    && validIssue(source.issue) && isText(source.repo_path) && isSha(source.base_sha)
    && validRef(source.resolved_ref) && isDate(source.resolved_at)
}

function frozenPayload(payload, check) {
  let body
  let submitted
  try { body = JSON.stringify(payload); submitted = JSON.parse(body) } catch { /* Invalid local JSON. */ }
  if (!check(submitted)) throw new ApiError('请求内容无效，请检查输入和冻结的请求标识。', 422)
  return { body, submitted }
}

const onlyKeys = (value, keys) => isObject(value) && Object.keys(value).every((key) => keys.includes(key))
const validResult = (value, key, check) => isObject(value) && isBoolean(value.replayed) && check(value[key])

/** Send once; the caller owns recovery and explicit retries of this frozen payload. */
export async function importIssue(payload) {
  const { body, submitted } = frozenPayload(payload, (value) => onlyKeys(value, ['request_id', 'url'])
    && isUuid(value.request_id) && issueUrl(value.url) !== null)
  return request('/api/sources/github/issues', {
    method: 'POST', body, uncertainMessage: uncertainImport,
    validate: (value) => validResult(value, 'source', validIssue)
      && sameUuid(value.source.request_id, submitted.request_id) && value.source.requested_url === issueUrl(submitted.url),
  })
}

export function getIssue(id, { signal } = {}) {
  return request(`/api/sources/github/issues/${encodeURIComponent(id)}`, {
    signal, validate: (value) => validIssue(value) && sameUuid(value.source_id, id),
  })
}

export async function prepareRepository(payload) {
  const { body, submitted } = frozenPayload(payload, (value) => onlyKeys(value, ['request_id', 'source_id', 'ref'])
    && isUuid(value.request_id) && isUuid(value.source_id) && (value.ref === undefined || nullable(validRef)(value.ref)))
  return request('/api/repositories/prepare', {
    method: 'POST', body, uncertainMessage: uncertainPreparation,
    validate: (value) => validResult(value, 'preparation', validPreparation)
      && sameUuid(value.preparation.request_id, submitted.request_id)
      && sameUuid(value.preparation.source_id, submitted.source_id)
      && value.preparation.requested_ref === (submitted.ref ?? null),
  })
}

export function getPreparation(id, { signal } = {}) {
  return request(`/api/repositories/${encodeURIComponent(id)}`, {
    signal,
    validate: (value) => isObject(value) && validPreparation(value.preparation)
      && sameUuid(value.preparation.preparation_id, id) && isString(value.reason)
      && (value.preparation.status === 'ready' ? isBoolean(value.checkout_intact) : value.checkout_intact === null),
  })
}

const executionDefaults = {
  provider: 'deepseek', platform: null, memory_mb: 2048, max_model_calls: 30,
  command_timeout_seconds: 60, verification_timeout_seconds: 180, budget_reminders: true, explicit_finish: false,
}
const defaultProtectedPaths = [
  'tests/**', '**/tests/**', 'testing/**', '**/testing/**', 'test_*.py', '**/test_*.py',
  '*_test.py', '**/*_test.py', 'conftest.py', '**/conftest.py',
]

function normalizedExecution(value) {
  if (!onlyKeys(value, ['image', 'model', ...Object.keys(executionDefaults)])) return null
  const config = { ...executionDefaults, ...value }
  config.model ??= { deepseek: 'deepseek-flash', glm: 'glm-5.3-flash' }[config.provider]
  const identifier = (text, max) => isText(text) && text.length <= max && !/\s/u.test(text)
  if (!identifier(config.image, 512) || !identifier(config.model, 256)
    || !['deepseek', 'glm'].includes(config.provider) || ![null, 'linux/amd64', 'linux/arm64'].includes(config.platform)
    || !['budget_reminders', 'explicit_finish'].every((key) => isBoolean(config[key]))) return null
  const ranges = { memory_mb: [256, 32768], max_model_calls: [1, 200], command_timeout_seconds: [1, 3600], verification_timeout_seconds: [1, 3600] }
  return Object.entries(ranges).every(([key, [min, max]]) => Number.isInteger(config[key]) && config[key] >= min && config[key] <= max) ? config : null
}

function normalizedPolicy(value = {}) {
  if (!onlyKeys(value, ['allowed_paths', 'protected_paths'])) return null
  const policy = { allowed_paths: [], protected_paths: defaultProtectedPaths, ...value }
  for (const key of Object.keys(policy)) {
    const paths = policy[key]
    if (!Array.isArray(paths) || paths.length > 100 || !paths.every((path) => isText(path) && path.length <= 512
      && !path.startsWith('/') && !path.split('/').some((part) => part === '.' || part === '..'))) return null
    policy[key] = [...new Set(paths.map((path) => path.endsWith('/') ? `${path}**` : path))]
  }
  return policy
}

function validWorkdir(value) {
  return value === '.' || (isText(value) && value.length <= 4096 && !value.startsWith('/') && !value.includes('\\')
    && value.split('/').every((part) => part !== '' && part !== '.' && part !== '..' && part.toLowerCase() !== '.git'))
}

const sameFields = (a, b) => isObject(a) && Object.entries(b).every(([key, value]) => a[key] === value)
const samePaths = (a, b) => Array.isArray(a) && a.length === b.length && a.every((path, index) => path === b[index])

export async function createIssueTask(payload) {
  const { body, submitted } = frozenPayload(payload, (value) => onlyKeys(value, [
    'request_id', 'preparation_id', 'goal', 'verification_command', 'execution', 'path_policy', 'workdir',
  ]) && isUuid(value.request_id) && isUuid(value.preparation_id)
    && ['goal', 'verification_command'].every((key) => isText(value[key]) && value[key].length <= 20000)
    && validWorkdir(value.workdir === undefined ? '.' : value.workdir) && normalizedExecution(value.execution) !== null
    && normalizedPolicy(value.path_policy) !== null)
  const execution = normalizedExecution(submitted.execution)
  const policy = normalizedPolicy(submitted.path_policy)
  return request('/api/tasks/from-issue', {
    method: 'POST', body, uncertainMessage: uncertainTask,
    validate: (value) => {
      if (!validResult(value, 'task', (task) => validTask(task, true) && isUuid(task.task_id)
        && validTaskSource(task.source))) return false
      const { task } = value
      return sameUuid(task.source.preparation_id, submitted.preparation_id)
        && task.source.repo_path === task.repo_path && task.source.base_sha === task.base_sha
        && task.goal === submitted.goal && task.workdir === (submitted.workdir ?? '.')
        && task.verification_command === submitted.verification_command && sameFields(task.execution, execution)
        && isObject(task.path_policy) && Object.entries(policy).every(([key, paths]) => samePaths(task.path_policy[key], paths))
    },
  })
}
