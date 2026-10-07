const isObject = (value) => value !== null && typeof value === 'object' && !Array.isArray(value)
const isNonnegative = (value) => typeof value === 'number' && Number.isFinite(value) && value >= 0

function displayText(value) {
  if (value == null) return ''
  return typeof value === 'string' ? value : JSON.stringify(value)
}

function turnTools(record) {
  const tools = Array.isArray(record.tools) ? record.tools.filter(isObject) : []
  const calls = Array.isArray(record.assistant.tool_calls) ? record.assistant.tool_calls : []
  // A tool can fail before its output is logged; retain the corresponding call.
  const missing = calls.filter((call) => isObject(call) && isObject(call.function)
    && !tools.some((tool) => typeof call.id === 'string' && tool.id === call.id))
    .map((call) => ({ id: call.id, name: call.function.name, arguments: call.function.arguments }))
  return [...tools, ...missing].map((tool) => ({
    id: displayText(tool.id),
    name: displayText(tool.name),
    arguments: displayText(tool.arguments),
    output: displayText(tool.output),
  }))
}

export function parseTrajectory(text) {
  const result = { turns: [], malformedLines: 0, hasPartialLine: false }
  if (typeof text !== 'string' || !text) return result
  const lines = text.split(/\r?\n/)
  for (const [index, line] of lines.entries()) {
    if (!line.trim()) continue
    let record
    try {
      record = JSON.parse(line)
    } catch {
      // Without a newline, the writer may still be appending this JSON record.
      if (index === lines.length - 1) result.hasPartialLine = true
      else result.malformedLines += 1
      continue
    }
    if (!isObject(record) || typeof record.type !== 'string') {
      result.malformedLines += 1
      continue
    }
    if (record.type !== 'turn') continue
    if (!Number.isSafeInteger(record.turn) || record.turn < 1 || !isObject(record.assistant)) {
      result.malformedLines += 1
      continue
    }
    const usage = isObject(record.usage)
      ? Object.fromEntries(['prompt_tokens', 'cached_tokens', 'completion_tokens', 'total_tokens']
        .filter((key) => isNonnegative(record.usage[key])).map((key) => [key, record.usage[key]]))
      : null
    result.turns.push({
      turn: record.turn,
      seconds: isNonnegative(record.seconds) ? record.seconds : null,
      content: displayText(record.assistant.content),
      tools: turnTools(record),
      usage,
    })
  }
  return result
}

export function parseDiff(text) {
  const result = { rows: [], added: 0, removed: 0, files: 0 }
  if (typeof text !== 'string' || !text) return result
  const lines = text.split(/\r?\n/)
  if (lines.at(-1) === '') lines.pop()
  let hunk = null
  let announcedFile = false
  for (const [index, line] of lines.entries()) {
    const row = { text: line, type: 'meta', oldLine: null, newLine: null }
    const match = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@.*$/.exec(line)
    if (/^diff --(?:git|cc|combined) /.test(line)) {
      result.files += 1
      announcedFile = true
      hunk = null
    } else if (match) {
      const [oldLine, oldLeft, newLine, newLeft] = [
        Number(match[1]), Number(match[2] ?? 1), Number(match[3]), Number(match[4] ?? 1),
      ]
      hunk = null
      if ([oldLine, oldLeft, newLine, newLeft].every(Number.isSafeInteger)) {
        hunk = { oldLine, newLine, oldLeft, newLeft }
        row.type = 'hunk'
      }
    } else if (line === '\\ No newline at end of file') {
      // This marker belongs to the preceding row and consumes no source line.
    } else if (hunk && (hunk.oldLeft > 0 || hunk.newLeft > 0)) {
      // Count hunk contents before considering --- / +++ file headers.
      if (line.startsWith('-') && hunk.oldLeft > 0) {
        row.type = 'remove'
        row.oldLine = hunk.oldLine++
        hunk.oldLeft -= 1
        result.removed += 1
      } else if (line.startsWith('+') && hunk.newLeft > 0) {
        row.type = 'add'
        row.newLine = hunk.newLine++
        hunk.newLeft -= 1
        result.added += 1
      } else if (line.startsWith(' ') && hunk.oldLeft > 0 && hunk.newLeft > 0) {
        row.type = 'context'
        row.oldLine = hunk.oldLine++
        row.newLine = hunk.newLine++
        hunk.oldLeft -= 1
        hunk.newLeft -= 1
      } else {
        hunk = null
      }
    } else if (line.startsWith('--- ') && lines[index + 1]?.startsWith('+++ ')) {
      if (!announcedFile) result.files += 1
      announcedFile = false
      hunk = null
    } else if (/^Binary files .+ and .+ differ$/.test(line)) {
      if (!announcedFile) result.files += 1
      announcedFile = false
    }
    result.rows.push(row)
  }
  return result
}
