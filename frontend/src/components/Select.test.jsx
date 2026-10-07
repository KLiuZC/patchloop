import { useState } from 'react'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import Select from './Select'

const options = [
  { value: '', label: '自动选择' },
  { value: 'unavailable', label: '不可用', disabled: true },
  { value: 'linux/arm64', label: 'ARM64' },
  { value: 'linux/amd64', label: 'AMD64' },
]

function Example({ disabled = false, onSubmit = () => {}, onChange = () => {} }) {
  const [value, setValue] = useState('')
  return <form data-testid="select-form" onSubmit={(event) => { event.preventDefault(); onSubmit() }}>
    <label htmlFor="architecture">镜像架构</label>
    <Select id="architecture" name="platform" value={value} disabled={disabled} options={options}
      onValueChange={(next) => { setValue(next); onChange(next) }} />
  </form>
}

describe('Select', () => {
  it('preserves explicit empty options in React state and form serialization', async () => {
    const onChange = vi.fn()
    const onSubmit = vi.fn()
    const user = userEvent.setup()
    render(<Example onChange={onChange} onSubmit={onSubmit} />)
    const trigger = screen.getByRole('combobox', { name: '镜像架构' })
    expect(trigger).toHaveTextContent('自动选择')
    await user.click(trigger)
    await user.click(await screen.findByRole('option', { name: 'ARM64' }))
    expect(trigger).toHaveTextContent('ARM64')
    expect(new FormData(screen.getByTestId('select-form')).get('platform')).toBe('linux/arm64')
    await user.click(trigger)
    await user.click(await screen.findByRole('option', { name: '自动选择' }))
    expect(onChange).toHaveBeenLastCalledWith('')
    expect(new FormData(screen.getByTestId('select-form')).get('platform')).toBe('')
    expect(onSubmit).not.toHaveBeenCalled()
    expect(trigger).toHaveFocus()
  })

  it('supports keyboard selection, skips disabled options, and restores focus with Escape', async () => {
    const onSubmit = vi.fn()
    const user = userEvent.setup()
    render(<Example onSubmit={onSubmit} />)
    const trigger = screen.getByRole('combobox', { name: '镜像架构' })
    trigger.focus()
    await user.keyboard('{Enter}')
    await screen.findByRole('listbox')
    await waitFor(() => expect(screen.getByRole('option', { name: '自动选择' })).toHaveFocus())
    await user.keyboard('{ArrowDown}{Enter}')
    expect(trigger).toHaveTextContent('ARM64')
    expect(onSubmit).not.toHaveBeenCalled()
    await user.keyboard('{ArrowDown}')
    await screen.findByRole('listbox')
    await user.keyboard('{ArrowDown}{Escape}')
    await waitFor(() => expect(screen.queryByRole('listbox')).not.toBeInTheDocument())
    expect(trigger).toHaveFocus()
    expect(trigger).toHaveTextContent('ARM64')
  })

  it('blocks disabled controls and omits their form value', async () => {
    const user = userEvent.setup()
    render(<Example disabled />)
    const trigger = screen.getByRole('combobox', { name: '镜像架构' })
    expect(trigger).toBeDisabled()
    await user.click(trigger)
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
    expect(new FormData(screen.getByTestId('select-form')).has('platform')).toBe(false)
  })
})
