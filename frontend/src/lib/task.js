export const executionStates = {
  queued: { label: '排队中', tone: 'neutral' },
  running: { label: '运行中', tone: 'blue' },
  completed: { label: '执行完成', tone: 'neutral' },
  interrupted: { label: '已中断', tone: 'amber' },
  error: { label: '执行出错', tone: 'red' },
}

export const validationStates = {
  not_run: { label: '尚未验证', tone: 'neutral' },
  passed: { label: '验证通过', tone: 'green' },
  failed: { label: '验证未通过', tone: 'red' },
  inconclusive: { label: '暂无法判定', tone: 'amber' },
  error: { label: '验证出错', tone: 'red' },
}

export function isActive(task) {
  return task?.status === 'running' || (task?.status === 'queued' && task.execution != null)
}

export function formatDate(value) {
  const date = value ? new Date(value) : null
  if (!date || Number.isNaN(date.getTime())) return '—'
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false,
  }).format(date)
}

export function formatDuration(value) {
  if (value == null || !Number.isFinite(value)) return '—'
  if (value < 60) return `${value.toFixed(1)} 秒`
  const rounded = Math.round(value)
  return `${Math.floor(rounded / 60)} 分 ${rounded % 60} 秒`
}

export function shortPath(task) {
  const repo = task.repo_path?.split(/[\\/]/).filter(Boolean).pop() || '仓库'
  return task.workdir && task.workdir !== '.' ? `${repo} / ${task.workdir}` : repo
}

export const defaultForm = {
  repo_path: '', goal: '', verification_command: 'python -m pytest -q',
  provider: 'deepseek', model: 'deepseek-flash', image: 'patchloop-toy',
  max_model_calls: '30', command_timeout_seconds: '60', verification_timeout_seconds: '180',
  memory_mb: '2048', platform: '', budget_reminders: true, explicit_finish: false,
  allowed_paths: '',
  protected_paths: 'tests/**\n**/tests/**\ntesting/**\n**/testing/**\ntest_*.py\n**/test_*.py\n*_test.py\n**/*_test.py\nconftest.py\n**/conftest.py',
}

const pathPatterns = (value = '') => value.split(/\r?\n/).filter((line) => line.length > 0)

export const modelPresets = { deepseek: 'deepseek-flash', glm: 'glm-5.3-flash' }

export function validateTaskForm(form) {
  const errors = {}
  for (const [key, label] of Object.entries({ repo_path: '仓库目录', goal: '修复要求', verification_command: '验证命令', image: 'Docker 镜像' })) {
    if (!form[key].trim()) errors[key] = `请填写${label}`
    else if (form[key].includes('\0')) errors[key] = `${label}不能包含空字符`
  }
  for (const key of ['image', 'model']) {
    if (form[key] && /\s/.test(form[key])) errors[key] = '名称中不能包含空格或换行'
  }
  for (const key of ['allowed_paths', 'protected_paths']) {
    const patterns = pathPatterns(form[key])
    if (patterns.length > 100 || patterns.some((pattern) => !pattern.trim() || pattern.length > 512
      || pattern.includes('\0') || pattern.startsWith('/') || pattern.split('/').some((part) => part === '.' || part === '..'))) {
      errors[key] = '每行填写一个仓库相对路径模式，最多 100 行；不能使用绝对路径或 ..。'
    }
  }
  for (const [key, [min, max]] of Object.entries({
    max_model_calls: [1, 200], command_timeout_seconds: [1, 3600],
    verification_timeout_seconds: [1, 3600], memory_mb: [256, 32768],
  })) {
    const value = Number(form[key])
    if (!Number.isInteger(value) || value < min || value > max) errors[key] = `请输入 ${min}–${max} 之间的整数`
  }
  return errors
}

export function buildTaskPayload(form) {
  // Preserve path and command whitespace: it may be significant to the repository or shell.
  return {
    repo_path: form.repo_path, goal: form.goal, verification_command: form.verification_command,
    path_policy: { allowed_paths: pathPatterns(form.allowed_paths), protected_paths: pathPatterns(form.protected_paths ?? defaultForm.protected_paths) },
    execution: {
      provider: form.provider, model: form.model || null, image: form.image,
      platform: form.platform || null, memory_mb: Number(form.memory_mb),
      max_model_calls: Number(form.max_model_calls), command_timeout_seconds: Number(form.command_timeout_seconds),
      verification_timeout_seconds: Number(form.verification_timeout_seconds),
      budget_reminders: form.budget_reminders, explicit_finish: form.explicit_finish,
    },
  }
}
