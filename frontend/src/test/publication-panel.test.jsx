import { webcrypto } from 'node:crypto'
import { TextEncoder } from 'node:util'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, afterEach, expect, it, vi } from 'vitest'
import { QueryClientProvider } from '@tanstack/react-query'
import GitHubPublicationPanel from '../components/GitHubPublicationPanel'
import { createQueryClient } from '../lib/query-client'
import { readPendingPublication, savePendingPublication } from '../lib/publication-draft'
import { jsonResponse, taskId, secondId } from './fixtures'
import { delivery, previewValue, publication, remoteValue, review, source, submission } from './publication-fixtures'

let server
const clients = []
beforeEach(() => {
  vi.stubGlobal('crypto', webcrypto); vi.stubGlobal('TextEncoder', TextEncoder); sessionStorage.clear()
  server = { configured: true, items: [], writes: [], calls: [], actor: { id: 77, login: 'publisher' } }
  vi.stubGlobal('fetch', vi.fn(async (url, options = {}) => {
    server.calls.push([url, options])
    if (options.method !== 'POST') return server.read ? server.read() : jsonResponse({ task_id: taskId, configured: server.configured, items: structuredClone(server.items) })
    const body = JSON.parse(options.body)
    if (url.endsWith('/preview')) return jsonResponse(await previewValue(body))
    if (url.endsWith('/check')) {
      if (server.check) return server.check(body)
      return jsonResponse(remoteValue(await previewValue(body), { actor: server.actor }))
    }
    server.writes.push(body)
    if (server.publish) return server.publish(body)
    const value = publication(await previewValue(body.preview), { request_id: body.request_id, submission: body })
    server.items = [value]
    return jsonResponse({ publication: value, replayed: false }, 201)
  }))
})
afterEach(() => { cleanup(); clients.splice(0).forEach((c) => c.clear()); vi.unstubAllGlobals(); vi.restoreAllMocks() })

function mount(initialReview = review(), initialDelivery = delivery()) {
  const client = createQueryClient()
  client.setDefaultOptions({ queries: { retry: false, gcTime: 0, networkMode: 'always' } }); clients.push(client)
  const tree = (state, local, id = taskId) => <QueryClientProvider client={client}><GitHubPublicationPanel key={id} taskId={id} source={source} reviewState={state} deliveryState={local} /></QueryClientProvider>
  const view = render(tree(initialReview, initialDelivery))
  return { ...view, client, update: (state, local = initialDelivery, id = taskId) => view.rerender(tree(state, local, id)) }
}
const confirmation = () => screen.getByRole('checkbox', { name: /我已核对仓库/ })
const publishButton = () => screen.getByRole('button', { name: '确认并发布 PR' })
async function prepare(user) {
  await waitFor(() => expect(screen.getByRole('button', { name: '生成发布预览' })).toBeEnabled())
  await user.click(screen.getByRole('button', { name: '生成发布预览' }))
  await screen.findByRole('heading', { name: '发布预览' })
}
async function checked(user) {
  await prepare(user)
  await user.click(screen.getByRole('button', { name: '检查远端' }))
  await screen.findByText('远端检查通过')
}

it('renders the full flow, requires explicit confirmation and sends one publication', async () => {
  const user = userEvent.setup(); mount(); await checked(user)
  expect(publishButton()).toBeDisabled()
  expect(server.writes).toHaveLength(0)
  await user.click(confirmation())
  await user.dblClick(publishButton())
  expect(await screen.findByRole('link', { name: /查看 PR #12/ })).toHaveAttribute('href', 'https://github.com/example/repair-demo/pull/12')
  expect(server.writes).toHaveLength(1)
  expect(server.writes[0].confirmed).toBe(true)
  expect(readPendingPublication(taskId).request).toBeNull()
  expect(screen.queryByRole('button', { name: '确认并发布 PR' })).not.toBeInTheDocument()
})

it('invalidates remote checks and confirmation when editable content changes', async () => {
  const user = userEvent.setup(); mount(); await checked(user); await user.click(confirmation())
  await user.type(screen.getByRole('textbox', { name: 'PR 标题' }), ' 修改')
  expect(screen.queryByText('远端检查通过')).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: '确认并发布 PR' })).not.toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: '更新发布预览' }))
  await screen.findByRole('heading', { name: '发布预览' })
  expect(confirmation()).not.toBeChecked()
  expect(publishButton()).toBeDisabled()
  expect(server.writes).toHaveLength(0)
})

it('allows offline preview without credentials, but disables remote checking', async () => {
  server.configured = false
  const user = userEvent.setup(); mount(); await prepare(user)
  expect(screen.getByRole('button', { name: '检查远端' })).toBeDisabled()
  expect(screen.getByText(/尚未配置 GitHub 发布凭证/)).toBeVisible()
  expect(server.writes).toHaveLength(0)
})

it('does not enable preview before the current local delivery is complete', async () => {
  mount(review(), { ...delivery(), delivery: null })
  await screen.findByText(/完成当前审查的本地交付/)
  expect(screen.getByRole('button', { name: '生成发布预览' })).toBeDisabled()
})

it('blocks publication as soon as the approval becomes stale', async () => {
  const user = userEvent.setup(); const view = mount(); await checked(user); await user.click(confirmation())
  view.update(review({ status: 'stale' }))
  expect(publishButton()).toBeDisabled()
  expect(confirmation()).not.toBeChecked()
  expect(server.writes).toHaveLength(0)
})

it('stops before writing when the rechecked account changes', async () => {
  const user = userEvent.setup(); mount(); await checked(user); await user.click(confirmation())
  server.actor = { id: 88, login: 'different' }
  await user.click(publishButton())
  await screen.findByText(/发布账号、方案或批准已变化/)
  expect(server.writes).toHaveLength(0)
})

it('does not publish when durable request storage fails', async () => {
  const user = userEvent.setup(); mount(); await checked(user); await user.click(confirmation())
  const original = sessionStorage
  vi.stubGlobal('sessionStorage', { getItem: original.getItem.bind(original), setItem: () => { throw new Error('full') } })
  await user.click(publishButton())
  await screen.findByText(/浏览器无法保存发布请求，本次没有发送/)
  expect(server.writes).toHaveLength(0)
})

it('preserves an uncertain request across reload and resumes the exact same payload', async () => {
  server.publish = async () => { throw new TypeError('offline') }
  const user = userEvent.setup(); const first = mount(); await checked(user); await user.click(confirmation()); await user.click(publishButton())
  await screen.findByText('有一条发布请求尚未确认')
  const original = structuredClone(server.writes[0])
  first.unmount(); mount()
  await screen.findByText('有一条发布请求尚未确认')
  expect(server.writes).toHaveLength(1)
  server.publish = null
  await waitFor(() => expect(screen.getByRole('button', { name: '使用原请求恢复' })).toBeEnabled())
  await user.click(screen.getByRole('button', { name: '使用原请求恢复' }))
  await screen.findByRole('link', { name: /查看 PR #12/ })
  expect(server.writes).toEqual([original, original])
})

it('reconciles a saved completed request on reload without sending another POST', async () => {
  const value = await previewValue()
  const body = submission(value)
  savePendingPublication(taskId, body)
  server.items = [publication(value)]
  mount()
  await screen.findByRole('link', { name: /查看 PR #12/ })
  await waitFor(() => expect(readPendingPublication(taskId).request).toBeNull())
  expect(server.calls.every(([, options]) => options.method !== 'POST')).toBe(true)
})

it('uses the server canonical request when recovering an uncertain PR', async () => {
  const value = await previewValue()
  server.items = [publication(value, { status: 'uncertain', phase: 'pull_request', pull_request: null, error: 'PR creation outcome is still unknown' })]
  const user = userEvent.setup(); mount()
  await user.click(await screen.findByRole('button', { name: '核对 PR 结果' }))
  await screen.findByRole('link', { name: /查看 PR #12/ })
  expect(server.writes).toEqual([submission(value)])
})

it('keeps a PR link visible when base drift blocked the publication', async () => {
  const value = await previewValue()
  server.items = [publication(value, { status: 'blocked', phase: 'pull_request',
    pull_request: { number: 12, url: 'https://github.com/example/repair-demo/pull/12', state: 'open', base_sha: '3'.repeat(40), head_sha: value.plan.head_sha },
    error: 'PR exists but its base moved; inspect the saved PR and revalidate before use' })]
  mount()
  await screen.findByRole('link', { name: /查看 PR #12/ })
  expect(screen.getByText('需要处理')).toBeVisible()
  expect(screen.queryByText('已发布')).not.toBeInTheDocument()
  expect(server.writes).toHaveLength(0)
})

it('does not send a write if the user leaves during the final read-only check', async () => {
  const user = userEvent.setup(); const view = mount(); await checked(user); await user.click(confirmation())
  let resolve
  server.check = (body) => new Promise((done) => { resolve = async () => done(jsonResponse(remoteValue(await previewValue(body)))) })
  await user.click(publishButton())
  await waitFor(() => expect(resolve).toBeTypeOf('function'))
  view.unmount()
  await act(async () => resolve())
  expect(server.writes).toHaveLength(0)
})

it('keeps completed history when an older response arrives later', async () => {
  const value = await previewValue()
  const done = publication(value)
  server.items = [done]
  const view = mount()
  await screen.findByRole('link', { name: /查看 PR #12/ })
  act(() => view.client.setQueryData(['github-publications', taskId], { task_id: taskId, configured: true,
    items: [publication(value, { status: 'pending', phase: 'push', pr_attempted: false, pull_request: null, updated_at: value.checked_at })] }))
  expect(screen.getByRole('link', { name: /查看 PR #12/ })).toBeVisible()
  expect(screen.queryByRole('button', { name: '按原方案恢复发布' })).not.toBeInTheDocument()
})

it('keeps drafts scoped by task and requires fresh checks after reopening', async () => {
  const user = userEvent.setup(); const first = mount(); await prepare(user)
  fireEvent.change(screen.getByRole('textbox', { name: 'PR 标题' }), { target: { value: '修改后的标题' } })
  first.unmount(); mount()
  expect(screen.getByRole('textbox', { name: 'PR 标题' })).toHaveValue('修改后的标题')
  expect(screen.queryByText('远端检查通过')).not.toBeInTheDocument()
  expect(readPendingPublication(secondId).request).toBeNull()
})

it('invalidates the old check when changing PR type using the shared dropdown', async () => {
  const user = userEvent.setup(); mount(); await checked(user); await user.click(confirmation())
  await user.click(screen.getByRole('combobox', { name: 'PR 类型' }))
  await user.click(screen.getByRole('option', { name: '普通 PR' }))
  expect(screen.getByRole('combobox', { name: 'PR 类型' })).toHaveTextContent('普通 PR')
  expect(screen.queryByText('远端检查通过')).not.toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: '更新发布预览' }))
  await screen.findByRole('heading', { name: '发布预览' })
  const call = server.calls.filter(([url]) => url.endsWith('/preview')).at(-1)
  expect(JSON.parse(call[1].body).draft).toBe(false)
  expect(server.writes).toHaveLength(0)
})

it('explains an outdated backend and blocks all publication controls', async () => {
  server.read = () => jsonResponse({ detail: 'Not Found' }, 404)
  mount()
  await screen.findByText(/发布接口暂不可用/)
  expect(screen.getByRole('button', { name: '生成发布预览' })).toBeDisabled()
  expect(server.writes).toHaveLength(0)
})

it('returns to editing only after a definitive rejection and a successful empty history lookup', async () => {
  server.publish = () => jsonResponse({ detail: 'preview changed' }, 409)
  const user = userEvent.setup(); mount(); await checked(user); await user.click(confirmation()); await user.click(publishButton())
  const back = await screen.findByRole('button', { name: '返回编辑方案' })
  expect(readPendingPublication(taskId).request).not.toBeNull()
  await user.click(back)
  await waitFor(() => expect(screen.getByRole('textbox', { name: 'PR 标题' })).toBeEnabled())
  expect(readPendingPublication(taskId).request).toBeNull()
  expect(server.writes).toHaveLength(1)
})

it('shows an earlier publication in history without calling it the current delivery', async () => {
  const value = await previewValue()
  server.items = [publication(value)]
  const nextReview = review({ revision: 2, latest_review: { ...review().latest_review, review_id: secondId, revision: 2 } })
  const nextDelivery = delivery({ delivery: { ...delivery().delivery, delivery_id: secondId, review_id: secondId, review_revision: 2 } })
  mount(nextReview, nextDelivery)
  await screen.findByText('发布历史（1）')
  expect(screen.getByRole('button', { name: '生成发布预览' })).toBeEnabled()
  expect(screen.getByText('已发布')).not.toBeVisible()
  expect(server.writes).toHaveLength(0)
})
