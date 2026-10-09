import { useEffect, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate } from 'react-router-dom'
import { ArrowLeft, ArrowUpRight, Check, LoaderCircle, Pencil, RefreshCw } from 'lucide-react'
import { importIssue, getIssue, prepareRepository, getPreparation, createIssueTask } from '../lib/github'
import { newGithubDraft, readGithubDraft, saveGithubDraft } from '../lib/github-draft'
import { buildTaskPayload, formatDate, modelPresets, validateTaskForm } from '../lib/task'
import { CopyButton, ErrorNotice, LoadingState } from '../components/ui'
import TaskConfiguration, { Field } from '../components/TaskConfiguration'
import GitHubIcon from '../components/GitHubIcon'

const operations = { issue: importIssue, repository: prepareRepository, task: createIssueTask }

export default function GithubTask() {
  const [flow, setFlow] = useState(readGithubDraft)
  const [editingSource, setEditingSource] = useState(() => Boolean(flow.sourceId && flow.pending?.stage === 'issue'))
  const mounted = useRef(true)
  const submitting = useRef(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [errorStage, setErrorStage] = useState(null)
  const [errors, setErrors] = useState({})
  const navigate = useNavigate()
  const cache = useQueryClient()
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])

  function update(change) {
    const next = { ...readGithubDraft(), ...change }
    const persisted = saveGithubDraft(next)
    setFlow(next)
    return persisted
  }

  const sourceQuery = useQuery({ queryKey: ['github-source', flow.sourceId],
    queryFn: ({ signal }) => getIssue(flow.sourceId, { signal }), enabled: Boolean(flow.sourceId), retry: false })
  const preparationQuery = useQuery({ queryKey: ['repository-preparation', flow.preparationId],
    queryFn: ({ signal }) => getPreparation(flow.preparationId, { signal }), enabled: Boolean(flow.preparationId), retry: false,
    refetchInterval: (query) => query.state.data?.preparation.status === 'preparing' ? 2000 : false })
  const source = sourceQuery.data
  const state = preparationQuery.data
  const preparation = state?.preparation
  const ready = !editingSource && source && preparation?.status === 'ready' && state.checkout_intact === true
    && preparation.source_id === source.source_id
    && preparation.repository.id === source.repository.id
    && (flow.ref || null) === preparation.requested_ref
  const locked = busy || Boolean(flow.pending)

  function change(name, value) {
    const next = { ...flow.form, [name]: value }
    if (name === 'provider') next.model = modelPresets[value]
    update({ form: next })
    setErrors({})
  }

  function input(name, extra = {}) {
    return { id: `task-${name}`, name, value: flow.form[name], onChange: (event) => change(name, event.target.value),
      'aria-invalid': Boolean(errors[name]), 'aria-describedby': `task-${name}-hint`, ...extra }
  }

  async function send(stage, body) {
    if (submitting.current) return
    submitting.current = true
    setBusy(true)
    setError(null)
    setErrorStage(stage)
    if (!update({ pending: { stage, body } })) {
      setError(new Error('浏览器无法保存请求编号，本次尚未发送。请恢复浏览器存储后，用原请求重试。'))
      setBusy(false)
      submitting.current = false
      return
    }
    try {
      const result = await operations[stage](body)
      const current = readGithubDraft()
      if (current.pending?.body.request_id !== body.request_id) {
        // Another mounted copy may have confirmed this same idempotent request.
        if (mounted.current) {
          setFlow(current)
          if (stage === 'issue' && current.confirmed?.requestId === body.request_id) setEditingSource(false)
          if (stage === 'task' && current.taskId === result.task.task_id) navigate(`/tasks/${current.taskId}`)
        }
        return
      }
      if (stage === 'issue') {
        cache.setQueryData(['github-source', result.source.source_id], result.source)
        update({ sourceId: result.source.source_id, preparationId: null, taskId: null, pending: null,
          confirmed: { stage, requestId: body.request_id } })
        if (mounted.current) setEditingSource(false)
      } else if (stage === 'repository') {
        update({ preparationId: result.preparation.preparation_id, pending: null, confirmed: { stage, requestId: body.request_id } })
        // The GET endpoint checks current files, unlike the preparation response.
        if (mounted.current) cache.invalidateQueries({ queryKey: ['repository-preparation', result.preparation.preparation_id] })
      } else {
        cache.setQueryData(['task', result.task.task_id], result.task)
        cache.invalidateQueries({ queryKey: ['tasks'] })
        update({ taskId: result.task.task_id, pending: null, confirmed: { stage, requestId: body.request_id } })
        if (mounted.current) navigate(`/tasks/${result.task.task_id}`)
      }
    } catch (failure) {
      const current = readGithubDraft()
      if (mounted.current) {
        if (current.pending?.body.request_id !== body.request_id) {
          setFlow(current)
          if (stage === 'issue' && current.confirmed?.requestId === body.request_id) setEditingSource(false)
          if (stage === 'task' && current.confirmed?.requestId === body.request_id && current.taskId) navigate(`/tasks/${current.taskId}`)
        } else setError(failure)
      }
      // Keep the exact request, including UUID, until its outcome is known.
    } finally {
      submitting.current = false
      if (mounted.current) setBusy(false)
    }
  }

  function importSource(event) {
    event.preventDefault()
    if (locked) return
    try {
      const url = new URL(flow.url.trim())
      if (url.protocol !== 'https:' || url.host !== 'github.com' || url.username || url.password
          || !/^\/[A-Za-z0-9-]+\/[A-Za-z0-9_.-]+\/issues\/\d+\/?$/.test(url.pathname)) throw new Error()
    } catch {
      setError(new Error('请填写公开 GitHub Issue 链接，例如 https://github.com/owner/repo/issues/123。'))
      setErrorStage('issue')
      return
    }
    send('issue', { request_id: crypto.randomUUID(), url: flow.url.trim() })
  }

  function prepare(event) {
    event.preventDefault()
    if (!source || editingSource || sourceQuery.isError || locked) return
    send('repository', { request_id: crypto.randomUUID(), source_id: source.source_id, ref: flow.ref || null })
  }

  function create(event) {
    event.preventDefault()
    if (!ready || sourceQuery.isError || preparationQuery.isError || locked || flow.taskId) return
    const found = validateTaskForm(flow.form)
    const wd = flow.form.workdir
    if (!wd || wd.includes('\0') || wd.includes('\\') || (wd !== '.' && (wd.startsWith('/') || wd.split('/').some((part) => !part || part === '.' || part === '..' || part.toLowerCase() === '.git')))) found.workdir = '请填写仓库相对目录，例如 . 或 src。'
    setErrors(found)
    if (Object.keys(found).length) {
      const element = document.getElementById(`task-${Object.keys(found)[0]}`)
      const details = element?.closest('details')
      if (details) details.open = true
      element?.focus()
      return
    }
    const configuration = buildTaskPayload(flow.form)
    delete configuration.repo_path
    send('task', { request_id: crypto.randomUUID(), preparation_id: preparation.preparation_id,
      workdir: wd, ...configuration })
  }

  function restart() {
    if (flow.pending && !window.confirm('上次请求可能已经成功。请先核对已有任务；开始新流程不会取消服务器上的操作。')) return
    const next = newGithubDraft()
    saveGithubDraft(next)
    setFlow(next); setError(null); setErrors({}); setEditingSource(false)
  }

  function editSource() {
    if (locked || flow.taskId) return
    update({ url: source?.url || flow.url })
    setEditingSource(true)
    setError(null)
    requestAnimationFrame(() => document.getElementById('issue-url')?.focus())
  }

  function stageFeedback(stage) {
    const pending = flow.pending?.stage === stage
    const stageError = errorStage === stage ? error : null
    const labels = { issue: '正在读取 Issue…', repository: '正在准备代码…', task: '正在创建任务…' }
    return <>
      {stageError && <ErrorNotice error={stageError} />}
      {pending && <div className="notice notice-info stage-feedback" role="status"><LoaderCircle size={18} className={busy ? 'spin' : ''} /><div><strong>{busy ? labels[stage] : '上次请求结果尚未确认'}</strong><p>请求编号：<code>{flow.pending.body.request_id}</code>。{busy ? '离开或刷新页面后可继续核对。' : '重试使用原编号，不会重复创建记录。'}</p>{!busy && <div className="stage-feedback-actions"><button type="button" className="button button-small button-quiet" onClick={() => send(stage, flow.pending.body)}>用原请求重试</button>{[404, 409, 422].includes(stageError?.status) && <button type="button" className="button button-small button-quiet" onClick={() => { update({ pending: null }); setError(null) }}>返回修改配置</button>}</div>}</div></div>}
    </>
  }

  return <div className="page github-task-page">
    <Link className="back-link" to="/tasks"><ArrowLeft size={15} />返回任务列表</Link>
    <div className="page-heading"><div><span className="eyebrow">GITHUB ISSUE</span><h1 id="page-title" tabIndex={-1}>从 Issue 创建任务</h1><p>导入问题，准备代码，确认修复配置。</p></div><button type="button" className="button button-quiet" disabled={busy} onClick={restart}>开始新流程</button></div>
    <ol className="github-steps" aria-label="创建进度">{['导入问题', '准备代码', '确认任务'].map((label, index) => <li key={label} className={(index === 0 && source) || (index === 1 && ready) || (index === 2 && flow.taskId) ? 'done' : ''}><span>{(index === 0 && source) || (index === 1 && ready) || (index === 2 && flow.taskId) ? <Check size={15} /> : index + 1}</span>{label}</li>)}</ol>
    {flow.storageError && <ErrorNotice error={new Error(flow.storageError)} />}
    <div className="github-workflow">
      <section className="panel github-section"><div className="section-heading"><span className="step-number">01</span><div><h2>导入问题</h2></div>{flow.sourceId && <button type="button" className="button button-quiet button-small issue-edit-button" disabled={locked || Boolean(flow.taskId) || editingSource} onClick={editSource}><Pencil size={14} />修改链接</button>}</div>
        {(!flow.sourceId || editingSource) && <form onSubmit={importSource}><fieldset disabled={locked || Boolean(flow.storageError)}><div className="field"><label htmlFor="issue-url">Issue 链接</label><input id="issue-url" type="url" value={flow.url} placeholder="https://github.com/owner/repo/issues/123" onChange={(event) => update({ url: event.target.value })} required /></div>{editingSource && <p className="field-hint issue-edit-hint">重新读取后，需要重新准备对应仓库。</p>}<div className="issue-import-actions"><button className="button button-primary" type="submit">{editingSource ? '重新读取 Issue' : '读取 Issue'}</button>{editingSource && <button type="button" className="button button-quiet" onClick={() => { setEditingSource(false); update({ url: source?.url || flow.url }); setError(null) }}>取消修改</button>}</div></fieldset></form>}
        {stageFeedback('issue')}
        {flow.sourceId && sourceQuery.isPending && <LoadingState label="正在恢复问题来源…" />}
        {sourceQuery.isError && <ErrorNotice error={sourceQuery.error} onRetry={() => sourceQuery.refetch()} />}
        {source && !editingSource && <div className="issue-preview"><a href={source.url} target="_blank" rel="noreferrer"><GitHubIcon />{source.repository.full_name} #{source.issue_number}<ArrowUpRight size={14} /></a><div className="issue-title-group"><span className="issue-section-label">问题标题</span><h3>{source.title}</h3></div><div className="issue-body-group"><span className="issue-section-label">问题描述</span><p className="issue-body" tabIndex={0} aria-label="Issue 正文">{source.body || '此 Issue 没有正文。'}</p></div><div className="source-caption">{source.state === 'open' ? '开放中' : '已关闭'} · 读取于 {formatDate(source.fetched_at)}</div></div>}
      </section>
      <section className="panel github-section"><div className="section-heading"><span className="step-number">02</span><div><h2>准备代码</h2></div></div>
        {!source && <p className="muted">先导入 Issue。</p>}
        {!source && stageFeedback('repository')}
        {source && <><form onSubmit={prepare}><fieldset disabled={locked || editingSource || sourceQuery.isError || preparation?.status === 'preparing' || Boolean(flow.taskId)}><div className="field"><label htmlFor="repository-ref">代码版本</label><input id="repository-ref" value={flow.ref} onChange={(event) => update({ ref: event.target.value })} placeholder="留空使用当前默认分支；也可填写分支、标签或 commit" maxLength={200} spellCheck={false} /><p className="field-hint">版本在准备时固定，后续分支更新不会改变本次基线。</p></div><button type="submit" className="button button-quiet repository-prepare-button">{preparation ? '准备另一个版本' : '准备仓库'}</button></fieldset></form>
          {stageFeedback('repository')}
          {flow.preparationId && preparationQuery.isPending && <LoadingState label="正在检查代码目录…" />}
          {preparationQuery.isError && <ErrorNotice error={preparationQuery.error} onRetry={() => preparationQuery.refetch()} />}
          {preparation && <div className="preparation-summary"><div className="panel-heading"><strong>{preparation.status === 'failed' ? '准备失败' : preparation.status === 'preparing' ? '正在准备' : state.checkout_intact ? '代码已就绪' : '代码需要重新检查'}</strong><button type="button" className="icon-button" aria-label="检查代码状态" disabled={preparationQuery.isFetching} onClick={() => preparationQuery.refetch()}><RefreshCw size={15} /></button></div>{preparation.error && <p role="alert">{preparation.error}</p>}{state.reason && <p>{state.reason}</p>}<dl><dt>代码版本</dt><dd>{preparation.resolved_ref || '尚未解析'}</dd><dt>固定提交</dt><dd className="fact-sha"><code className="fact-value">{preparation.base_sha || '—'}</code>{preparation.base_sha && <CopyButton value={preparation.base_sha} label="复制固定提交" />}</dd><dt>代码目录</dt><dd className="mono">{preparation.repo_path}</dd></dl></div>}
        </>}
        {preparation?.status === 'preparing' && flow.pending?.stage !== 'repository' && <LoadingState label="正在准备代码…" />}
      </section>
      <section className="panel github-section github-config"><div className="section-heading"><span className="step-number">03</span><div><h2>确认任务</h2></div></div>
        {flow.taskId && <div className="notice notice-info"><Check size={18} /><div><strong>任务已创建</strong><p><Link to={`/tasks/${flow.taskId}`}>查看任务 {flow.taskId}</Link></p></div></div>}
        <form onSubmit={create} noValidate className="create-form"><fieldset disabled={!ready || locked || sourceQuery.isError || preparationQuery.isError || Boolean(flow.taskId)}>
          <Field name="goal" label="修复要求" hint="确认要解决的问题和保留的行为；模型还会收到上方保存的 Issue 内容。" error={errors.goal}><textarea {...input('goal', { rows: 4, maxLength: 20000 })} /></Field>
          <div className="field-grid"><Field name="verification_command" label="验证命令" hint="使用已准备镜像中的测试或复现命令。" error={errors.verification_command}><input {...input('verification_command', { maxLength: 20000, spellCheck: false })} /></Field><Field name="workdir" label="工作目录" hint="仓库相对目录，默认 .。" error={errors.workdir}><input {...input('workdir', { maxLength: 4096, spellCheck: false })} /></Field></div>
          <TaskConfiguration form={flow.form} errors={errors} input={input} change={change} disabled={!ready || locked || Boolean(flow.taskId)} stepLabel={null} />
          <p className="field-hint">填写适合该仓库的本地镜像，需已安装依赖。提交只创建任务，独立 worker 负责执行。</p>
          <div className="form-actions"><button type="submit" className="button button-primary"><ArrowUpRight size={16} />确认并创建任务</button></div>
        </fieldset></form>
        {stageFeedback('task')}
      </section>
    </div>
  </div>
}
