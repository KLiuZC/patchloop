import { useEffect, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Check, RefreshCw, X } from 'lucide-react'
import { ApiError } from '../lib/api'
import { getReview, listReviews, loadReviewSnapshot, sameEvidence, submitReview } from '../lib/reviews'
import { clearPendingReview, keepLatestReview, readPendingReview, reviewError, reviewReason, savePendingReview } from '../lib/review-draft'
import { formatDate } from '../lib/task'
import DiffViewer from './DiffViewer'
import ValidationPanel from './ValidationPanel'
import DeliveryPanel from './DeliveryPanel'
import IssueSourcePanel from './IssueSourcePanel'
import VerificationCommand from './VerificationCommand'
import ReviewStepHeading from './ReviewStepHeading'
import { ErrorNotice, LoadingState, ReviewBadge } from './ui'

function EvidenceDetails({ evidence, label = '审查依据' }) {
  if (!evidence) return null
  return <details className="review-evidence"><summary>{label}</summary><dl>
    <dt>基线提交</dt><dd><code>{evidence.base_sha}</code></dd>
    <dt>补丁 SHA-256</dt><dd><code>{evidence.patch_sha256}</code></dd>
    <dt>验证报告 SHA-256</dt><dd><code>{evidence.validation_sha256}</code></dd>
  </dl></details>
}

export default function ReviewPanel({ taskId, source }) {
  return <ReviewSession key={taskId} taskId={taskId} source={source} />
}

function ReviewSession({ taskId, source }) {
  const client = useQueryClient()
  const [pending, setPending] = useState(() => readPendingReview(taskId))
  const [comment, setComment] = useState(() => readPendingReview(taskId)?.comment ?? client.getQueryData(['review-comment', taskId]) ?? '')
  const [busy, setBusy] = useState(false)
  const [editingDecision, setEditingDecision] = useState(false)
  const [error, setError] = useState(null)
  const [feedback, setFeedback] = useState('')
  const [conflict, setConflict] = useState(false)
  const working = useRef(false)
  const mounted = useRef(true)
  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false }
  }, [])

  const current = useQuery({ queryKey: ['review', taskId], queryFn: ({ signal }) => getReview(taskId, { signal }),
    refetchInterval: 5000, structuralSharing: keepLatestReview })
  // Keep the displayed evidence fixed while the user writes a decision.
  const snapshot = useQuery({ queryKey: ['review-snapshot', taskId], queryFn: ({ signal }) => loadReviewSnapshot(taskId, { signal }),
    staleTime: Infinity, gcTime: 0, refetchOnWindowFocus: false, refetchOnReconnect: false, retry: false })
  const history = useQuery({ queryKey: ['reviews', taskId], queryFn: ({ signal }) => listReviews(taskId, { signal }), refetchInterval: 5000 })
  const frozen = snapshot.data?.state
  const changed = Boolean(frozen && current.data && (frozen.revision !== current.data.revision || !sameEvidence(frozen.evidence, current.data.evidence)))
  const blocked = busy || Boolean(pending) || conflict || changed || current.isError || current.isPending
    || snapshot.isFetching || snapshot.isError || !frozen?.evidence

  async function rememberCurrent(state) {
    const key = ['review', taskId]
    const known = client.getQueryData(key)
    await client.cancelQueries({ queryKey: key, exact: true })
    return client.setQueryData(key, (previous) => keepLatestReview(previous, keepLatestReview(known, state)))
  }

  async function saved(record, receivedState) {
    const state = await rememberCurrent(receivedState)
    const ownsPending = readPendingReview(taskId)?.request_id === record.request_id
    if (ownsPending) {
      clearPendingReview(taskId, record.request_id)
      client.removeQueries({ queryKey: ['review-comment', taskId], exact: true })
    }
    const displayed = client.getQueryData(['review-snapshot', taskId])
    if (displayed && sameEvidence(displayed.state.evidence, state.evidence)) {
      client.setQueryData(['review-snapshot', taskId], { ...displayed, state })
    }
    client.invalidateQueries({ queryKey: ['reviews', taskId] })
    if (mounted.current) {
      if (ownsPending || pending?.request_id === record.request_id) {
        setPending(null)
        setComment('')
        setEditingDecision(false)
      }
      setError(null)
      setFeedback(state.status === 'stale' ? '审查已记录，但依据已变化，需要重新审查。' : '审查已保存。')
    }
  }

  async function send(body, retry = false) {
    if (working.current) return
    working.current = true
    setBusy(true)
    setError(null)
    setFeedback('')
    let sent = false
    try {
      if (!retry) {
        const received = await getReview(taskId)
        if (!mounted.current) return
        const latest = await rememberCurrent(received)
        if (!mounted.current) return
        if (latest.revision !== body.expected_revision || !sameEvidence(latest.evidence, body.evidence)) {
          throw new ApiError('审查状态或依据已更新，请重新加载资料后再提交。', 409)
        }
        if (!(body.decision === 'accepted' ? latest.can_accept : latest.can_reject)) {
          throw new ApiError(reviewReason(latest), 409)
        }
      }
      savePendingReview(taskId, body)
      if (mounted.current) setPending(body)
      sent = true
      const response = await submitReview(taskId, body)
      await saved(response.review, response.current)
    } catch (cause) {
      const definitive = [400, 401, 403, 404, 409, 422].includes(cause.status)
      if (sent && definitive) clearPendingReview(taskId, body.request_id)
      if (mounted.current) {
        if (definitive) setPending(null)
        if (cause.status === 409) setConflict(true)
        setError(new ApiError(cause.status === 409 ? '审查状态或依据已变化，请重新加载资料后再提交。' : reviewError(cause), cause.status))
      }
    } finally {
      working.current = false
      if (mounted.current) setBusy(false)
    }
  }

  function decide(decision) {
    if (blocked || comment.length > 4000 || comment.includes('\0')) return
    send({ request_id: crypto.randomUUID(), expected_revision: frozen.revision, decision, comment, evidence: frozen.evidence })
  }

  async function reconcile() {
    if (!pending || working.current) return
    working.current = true
    setBusy(true)
    setError(null)
    try {
      const [records, receivedState] = await Promise.all([listReviews(taskId), getReview(taskId)])
      let state = await rememberCurrent(receivedState)
      client.setQueryData(['reviews', taskId], records)
      const found = records.items.find((item) => item.request_id === pending.request_id)
      if (found) {
        if (found.revision !== pending.expected_revision + 1 || found.decision !== pending.decision
          || found.comment !== pending.comment || !sameEvidence(found.evidence, pending.evidence)) {
          throw new ApiError('历史记录与原提交不一致，请保留原请求并核查。', 409)
        }
        if (state.revision < found.revision) state = await rememberCurrent(await getReview(taskId))
        if (state.revision < found.revision
          || (state.revision === found.revision && state.latest_review?.review_id !== found.review_id)) {
          throw new ApiError('审查状态尚未同步，请稍后再次核对。', 409)
        }
        await saved(found, state)
      } else if (mounted.current) {
        setFeedback('尚未确认这次提交，可使用原请求重试。')
      }
    } catch (cause) {
      if (mounted.current) setError(new ApiError(reviewError(cause), cause.status))
    } finally {
      working.current = false
      if (mounted.current) setBusy(false)
    }
  }

  async function reload() {
    if (working.current) return
    working.current = true
    setBusy(true)
    try {
      setConflict(false)
      setError(null)
      setFeedback('')
      await client.resetQueries({ queryKey: ['review', taskId], exact: true })
      await Promise.all([snapshot.refetch(), history.refetch()])
    } finally {
      working.current = false
      if (mounted.current) setBusy(false)
    }
  }

  const decision = current.data?.latest_review
  const settled = decision && ['accepted', 'rejected'].includes(current.data?.status)
    && !current.isError && !snapshot.isError && !changed && !conflict
  const showEditor = !settled || editingDecision || Boolean(pending) || Boolean(comment)

  return <div className="review-layout">
    <header className="review-statusbar">
      <div className="review-current"><span>当前状态</span>{current.isError ? <span>暂无法读取</span> : current.data ? <ReviewBadge value={current.data.status} /> : <span>正在读取…</span>}<span className="muted">{current.data ? `已记录 ${current.data.revision} 次审查` : ''}</span></div>
      <button type="button" className="button button-quiet button-small" disabled={busy || snapshot.isFetching} onClick={reload}><RefreshCw size={14} className={snapshot.isFetching ? 'spin' : ''} />重新加载审查资料</button>
    </header>
    {current.isError && <ErrorNotice error={new ApiError(reviewError(current.error), current.error.status)} onRetry={() => current.refetch()} />}
    {frozen && reviewReason(frozen) && <p className="notice notice-warning">{reviewReason(frozen)}</p>}
    {(changed || conflict) && <p className="notice notice-warning" role="alert">审查资料或其他审查决定已更新。你的意见已保留，请重新加载资料后核对。</p>}
    {snapshot.isPending && <LoadingState label="正在核对审查资料…" />}
    {snapshot.isError && <ErrorNotice error={new ApiError(reviewError(snapshot.error), snapshot.error.status)} onRetry={reload} />}

    <section className="panel review-step review-context">
      <ReviewStepHeading number="01" title="任务要求" />
      {snapshot.data?.task ? <>
        <p className="review-goal">{snapshot.data.presentation?.goal ?? snapshot.data.task.goal}</p>
        {snapshot.data.presentation?.source && <IssueSourcePanel source={snapshot.data.presentation.source}
          baseSha={snapshot.data.task.base_sha} repoPath={snapshot.data.task.repo_path} collapseBody embedded />}
      </> : <p className="review-help">可核对的任务资料将在修复结束后显示。</p>}
    </section>

    <section className="panel review-step review-inspection">
      <ReviewStepHeading number="02" title="检查修改" />
      <DiffViewer patch={snapshot.data?.patch} embedded downloadable={Boolean(snapshot.data?.patch)} />
      <ValidationPanel validation={snapshot.data?.validation} compact embedded />
      {snapshot.data?.task && <details className="review-verification"><summary>查看验证命令</summary><VerificationCommand command={snapshot.data.task.verification.command} /></details>}
      <EvidenceDetails evidence={frozen?.evidence} label="详细记录" />
    </section>

    <section className="panel review-step review-form" aria-label="提交人工审查">
      <ReviewStepHeading number="03" title="审查决定" />
      {settled && <div className="review-decision-summary"><div><ReviewBadge value={decision.decision} /><time dateTime={decision.created_at}>{formatDate(decision.created_at)}</time><span>第 {decision.revision} 次审查</span></div><p className="review-comment">{decision.comment || '未填写意见。'}</p>{!showEditor && <button type="button" className="button button-quiet button-small" disabled={busy} onClick={() => setEditingDecision(true)}>修改审查决定</button>}</div>}
      {showEditor && <>
        <div className="field"><label htmlFor="review-comment">意见（选填）</label><textarea id="review-comment" value={comment} rows={3} maxLength={4000} disabled={busy || Boolean(pending)} aria-describedby="review-comment-hint" placeholder="说明是否符合要求，以及需要调整的内容。" onChange={(event) => { setComment(event.target.value); client.setQueryData(['review-comment', taskId], event.target.value); setFeedback('') }} /><div className="field-hint" id="review-comment-hint">{comment.length} / 4000</div></div>
        <div className="review-actions"><button type="button" className="button button-quiet review-reject" disabled={blocked || !frozen?.can_reject || frozen.status === 'rejected'} onClick={() => decide('rejected')}><X size={16} />拒绝补丁</button>
          <button type="button" className="button button-primary" disabled={blocked || !frozen?.can_accept || frozen.status === 'accepted'} onClick={() => decide('accepted')}><Check size={16} />接受补丁</button>
          {settled && !pending && <button type="button" className="button button-quiet button-small" disabled={busy} onClick={() => { setEditingDecision(false); setComment(''); client.removeQueries({ queryKey: ['review-comment', taskId], exact: true }) }}>取消修改</button>}
          {busy && <span className="review-help" role="status">正在处理审查…</span>}</div>
      </>}
      {pending && <div className="notice notice-warning review-retry"><div><strong>有一条审查提交尚未确认</strong><p>核对结果或重试原请求后，再提交新的决定。</p></div><div className="review-actions"><button type="button" className="button button-quiet button-small" disabled={busy} onClick={reconcile}>核对提交结果</button><button type="button" className="button button-quiet button-small" disabled={busy} onClick={() => send(pending, true)}>重试原提交</button></div></div>}
      {error && <ErrorNotice error={error} />}
      {feedback && <p className="review-feedback" role="status">{feedback}</p>}
      <details className="review-history"><summary>审查历史{history.data ? `（${history.data.items.length}）` : ''}</summary>
        {history.isPending && <LoadingState label="正在读取审查历史…" />}
        {history.isError && <ErrorNotice error={new ApiError(reviewError(history.error), history.error.status)} onRetry={() => history.refetch()} />}
        {!history.isPending && !history.isError && !history.data?.items.length && <p className="review-help">暂无审查记录。</p>}
        {history.data?.items.length > 0 && <ol aria-label="审查历史">{history.data.items.slice().reverse().map((item) => <li key={item.review_id}>
          <div className="review-history-heading"><ReviewBadge value={item.decision} /><span>第 {item.revision} 次审查</span><time dateTime={item.created_at} title={item.created_at}>{formatDate(item.created_at)}</time></div>
          <p className="review-comment">{item.comment || '未填写意见。'}</p><EvidenceDetails evidence={item.evidence} />
        </li>)}</ol>}
      </details>
    </section>

    <DeliveryPanel key={`${taskId}-${current.data?.revision ?? 0}-${current.data?.status ?? 'unknown'}`} taskId={taskId} reviewState={current.isError ? null : current.data} variant="review" githubSource={source} />
  </div>
}
