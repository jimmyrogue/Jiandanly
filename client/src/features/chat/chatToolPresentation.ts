import type { Translator } from '../../shared/i18n/i18n'
import type { AgentToolDetail } from '../../shared/local-data/types'

export const TOOL_TARGET_MAX = 40

export function stringValue(value: unknown): string {
  return typeof value === 'string' ? value : ''
}

export function truncate(value: string, max: number): string {
  return value.length > max ? `${value.slice(0, max - 1)}…` : value
}

/** A short, human concrete target for the current operation — a file name,
 *  a URL host, the command, or the search query. Back-compat single
 *  string; new code should call `toolDetail()` and read `.text`. */
export function toolTarget(payload: Record<string, unknown>, tool?: string): string {
  return toolDetail(payload, tool)?.text ?? ''
}

/** Rich primary-argument badge per tool. Reads `payload.arguments`
 *  (assembled from the runtime's `tool.requested` event), picks the
 *  most informative field for the tool, and returns a renderable
 *  display shape. Returns `undefined` when there's nothing useful
 *  to surface — the renderer should fall back to a plain verb. */
export function toolDetail(
  payload: Record<string, unknown>,
  tool?: string,
): AgentToolDetail | undefined {
  const args =
    payload.arguments && typeof payload.arguments === 'object' && !Array.isArray(payload.arguments)
      ? (payload.arguments as Record<string, unknown>)
      : {}

  // `task` (deepagents subagent dispatcher) needs special handling —
  // its real arg names are `description` + `subagent_type`. Older code
  // in this file guessed `task_description` + `subagent_name`, which
  // matched nothing on real runs, so the task headline showed only the
  // verb. We keep the old names as aliases so any persisted timeline
  // items from before this fix still render something sensible.
  if (tool === 'task') {
    const subagent = stringValue(args.subagent_type) || stringValue(args.subagent_name)
    const description = stringValue(args.description) || stringValue(args.task_description)
    if (description) {
      return {
        kind: 'text',
        text: truncate(description, TOOL_TARGET_MAX),
        tooltip: subagent ? `${subagent}: ${description}` : description,
      }
    }
    if (subagent) {
      return { kind: 'text', text: subagent }
    }
  }

  // Web tools — host + globe icon. Tooltip carries the full URL.
  const url = stringValue(args.url) || stringValue(payload.url)
  if (url) {
    try {
      const host = new URL(url).hostname.replace(/^www\./, '')
      return { kind: 'host', text: host, tooltip: url, showWebIcon: true }
    } catch {
      // Malformed URL — fall back to truncated raw string, no icon.
      return { kind: 'text', text: truncate(url, TOOL_TARGET_MAX), tooltip: url }
    }
  }

  // Filesystem tools — basename + full path tooltip.
  const path = stringValue(args.path) || stringValue(args.file_path)
  if (path) {
    const segments = path.split(/[\\/]/).filter(Boolean)
    const basename = segments[segments.length - 1] || path
    const trailing = tool === 'ls' || tool === 'fs.list' || tool === 'workspace.open' ? '/' : ''
    return { kind: 'text', text: basename + trailing, tooltip: path }
  }

  // Search / question / prompt-style tools — pick the most natural arg.
  const command = stringValue(args.command)
  if (command) {
    return { kind: 'text', text: truncate(command, TOOL_TARGET_MAX), tooltip: command }
  }
  const query = stringValue(args.query)
  if (query) {
    return { kind: 'text', text: truncate(query, TOOL_TARGET_MAX), tooltip: query }
  }
  const task = stringValue(args.task)
  if (task) {
    return { kind: 'text', text: truncate(task, TOOL_TARGET_MAX), tooltip: task }
  }
  const prompt = stringValue(args.prompt)
  if (prompt) {
    return { kind: 'text', text: truncate(prompt, TOOL_TARGET_MAX), tooltip: prompt }
  }
  const question = stringValue(args.question)
  if (question) {
    return { kind: 'text', text: truncate(question, 30), tooltip: question }
  }
  const pattern = stringValue(args.pattern)
  if (pattern) {
    return { kind: 'text', text: truncate(pattern, TOOL_TARGET_MAX), tooltip: pattern }
  }

  // Count-style tools.
  if (Array.isArray(args.todos)) {
    return { kind: 'count', text: String(args.todos.length) }
  }
  if (Array.isArray(args.checks)) {
    return { kind: 'count', text: String(args.checks.length) }
  }

  // Last-ditch: an event-level title (browser.observed, source.collected).
  const title = stringValue(payload.title)
  if (title) {
    return { kind: 'text', text: truncate(title, TOOL_TARGET_MAX), tooltip: title }
  }
  return undefined
}

export function toolActionLabel(tool: string, t: Translator): string {
  const labels: Record<string, string> = {
    'fs.list': t('chat.tool.fs.list'),
    'fs.read': t('chat.tool.fs.read'),
    'fs.search': t('chat.tool.fs.search'),
    'fs.write': t('chat.tool.fs.write'),
    'file.read': t('chat.tool.fs.read'),
    'file.search': t('chat.tool.fs.search'),
    'file.write': t('chat.tool.fs.write'),
    'workspace.open': t('chat.tool.workspace.open'),
    'open.url': t('chat.tool.open.url'),
    'open.file': t('chat.tool.open.file'),
    'clipboard.read': t('chat.tool.clipboard.read'),
    'clipboard.write': t('chat.tool.clipboard.write'),
    'task.verify': t('chat.tool.task.verify'),
    'browser.open': t('chat.tool.browser.open'),
    'browser.search': t('chat.tool.browser.search'),
    'browser.snapshot': t('chat.tool.browser.snapshot'),
    'browser.read': t('chat.tool.browser.read'),
    'browser.verify': t('chat.tool.browser.verify'),
    'browser.screenshot': t('chat.tool.browser.screenshot'),
    'browser.click': t('chat.tool.browser.click'),
    'browser.type': t('chat.tool.browser.type'),
    'browser.scroll': t('chat.tool.browser.scroll'),
    'browser.close': t('chat.tool.browser.close'),
    'environment.observe': t('chat.tool.environment.observe'),
    'shell.run': t('chat.tool.shell.run'),
    'code.execute': t('chat.tool.code.execute'),
    'pdf.inspect': t('chat.tool.pdf.inspect'),
    'web.fetch': t('chat.tool.web.fetch'),
    'web.search': t('chat.tool.web.search'),
    'mcp.call': t('chat.tool.mcp.call'),
    'document.read': t('chat.tool.document.read'),
    'time.now': t('chat.tool.time.now'),
    // Runtime-side tools (deepagents built-ins + our ALWAYS_INCLUDE
    // set) that previously leaked their raw names into the timeline.
    'user.ask': t('chat.tool.user.ask'),
    write_todos: t('chat.tool.write_todos'),
    task: t('chat.tool.task'),
    read_file: t('chat.tool.read_file'),
    write_file: t('chat.tool.write_file'),
    edit_file: t('chat.tool.edit_file'),
    ls: t('chat.tool.ls'),
    glob: t('chat.tool.glob'),
    grep: t('chat.tool.grep'),
    execute: t('chat.tool.execute'),
    'memory.search': t('chat.tool.memory.search'),
    'memory.write': t('chat.tool.memory.write'),
    'image.generate': t('chat.tool.image.generate'),
    'image.edit': t('chat.tool.image.edit'),
    'plugin.org.shejane.browser-qa.open': t('chat.tool.browserQa.open'),
    'plugin.org.shejane.browser-qa.observe': t('chat.tool.browserQa.observe'),
    'plugin.org.shejane.browser-qa.act': t('chat.tool.browserQa.act'),
    'plugin.org.shejane.browser-qa.inspect': t('chat.tool.browserQa.inspect'),
    'plugin.org.shejane.browser-qa.close': t('chat.tool.browserQa.close'),
    'plugin.org.shejane.ocr.ocr.recognize_images': t('chat.tool.ocr.recognize'),
    // Office tools — read + outline + read_range + 10 write tools.
    'office.read': t('chat.tool.office.read'),
    'office.outline': t('chat.tool.office.outline'),
    'office.read_range': t('chat.tool.office.read_range'),
    'office.find_replace': t('chat.tool.office.find_replace'),
    'office.insert_paragraph': t('chat.tool.office.insert_paragraph'),
    'office.update_paragraph': t('chat.tool.office.update_paragraph'),
    'office.delete_paragraph': t('chat.tool.office.delete_paragraph'),
    'office.apply_style': t('chat.tool.office.apply_style'),
    'office.set_cells': t('chat.tool.office.set_cells'),
    'office.set_formula': t('chat.tool.office.set_formula'),
    'office.set_cell_format': t('chat.tool.office.set_cell_format'),
    'office.merge_cells': t('chat.tool.office.merge_cells'),
    'office.add_row': t('chat.tool.office.add_row'),
    // Phase 3 — pptx
    'office.create_pptx': t('chat.tool.office.create_pptx'),
    'office.add_slide': t('chat.tool.office.add_slide'),
    'office.update_slide': t('chat.tool.office.update_slide'),
    'office.delete_slide': t('chat.tool.office.delete_slide'),
    'office.reorder_slides': t('chat.tool.office.reorder_slides'),
    'office.set_slide_title': t('chat.tool.office.set_slide_title'),
    'office.set_slide_bullets': t('chat.tool.office.set_slide_bullets'),
    'office.set_slide_notes': t('chat.tool.office.set_slide_notes'),
    'office.add_image_to_slide': t('chat.tool.office.add_image_to_slide'),
    'office.read_slides': t('chat.tool.office.read_slides'),
  }
  return labels[tool] || tool || t('chat.tool.fallback')
}
