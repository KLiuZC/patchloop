import { useEffect, useRef, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate } from 'react-router-dom'
import { ArrowLeft, ArrowUpRight, ChevronDown, FlaskConical, LoaderCircle, Play, Settings2, ShieldCheck, Sparkles } from 'lucide-react'
import { motion as Motion } from 'motion/react'
import { createTask } from '../lib/api'
import { buildTaskPayload, defaultForm, modelPresets, validateTaskForm } from '../lib/task'
import { ErrorNotice } from '../components/ui'
import Select from '../components/Select'

const providerOptions = [{ value: 'deepseek', label: 'DeepSeek' }, { value: 'glm', label: '智谱 GLM' }]
const platformOptions = [
  { value: '', label: '按本地镜像自动选择' },
  { value: 'linux/arm64', label: 'linux/arm64' },
  { value: 'linux/amd64', label: 'linux/amd64' },
]

function Field({ name, label, hint, error, children }) {
  return <div className={`field ${error ? 'field-error' : ''}`}><label htmlFor={`task-${name}`}>{label}</label>{children}<div id={`task-${name}-hint`} className="field-hint">{error ? <span role="alert">{error}</span> : hint}</div></div>
}

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
    <div className="page-heading"><div><span className="eyebrow">A NEW REPAIR</span><h1 id="page-title" tabIndex={-1}>描述问题，开始修复</h1><p>指定仓库和验证方式，让每一步都有明确的目标。</p></div></div>
    <div className="create-layout">
      <form onSubmit={submit} noValidate className="create-form">
        <fieldset disabled={mutation.isPending}>
          <section className="form-section"><div className="section-heading"><span className="step-number">01</span><div><h2>修复什么</h2><p>任务会固定在提交时的 Git 版本。</p></div><button className="button button-quiet button-small example-button" type="button" onClick={fillExample}><FlaskConical size={14} />填入示例</button></div>
            <Field name="repo_path" label="仓库目录" hint="运行服务的电脑上的 Git 仓库路径，也支持相对于服务启动目录的路径。" error={errors.repo_path}><input {...input('repo_path', { placeholder: '例如 examples/toy_repo', maxLength: 4096, spellCheck: false, autoComplete: 'off' })} /></Field>
            <Field name="goal" label="修复要求" hint="写清出现的问题、预期行为，以及需要保留的约束。" error={errors.goal}><textarea {...input('goal', { rows: 5, maxLength: 20000, placeholder: '例如：版本号解析不支持 v 前缀。请修复这个问题，保持已有行为，不修改测试。' })} /></Field>
            <Field name="verification_command" label="验证命令" hint="在干净基线和候选补丁上分别执行，用于确认问题是否被修复。" error={errors.verification_command}><input className="mono-input" {...input('verification_command', { maxLength: 20000, spellCheck: false })} /></Field>
          </section>
          <section className="form-section"><div className="section-heading"><span className="step-number">02</span><div><h2>如何执行</h2><p>选择模型，并设置这次任务的预算。</p></div></div>
            <div className="field-grid"><Field name="provider" label="模型提供方"><Select id="task-provider" name="provider" value={form.provider} onValueChange={(value) => change('provider', value)} options={providerOptions} disabled={mutation.isPending} aria-describedby="task-provider-hint" /></Field>
              <Field name="model" label="模型名称" hint="可自定义；留空时由服务选择默认模型。" error={errors.model}><input {...input('model', { maxLength: 256, spellCheck: false })} /></Field></div>
            <div className="field-grid"><Field name="image" label="Docker 镜像" hint="使用本机已准备好的镜像。" error={errors.image}><input {...input('image', { maxLength: 512, spellCheck: false })} /></Field>
              <Field name="max_model_calls" label="最大模型轮数" hint="1–200 轮，到达上限后保存已有补丁。" error={errors.max_model_calls}><input {...input('max_model_calls', { type: 'number', min: 1, max: 200, step: 1 })} /></Field></div>
          </section>
          <details className="advanced-settings"><summary><Settings2 size={16} /><span>高级设置</span><ChevronDown size={16} /></summary><div className="advanced-body"><div className="field-grid">
            <Field name="command_timeout_seconds" label="单条命令超时（秒）" error={errors.command_timeout_seconds}><input {...input('command_timeout_seconds', { type: 'number', min: 1, max: 3600, step: 1 })} /></Field>
            <Field name="verification_timeout_seconds" label="验证命令超时（秒）" error={errors.verification_timeout_seconds}><input {...input('verification_timeout_seconds', { type: 'number', min: 1, max: 3600, step: 1 })} /></Field>
            <Field name="memory_mb" label="容器内存（MB）" error={errors.memory_mb}><input {...input('memory_mb', { type: 'number', min: 256, max: 32768, step: 1 })} /></Field>
            <Field name="platform" label="镜像架构"><Select id="task-platform" name="platform" value={form.platform} onValueChange={(value) => change('platform', value)} options={platformOptions} disabled={mutation.isPending} aria-describedby="task-platform-hint" /></Field>
          </div><Field name="allowed_paths" label="允许修改的路径" hint="每行一个仓库根目录相对模式，例如 src/**。留空表示不额外限定范围。" error={errors.allowed_paths}><textarea className="mono-input" {...input('allowed_paths', { rows: 2, spellCheck: false })} /></Field>
          <Field name="protected_paths" label="禁止修改的路径" hint="默认保护常见测试路径。每行一个模式，例如 tests/**；规则会固定到本次任务，验证和交付时强制检查。" error={errors.protected_paths}><textarea className="mono-input" {...input('protected_paths', { rows: 4, spellCheck: false })} /></Field>
          <label className="checkbox-label"><input type="checkbox" checked={form.budget_reminders} onChange={(event) => change('budget_reminders', event.target.checked)} />启用剩余轮数提醒</label><label className="checkbox-label"><input type="checkbox" checked={form.explicit_finish} onChange={(event) => change('explicit_finish', event.target.checked)} />要求模型通过 finish 工具交卷</label></div></details>
        </fieldset>
        {mutation.isError && <ErrorNotice error={mutation.error}><Link to="/tasks">查看任务列表</Link>，核对是否已经创建成功。</ErrorNotice>}
        <div className="form-actions"><p>任务提交后，可以在详情页跟踪进度。</p><button className="button button-primary" type="submit" disabled={mutation.isPending}>{mutation.isPending ? <LoaderCircle size={16} className="spin" /> : <ArrowUpRight size={17} />}{mutation.isPending ? '正在提交…' : '提交任务'}</button></div>
      </form>
      <Motion.aside className="submission-aside" initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} transition={{ type: 'spring', bounce: 0, duration: 0.35 }}>
        <div className="aside-symbol"><Sparkles size={25} strokeWidth={1.6} /></div><span className="eyebrow">REPAIR, WITH EVIDENCE</span><h2>一个任务，<br />一份可核对的结果。</h2><p>修改在独立副本中生成。任务结束后，你会得到补丁和独立验证记录。</p>
        <ol className="process-list"><li><span>1</span><div><strong>提交任务</strong><small>保存目标、版本和执行配置</small></div></li><li><span>2</span><div><strong>后台修复</strong><small>模型检查代码、修改并测试</small></div></li><li><span>3</span><div><strong>核对结果</strong><small>查看改动及修复前后的验证</small></div></li></ol>
        <div className="submission-summary"><span>本次执行预算</span><strong>{Number(form.max_model_calls) || '—'} <small>轮以内</small></strong><p>{form.provider === 'glm' ? '智谱 GLM' : 'DeepSeek'} · {form.model || '服务默认模型'}</p></div>
        <div className="aside-note"><Play size={15} /><p>任务由独立 worker 领取。实际执行会产生模型调用费用。</p></div><div className="aside-note"><ShieldCheck size={15} /><p>完成执行、通过验证、人工审查分别记录。</p></div>
      </Motion.aside>
    </div>
  </div>
}
