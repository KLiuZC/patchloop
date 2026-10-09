import { useCallback, useEffect, useRef, useState } from 'react'
import { GitBranch, RefreshCw } from 'lucide-react'
import { ApiError } from '../lib/api'
import {
  acceptsDeliveryReview, clearPendingDelivery, deliveryReason, getDelivery, matchesDelivery,
  readPendingDelivery, sameDeliveryPlan, savePendingDelivery, submitDelivery,
} from '../lib/delivery'
import { CopyButton, ErrorNotice, LoadingState } from './ui'
import ReviewStepHeading from './ReviewStepHeading'

const changedMessage = '审查、依据或交付计划已变化，请刷新交付计划后重新核对。'
const terminal = (record) => record && record.status !== 'preparing'
const recordedRequest = (record) => ({
  request_id: record.request_id, review_id: record.review_id, review_revision: record.review_revision,
  evidence: { ...record.evidence }, target_head: record.target_head,
})

export default function DeliveryPanel({ taskId, reviewState, variant = 'default' }) {
  return <DeliverySession key={taskId} taskId={taskId} reviewState={reviewState} variant={variant} />
}

function DeliverySession({ taskId, reviewState, variant }) {
  const [plan, setPlan] = useState(null)
  const [pending, setPending] = useState(() => readPendingDelivery(taskId))
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [readError, setReadError] = useState(null)
  const [conflict, setConflict] = useState(false)
  const [feedback, setFeedback] = useState('')
  const mounted = useRef(false)
  const working = useRef(false)
  const generation = useRef(0)
  const terminalRecords = useRef(new Map())
  const latestReview = useRef(reviewState)
  useEffect(() => { latestReview.current = reviewState }, [reviewState])

  const receive = useCallback((state) => {
    const incoming = state.delivery
    const known = incoming && terminalRecords.current.get(incoming.delivery_id)
    // An older GET must never turn an observed completed delivery back into
    // preparing (or offer to resume it). Terminal records are immutable.
    if (known && (incoming.status === 'preparing' || known.status === 'completed')) {
      state = { ...state, delivery: known,
        can_deliver: state.review_id === known.review_id ? false : state.can_deliver }
    } else if (terminal(incoming)) {
      terminalRecords.current.set(incoming.delivery_id, incoming)
    }
    const saved = readPendingDelivery(taskId)
    const matching = saved && matchesDelivery(state.delivery, saved, taskId)
    let resolution = null
    if (saved && terminal(state.delivery) && matching) {
      clearPendingDelivery(taskId, saved.request_id)
      resolution = 'resolved'
    } else if (saved && state.task_id === taskId && state.review_revision > saved.review_revision && !matching) {
      // Review revisions only increase. An unrecorded request for an older
      // approval cannot subsequently publish, because the server checks the
      // current approval before creating a record and before publishing.
      // A matching preparing record is kept for explicit recovery instead.
      clearPendingDelivery(taskId, saved.request_id)
      resolution = 'superseded'
    }
    // After a browser/process restart, the server's existing request can be
    // resumed explicitly. Merely adopting it never sends a POST.
    if (!readPendingDelivery(taskId) && state.delivery?.status === 'preparing') {
      savePendingDelivery(taskId, recordedRequest(state.delivery))
    }
    if (mounted.current) {
      setPlan(state)
      setPending(readPendingDelivery(taskId))
      setReadError(null)
      if (resolution === 'superseded') setFeedback('审查已更新，旧交付请求的待确认状态已解除。')
    }
    return resolution
  }, [taskId])

  useEffect(() => {
    mounted.current = true
    const controller = new AbortController()
    const version = ++generation.current
    getDelivery(taskId, { signal: controller.signal }).then((state) => {
      if (!controller.signal.aborted && version === generation.current) receive(state)
    }).catch((cause) => {
      if (!controller.signal.aborted && version === generation.current) setReadError(cause)
    }).finally(() => {
      if (!controller.signal.aborted && version === generation.current) setLoading(false)
    })
    return () => { mounted.current = false; controller.abort() }
  }, [taskId, receive])

  // Poll only an observed operation; refreshing or reopening never submits a request.
  useEffect(() => {
    if (plan?.delivery?.status !== 'preparing' || busy) return
    const controller = new AbortController()
    const version = generation.current
    let timer
    const poll = async () => {
      let again = true
      try {
        const state = await getDelivery(taskId, { signal: controller.signal })
        again = state.delivery?.status === 'preparing'
        if (!controller.signal.aborted && version === generation.current) receive(state)
      } catch (cause) {
        if (!controller.signal.aborted && version === generation.current) setReadError(cause)
      }
      if (again && !controller.signal.aborted && version === generation.current) timer = setTimeout(poll, 1000)
    }
    timer = setTimeout(poll, 1000)
    return () => { clearTimeout(timer); controller.abort() }
  }, [plan?.delivery?.status, busy, taskId, receive])

  const record = plan?.delivery
  const accepted = acceptsDeliveryReview(reviewState, plan)
  const ownsRecord = record && record.review_id === plan?.review_id
  const preparing = record?.status === 'preparing'
  const recoveryBlocked = loading || busy || Boolean(readError) || conflict
  const blocked = loading || busy || Boolean(readError) || conflict || !accepted || !plan?.can_deliver
  const canRetry = pending && matchesDelivery(plan, pending, taskId) && !ownsRecord && !preparing
  const reason = deliveryReason(plan?.reason)
  const summaryOnly = variant === 'review' && ownsRecord && record.status === 'completed'

  async function refresh(reconcile = false) {
    if (working.current) return
    working.current = true
    generation.current += 1
    setBusy(true)
    setError(null)
    setFeedback('')
    try {
      const state = await getDelivery(taskId)
      if (!mounted.current) return
      const resolution = receive(state)
      setConflict(false)
      if (reconcile && pending && !resolution && !matchesDelivery(state.delivery, pending, taskId)) {
        setFeedback('尚未确认这次交付。计划仍一致时，可重试原请求。')
      }
    } catch (cause) {
      if (mounted.current) setReadError(cause)
    } finally {
      working.current = false
      if (mounted.current) { setBusy(false); setLoading(false) }
    }
  }

  async function send(mode = 'new') {
    const recovering = mode === 'recover'
    if (working.current || (recovering ? recoveryBlocked || !preparing
      : blocked || (mode === 'retry' ? !canRetry : pending || ownsRecord || preparing))) return
    working.current = true
    generation.current += 1
    setBusy(true)
    setError(null)
    setFeedback('')
    let body
    let sent = false
    let checked = false
    try {
      body = recovering ? recordedRequest(record) : mode === 'retry' ? pending : {
        request_id: crypto.randomUUID(), review_id: plan.review_id, review_revision: plan.review_revision,
        evidence: { ...plan.evidence }, target_head: plan.target_head,
      }
      const current = await getDelivery(taskId)
      checked = true
      if (!mounted.current) return
      // A second tab may already have delivered this review. Reconcile without another POST.
      const matchingRecord = matchesDelivery(current.delivery, body, taskId)
      if (matchingRecord && !(recovering && current.delivery.status === 'preparing')) {
        receive(current)
        return
      }
      if (recovering) {
        // Recovery refers only to an existing record. The server decides whether
        // published output can be recovered without the original approval.
        if (!matchingRecord || current.delivery.delivery_id !== record.delivery_id
          || current.delivery.request_id !== body.request_id) throw new ApiError(changedMessage, 409)
      } else if (!sameDeliveryPlan(plan, current) || !acceptsDeliveryReview(latestReview.current, current)
        || !current.can_deliver || (!matchingRecord && (current.delivery?.review_id === body.review_id
        || current.delivery?.status === 'preparing'))) throw new ApiError(changedMessage, 409)
      savePendingDelivery(taskId, body)
      setPending(body)
      sent = true
      const response = await submitDelivery(taskId, body)
      // receive also clears persisted requests when the user has left the panel.
      receive({ ...current, can_deliver: false, delivery: response.delivery, delivery_intact: null })
    } catch (cause) {
      if (mounted.current) {
        if (!checked && body) setReadError(cause)
        else setError(new ApiError(cause.status === 409
          ? '交付未确认，可能有其他操作正在进行。请核对结果或刷新交付计划。'
          : cause.message || '无法提交交付请求，请核对结果。', cause.status))
        if (cause.status === 409) {
          setConflict(true)
          if (!sent) setError(new ApiError(changedMessage, 409))
        }
      }
      // 409 also means an operation is preparing; discover it and start GET polling.
      if (sent && cause.status === 409) {
        try {
          const current = await getDelivery(taskId)
          if (matchesDelivery(current.delivery, body, taskId)) {
            receive(current)
            if (mounted.current) { setError(null); setConflict(false) }
          }
        } catch (readFailure) {
          if (mounted.current) setReadError(readFailure)
        }
      }
    } finally {
      working.current = false
      if (mounted.current) setBusy(false)
    }
  }

  const planFields = plan && (
      <dl className="delivery-fields">
        <dt>补丁基线</dt><dd><code>{plan.evidence?.base_sha || '暂不可用'}</code></dd>
        <dt>目标 HEAD</dt><dd><code>{plan.target_head || (ownsRecord && record.target_head) || '暂不可用'}</code></dd>
        <dt>修改分支</dt><dd><code>{plan.branch || (ownsRecord && record.branch) || '暂不可用'}</code></dd>
        <dt>工作区路径</dt><dd><code>{plan.worktree_path || (ownsRecord && record.worktree_path) || '暂不可用'}</code></dd>
        <dt>允许修改路径</dt><dd>{plan.policy.allowed_paths.length ? plan.policy.allowed_paths.join('、') : '不限路径（受保护路径除外）'}</dd>
        <dt>受保护路径</dt><dd>{plan.policy.protected_paths.length ? plan.policy.protected_paths.join('、') : '未配置'}</dd>
        <dt>修改文件</dt><dd>{plan.changed_paths.length ? <ul>{plan.changed_paths.map((path, index) => <li key={`${index}:${path}`}><code>{path}</code></li>)}</ul> : '暂无'}</dd>
      </dl>
  )

  return <section className={`panel delivery-panel${variant === 'review' ? ' review-step' : ''}`} aria-label="本地交付">
    {variant === 'review' ? <ReviewStepHeading number="04" title="本地交付"><button type="button" className="button button-quiet button-small" disabled={busy || loading} onClick={() => refresh()}><RefreshCw size={14} aria-hidden="true" />刷新交付计划</button></ReviewStepHeading>
      : <div className="panel-heading"><h2><GitBranch size={18} aria-hidden="true" />本地交付</h2><button type="button" className="button button-quiet button-small" disabled={busy || loading} onClick={() => refresh()}><RefreshCw size={14} aria-hidden="true" />刷新交付计划</button></div>}
    {!summaryOnly && <p className="muted">接受补丁后，可生成独立修改分支。</p>}
    {loading && <LoadingState label="正在读取交付计划…" />}
    {readError && <ErrorNotice error={readError} />}
    {summaryOnly && !accepted && <p className="notice notice-warning">当前审查与交付记录不一致，请刷新交付计划核对。已有交付结果保留。</p>}
    {summaryOnly && plan.reason && plan.reason !== 'delivery already completed' && <div className="notice notice-warning" role="alert"><p>{reason || plan.reason}</p></div>}
    {plan && !summaryOnly && <>
      {planFields}
      <p className={plan.dirty ? 'notice notice-warning' : 'muted'}>{plan.dirty ? '原工作区有未提交改动；交付会保留原改动，在独立工作区生成提交。' : plan.target_head ? '原工作区干净；交付将在独立工作区生成提交。' : '本地交付使用独立工作区，原工作区改动会保留。'}</p>
      {!accepted && <p className="notice notice-warning">仅最新且依据一致的已接受审查可交付。审查更新后，请刷新交付计划。</p>}
      {!plan.can_deliver && plan.reason && <div className="notice notice-warning"><div><p>{reason || '当前暂不可交付。'}</p>
        {!reason && <details><summary>查看原因</summary><p>{plan.reason}</p></details>}</div></div>}
    </>}
    {record && <div>
      <p role="status" className={summaryOnly ? 'delivery-complete-status' : undefined}>{record.status === 'preparing' ? '本地交付进行中，正在核对结果…' : record.status === 'completed' ? '本地交付已完成。' : '本地交付失败。'}</p>
      <dl className="delivery-fields">
        <dt>交付审查</dt><dd>第 {record.review_revision} 次审查{!ownsRecord && '（历史交付）'}</dd>
        <dt>交付分支</dt><dd><code>{record.branch}</code></dd>
        <dt>交付工作区</dt><dd className="delivery-copy-value"><code>{record.worktree_path}</code>{record.status === 'completed' && <CopyButton value={record.worktree_path} label="复制工作区路径" />}</dd>
        {record.status === 'completed' && <><dt>提交 SHA</dt><dd className="delivery-copy-value"><code>{record.commit_sha || '尚无提交编号'}</code>{record.commit_sha && <CopyButton value={record.commit_sha} label="复制提交 SHA" />}</dd></>}
      </dl>
      {record.status === 'failed' && <ErrorNotice error={{ message: record.error || '交付失败，请核查本地工作区后重新审查。' }} />}
      {record.status === 'completed' && plan.delivery_intact === false && <p className="notice notice-warning" role="alert">交付分支或工作区已变化，请核查本地文件。</p>}
      {record.status === 'completed' && plan.delivery_intact === null && <p className="muted">可刷新交付计划，核对分支和工作区是否完整。</p>}
    </div>}
    {summaryOnly && <details className="delivery-plan-details"><summary>交付详细记录</summary>{planFields}</details>}
    {pending && <div className="notice notice-warning">
      <div><strong>{preparing ? '交付请求已保留' : '有一条交付请求尚未确认'}</strong><p>{preparing ? '正在定期核对结果。若交付进程已中断，可手动恢复交付。' : '原请求已保留。核对结果或手动重试后，再发起新的交付。'}</p>
        <div className="delivery-actions"><button type="button" className="button button-quiet button-small" disabled={busy || loading} onClick={() => refresh(true)}>核对交付结果</button>
          {!preparing && <button type="button" className="button button-quiet button-small" disabled={blocked || !canRetry} onClick={() => send('retry')}>重试原交付</button>}</div>
      </div>
    </div>}
    {error && <ErrorNotice error={error} />}
    {feedback && <p role="status">{feedback}</p>}
    {!summaryOnly && <div className="delivery-actions"><button type="button" className="button button-primary" disabled={blocked || Boolean(pending) || Boolean(ownsRecord) || preparing} onClick={() => send()}><GitBranch size={16} aria-hidden="true" />生成修改分支</button>
      {preparing && <button type="button" className="button button-quiet" disabled={recoveryBlocked} onClick={() => send('recover')}>恢复交付</button>}
      {busy && <span className="muted" role="status">正在核对交付…</span>}</div>}
  </section>
}
