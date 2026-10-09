import { ApiError } from './api'
import { validPublicationSubmission } from './publications'

const pendingKey = (id) => `patchloop:pending-publication:${id}`
const formKey = (id) => `patchloop:publication-form:${id}`

export function readPendingPublication(id) {
  try {
    const text = sessionStorage.getItem(pendingKey(id))
    if (text === null) return { request: null, error: '' }
    const request = JSON.parse(text)
    if (!validPublicationSubmission(request)) throw new Error()
    return { request, error: '' }
  } catch {
    return { request: null, error: '无法读取待确认的发布请求，请恢复浏览器存储后刷新，并核对已有发布记录。' }
  }
}

export function savePendingPublication(id, request) {
  if (!validPublicationSubmission(request)) throw new ApiError('发布请求无效，请重新检查方案。', 422)
  const value = JSON.stringify(request)
  try {
    sessionStorage.setItem(pendingKey(id), value)
    if (sessionStorage.getItem(pendingKey(id)) !== value) throw new Error()
  } catch {
    throw new ApiError('浏览器无法保存发布请求，本次没有发送。请恢复浏览器存储后再试。')
  }
}

export function clearPendingPublication(id, requestId) {
  const saved = readPendingPublication(id)
  if (saved.request?.request_id !== requestId) return
  try { sessionStorage.removeItem(pendingKey(id)) } catch { /* Server records still allow reconciliation. */ }
}

export function readPublicationForm(id, branch = '') {
  try {
    const value = JSON.parse(sessionStorage.getItem(formKey(id)))
    if (value && ['base_branch', 'title', 'body'].every((key) => typeof value[key] === 'string')
      && typeof value.draft === 'boolean' && typeof value.initialized === 'boolean') return value
  } catch { /* A draft can still be edited without storage. Publishing cannot. */ }
  return { base_branch: branch, title: '', body: '', draft: true, initialized: false }
}

export function savePublicationForm(id, value) {
  try { sessionStorage.setItem(formKey(id), JSON.stringify(value)) } catch { /* Retain the on-screen draft. */ }
}
