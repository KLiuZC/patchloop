import { describe, expect, it } from 'vitest'
import { parseDiff, parseTrajectory } from './artifacts.js'

const turn = (overrides = {}) => ({
  type: 'turn', turn: 1, seconds: 1.25,
  assistant: { role: 'assistant', content: '检查实现', reasoning_content: 'private reasoning' },
  tools: [{ id: 'call-1', name: 'bash', arguments: '{"cmd":"echo ok"}', output: 'ok\n' }],
  usage: { prompt_tokens: 20, cached_tokens: 10, completion_tokens: 5 },
  ...overrides,
})
const jsonl = (...records) => records.map((record) => JSON.stringify(record)).join('\n') + '\n'

describe('parseTrajectory', () => {
  it('reads real turn records, ignoring reasoning and non-turn records', () => {
    const result = parseTrajectory(jsonl(
      { type: 'meta', task: 'repair', system_prompt: 'not a turn' },
      { type: 'budget_reminder', turn: 1, message: { content: 'remaining budget' } },
      turn(),
      { type: 'finish_required', turn: 2 },
      turn({ turn: 2, seconds: 0, assistant: { content: 'All tests passed!' }, tools: [], usage: null }),
      { type: 'result', finished: true, validation_status: 'failed' },
    ))
    expect(result).toEqual({
      turns: [
        {
          turn: 1, seconds: 1.25, content: '检查实现', tools: turn().tools,
          usage: { prompt_tokens: 20, cached_tokens: 10, completion_tokens: 5 },
        },
        { turn: 2, seconds: 0, content: 'All tests passed!', tools: [], usage: null },
      ],
      malformedLines: 0, hasPartialLine: false,
    })
    expect(JSON.stringify(result)).not.toContain('reasoning')
    expect(result.turns[1]).not.toHaveProperty('validation_status')
  })

  it('retains complete turns when the final record is still being written', () => {
    const result = parseTrajectory(jsonl(turn()) + '{"type":"turn","assistant":')
    expect(result.turns).toHaveLength(1)
    expect(result).toMatchObject({ malformedLines: 0, hasPartialLine: true })
  })

  it('accepts a complete final JSON record without a trailing newline', () => {
    const result = parseTrajectory(JSON.stringify(turn()))
    expect(result.turns).toHaveLength(1)
    expect(result).toMatchObject({ malformedLines: 0, hasPartialLine: false })
  })

  it('counts broken complete lines and malformed shapes while preserving later turns', () => {
    const text = 'not json\nnull\n[]\n{}\n'
      + jsonl({ type: 'turn', turn: 0, assistant: {} }, { type: 'turn', turn: 1, assistant: null }, turn({ turn: 2 }))
      + '{"type":\n'
    const result = parseTrajectory(text)
    expect(result.turns.map((entry) => entry.turn)).toEqual([2])
    expect(result).toMatchObject({ malformedLines: 7, hasPartialLine: false })
  })

  it('reports interior corruption and an unfinished last line independently', () => {
    const result = parseTrajectory('broken\n' + jsonl(turn()) + '{')
    expect(result).toMatchObject({ malformedLines: 1, hasPartialLine: true })
    expect(result.turns).toHaveLength(1)
  })

  it('counts a structurally invalid final JSON record as malformed, not partial', () => {
    expect(parseTrajectory('{"type":"turn","turn":"1"}')).toEqual({
      turns: [], malformedLines: 1, hasPartialLine: false,
    })
  })

  it('supports CRLF and whitespace-only lines', () => {
    const result = parseTrajectory('\r\n  \r\n' + jsonl(turn()).replaceAll('\n', '\r\n') + '  ')
    expect(result.turns).toHaveLength(1)
    expect(result).toMatchObject({ malformedLines: 0, hasPartialLine: false })
  })

  it('turns unexpected tool values into inert strings without parsing arguments', () => {
    const source = '<script>globalThis.executed = true</script>'
    const result = parseTrajectory(jsonl(turn({
      assistant: { content: null, reasoning_content: 'private reasoning' },
      tools: [null, false, {
        id: 7, name: { tool: 'bash' }, arguments: { cmd: source }, output: [source, 42],
      }, { id: 'invalid-json', name: 'bash', arguments: '{not json', output: null }],
    })))
    expect(result.turns[0].content).toBe('')
    expect(result.turns[0].tools).toEqual([
      { id: '7', name: '{"tool":"bash"}', arguments: JSON.stringify({ cmd: source }), output: JSON.stringify([source, 42]) },
      { id: 'invalid-json', name: 'bash', arguments: '{not json', output: '' },
    ])
  })

  it('retains unexecuted assistant tool calls without duplicating recorded calls', () => {
    const result = parseTrajectory(jsonl(turn({ assistant: {
      content: '', tool_calls: [
        { id: 'call-1', function: { name: 'bash', arguments: '{"cmd":"echo ok"}' } },
        { id: 'call-2', function: { name: 'bash', arguments: '{"cmd":"failing command"}' } },
        null,
      ],
    } })))
    expect(result.turns[0].tools).toEqual([
      turn().tools[0],
      { id: 'call-2', name: 'bash', arguments: '{"cmd":"failing command"}', output: '' },
    ])
  })

  it('normalizes invalid optional fields to safe values', () => {
    const result = parseTrajectory(jsonl(turn({
      seconds: -1, tools: {},
      usage: { prompt_tokens: '10', completion_tokens: 0, cached_tokens: -2, extra: {} },
    })))
    expect(result.turns[0]).toMatchObject({ seconds: null, tools: [], usage: { completion_tokens: 0 } })
  })

  it.each(['', null, undefined])('handles unavailable text: %j', (text) => {
    expect(parseTrajectory(text)).toEqual({ turns: [], malformedLines: 0, hasPartialLine: false })
  })
})

const row = (text, type = 'meta', oldLine = null, newLine = null) => ({ text, type, oldLine, newLine })

describe('parseDiff', () => {
  it('tracks multiple hunks and files with independent old and new line numbers', () => {
    const result = parseDiff([
      'diff --git a/a.txt b/a.txt', 'index 123..456 100644', '--- a/a.txt', '+++ b/a.txt',
      '@@ -2,3 +2,4 @@ function', ' keep', '-old', '+new', '+extra', ' end',
      '@@ -10 +11 @@', '-before', '+after',
      'diff --git a/b.txt b/b.txt', 'new file mode 100644', '--- /dev/null', '+++ b/b.txt',
      '@@ -0,0 +1,2 @@', '+one', '+two', '',
    ].join('\n'))
    expect(result).toMatchObject({ added: 5, removed: 2, files: 2 })
    expect(result.rows.filter((entry) => !['meta', 'hunk'].includes(entry.type))).toEqual([
      row(' keep', 'context', 2, 2), row('-old', 'remove', 3), row('+new', 'add', null, 3),
      row('+extra', 'add', null, 4), row(' end', 'context', 4, 5),
      row('-before', 'remove', 10), row('+after', 'add', null, 11),
      row('+one', 'add', null, 1), row('+two', 'add', null, 2),
    ])
    expect(result.rows.filter((entry) => entry.type === 'hunk')).toHaveLength(3)
  })

  it('treats +++ and --- inside a hunk as real additions and removals', () => {
    const result = parseDiff('diff --git a/a b/a\n--- a/a\n+++ b/a\n@@ -1 +1 @@\n--- old text\n+++ new text\n')
    expect(result).toMatchObject({ added: 1, removed: 1, files: 1 })
    expect(result.rows.slice(-2)).toEqual([
      row('--- old text', 'remove', 1), row('+++ new text', 'add', null, 1),
    ])
  })

  it('does not advance source lines for no-newline markers', () => {
    const marker = '\\ No newline at end of file'
    const result = parseDiff(`--- a/a\n+++ b/a\n@@ -7 +9 @@\n-old\n${marker}\n+new\n${marker}`)
    expect(result).toMatchObject({ added: 1, removed: 1, files: 1 })
    expect(result.rows.slice(-4)).toEqual([
      row('-old', 'remove', 7), row(marker), row('+new', 'add', null, 9), row(marker),
    ])
  })

  it('counts bare unified file headers once each and supports zero-length ranges', () => {
    const result = parseDiff([
      '--- a/removed', '+++ /dev/null', '@@ -1,2 +0,0 @@', '-one', '-two',
      '--- /dev/null', '+++ b/added', '@@ -0,0 +1 @@', '+three',
    ].join('\n'))
    expect(result).toMatchObject({ added: 1, removed: 2, files: 2 })
    expect(result.rows.filter((entry) => entry.type === 'remove')).toEqual([
      row('-one', 'remove', 1), row('-two', 'remove', 2),
    ])
    expect(result.rows.at(-1)).toEqual(row('+three', 'add', null, 1))
  })

  it('preserves binary, rename and mode metadata without inflating line counts', () => {
    const lines = [
      'diff --git a/image.png b/image.png', 'index abc..def 100644', 'Binary files a/image.png and b/image.png differ',
      'diff --git a/old.txt b/new.txt', 'similarity index 100%', 'rename from old.txt', 'rename to new.txt',
      'diff --git a/script b/script', 'old mode 100644', 'new mode 100755',
      'diff --git a/data.bin b/data.bin', 'GIT binary patch', 'literal 4', 'Lc${NkU|;|M00aO5', '',
    ]
    const result = parseDiff(lines.join('\n'))
    expect(result).toMatchObject({ added: 0, removed: 0, files: 4 })
    expect(result.rows).toEqual(lines.slice(0, -1).map((line) => row(line)))
  })

  it('counts standalone binary changes', () => {
    const result = parseDiff('Binary files old/a.png and new/a.png differ\n')
    expect(result).toMatchObject({ added: 0, removed: 0, files: 1 })
    expect(result.rows[0].type).toBe('meta')
  })

  it('echoes nonstandard and HTML-like content as metadata', () => {
    const lines = ['<script>alert(1)</script>', '+not a hunk', '-not a hunk', '@@ invalid @@', '', '  arbitrary text']
    expect(parseDiff(lines.join('\n'))).toEqual({
      rows: lines.map((line) => row(line)), added: 0, removed: 0, files: 0,
    })
  })

  it('ends a hunk at its declared bounds so trailing text gets no invented line numbers', () => {
    const result = parseDiff('@@ -1 +1 @@\n-old\n+new\n+trailing\n context\n')
    expect(result).toMatchObject({ added: 1, removed: 1 })
    expect(result.rows.slice(-2)).toEqual([row('+trailing'), row(' context')])
  })

  it('recovers at the next hunk after malformed content', () => {
    const result = parseDiff('@@ -1,2 +1,2 @@\ninvalid\n+untrusted\n@@ -8 +9 @@\n-old\n+new')
    expect(result.rows.slice(1, 3)).toEqual([row('invalid'), row('+untrusted')])
    expect(result.rows.slice(-2)).toEqual([row('-old', 'remove', 8), row('+new', 'add', null, 9)])
    expect(result).toMatchObject({ added: 1, removed: 1 })
  })

  it('preserves unsupported combined diff hunks as metadata', () => {
    const lines = ['diff --cc file', '@@@ -1,1 -1,1 +1,1 @@@', '++combined']
    expect(parseDiff(lines.join('\n'))).toEqual({
      rows: lines.map((line) => row(line)), added: 0, removed: 0, files: 1,
    })
  })

  it('handles CRLF, empty source lines and a missing final newline', () => {
    const result = parseDiff('@@ -1,2 +1,2 @@\r\n \r\n-\r\n+')
    expect(result.rows.slice(1)).toEqual([row(' ', 'context', 1, 1), row('-', 'remove', 2), row('+', 'add', null, 2)])
    expect(result).toMatchObject({ added: 1, removed: 1 })
  })

  it.each(['', null, undefined])('handles unavailable text: %j', (text) => {
    expect(parseDiff(text)).toEqual({ rows: [], added: 0, removed: 0, files: 0 })
  })
})
