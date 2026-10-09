import { useEffect, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowUpRight, LoaderCircle, RefreshCw } from 'lucide-react'
import { ApiError } from '../lib/api'
import {
  checkPublication, getPublications, keepLatestPublications, previewPublication,
  publicationMatchesApproval, publicationMessage, submitPublication, validPreviewInput,
} from '../lib/publications'
import {
  clearPendingPublication, readPendingPublication, readPublicationForm, savePendingPublication, savePublicationForm,
} from '../lib/publication-draft'
import { formatDate } from '../lib/task'
import { CopyButton, ErrorNotice, LoadingState } from './ui'
import GitHubIcon from './GitHubIcon'
import ReviewStepHeading from './ReviewStepHeading'
import Select from './Select'
import MarkdownBody from './MarkdownBody'

const statusLabels = { pending: '发布进行中', blocked: '需要处理', uncertain: '结果待核对', completed: '已发布' }
const phases = { check: '核对发布条件', push: '推送修改分支', pull_request: '创建或核对 PR', completed: '发布完成' }
const actionLabels = { preview: '正在生成预览…', check: '正在检查远端…', publish: '正在发布…', recover: '正在核对原发布…' }
const matching = (record, request) => record.plan_sha256 === request?.plan_sha256
const sameActor = (a, b) => a?.id === b?.id && a?.login === b?.login

function Feedback({ error }) {
  if (!error) return null
  const message = publicationMessage(error)
  const detail = typeof error === 'string' ? error : error.message
  return <><ErrorNotice error={{ message }} />{detail && detail !== message && <details className="publication-detail"><summary>查看接口详情</summary><pre>{detail}</pre></details>}</>
}

function PlanFacts({ plan }) {
  return <dl className="delivery-fields publication-facts">
    <dt>目标分支</dt><dd><code>{plan.base_branch}</code><span className="publication-inline-note">基线 {plan.base_sha.slice(0, 12)}</span></dd>
    <dt>修改分支</dt><dd><code>{plan.head_branch}</code></dd>
    <dt>交付提交</dt><dd className="delivery-copy-value"><code title={plan.head_sha}>{plan.head_sha.slice(0, 12)}</code><CopyButton value={plan.head_sha} label="复制发布提交" /></dd>
    <dt>修改文件</dt><dd><ul>{plan.changed_paths.map((path) => <li key={path}><code>{path}</code></li>)}</ul></dd>
  </dl>
}

function PublicationRecord({ record, onResume, disabled }) {
  return <div className="publication-record">
    <div className="publication-record-heading"><span className={`publication-status is-${record.status}`}>{statusLabels[record.status]}</span><span>第 {record.plan.review_revision} 次审查</span><time dateTime={record.updated_at}>{formatDate(record.updated_at)}</time></div>
    <h3>{record.plan.title}</h3>
    <p className="publication-phase">{phases[record.phase]}{record.actor && ` · 发布账号 ${record.actor.login}`}</p>
    {record.pull_request && <a className="publication-pr-link" href={record.pull_request.url} target="_blank" rel="noreferrer"><GitHubIcon />查看 PR #{record.pull_request.number}<span>{record.pull_request.state === 'closed' ? '已关闭' : record.plan.draft ? '草稿' : '开放中'}</span><ArrowUpRight size={15} /></a>}
    <PlanFacts plan={record.plan} />
    {record.error && <Feedback error={record.error} />}
    <div className="publication-body"><MarkdownBody body={record.plan.body} baseUrl={record.pull_request?.url || record.plan.issue_url}
      collapse label="已确认的 PR 正文" rawLabel="PR 原文" collapseLabel="已确认的 PR 内容" /></div>
    {record.status !== 'completed' && <div className="publication-actions"><button type="button" className="button button-quiet button-small" disabled={disabled} onClick={() => onResume(record)}>{record.pr_attempted ? '核对 PR 结果' : '按原方案恢复发布'}</button><span className="field-hint">{record.pr_attempted ? '创建结果不明确时，只核对已有 PR。' : '沿用已确认的内容，可能继续推送或创建 PR。'}</span></div>}
  </div>
}

export default function GitHubPublicationPanel({ taskId, source, reviewState, deliveryState }) {
  const client = useQueryClient()
  const key = ['github-publications', taskId]
  const [form, setForm] = useState(() => readPublicationForm(taskId, source.issue.repository.default_branch || ''))
  const [saved, setSaved] = useState(() => readPendingPublication(taskId))
  const [preview, setPreview] = useState(null)
  const [remote, setRemote] = useState(null)
  const [confirmed, setConfirmed] = useState(false)
  const [action, setAction] = useState(null)
  const [error, setError] = useState(null)
  const [definitiveFailure, setDefinitiveFailure] = useState(false)
  const mounted = useRef(true)
  const working = useRef(false)
  const latest = useRef({ reviewState, deliveryState })
  useEffect(() => { latest.current = { reviewState, deliveryState } }, [reviewState, deliveryState])
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])

  const state = useQuery({ queryKey: key, queryFn: ({ signal }) => getPublications(taskId, { signal, source }),
    retry: false, structuralSharing: keepLatestPublications,
    refetchInterval: (query) => (saved.request && !query.state.data?.items.some((r) => matching(r, saved.request)))
      || query.state.data?.items.some((r) => r.status === 'pending') ? 2500 : false })
  const records = state.data?.items || []
  const pendingKnown = saved.request && records.some((record) => matching(record, saved.request))
  const pending = pendingKnown ? null : saved.request
  useEffect(() => {
    if (pendingKnown) clearPendingPublication(taskId, saved.request.request_id)
  }, [pendingKnown, taskId, saved.request])
  const localRecord = deliveryState?.delivery
  const current = records.find((r) => r.plan.delivery_id === localRecord?.delivery_id)
  const ready = localRecord?.status === 'completed' && deliveryState.delivery_intact !== false
    && reviewState?.status === 'accepted' && reviewState.can_accept
    && localRecord.review_id === reviewState.latest_review?.review_id && localRecord.review_revision === reviewState.revision
  const unavailable = state.isPending || state.isError || Boolean(saved.error)
  const busy = Boolean(action)
  const requestInput = { base_branch: form.base_branch, draft: form.draft,
    ...(form.initialized ? { title: form.title, body: form.body } : {}) }
  const valid = validPreviewInput(requestInput)
  const canPrepare = ready && !unavailable && !busy && !pending && !current
  const approvalMatches = preview && publicationMatchesApproval(preview.plan, reviewState, deliveryState)
  const checked = remote?.can_publish && remote.preview.plan_sha256 === preview?.plan_sha256 && approvalMatches
  const canSend = canPrepare && valid && checked && confirmed && state.data?.configured

  function change(name, value) {
    const next = { ...form, [name]: value }
    setForm(next)
    savePublicationForm(taskId, next)
    setPreview(null); setRemote(null); setConfirmed(false); setError(null)
  }

  function receive(next) {
    return client.setQueryData(key, (previous) => keepLatestPublications(previous, next))
  }

  function receiveRecord(record, configured) {
    receive({ task_id: taskId, configured, items: [record] })
  }

  async function generate(event) {
    event.preventDefault()
    if (working.current || !canPrepare || !valid) return
    working.current = true; setAction('preview'); setError(null); setConfirmed(false); setRemote(null)
    try {
      const result = await previewPublication(taskId, requestInput, { source })
      if (!mounted.current) return
      const context = latest.current
      if (!publicationMatchesApproval(result.plan, context.reviewState, context.deliveryState)) throw new ApiError('审查或本地交付已变化，请先刷新交付计划。', 409)
      setPreview(result)
      const next = { ...form, title: result.plan.title, body: result.plan.body, initialized: true }
      setForm(next); savePublicationForm(taskId, next)
    } catch (cause) { if (mounted.current) setError(cause) }
    finally { working.current = false; if (mounted.current) setAction(null) }
  }

  async function checkRemote() {
    if (working.current || !canPrepare || !preview || !valid || !state.data.configured) return
    working.current = true; setAction('check'); setError(null); setConfirmed(false); setRemote(null)
    try {
      const result = await checkPublication(taskId, requestInput, { source })
      if (!mounted.current) return
      const context = latest.current
      if (result.preview.plan_sha256 !== preview.plan_sha256
        || !publicationMatchesApproval(result.preview.plan, context.reviewState, context.deliveryState)) throw new ApiError('发布方案已变化，请重新生成预览并检查。', 409)
      setRemote(result)
      if (!result.can_publish) await state.refetch()
    } catch (cause) { if (mounted.current) setError(cause) }
    finally { working.current = false; if (mounted.current) setAction(null) }
  }

  async function publish() {
    if (working.current || !canSend) return
    working.current = true; setAction('publish'); setError(null); setConfirmed(false); setDefinitiveFailure(false)
    let body
    let sent = false
    try {
      const now = receive(await getPublications(taskId, { source }))
      if (!mounted.current) return
      if (now.items.some((r) => r.plan.delivery_id === preview.plan.delivery_id)) return
      const checkedNow = await checkPublication(taskId, requestInput, { source })
      if (!mounted.current) return
      const context = latest.current
      if (!now.configured || !checkedNow.can_publish || checkedNow.preview.plan_sha256 !== preview.plan_sha256
        || !sameActor(checkedNow.actor, remote.actor)
        || !publicationMatchesApproval(checkedNow.preview.plan, context.reviewState, context.deliveryState)) throw new ApiError('发布账号、方案或批准已变化，请重新检查后确认。', 409)
      body = { request_id: crypto.randomUUID(), preview: { ...requestInput }, plan_sha256: preview.plan_sha256, confirmed: true }
      savePendingPublication(taskId, body)
      setSaved({ request: body, error: '' })
      sent = true
      const result = await submitPublication(taskId, body, { source })
      receiveRecord(result.publication, now.configured)
      clearPendingPublication(taskId, body.request_id)
    } catch (cause) {
      if (mounted.current) {
        setError(cause); setRemote(null)
        setDefinitiveFailure(sent && [400, 404, 409, 422].includes(cause.status))
      }
      if (sent) client.invalidateQueries({ queryKey: key })
    } finally { working.current = false; if (mounted.current) setAction(null) }
  }

  async function resume(record = null) {
    if (working.current || unavailable || !state.data?.configured) return
    working.current = true; setAction('recover'); setError(null); setDefinitiveFailure(false)
    let body = record?.submission || pending
    let sent = false
    try {
      const now = receive(await getPublications(taskId, { source }))
      if (!mounted.current) return
      const found = now.items.find((r) => matching(r, body))
      if (found?.status === 'completed') { clearPendingPublication(taskId, body.request_id); return }
      if (record && (!found || found.publication_id !== record.publication_id)) throw new ApiError('已有发布记录已变化，请刷新后核对。', 409)
      if (!now.configured) throw new ApiError('后端尚未配置发布凭证。')
      body = found?.submission || body
      if (!body) throw new ApiError('没有可恢复的发布请求。')
      savePendingPublication(taskId, body)
      setSaved({ request: body, error: '' })
      sent = true
      const result = await submitPublication(taskId, body, { source })
      receiveRecord(result.publication, now.configured)
      clearPendingPublication(taskId, body.request_id)
    } catch (cause) {
      if (mounted.current) { setError(cause); setDefinitiveFailure(sent && [400, 404, 409, 422].includes(cause.status)) }
      if (sent) client.invalidateQueries({ queryKey: key })
    } finally { working.current = false; if (mounted.current) setAction(null) }
  }

  async function returnToEditing() {
    if (working.current || !pending || !definitiveFailure) return
    working.current = true; setAction('check')
    try {
      const now = receive(await getPublications(taskId, { source }))
      if (!mounted.current || now.items.some((r) => matching(r, pending))) return
      clearPendingPublication(taskId, pending.request_id)
      setSaved(readPendingPublication(taskId)); setRemote(null); setConfirmed(false); setError(null)
    } catch (cause) { if (mounted.current) setError(cause) }
    finally { working.current = false; if (mounted.current) setAction(null) }
  }

  return <section className="panel review-step publication-panel" aria-label="GitHub 发布">
    <ReviewStepHeading number="05" title="GitHub 发布"><button type="button" className="button button-quiet button-small" disabled={busy || state.isFetching} onClick={() => { setConfirmed(false); setRemote(null); state.refetch() }}><RefreshCw size={14} aria-hidden="true" />刷新发布状态</button></ReviewStepHeading>
    <a className="publication-repository" href={source.issue.repository.html_url} target="_blank" rel="noreferrer"><GitHubIcon />{source.issue.repository.full_name}<ArrowUpRight size={14} /></a>
    {state.isPending && <LoadingState label="正在读取发布记录…" />}
    {state.isError && <Feedback error={state.error} />}
    {saved.error && <Feedback error={{ message: saved.error }} />}
    {state.data && !state.data.configured && <p className="notice notice-info">尚未配置 GitHub 发布凭证。可以先生成预览，配置后端后再检查远端。</p>}
    {current ? <PublicationRecord record={current} disabled={busy || unavailable || !state.data?.configured} onResume={resume} /> : <>
      {!ready && <p className="publication-help">完成当前审查的本地交付后，可以准备发布。</p>}
      <form className="publication-form" onSubmit={generate}>
        <fieldset disabled={!canPrepare}>
          <div className="field-grid"><div className="field"><label htmlFor={`publication-base-${taskId}`}>目标分支</label><input id={`publication-base-${taskId}`} value={form.base_branch} maxLength={200} onChange={(e) => change('base_branch', e.target.value)} placeholder="例如 main" spellCheck={false} /></div>
            <div className="field"><label htmlFor={`publication-type-${taskId}`}>PR 类型</label><Select id={`publication-type-${taskId}`} value={form.draft ? 'draft' : 'ready'} onValueChange={(v) => change('draft', v === 'draft')} options={[{ value: 'draft', label: '草稿 PR' }, { value: 'ready', label: '普通 PR' }]} disabled={!canPrepare} /></div></div>
          {form.initialized && <><div className="field"><label htmlFor={`publication-title-${taskId}`}>PR 标题</label><input id={`publication-title-${taskId}`} value={form.title} maxLength={256} onChange={(e) => change('title', e.target.value)} /></div><div className="field"><label htmlFor={`publication-body-${taskId}`}>PR 正文</label><textarea id={`publication-body-${taskId}`} value={form.body} rows={8} maxLength={60000} onChange={(e) => change('body', e.target.value)} spellCheck={false} /><p className="field-hint">支持 Markdown，按当前文本发布。</p></div></>}
          <div className="publication-actions"><button type="submit" className="button button-quiet button-small" disabled={!canPrepare || !valid}>{form.initialized ? '更新发布预览' : '生成发布预览'}</button></div>
        </fieldset>
      </form>
      {preview && <div className="publication-preview"><h3>发布预览</h3><PlanFacts plan={preview.plan} />
        {!approvalMatches && <p className="notice notice-warning">审查或交付已变化，请重新核对并生成预览。</p>}
        <div className="publication-actions"><button type="button" className="button button-quiet button-small" disabled={!canPrepare || !approvalMatches || !state.data?.configured} onClick={checkRemote}>检查远端</button></div>
        {remote && <div className={checked ? 'publication-remote' : 'notice notice-warning'} role="status">{checked ? <><strong>远端检查通过</strong><span>发布账号 {remote.actor.login} · 目标分支 {remote.preview.plan.base_branch}</span></> : <span>{publicationMessage(remote.reason)}</span>}</div>}
        <div className="publication-confirm"><label className="checkbox-label"><input type="checkbox" checked={confirmed && Boolean(checked)} disabled={!checked || !canPrepare} onChange={(e) => setConfirmed(e.target.checked)} />我已核对仓库、分支和 PR 内容，确认发布到 GitHub。</label><button type="button" className="button button-primary" disabled={!canSend} onClick={publish}><ArrowUpRight size={16} />确认并发布 PR</button></div>
      </div>}
    </>}
    {pending && !busy && <div className="notice notice-warning publication-pending"><strong>有一条发布请求尚未确认</strong><p>刷新只读取已有记录。恢复时沿用原请求，不更换发布内容。</p><div className="publication-actions"><button type="button" className="button button-quiet button-small" disabled={unavailable || !state.data?.configured} onClick={() => resume()}>使用原请求恢复</button>{definitiveFailure && <button type="button" className="button button-quiet button-small" onClick={returnToEditing}>返回编辑方案</button>}</div></div>}
    {action && <p className="publication-progress" role="status"><LoaderCircle size={16} className="spin" aria-hidden="true" />{actionLabels[action]}</p>}
    <Feedback error={error} />
    {records.some((r) => r.publication_id !== current?.publication_id) && <details className="publication-history"><summary>发布历史（{records.filter((r) => r.publication_id !== current?.publication_id).length}）</summary>{records.filter((r) => r.publication_id !== current?.publication_id).slice().reverse().map((record) => <PublicationRecord key={record.publication_id} record={record} disabled={busy || unavailable || !state.data?.configured} onResume={resume} />)}</details>}
  </section>
}
