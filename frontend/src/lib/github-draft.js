import { defaultForm } from './task'

const storageKey = 'patchloop:github-task-draft:v1'
let memory = null
let memoryOnly = false
const uuid = (value) => typeof value === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value)

export function newGithubDraft() {
  return { version: 1, url: '', ref: '', sourceId: null, preparationId: null, taskId: null,
    form: { ...defaultForm, repo_path: 'prepared-repository', image: '', workdir: '.' }, pending: null, confirmed: null }
}

export function saveGithubDraft(value) {
  memory = value
  try {
    const serialized = JSON.stringify(value)
    sessionStorage.setItem(storageKey, serialized)
    if (sessionStorage.getItem(storageKey) !== serialized) throw new Error('request was not persisted')
    memoryOnly = false
    return true
  } catch {
    memoryOnly = true
    return false
  }
}

export function readGithubDraft() {
  if (memoryOnly && memory) return memory
  let stored
  try { stored = sessionStorage.getItem(storageKey) } catch { return memory || newGithubDraft() }
  if (!stored) return memory || newGithubDraft()
  try {
    const draft = JSON.parse(stored)
    if (draft?.version !== 1 || typeof draft.url !== 'string' || typeof draft.ref !== 'string'
        || !['sourceId', 'preparationId', 'taskId'].every((key) => draft[key] === null || uuid(draft[key]))
        || !draft.form || !Object.keys(newGithubDraft().form).every((key) => typeof draft.form[key] === typeof newGithubDraft().form[key])) throw new Error('invalid draft')
    if (draft.pending !== null && (!draft.pending || !['issue', 'repository', 'task'].includes(draft.pending.stage)
        || !uuid(draft.pending.body?.request_id))) throw new Error('invalid request')
    if (draft.confirmed != null && (!uuid(draft.confirmed.requestId)
        || !['issue', 'repository', 'task'].includes(draft.confirmed.stage))) throw new Error('invalid confirmation')
    return draft
  } catch {
    return { ...newGithubDraft(), storageError: '无法恢复此前的流程，请先检查任务列表，再开始新的导入。' }
  }
}
