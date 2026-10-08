// Keep an uncertain submission across tab changes and, when available, reloads.
const pending = new Map()
const key = (id) => `patchloop:pending-review:${id}`

export function keepLatestReview(previous, incoming) {
  if (!incoming) return incoming
  if (previous && previous.revision > incoming.revision) return previous
  // Once changed evidence is observed, require an explicit reload to recover
  // the same revision; an older in-flight response must not restore approval.
  if (previous?.revision === incoming.revision && previous.status === 'stale' && incoming.status !== 'stale') return previous
  return incoming
}

function validRequest(value) {
  return value && typeof value === 'object'
    && typeof value.request_id === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value.request_id)
    && Number.isSafeInteger(value.expected_revision) && value.expected_revision >= 0
    && ['accepted', 'rejected'].includes(value.decision)
    && typeof value.comment === 'string' && value.comment.length <= 4000 && !value.comment.includes('\0')
    && value.evidence && typeof value.evidence === 'object'
    && typeof value.evidence.base_sha === 'string' && /^(?:[0-9a-f]{40}|[0-9a-f]{64})$/.test(value.evidence.base_sha)
    && ['patch_sha256', 'validation_sha256', 'task_sha256', 'summary_sha256', 'record_sha256']
      .every((field) => typeof value.evidence[field] === 'string' && /^[0-9a-f]{64}$/.test(value.evidence[field]))
}

export function readPendingReview(id) {
  try {
    const stored = JSON.parse(sessionStorage.getItem(key(id)))
    if (validRequest(stored)) return stored
  } catch { /* Storage may be unavailable; the in-memory copy still supports retries. */ }
  return pending.get(id) || null
}

export function savePendingReview(id, body) {
  pending.set(id, body)
  try { sessionStorage.setItem(key(id), JSON.stringify(body)) } catch { /* Keep the in-memory copy. */ }
}

export function clearPendingReview(id, requestId) {
  if (readPendingReview(id)?.request_id !== requestId) return
  pending.delete(id)
  try { sessionStorage.removeItem(key(id)) } catch { /* Storage may be unavailable. */ }
}

export function reviewReason(state) {
  if (!state) return '正在读取审查状态。'
  if (state.status === 'stale') return '审查依据已变化，原决定不适用于当前资料。请重新查看后提交审查。'
  if (state.can_accept) return ''
  if (state.can_reject) return '现有测试证据不足以接受补丁，仍可提交拒绝意见。'
  const reason = state.reason || ''
  if (reason.includes('has not finished')) return '任务结束后才能审查。'
  if (reason.includes('cleanup')) return '任务环境仍在清理，完成后才能审查。'
  if (reason.includes('empty patch')) return '这次任务没有可审查的补丁。'
  if (reason.includes('no saved repair evidence')) return '这次任务尚无完整的修复记录。'
  return '审查资料缺失或不一致，请检查任务产物。'
}

export function reviewError(error) {
  if (error?.status === 404) return '无法读取审查接口，请确认任务存在且后端已更新。'
  return error?.message || '无法读取审查资料，请重试。'
}
