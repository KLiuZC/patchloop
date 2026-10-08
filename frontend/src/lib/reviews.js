import { ApiError, getArtifact, request } from './api.js'

const evidenceKeys = [
  'base_sha', 'patch_sha256', 'validation_sha256', 'task_sha256', 'summary_sha256', 'record_sha256',
]
const artifacts = [
  ['task.json', 'task_sha256'],
  ['summary.json', 'summary_sha256'],
  ['validation.json', 'validation_sha256'],
  ['patch.diff', 'patch_sha256'],
]
const reviewStatuses = new Set(['pending', 'accepted', 'rejected', 'stale'])
const decisions = new Set(['accepted', 'rejected'])
const validationStatuses = new Set(['not_run', 'passed', 'failed', 'inconclusive'])
const isObject = (value) => value !== null && typeof value === 'object' && !Array.isArray(value)
const isString = (value) => typeof value === 'string'
const isBoolean = (value) => typeof value === 'boolean'
const isCount = (value) => Number.isSafeInteger(value) && value >= 0
const isDate = (value) => isString(value) && Number.isFinite(Date.parse(value))
const isUuid = (value) => isString(value)
  && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value)
const isBaseSha = (value) => isString(value) && /^(?:[0-9a-f]{40}|[0-9a-f]{64})$/.test(value)
const isSha256 = (value) => isString(value) && /^[0-9a-f]{64}$/.test(value)
const nullable = (check) => (value) => value === null || check(value)
const uncertainReview = '审查结果未确认。请先刷新审查状态；如需重试，请保留相同 request_id。'

function validEvidence(value) {
  return isObject(value) && evidenceKeys.every((key) =>
    Object.hasOwn(value, key) && (key === 'base_sha' ? isBaseSha(value[key]) : isSha256(value[key])))
}

/** Compare exactly the six evidence fields; missing fields are never equal evidence. */
export function sameEvidence(a, b) {
  if (a === null || b === null) return a === b
  return isObject(a) && isObject(b) && evidenceKeys.every((key) =>
    Object.hasOwn(a, key) && Object.hasOwn(b, key) && a[key] === b[key])
}

function validReview(value, taskId) {
  return isObject(value)
    && isUuid(value.task_id) && value.task_id === taskId
    && isUuid(value.review_id) && isUuid(value.request_id)
    && isCount(value.revision) && value.revision > 0
    && decisions.has(value.decision) && isString(value.comment)
    && validEvidence(value.evidence) && isDate(value.created_at)
}

function validState(value, taskId) {
  return isObject(value)
    && isUuid(value.task_id) && value.task_id === taskId
    && reviewStatuses.has(value.status) && isCount(value.revision)
    && nullable(validEvidence)(value.evidence)
    && isBoolean(value.can_accept) && isBoolean(value.can_reject) && isString(value.reason)
    && (value.latest_review === null
      ? value.revision === 0
      : validReview(value.latest_review, taskId) && value.latest_review.revision === value.revision)
}

function sameReview(a, b) {
  return ['review_id', 'task_id', 'revision', 'request_id', 'decision', 'comment', 'created_at']
    .every((key) => a[key] === b[key]) && sameEvidence(a.evidence, b.evidence)
}

function validResponse(value, taskId, submitted) {
  if (!isObject(value) || !isBoolean(value.replayed)
    || !validReview(value.review, taskId) || !validState(value.current, taskId)) return false
  const { review, current } = value
  return review.request_id === submitted.request_id
    && review.decision === submitted.decision
    && review.comment === (submitted.comment === undefined ? '' : submitted.comment)
    && sameEvidence(review.evidence, submitted.evidence)
    && isCount(submitted.expected_revision) && review.revision === submitted.expected_revision + 1
    && current.revision >= review.revision
    // A replay may return an older review alongside a newer (or stale) current state.
    && (current.revision > review.revision || sameReview(current.latest_review, review))
}

export function getReview(taskId, { signal } = {}) {
  return request(`/api/tasks/${encodeURIComponent(taskId)}/review`, {
    signal, validate: (value) => validState(value, taskId),
  })
}

export function listReviews(taskId, { signal } = {}) {
  return request(`/api/tasks/${encodeURIComponent(taskId)}/reviews`, {
    signal,
    validate: (value) => isObject(value) && value.task_id === taskId && isUuid(value.task_id)
      && Array.isArray(value.items) && value.items.every((review, index) =>
        validReview(review, taskId) && (index === 0 || review.revision > value.items[index - 1].revision)),
  })
}

/** Send once. The caller owns explicit retries using the same frozen request_id and payload. */
export function submitReview(taskId, payload) {
  const body = JSON.stringify(payload)
  const submitted = JSON.parse(body)
  return request(`/api/tasks/${encodeURIComponent(taskId)}/reviews`, {
    method: 'POST', body, uncertainMessage: uncertainReview,
    validate: (value) => validResponse(value, taskId, submitted),
  })
}

function snapshotConflict() {
  return new ApiError('审查证据或状态已变化，无法确认此快照。请显式刷新后重新审查。', 409)
}

// Only check fields used for safe display and identity; the server owns the canonical record hash.
function validSavedTask(value, taskId, evidence) {
  return isObject(value) && value.task_id === taskId && value.base_sha === evidence.base_sha
    && ['goal', 'repo_path', 'workdir'].every((key) => isString(value[key]))
    && isObject(value.verification)
    && ['id', 'command'].every((key) => isString(value.verification[key]))
}

function validVerification(value, baseline = false) {
  return isObject(value)
    && (validationStatuses.has(value.status) || (baseline && value.status === 'error'))
    && ['profile_id', 'command', 'base_sha', 'output', 'error'].every((key) => isString(value[key]))
    && nullable(Number.isSafeInteger)(value.exit_code)
    && Number.isFinite(value.seconds) && value.seconds >= 0
    && (baseline || nullable(isSha256)(value.patch_sha256))
}

function validSavedValidation(value, task, evidence) {
  return validVerification(value)
    && nullable((report) => validVerification(report, true))(value.baseline)
    && nullable((report) => validVerification(report))(value.candidate)
    && value.base_sha === evidence.base_sha && value.patch_sha256 === evidence.patch_sha256
    && value.profile_id === task.verification.id && value.command === task.verification.command
}

async function digest(text) {
  if (!globalThis.crypto?.subtle) throw new ApiError('当前环境无法核对审查证据的 SHA-256，请在安全上下文中重试。')
  const bytes = new TextEncoder().encode(text)
  const hash = await globalThis.crypto.subtle.digest('SHA-256', bytes)
  return Array.from(new Uint8Array(hash), (byte) => byte.toString(16).padStart(2, '0')).join('')
}

/** Return only evidence checked against both the saved hashes and a second review read. */
export async function loadReviewSnapshot(taskId, { signal } = {}) {
  const state = await getReview(taskId, { signal })
  if (state.evidence === null) return { state, patch: null, validation: null, task: null }

  const contents = await Promise.all(artifacts.map(([name]) => getArtifact(taskId, name, { signal })))
  if (contents.some((text) => text === null)) throw snapshotConflict()
  const hashes = await Promise.all(contents.map(digest))
  if (artifacts.some(([, key], index) => hashes[index] !== state.evidence[key])) throw snapshotConflict()

  let task
  let validation
  try {
    task = JSON.parse(contents[0])
    validation = JSON.parse(contents[2])
  } catch {
    throw snapshotConflict()
  }
  if (!validSavedTask(task, taskId, state.evidence)
    || !validSavedValidation(validation, task, state.evidence)) throw snapshotConflict()

  let current
  try {
    current = await getReview(taskId, { signal })
  } catch (error) {
    // A malformed confirmation cannot certify the evidence already read either.
    if (error instanceof ApiError && error.status >= 200 && error.status < 300) throw snapshotConflict()
    throw error
  }
  if (current.revision !== state.revision || !sameEvidence(current.evidence, state.evidence)
    || current.status !== state.status || current.can_accept !== state.can_accept
    || current.can_reject !== state.can_reject || current.reason !== state.reason
    || (state.latest_review === null
      ? current.latest_review !== null
      : current.latest_review === null || !sameReview(current.latest_review, state.latest_review))) {
    throw snapshotConflict()
  }
  // Hashes use the original UTF-8 text, never reserialized JSON or live task detail.
  return { state: current, patch: contents[3], validation, task }
}
