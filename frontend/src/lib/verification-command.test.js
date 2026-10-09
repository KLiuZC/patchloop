import { readFileSync } from 'node:fs'
import { URL as NodeURL } from 'node:url'
import { describe, expect, it } from 'vitest'
import { describeVerificationCommand } from './verification-command.js'

describe('describeVerificationCommand', () => {
  it('recovers exactly the Python heredoc from the real Flask profile', () => {
    const profile = new NodeURL('../../../evals/profiles/github-flask-6146/', import.meta.url)
    const command = readFileSync(new NodeURL('verification_command.txt', profile), 'utf8')
    const script = readFileSync(new NodeURL('verify.sh', profile), 'utf8')
    expect(script.startsWith("python - <<'PY'\n")).toBe(true)
    expect(script.endsWith('PY\n')).toBe(true)
    expect(describeVerificationCommand(command)).toEqual({
      language: 'python', code: script.slice("python - <<'PY'\n".length, -3), extracted: true,
    })
  })

  it('extracts the complete literal Python argument, preserving indentation', () => {
    const code = 'if True:\n    print("hello")\n'
    expect(describeVerificationCommand(`python3.12 -c '${code}'`)).toEqual({
      language: 'python', code, extracted: true,
    })
  })

  it.each([
    ["python -c 'print('\"'\"'quoted'\"'\"')'", "print('quoted')"],
    ["python -c 'print('\\''quoted'\\'')'", "print('quoted')"],
    [String.raw`python3 -c "print('a\nb')"`, String.raw`print('a\nb')`],
    ['python -c "print(\'\\$HOME \\`date\\`\')"', "print('$HOME `date`')"],
    ['python -c \'print("$HOME `date` ; # > *")\'', 'print("$HOME `date` ; # > *")'],
    ['python -c \'exec("pass")\'\'; print("after")\'', 'exec("pass"); print("after")'],
    [" \tpython3.14\t-c 'pass' \t\n", 'pass'],
    ['python -c pass', 'pass'], ["python -c ''", ''], ['python -c ""', ''],
  ])('decodes literal shell words without changing their content: %s', (command, code) => {
    expect(describeVerificationCommand(command)).toEqual({ language: 'python', code, extracted: true })
  })

  it.each([
    [String.raw`python -c 'exec("pattern = r\"\\d+\\s*\\n\"\n")'`, String.raw`pattern = r"\d+\s*\n"` + '\n'],
    [String.raw`python -c 'exec("\x41\u4e2d\U0001F40D\101\12\1234")'`, 'A中🐍A\nS4'],
    [String.raw`python -c 'exec("\a\b\f\n\r\t\v\0\x1b")'`, '\x07\b\f\n\r\t\v\0\x1b'],
    [String.raw`python -c 'exec("\\n\n\\t\t\\\\")'`, '\\n\n\\t\t\\\\'],
    [String.raw`python -c 'exec("\"quoted\" \\path")'`, '"quoted" \\path'],
    [String.raw`python -c "exec('print(\"hello\")\\n')"`, 'print("hello")\n'],
    ['python -c \'exec ( "中文 ✓" )\'', '中文 ✓'],
    ['python -c \'exec("")\'', ''],
  ])('unwraps exactly one ordinary Python string: %s', (command, code) => {
    expect(describeVerificationCommand(command)).toEqual({ language: 'python', code, extracted: true })
  })

  it.each([
    '; echo hidden', ' && echo hidden', ' || echo hidden', ' | cat', ' &', ';',
    ' > output', '>>output', ' < input', ' 2>&1', ' # comment', '#comment',
    ' extra', " ''", '\n echo hidden', '\n# comment', '\r', '\r\necho hidden',
  ])('keeps the entire shell command when it has trailing syntax: %j', (suffix) => {
    const command = 'python -c \'exec("pass")\'' + suffix
    expect(describeVerificationCommand(command)).toEqual({ language: 'bash', code: command, extracted: false })
  })

  it.each([
    '', 'bash verify.sh', 'python -m pytest', 'python -I -c "pass"', 'python -cpass',
    '/usr/bin/python -c "pass"', 'python2 -c "pass"', 'python3.12.1 -c "pass"',
    'env python -c "pass"', 'X=1 python -c "pass"', "python -c 'unfinished", 'python -c "unfinished',
    'python -c', 'python -c   ', 'python -c \n', 'python -c pass\\', 'python -c pass\\\n',
    'python -c "$HOME"', 'python -c "${CODE}"', 'python -c "$(date)"', 'python -c "`date`"',
    'python -c $CODE', 'python -c $(date)', 'python -c `date`', "python -c $'pass'",
    'python -c *', 'python -c ~', 'python -c {a,b}', 'python -c [ab]',
    'python -c <(cat source)', "python -c 'pass'\0; hidden", 'python -c \vpass',
  ])('leaves unsupported, expanding, or malformed shell untouched: %j', (command) => {
    expect(describeVerificationCommand(command)).toEqual({ language: 'bash', code: command, extracted: false })
  })

  it.each([
    'exec("pass"); print("visible")', 'exec("pass")\nprint("visible")', 'exec("pass") # comment',
    'exec("pass", {})', 'exec("one" "two")', 'exec("one" + "two")', 'exec(variable)',
    'exec(r"raw\\n")', 'exec(b"bytes")', 'exec(f"{value}")', 'exec("""triple""")',
    String.raw`exec("\x0")`, String.raw`exec("\xGG")`, String.raw`exec("\u123")`,
    String.raw`exec("\U00110000")`, String.raw`exec("\N{SNOWMAN}")`, String.raw`exec("\q")`,
    'exec("line\nbreak")', 'exec("line\rbreak")', 'exec("unterminated)', 'exec("pass"',
    'exec("pass"))', 'if True:\n    exec("pass")\n',
  ])('retains all Python when unwrapping is unsupported or would hide code: %j', (code) => {
    expect(describeVerificationCommand(`python -c '${code}'`)).toEqual({ language: 'python', code, extracted: true })
  })

  it('bounds decoding work while returning oversized commands intact', () => {
    const code = 'x'.repeat(60_000)
    expect(describeVerificationCommand(`python -c '${code}'`)).toEqual({ language: 'python', code, extracted: true })
    const command = `python -c '${'x'.repeat(1_000_000)}'`
    expect(describeVerificationCommand(command)).toEqual({ language: 'bash', code: command, extracted: false })
  })
})
