import { ChevronDown, Settings2 } from 'lucide-react'
import Select from './Select'

const providerOptions = [{ value: 'deepseek', label: 'DeepSeek' }, { value: 'glm', label: '智谱 GLM' }]
const platformOptions = [
  { value: '', label: '按本地镜像自动选择' },
  { value: 'linux/arm64', label: 'linux/arm64' },
  { value: 'linux/amd64', label: 'linux/amd64' },
]

export function Field({ name, label, hint, error, children }) {
  return <div className={`field ${error ? 'field-error' : ''}`}><label htmlFor={`task-${name}`}>{label}</label>{children}<div id={`task-${name}-hint`} className="field-hint">{error ? <span role="alert">{error}</span> : hint}</div></div>
}

export default function TaskConfiguration({ form, errors, input, change, disabled, stepLabel = '02' }) {
  return <>
          <section className="form-section"><div className="section-heading">{stepLabel && <span className="step-number">{stepLabel}</span>}<div><h2>如何执行</h2><p>选择模型，并设置这次任务的预算。</p></div></div>
            <div className="field-grid"><Field name="provider" label="模型提供方"><Select id="task-provider" name="provider" value={form.provider} onValueChange={(value) => change('provider', value)} options={providerOptions} disabled={disabled} aria-describedby="task-provider-hint" /></Field>
              <Field name="model" label="模型名称" hint="可自定义；留空时由服务选择默认模型。" error={errors.model}><input {...input('model', { maxLength: 256, spellCheck: false })} /></Field></div>
            <div className="field-grid"><Field name="image" label="Docker 镜像" hint="使用本机已准备好的镜像。" error={errors.image}><input {...input('image', { maxLength: 512, spellCheck: false })} /></Field>
              <Field name="max_model_calls" label="最大模型轮数" hint="1–200 轮，到达上限后保存已有补丁。" error={errors.max_model_calls}><input {...input('max_model_calls', { type: 'number', min: 1, max: 200, step: 1 })} /></Field></div>
          </section>
          <details className="advanced-settings"><summary><Settings2 size={16} /><span>高级设置</span><ChevronDown size={16} /></summary><div className="advanced-body"><div className="field-grid">
            <Field name="command_timeout_seconds" label="单条命令超时（秒）" error={errors.command_timeout_seconds}><input {...input('command_timeout_seconds', { type: 'number', min: 1, max: 3600, step: 1 })} /></Field>
            <Field name="verification_timeout_seconds" label="验证命令超时（秒）" error={errors.verification_timeout_seconds}><input {...input('verification_timeout_seconds', { type: 'number', min: 1, max: 3600, step: 1 })} /></Field>
            <Field name="memory_mb" label="容器内存（MB）" error={errors.memory_mb}><input {...input('memory_mb', { type: 'number', min: 256, max: 32768, step: 1 })} /></Field>
            <Field name="platform" label="镜像架构"><Select id="task-platform" name="platform" value={form.platform} onValueChange={(value) => change('platform', value)} options={platformOptions} disabled={disabled} aria-describedby="task-platform-hint" /></Field>
          </div><Field name="allowed_paths" label="允许修改的路径" hint="每行一个仓库根目录相对模式，例如 src/**。留空表示不额外限定范围。" error={errors.allowed_paths}><textarea className="mono-input" {...input('allowed_paths', { rows: 2, spellCheck: false })} /></Field>
          <Field name="protected_paths" label="禁止修改的路径" hint="默认保护常见测试路径。每行一个模式，例如 tests/**；规则会固定到本次任务，验证和交付时强制检查。" error={errors.protected_paths}><textarea className="mono-input" {...input('protected_paths', { rows: 4, spellCheck: false })} /></Field>
          <label className="checkbox-label"><input type="checkbox" checked={form.budget_reminders} onChange={(event) => change('budget_reminders', event.target.checked)} />启用剩余轮数提醒</label><label className="checkbox-label"><input type="checkbox" checked={form.explicit_finish} onChange={(event) => change('explicit_finish', event.target.checked)} />要求模型通过 finish 工具交卷</label></div></details>
  </>
}
