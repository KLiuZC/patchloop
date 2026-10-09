import { taskId, taskRecord } from './fixtures'

export const sourceId = 'dd62090e-a204-4ff2-b2ef-7f33fdf633ba'
export const preparationId = 'ea94d49a-d9f2-4c68-9244-4e6016c50b33'
export const issueUrl = 'https://github.com/example/repair-demo/issues/42'
export const repository = { id: 123, full_name: 'example/repair-demo', html_url: 'https://github.com/example/repair-demo', default_branch: 'main', archived: false, visibility: 'public' }
const timestamp = '2026-10-09T07:00:00Z'

export function issue(overrides = {}) {
  return { source_id: sourceId, request_id: '21901851-bde6-419e-817d-580f87468fd1', kind: 'github_issue',
    requested_url: issueUrl, url: issueUrl, api_url: 'https://api.github.com/repos/example/repair-demo/issues/42',
    repository, issue_id: 456, issue_number: 42, title: '版本号无法处理 v 前缀', body: '输入 v1.2.3 会报错。\n期望返回 (1, 2, 3)。',
    state: 'open', issue_created_at: timestamp, issue_updated_at: timestamp, fetched_at: timestamp,
    api_version: '2026-03-10', snapshot_sha256: 'c'.repeat(64), ...overrides }
}

export function preparation(overrides = {}) {
  return { preparation_id: preparationId, request_id: '47683bb7-04d9-49b1-8c53-bc1a0c0e2c2a', source_id: sourceId,
    repository, requested_ref: null, resolved_ref: 'main', base_sha: 'a'.repeat(40), resolved_at: timestamp,
    clone_url: 'https://github.com/example/repair-demo.git', repo_path: '/managed/repositories/demo/repo',
    status: 'ready', created_at: timestamp, updated_at: timestamp, finished_at: timestamp, error: '', error_status: null,
    ...overrides }
}

export function issueTask(payload, source = issue(), prepared = preparation()) {
  return taskRecord({ task_id: taskId, repo_path: prepared.repo_path, workdir: payload.workdir ?? '.', base_sha: prepared.base_sha,
    goal: payload.goal, verification_command: payload.verification_command, execution: payload.execution,
    path_policy: payload.path_policy, source: { kind: 'github_issue', preparation_id: prepared.preparation_id,
      issue: source, repo_path: prepared.repo_path, base_sha: prepared.base_sha,
      resolved_ref: prepared.resolved_ref, resolved_at: prepared.resolved_at } })
}
