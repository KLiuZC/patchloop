const MAX_COMMAND_LENGTH = 64 * 1024

// Deliberately limited to one literal argument, not a general shell tokenizer.
function pythonArgument(command) {
  const prefix = /^[ \t]*python(?:3(?:\.[0-9]+)?)?[ \t]+-c[ \t]+/.exec(command)
  if (!prefix || prefix[0].length === command.length) return null
  let quote = '', code = ''
  for (let i = prefix[0].length; i < command.length; i++) {
    const char = command[i]
    if (quote === "'") {
      if (char === "'") quote = ''
      else code += char
    } else if (char === '\\') {
      const next = command[++i]
      if (next === undefined || next === '\n' || next === '\r') return null
      // In double quotes, backslashes before ordinary characters stay literal.
      code += quote === '"' && !'\\"$`'.includes(next) ? `\\${next}` : next
    } else if (quote === '"') {
      if (char === '"') quote = ''
      else if ('$`'.includes(char)) return null
      else code += char
    } else if (char === "'" || char === '"') {
      quote = char
    } else if (' \t\n'.includes(char)) {
      return i > prefix[0].length && !/[^ \t\n]/.test(command.slice(i)) ? code : null
    } else if (/[a-zA-Z0-9_./,:=+%@-]/.test(char)) {
      code += char
    } else return null // Expansions, globbing, operators, comments, or extra syntax.
  }
  return quote ? null : code
}

const PYTHON_ESCAPES = { '\\': '\\', "'": "'", '"': '"', a: '\x07', b: '\b', f: '\f', n: '\n', r: '\r', t: '\t', v: '\v' }

// Only exec(one ordinary quoted string). Unsupported literals retain all Python.
function execSource(code) {
  const start = /^exec[ \t]*\([ \t]*(['"])/.exec(code)
  if (!start) return null
  let source = ''
  for (let i = start[0].length; i < code.length; i++) {
    const char = code[i]
    if (char === start[1]) return /^[ \t]*\)[ \t\r\n]*$/.test(code.slice(i + 1)) ? source : null
    if (char === '\n' || char === '\r') return null
    if (char !== '\\') {
      source += char
      continue
    }
    const escape = code[++i]
    if (Object.hasOwn(PYTHON_ESCAPES, escape)) source += PYTHON_ESCAPES[escape]
    else if (escape !== undefined && /[0-7]/.test(escape)) {
      const digits = /^[0-7]{1,3}/.exec(code.slice(i))[0]
      source += String.fromCodePoint(parseInt(digits, 8))
      i += digits.length - 1
    } else if (escape === 'x' || escape === 'u' || escape === 'U') {
      const length = { x: 2, u: 4, U: 8 }[escape]
      const digits = code.slice(i + 1, i + 1 + length)
      if (digits.length !== length || /[^0-9a-fA-F]/.test(digits)) return null
      const point = parseInt(digits, 16)
      if (point > 0x10ffff) return null
      source += String.fromCodePoint(point)
      i += length
    } else return null
  }
  return null
}

/** Display-only decoding. The caller retains the original command for execution. */
export function describeVerificationCommand(command) {
  const fallback = { language: 'bash', code: command, extracted: false }
  if (typeof command !== 'string' || command.length > MAX_COMMAND_LENGTH || command.includes('\0')) return fallback
  const code = pythonArgument(command)
  return code === null ? fallback : { language: 'python', code: execSource(code) ?? code, extracted: true }
}
