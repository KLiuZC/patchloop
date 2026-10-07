import { useInfiniteQuery } from '@tanstack/react-query'
import { Link, useSearchParams } from 'react-router-dom'
import { ArrowDown, ArrowRight, CheckCheck, FolderGit2, Layers3, Plus, RefreshCw } from 'lucide-react'
import { listTasks } from '../lib/api'
import { executionStates, formatDate, shortPath } from '../lib/task'
import { EmptyState, ErrorNotice, LoadingState, StatusBadge } from '../components/ui'
import Select from '../components/Select'

const statusOptions = [{ value: '', label: '所有执行状态' }, ...Object.entries(executionStates).map(([value, info]) => ({ value, label: info.label }))]

export default function TaskList() {
  const [params, setParams] = useSearchParams()
  const rawStatus = params.get('status')
  const status = Object.hasOwn(executionStates, rawStatus) ? rawStatus : undefined
  const query = useInfiniteQuery({
    queryKey: ['tasks', status || 'all'], initialPageParam: null,
    queryFn: ({ pageParam, signal }) => listTasks({ status, cursor: pageParam, signal }),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    refetchInterval: 3000,
  })
  const tasks = [...new Map((query.data?.pages.flatMap((page) => page.items) || []).map((task) => [task.task_id, task])).values()]
  function changeFilter(value) {
    setParams(value ? { status: value } : {})
  }

  return <div className="page list-page">
    <div className="page-heading"><div><span className="eyebrow">YOUR WORKSPACE</span><h1 id="page-title" tabIndex={-1}>修复任务</h1><p>跟踪每一步，查看每一次改动与验证。</p></div>
      <Link to="/tasks/new" className="button button-primary"><Plus size={17} />新建任务</Link></div>
    <div className="workspace-intro"><div className="intro-mark"><CheckCheck size={23} strokeWidth={1.6} /></div><div><strong>让结果有据可查</strong><p>模型完成执行后，补丁会在干净环境中再次验证。</p></div><span className="intro-label">独立验证</span></div>
    <section className="task-panel" aria-label="任务列表">
      <div className="list-toolbar"><div className="toolbar-title"><Layers3 size={17} /><strong>所有修复任务</strong><span className="count-chip">已加载 {tasks.length} 项</span></div>
        <div className="toolbar-actions"><Select aria-label="执行状态筛选" value={status || ''} onValueChange={changeFilter} options={statusOptions} size="compact" />
          <button type="button" className="icon-button" title="刷新任务" aria-label="刷新任务" disabled={query.isFetching} onClick={() => query.refetch()}><RefreshCw size={16} className={query.isFetching ? 'spin' : ''} /></button></div></div>
      {query.isError && <ErrorNotice error={query.error} onRetry={() => query.refetch()}>{tasks.length ? '以下为上次成功读取的记录。' : '请确认任务服务已启动。'}</ErrorNotice>}
      {query.isPending ? <LoadingState /> : tasks.length ? <>
        <div className="task-table-header" aria-hidden="true"><span>任务 / 仓库</span><span>执行状态</span><span>验证结果</span><span>创建时间</span><span /></div>
        <ul className="task-list">{tasks.map((task) => <li key={task.task_id}><Link className="task-row" to={`/tasks/${task.task_id}`}>
          <div className="task-identity"><div className={`task-icon ${task.validation_status === 'passed' ? 'task-icon-success' : ''}`}><FolderGit2 size={19} strokeWidth={1.7} /></div><div className="task-title-block"><strong title={task.goal}>{task.goal}</strong><span className="task-path" title={`${task.repo_path}/${task.workdir}`}>{shortPath(task)}</span></div></div>
          <div className="row-state"><StatusBadge value={task.status} /></div><div className="row-validation"><StatusBadge value={task.validation_status} validation /></div><time className="row-time" dateTime={task.created_at}>{formatDate(task.created_at)}</time><ArrowRight size={16} className="row-arrow" aria-hidden="true" />
        </Link></li>)}</ul>
        <div className="list-bottom"><span>按创建时间排列 · 最新优先</span>{query.hasNextPage ? <button className="button button-quiet button-small" onClick={() => query.fetchNextPage()} disabled={query.isFetching}><ArrowDown size={14} />{query.isFetchingNextPage ? '正在加载…' : '加载更多'}</button> : <span>已显示全部 {tasks.length} 项</span>}</div>
      </> : !query.isError && <EmptyState icon={FolderGit2} title={status ? '没有符合条件的任务' : '开始你的第一次修复'} action={status ? <button className="button button-quiet" onClick={() => changeFilter('')}>查看全部任务</button> : <Link to="/tasks/new" className="button button-primary"><Plus size={16} />新建任务</Link>}>{status ? '可以切换执行状态，查看其他任务。' : '选择一个仓库，描述需要修复的问题。补丁和验证结果会保存在这里。'}</EmptyState>}
    </section>
    <p className="list-footnote"><span className="small-dot" />页面自动更新 · 查看任务不会重新执行修复</p>
  </div>
}
