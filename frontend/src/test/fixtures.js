export const taskId = '72806f28-8d03-4ce6-93fc-dff92e13fce4'
export const secondId = 'a4b7071a-ad29-47d4-9f31-4e3cffb4e001'

export function taskRecord(overrides = {}) {
  return {
    task_id: taskId, repo_path: '/repo/patchloop', workdir: 'examples/toy_repo', base_sha: 'a'.repeat(40),
    goal: '修复版本号前缀', verification_command: 'python -m pytest -q', status: 'queued',
    validation_status: 'not_run', created_at: '2026-10-07T07:00:00Z', updated_at: '2026-10-07T07:00:00Z',
    started_at: null, finished_at: null, artifacts_dir: null, error: '', result: null, cleanup_pending: false,
    execution: { provider: 'deepseek', model: 'deepseek-flash', image: 'patchloop-toy', max_model_calls: 10 },
    ...overrides,
  }
}

export function jsonResponse(data, status = 200) {
  return new Response(JSON.stringify(data), { status, headers: { 'Content-Type': 'application/json' } })
}
