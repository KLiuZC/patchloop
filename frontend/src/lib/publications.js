import { ApiError, request } from './api'
import { validRef, validRepository } from './github'
import { sameEvidence } from './reviews'

const object = (v) => v !== null && typeof v === 'object' && !Array.isArray(v)
const text = (v) => typeof v === 'string'
const uuid = (v) => text(v) && /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i.test(v)
const sha = (v) => text(v) && /^[0-9a-f]{40}$/.test(v)
const hash = (v) => text(v) && /^[0-9a-f]{64}$/.test(v)
const positive = (v) => Number.isSafeInteger(v) && v > 0
const date = (v) => text(v) && Number.isFinite(Date.parse(v))
const evidenceKeys = ['base_sha', 'patch_sha256', 'validation_sha256', 'task_sha256', 'summary_sha256', 'record_sha256']
const only = (v, keys) => object(v) && Object.keys(v).every((key) => keys.includes(key))
const api = (id) => `/api/tasks/${encodeURIComponent(id)}/github-delivery`
const uncertain = '发布结果未确认。请核对已有记录，或使用原请求恢复。'

function validEvidence(v) {
  return object(v) && evidenceKeys.every((key) => (key === 'base_sha' ? sha : hash)(v[key]))
}

export function validPreviewInput(v) {
  return only(v, ['base_branch', 'title', 'body', 'draft']) && validRef(v.base_branch)
    && v.base_branch !== 'HEAD' && !v.base_branch.startsWith('refs/')
    && [undefined, true, false].includes(v.draft)
    && ['title', 'body'].every((key) => v[key] == null || (text(v[key]) && v[key].trim()
      && Array.from(v[key]).length <= (key === 'title' ? 256 : 60000)
      && !Array.from(v[key]).some((char) => (char.charCodeAt(0) < 32 || char.charCodeAt(0) === 127)
        && !(key === 'body' && '\n\r\t'.includes(char)))))
}

export function validPublicationSubmission(v) {
  return only(v, ['request_id', 'preview', 'plan_sha256', 'confirmed']) && uuid(v.request_id)
    && validPreviewInput(v.preview) && hash(v.plan_sha256) && v.confirmed === true
}

function inputMatches(plan, input) {
  return validPreviewInput(input) && plan.base_branch === input.base_branch && plan.draft === (input.draft ?? true)
    && ['title', 'body'].every((key) => input[key] == null || plan[key] === input[key])
}

export function matchesPublicationSource(plan, source) {
  return Boolean(source && plan && plan.source_id === source.issue.source_id && plan.preparation_id === source.preparation_id
    && plan.base_sha === source.base_sha && plan.issue_url === source.issue.url
    && plan.repository.id === source.issue.repository.id
    && plan.repository.full_name === source.issue.repository.full_name)
}

function validPlan(v, taskId, source) {
  if (!object(v) || v.version !== 1 || v.task_id !== taskId || !uuid(v.task_id)
    || !['source_id', 'preparation_id', 'review_id', 'delivery_id'].every((key) => uuid(v[key]))
    || !validRepository(v.repository) || !positive(v.review_revision) || !validEvidence(v.evidence)
    || !['base_sha', 'head_sha', 'tree_sha'].every((key) => sha(v[key])) || v.base_sha !== v.evidence.base_sha
    || v.head_branch !== `patchloop/${taskId}/r${v.review_revision}` || v.head_branch === v.base_branch
    || !validPreviewInput({ base_branch: v.base_branch, title: v.title, body: v.body, draft: v.draft })
    || !text(v.title) || !text(v.body) || typeof v.draft !== 'boolean'
    || !Array.isArray(v.changed_paths) || !v.changed_paths.length || !v.changed_paths.every((path) => text(path) && path && !path.includes('\0'))
    || !object(v.verification) || !text(v.verification.command)
    || v.verification.baseline_status !== 'failed' || v.verification.baseline_exit_code !== 1
    || v.verification.candidate_status !== 'passed' || v.verification.candidate_exit_code !== 0) return false
  const prefix = `https://github.com/${v.repository.full_name.toLowerCase()}/issues/`
  return text(v.issue_url) && v.issue_url.startsWith(prefix) && /^[1-9][0-9]*$/.test(v.issue_url.slice(prefix.length))
    && (!source || matchesPublicationSource(v, source))
}

function validPreview(v, id, input, source) {
  return object(v) && v.status === 'preview' && v.publish_enabled === false && v.remote_checks === 'not_checked'
    && date(v.checked_at) && hash(v.plan_sha256) && validPlan(v.plan, id, source) && inputMatches(v.plan, input)
}

const actor = (v) => object(v) && positive(v.id) && text(v.login) && /^[A-Za-z0-9][A-Za-z0-9-]{0,99}$/.test(v.login)

function validRecord(v, id, source) {
  if (!object(v) || v.task_id !== id || !uuid(v.publication_id) || !uuid(v.request_id)
    || !validPublicationSubmission(v.submission) || v.submission.request_id !== v.request_id
    || !validPlan(v.plan, id, source) || !inputMatches(v.plan, v.submission.preview)
    || !hash(v.plan_sha256) || v.plan_sha256 !== v.submission.plan_sha256
    || !['pending', 'blocked', 'uncertain', 'completed'].includes(v.status)
    || !['check', 'push', 'pull_request', 'completed'].includes(v.phase)
    || !['push_attempted', 'branch_pushed', 'pr_attempted'].every((key) => typeof v[key] === 'boolean')
    || ![v.created_at, v.updated_at].every(date) || !text(v.error) || !(v.actor === null || actor(v.actor))) return false
  const pr = v.pull_request
  if (pr !== null && (!object(pr) || !positive(pr.number)
    || pr.url !== `https://github.com/${v.plan.repository.full_name}/pull/${pr.number}`
    || !['open', 'closed'].includes(pr.state) || !sha(pr.base_sha) || pr.head_sha !== v.plan.head_sha)) return false
  if (v.branch_pushed && !v.push_attempted) return false
  if (v.pr_attempted && (!v.branch_pushed || !v.actor)) return false
  if (pr && !v.pr_attempted) return false
  return v.status === 'completed' ? Boolean(pr && pr.base_sha === v.plan.base_sha && v.phase === 'completed' && v.error === '')
    : v.phase !== 'completed'
}

function canonical(value) {
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`
  if (object(value)) return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonical(value[key])}`).join(',')}}`
  return JSON.stringify(value)
}

export async function planHash(plan) {
  if (!globalThis.crypto?.subtle) throw new ApiError('当前浏览器无法核对发布方案，请通过 localhost 或 HTTPS 访问。')
  const bytes = new TextEncoder().encode(canonical(plan))
  const digest = await crypto.subtle.digest('SHA-256', bytes)
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, '0')).join('')
}

async function verifyPlan(value) {
  if (await planHash(value.plan) !== value.plan_sha256) throw new ApiError('发布方案内容与摘要不一致，请刷新后重新核对。', 409)
  return value
}

function frozenInput(payload, valid) {
  const body = JSON.stringify(payload)
  const input = JSON.parse(body)
  if (!valid(input)) throw new ApiError('发布参数无效，请检查分支、标题和正文。', 422)
  return { body, input }
}

export async function previewPublication(id, payload, { signal, source } = {}) {
  const { body, input } = frozenInput(payload, validPreviewInput)
  return verifyPlan(await request(api(id) + '/preview', { method: 'POST', body, signal,
    uncertainMessage: '本次只生成预览，没有发布。', validate: (v) => validPreview(v, id, input, source) }))
}

export async function checkPublication(id, payload, { signal, source } = {}) {
  const { body, input } = frozenInput(payload, validPreviewInput)
  const result = await request(api(id) + '/check', { method: 'POST', body, signal,
    uncertainMessage: '本次只检查远端，没有发布。',
    validate: (v) => object(v) && validPreview(v.preview, id, input, source) && actor(v.actor)
      && v.remote_base_sha === v.preview.plan.base_sha && (v.remote_head_sha === null || sha(v.remote_head_sha))
      && typeof v.can_publish === 'boolean' && text(v.reason) && (!v.can_publish || v.remote_head_sha === null),
  })
  await verifyPlan(result.preview)
  return result
}

export async function getPublications(id, { signal, source } = {}) {
  const result = await request(api(id), { signal, validate: (v) => object(v) && v.task_id === id
    && typeof v.configured === 'boolean' && Array.isArray(v.items) && v.items.every((record) => validRecord(record, id, source))
    && new Set(v.items.map((record) => record.publication_id)).size === v.items.length
    && new Set(v.items.map((record) => record.plan.delivery_id)).size === v.items.length })
  await Promise.all(result.items.map(verifyPlan))
  return result
}

export async function submitPublication(id, payload, { source } = {}) {
  const { body, input } = frozenInput(payload, validPublicationSubmission)
  const result = await request(api(id), { method: 'POST', body, uncertainMessage: uncertain,
    validate: (v) => object(v) && typeof v.replayed === 'boolean' && validRecord(v.publication, id, source)
      && v.publication.plan_sha256 === input.plan_sha256 && inputMatches(v.publication.plan, input.preview),
  })
  try { await verifyPlan(result.publication) }
  catch { throw new ApiError(`发布响应未通过核对。${uncertain}`) }
  return result
}

function older(a, b) {
  const difference = Date.parse(a) - Date.parse(b)
  if (difference) return difference < 0
  const micro = (v) => Number((/\.(\d+)/.exec(v)?.[1] || '').padEnd(6, '0').slice(3, 6))
  return micro(a) < micro(b)
}

/** Preserve observed completed publications and newer recovery checkpoints. */
export function keepLatestPublications(previous, incoming) {
  if (!previous || !incoming || previous.task_id !== incoming.task_id) return incoming
  const records = new Map(previous.items.map((record) => [record.publication_id, record]))
  for (const next of incoming.items) {
    const current = records.get(next.publication_id)
    if (!current || (current.status !== 'completed' && !older(next.updated_at, current.updated_at)
      && !(next.updated_at === current.updated_at && current.status !== 'pending' && next.status === 'pending'))) records.set(next.publication_id, next)
  }
  return { ...incoming, items: [...records.values()].sort((a, b) => Date.parse(a.created_at) - Date.parse(b.created_at)) }
}

export function publicationMatchesApproval(plan, review, delivery) {
  const record = delivery?.delivery
  return Boolean(plan && review?.status === 'accepted' && review.can_accept && review.latest_review?.decision === 'accepted'
    && review.task_id === plan.task_id && review.revision === plan.review_revision
    && review.latest_review.review_id === plan.review_id && sameEvidence(review.evidence, plan.evidence)
    && sameEvidence(review.latest_review.evidence, plan.evidence) && record?.status === 'completed'
    && record.task_id === plan.task_id && record.delivery_id === plan.delivery_id
    && record.review_id === plan.review_id && record.review_revision === plan.review_revision
    && record.commit_sha === plan.head_sha && record.tree_sha === plan.tree_sha
    && record.branch === plan.head_branch && record.target_head === plan.base_sha
    && sameEvidence(record.evidence, plan.evidence) && delivery.delivery_intact !== false)
}

export function publicationMessage(error) {
  const raw = typeof error === 'string' ? error : error?.message || ''
  if (/[\u3400-\u9fff]/.test(raw)) return raw
  if (error?.status === 404) return '发布接口暂不可用，请确认任务存在并重启最新版本的后端。'
  if (raw.includes('configure PATCHLOOP_GITHUB_TOKEN')) return '后端尚未配置 GitHub 发布凭证，配置后请重启后端。'
  if (raw.includes('base differs') || raw.includes('base moved')) return '远端目标分支已变化，需要基于当前版本重新修复、验证和审查。'
  if (raw.includes('outcome is still unknown')) return 'PR 创建结果仍未确认。本次只查询了 GitHub，没有重复创建。'
  if (raw.includes('account changed')) return '发布账号已变化，请使用原账号核对已有发布。'
  if (raw.includes('write permission')) return '远端仓库身份或写权限不符合要求，请核对目标仓库和发布账号。'
  if (raw.includes('already exists') || raw.includes('already uses') || raw.includes('already has')) return '该分支或发布方案已有记录，请先核对结果。'
  if (raw.includes('publication is running')) return '发布正在进行，请先刷新发布状态。'
  if (raw.includes('accepted review') || raw.includes('approval') || raw.includes('approved')) return '批准或修复依据已变化，请重新核对审查和本地交付。'
  if (raw.includes('HTTP 401') || raw.includes('HTTP 403')) return 'GitHub 拒绝了请求，请检查后端凭证和仓库权限。'
  return '发布暂未完成，请核对已有结果和接口详情。'
}
