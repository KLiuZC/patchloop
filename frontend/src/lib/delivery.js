import { ApiError, request } from './api.js'
import { sameEvidence } from './reviews.js'

const evidenceKeys = ['base_sha', 'patch_sha256', 'validation_sha256', 'task_sha256', 'summary_sha256', 'record_sha256']
const isObject = (value) => value !== null && typeof value === 'object' && !Array.isArray(value)
const isString = (value) => typeof value === 'string'
const isText = (value) => isString(value) && value.trim().length > 0 && !value.includes('\0')
const isBoolean = (value) => typeof value === 'boolean'
const isCount = (value) => Number.isSafeInteger(value) && value >= 0
const isUuid = (value) => isString(value) && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value)
const isSha = (value) => isString(value) && /^(?:[0-9a-f]{40}|[0-9a-f]{64})$/.test(value)
const isSha256 = (value) => isString(value) && /^[0-9a-f]{64}$/.test(value)
const isDate = (value) => isString(value) && Number.isFinite(Date.parse(value))
const nullable = (check) => (value) => value === null || check(value)
const isPaths = (value) => Array.isArray(value) && value.every(isText)
const uncertainDelivery = '交付结果未确认。请先核对结果；如需重试，将沿用原请求。'
const pending = new Map()
const storageKey = (id) => `patchloop:pending-delivery:${id}`

function validEvidence(value) {
  return isObject(value) && Object.keys(value).length === evidenceKeys.length && evidenceKeys.every((key) =>
    (key === 'base_sha' ? isSha : isSha256)(value[key]))
}

function validPolicy(value) {
  const patterns = (paths) => isPaths(paths) && paths.length <= 100 && paths.every((path) =>
    path.length <= 512 && !path.startsWith('/') && !path.split('/').some((part) => part === '.' || part === '..'))
  return isObject(value) && Object.keys(value).length === 2 && patterns(value.allowed_paths) && patterns(value.protected_paths)
}

function validSubmission(value) {
  return isObject(value) && isUuid(value.request_id) && isUuid(value.review_id)
    && isCount(value.review_revision) && value.review_revision > 0
    && validEvidence(value.evidence) && isSha(value.target_head)
    && Object.keys(value).every((key) => ['request_id', 'review_id', 'review_revision', 'evidence', 'target_head'].includes(key))
}

function validRecord(value, taskId) {
  return isObject(value) && isUuid(value.delivery_id) && isUuid(value.task_id) && value.task_id === taskId
    && isUuid(value.request_id) && isUuid(value.review_id)
    && isCount(value.review_revision) && value.review_revision > 0
    && validEvidence(value.evidence) && isSha(value.target_head)
    && isText(value.branch) && isText(value.worktree_path)
    && ['preparing', 'completed', 'failed'].includes(value.status)
    && nullable(isSha)(value.commit_sha) && nullable(isSha)(value.tree_sha)
    && isString(value.error) && isDate(value.created_at) && isDate(value.updated_at)
}

function validState(value, taskId) {
  return isObject(value) && isUuid(value.task_id) && value.task_id === taskId
    && isBoolean(value.can_deliver) && isString(value.reason)
    && nullable(isUuid)(value.review_id) && isCount(value.review_revision)
    && nullable(validEvidence)(value.evidence) && nullable(isSha)(value.target_head)
    && nullable(isText)(value.branch) && nullable(isText)(value.worktree_path)
    && isBoolean(value.dirty) && isPaths(value.changed_paths) && validPolicy(value.policy)
    && nullable((record) => validRecord(record, taskId))(value.delivery)
    && nullable(isBoolean)(value.delivery_intact)
    && (!value.can_deliver || (isUuid(value.review_id) && value.review_revision > 0
      && validEvidence(value.evidence) && isSha(value.target_head) && isText(value.branch) && isText(value.worktree_path)))
}

/** A review has one delivery, even if another browser supplied a different request_id. */
export function matchesDelivery(record, submission, taskId) {
  return Boolean(record && submission && record.task_id === taskId
    && record.review_id === submission.review_id && record.review_revision === submission.review_revision
    && sameEvidence(record.evidence, submission.evidence) && record.target_head === submission.target_head)
}

export function sameDeliveryPlan(a, b) {
  const sameList = (x, y) => Array.isArray(x) && Array.isArray(y)
    && x.length === y.length && x.every((item, index) => item === y[index])
  return Boolean(a && b && ['task_id', 'review_id', 'review_revision', 'target_head', 'branch', 'worktree_path', 'dirty']
    .every((key) => a[key] === b[key]) && sameEvidence(a.evidence, b.evidence)
    && sameList(a.changed_paths, b.changed_paths)
    && sameList(a.policy?.allowed_paths, b.policy?.allowed_paths)
    && sameList(a.policy?.protected_paths, b.policy?.protected_paths))
}

export function acceptsDeliveryReview(review, plan) {
  const latest = review?.latest_review
  return Boolean(plan && review?.status === 'accepted' && latest?.decision === 'accepted'
    && review.task_id === plan.task_id && latest.task_id === plan.task_id
    && latest.review_id === plan.review_id && latest.revision === plan.review_revision
    && review.revision === plan.review_revision
    && sameEvidence(review.evidence, plan.evidence) && sameEvidence(latest.evidence, plan.evidence))
}

/** Unknown backend details remain available separately, without inventing a diagnosis. */
export function deliveryReason(reason) {
  if (!isString(reason) || !reason) return null
  if (/[\u3400-\u9fff]/.test(reason)) return reason
  const text = reason.toLowerCase()
  if (text.includes('head differs') || text.includes('target changed') || text.includes('retargeted')) {
    return '目标 HEAD 已变化，请基于当前版本创建任务并重新审查。'
  }
  if (text.includes('accepted review') || text.includes('approval') || text.includes('review or repair evidence changed')) {
    return '需要最新且依据一致的已接受审查。'
  }
  if (text.includes('already completed')) return '此审查已完成本地交付。'
  if (text.includes('branch or worktree has changed')) return '已交付的分支或工作区已变化，请核查本地文件。'
  if (text.includes('already exists') || text.includes('not owned')) return '交付分支或目录已存在，无法覆盖。'
  return null
}

export function getDelivery(taskId, { signal } = {}) {
  return request(`/api/tasks/${encodeURIComponent(taskId)}/delivery`, {
    signal, validate: (value) => validState(value, taskId),
  })
}

/** No automatic retries: callers retain the exact serialized request until reconciled. */
export function submitDelivery(taskId, payload) {
  const body = JSON.stringify(payload)
  const submitted = JSON.parse(body)
  if (!validSubmission(submitted)) return Promise.reject(new ApiError('交付请求格式无效，请刷新交付计划。', 422))
  return request(`/api/tasks/${encodeURIComponent(taskId)}/delivery`, {
    method: 'POST', body, uncertainMessage: uncertainDelivery,
    validate: (value) => isObject(value) && isBoolean(value.replayed)
      && validRecord(value.delivery, taskId) && matchesDelivery(value.delivery, submitted, taskId),
  })
}

export function readPendingDelivery(taskId) {
  try {
    const stored = JSON.parse(sessionStorage.getItem(storageKey(taskId)))
    if (validSubmission(stored)) return stored
  } catch { /* Storage can be unavailable; preserve the in-memory request. */ }
  const stored = pending.get(taskId)
  return stored ? structuredClone(stored) : null
}

export function savePendingDelivery(taskId, body) {
  if (!validSubmission(body)) throw new ApiError('交付请求格式无效，请刷新交付计划。', 422)
  const serialized = JSON.stringify(body)
  pending.set(taskId, JSON.parse(serialized))
  try { sessionStorage.setItem(storageKey(taskId), serialized) } catch { /* Keep the in-memory copy. */ }
}

export function clearPendingDelivery(taskId, requestId) {
  if (readPendingDelivery(taskId)?.request_id !== requestId) return
  pending.delete(taskId)
  try { sessionStorage.removeItem(storageKey(taskId)) } catch { /* Storage can be unavailable. */ }
}
