import { useEffect, useRef, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate } from 'react-router-dom'
import { ArrowLeft, ArrowUpRight, FlaskConical, LoaderCircle } from 'lucide-react'
import { createTask } from '../lib/api'
import { buildTaskPayload, defaultForm, modelPresets, validateTaskForm } from '../lib/task'
import { ErrorNotice } from '../components/ui'
import TaskConfiguration, { Field } from '../components/TaskConfiguration'

export default function NewTask() {
  const [form, setForm] = useState({ ...defaultForm })
  const [errors, setErrors] = useState({})
  const submitting = useRef(false)
  const mounted = useRef(true)
  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false }
  }, [])
  const mutation = useMutation({ mutationFn: createTask, networkMode: 'always', retry: false })
  const queryClient = useQueryClient()
  const navigate = useNavigate()

  function change(name, value) {
    const next = { ...form, [name]: value }
    if (name === 'provider') next.model = modelPresets[value]
    setForm(next)
    if (errors[name]) setErrors({ ...errors, [name]: validateTaskForm(next)[name] })
  }

  function input(name, extra = {}) {
    return { id: `task-${name}`, name, value: form[name],
      onChange: (event) => change(name, event.target.value),
      onBlur: () => setErrors((current) => ({ ...current, [name]: validateTaskForm(form)[name] })),
      'aria-invalid': Boolean(errors[name]), 'aria-describedby': `task-${name}-hint`, ...extra }
  }

  function fillExample() {
    setForm({ ...defaultForm, repo_path: 'examples/toy_repo',
      goal: '支持版本号字符串的 v 前缀，不改变现有行为。不要修改 tests/ 目录。', max_model_calls: '10' })
    setErrors({})
    mutation.reset()
  }

  async function submit(event) {
    event.preventDefault()
    if (submitting.current) return
    const found = validateTaskForm(form)
    setErrors(found)
    if (Object.keys(found).length) {
      const element = document.getElementById(`task-${Object.keys(found)[0]}`)
      const details = element?.closest('details')
      if (details) details.open = true
      element?.focus()
      return
    }
    submitting.current = true
    try {
      const task = await mutation.mutateAsync(buildTaskPayload(form))
      queryClient.setQueryData(['task', task.task_id], task)
      queryClient.invalidateQueries({ queryKey: ['tasks'] })
      if (mounted.current) navigate(`/tasks/${task.task_id}`)
    } catch {
      // The mutation retains the error and form values for the user to inspect.
    } finally {
      submitting.current = false
    }
  }

  return <div className="page new-task-page">
    <Link className="back-link" to="/tasks"><ArrowLeft size={15} />返回任务列表</Link>
    <div className="page-heading"><div><span className="eyebrow">A NEW REPAIR</span><h1 id="page-title" tabIndex={-1}>描述问题，开始修复</h1><p>配置仓库、修复要求和验证命令。</p></div></div>
    <form onSubmit={submit} noValidate className="create-form">
      <fieldset disabled={mutation.isPending}>
        <section className="form-section"><div className="section-heading"><span className="step-number">01</span><div><h2>修复什么</h2><p>任务会固定在提交时的 Git 版本。</p></div><button className="button button-quiet button-small example-button" type="button" onClick={fillExample}><FlaskConical size={14} />填入示例</button></div>
          <Field name="repo_path" label="仓库目录" hint="运行服务的电脑上的 Git 仓库路径，也支持相对于服务启动目录的路径。" error={errors.repo_path}><input {...input('repo_path', { placeholder: '例如 examples/toy_repo', maxLength: 4096, spellCheck: false, autoComplete: 'off' })} /></Field>
          <Field name="goal" label="修复要求" hint="写清出现的问题、预期行为，以及需要保留的约束。" error={errors.goal}><textarea {...input('goal', { rows: 5, maxLength: 20000, placeholder: '例如：版本号解析不支持 v 前缀。请修复这个问题，保持已有行为，不修改测试。' })} /></Field>
          <Field name="verification_command" label="验证命令" hint="在干净基线和候选补丁上分别执行，用于确认问题是否被修复。" error={errors.verification_command}><input className="mono-input" {...input('verification_command', { maxLength: 20000, spellCheck: false })} /></Field>
        </section>
        <TaskConfiguration form={form} errors={errors} input={input} change={change} disabled={mutation.isPending} />
      </fieldset>
      {mutation.isError && <ErrorNotice error={mutation.error}><Link to="/tasks">查看任务列表</Link>，核对是否已经创建成功。</ErrorNotice>}
      <div className="form-actions"><p>任务提交后，可以在详情页跟踪进度。</p><button className="button button-primary" type="submit" disabled={mutation.isPending}>{mutation.isPending ? <LoaderCircle size={16} className="spin" /> : <ArrowUpRight size={17} />}{mutation.isPending ? '正在提交…' : '提交任务'}</button></div>
    </form>
  </div>
}
