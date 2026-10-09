import { planHash } from '../lib/publications'
import { taskId } from './fixtures'
import { issueTask } from './github-fixtures'

export const reviewId = '730ac7b8-a3c0-4000-a000-000000000001'
export const deliveryId = '830ac7b8-a3c0-4000-a000-000000000001'
export const requestId = '630ac7b8-a3c0-4000-a000-000000000001'
export const publicationId = '930ac7b8-a3c0-4000-a000-000000000001'
export const timestamp = '2026-10-09T09:00:00.000001Z'
export const evidence = { base_sha: 'a'.repeat(40), patch_sha256: 'b'.repeat(64), validation_sha256: 'c'.repeat(64),
  task_sha256: 'd'.repeat(64), summary_sha256: 'e'.repeat(64), record_sha256: 'f'.repeat(64) }
export const source = issueTask({ goal: '修复版本号', verification_command: 'pytest -q' }).source

export function review(overrides = {}) {
  return { task_id: taskId, status: 'accepted', revision: 1, can_accept: true, can_reject: true,
    reason: '', evidence: { ...evidence }, latest_review: { task_id: taskId, review_id: reviewId,
      revision: 1, decision: 'accepted', evidence: { ...evidence } }, ...overrides }
}

export function delivery(overrides = {}) {
  return { task_id: taskId, delivery_intact: true, delivery: { task_id: taskId,
    delivery_id: deliveryId, review_id: reviewId, review_revision: 1, status: 'completed',
    commit_sha: '1'.repeat(40), tree_sha: '2'.repeat(40), branch: `patchloop/${taskId}/r1`,
    target_head: evidence.base_sha, evidence: { ...evidence } }, ...overrides }
}

export function plan(overrides = {}) {
  return { version: 1, task_id: taskId, source_id: source.issue.source_id,
    preparation_id: source.preparation_id, issue_url: source.issue.url, repository: { ...source.issue.repository },
    base_branch: 'main', base_sha: evidence.base_sha, head_branch: `patchloop/${taskId}/r1`,
    head_sha: '1'.repeat(40), tree_sha: '2'.repeat(40), delivery_id: deliveryId, review_id: reviewId,
    review_revision: 1, evidence: { ...evidence }, changed_paths: ['src/version.py'],
    verification: { command: 'python -m pytest -q', baseline_status: 'failed', baseline_exit_code: 1,
      candidate_status: 'passed', candidate_exit_code: 0 },
    title: '修复 #42：版本号前缀', body: '## 修复内容\n\n处理小写 v 前缀。\n\n## 验证\n\npython -m pytest -q', draft: true,
    ...overrides }
}

export async function previewValue(overrides = {}) {
  const value = plan(overrides)
  return { status: 'preview', publish_enabled: false, remote_checks: 'not_checked', checked_at: timestamp,
    plan: value, plan_sha256: await planHash(value) }
}

export function remoteValue(preview, overrides = {}) {
  return { preview, actor: { id: 77, login: 'publisher' }, remote_base_sha: preview.plan.base_sha,
    remote_head_sha: null, can_publish: true, reason: '', ...overrides }
}

export function submission(preview, overrides = {}) {
  return { request_id: requestId, preview: { base_branch: preview.plan.base_branch, title: preview.plan.title,
    body: preview.plan.body, draft: preview.plan.draft }, plan_sha256: preview.plan_sha256, confirmed: true, ...overrides }
}

export function publication(preview, overrides = {}) {
  return { publication_id: publicationId, task_id: taskId, request_id: requestId,
    submission: submission(preview), plan_sha256: preview.plan_sha256, plan: preview.plan,
    status: 'completed', phase: 'completed', actor: { id: 77, login: 'publisher' },
    push_attempted: true, branch_pushed: true, pr_attempted: true,
    pull_request: { number: 12, url: 'https://github.com/example/repair-demo/pull/12', state: 'open',
      base_sha: preview.plan.base_sha, head_sha: preview.plan.head_sha }, error: '', created_at: timestamp,
    updated_at: '2026-10-09T09:00:01.000001Z', ...overrides }
}
