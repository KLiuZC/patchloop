import * as SelectPrimitive from '@radix-ui/react-select'
import { Check, ChevronDown, ChevronUp } from 'lucide-react'

const EMPTY_OPTION = '__patchloop_empty_option__'

export default function Select({
  value = '', onValueChange, options, name, disabled = false,
  size = 'default', placeholder = '请选择', className = '', ...triggerProps
}) {
  // Radix reserves an empty value for its placeholder. Our "all/automatic"
  // options use an internal value while the form and API keep the original ''.
  return <>
    <SelectPrimitive.Root
      value={value === '' ? EMPTY_OPTION : value}
      onValueChange={(next) => onValueChange(next === EMPTY_OPTION ? '' : next)}
      disabled={disabled}
    >
      <SelectPrimitive.Trigger {...triggerProps} className={`select-trigger select-trigger-${size} ${className}`}>
        <SelectPrimitive.Value placeholder={placeholder} />
        <SelectPrimitive.Icon asChild><ChevronDown className="select-chevron" size={16} aria-hidden="true" /></SelectPrimitive.Icon>
      </SelectPrimitive.Trigger>
      <SelectPrimitive.Portal>
        <SelectPrimitive.Content
          className={`select-content select-content-${size}`}
          position="popper" side="bottom" align="start" sideOffset={8}
          collisionPadding={12}
        >
          <SelectPrimitive.ScrollUpButton className="select-scroll-button"><ChevronUp size={14} aria-hidden="true" /></SelectPrimitive.ScrollUpButton>
          <SelectPrimitive.Viewport className="select-viewport">
            {options.map((option) => <SelectPrimitive.Item
              className="select-option" key={option.value}
              value={option.value === '' ? EMPTY_OPTION : option.value}
              disabled={option.disabled}
            >
              <SelectPrimitive.ItemText>{option.label}</SelectPrimitive.ItemText>
              <SelectPrimitive.ItemIndicator className="select-check"><Check size={14} aria-hidden="true" /></SelectPrimitive.ItemIndicator>
            </SelectPrimitive.Item>)}
          </SelectPrimitive.Viewport>
          <SelectPrimitive.ScrollDownButton className="select-scroll-button"><ChevronDown size={14} aria-hidden="true" /></SelectPrimitive.ScrollDownButton>
        </SelectPrimitive.Content>
      </SelectPrimitive.Portal>
    </SelectPrimitive.Root>
    {name && <input type="hidden" name={name} value={value} disabled={disabled} />}
  </>
}
