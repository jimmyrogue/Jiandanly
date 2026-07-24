// The chat draft is a single string (App's source of truth). Inline skill
// mentions are embedded with collision-proof Unicode Private Use Area
// sentinels so normal text / Chinese input can never produce them. These pure
// helpers convert between the raw draft string and a structured view, and are
// the deterministic correctness core for both the editor and the send path.

export const SKILL_OPEN = ''
export const SKILL_CLOSE = ''

// Token = OPEN, then a name with no sentinel chars, then CLOSE.
// Function (capability) inline tokens use a separate PUA sentinel pair so the
// menu can offer two distinct kinds ("功能" above "Skill") and the send path can
// inject a per-function directive. Extensible: more function ids later.
export const FUNC_OPEN = ''
export const FUNC_CLOSE = ''

// MCP server inline tokens — third sentinel pair so the slash menu can
// render three distinct groups (功能 / Skill / MCP) and the send path can
// flip the per-server allowlist + inject a "prefer these MCP tools"
// directive. Kept as a separate token kind rather than collapsing into
// skill: directive text differs, and the per-run settings overrides
// touch different fields (`mcpDisabled` instead of `skills`).
export const MCP_OPEN = ''
export const MCP_CLOSE = ''

export const PLUGIN_OPEN = ''
export const PLUGIN_CLOSE = ''
export const PLUGIN_COMMAND_OPEN = ''
export const PLUGIN_COMMAND_CLOSE = ''

export interface PluginDraftReference {
  pluginId: string
  name: string
  expectedDigest: string
}

export interface PluginCommandDraftReference {
  pluginId: string
  pluginName: string
  commandId: string
  title: string
  expectedDigest: string
}

export type DraftNode =
  | { type: 'text'; value: string }
  | { type: 'skill'; name: string }
  | { type: 'function'; name: string }
  | { type: 'mcp'; name: string }
  | ({ type: 'plugin' } & PluginDraftReference)
  | ({ type: 'plugin_command' } & PluginCommandDraftReference)

/** Wrap a skill name into its sentinel token form. */
export function skillToken(name: string): string {
  return `${SKILL_OPEN}${name}${SKILL_CLOSE}`
}

/** Wrap a function id into its sentinel token form. */
export function functionToken(name: string): string {
  return `${FUNC_OPEN}${name}${FUNC_CLOSE}`
}

/** Wrap an MCP server name into its sentinel token form. */
export function mcpToken(name: string): string {
  return `${MCP_OPEN}${name}${MCP_CLOSE}`
}

export function pluginToken(reference: PluginDraftReference): string {
  return `${PLUGIN_OPEN}${encodeURIComponent(JSON.stringify([
    reference.pluginId,
    reference.name,
    reference.expectedDigest,
  ]))}${PLUGIN_CLOSE}`
}

export function pluginCommandToken(reference: PluginCommandDraftReference): string {
  return `${PLUGIN_COMMAND_OPEN}${encodeURIComponent(JSON.stringify([
    reference.pluginId,
    reference.pluginName,
    reference.commandId,
    reference.title,
    reference.expectedDigest,
  ]))}${PLUGIN_COMMAND_CLOSE}`
}

function decodeTuple(value: string, length: number): string[] | undefined {
  try {
    const parsed: unknown = JSON.parse(decodeURIComponent(value))
    return Array.isArray(parsed) && parsed.length === length && parsed.every((item) => typeof item === 'string')
      ? parsed
      : undefined
  } catch {
    return undefined
  }
}

/**
 * Split a raw draft into ordered text / skill nodes. Malformed sentinels
 * (an open without a matching close, or an empty name) are treated as plain
 * text so nothing is ever lost.
 */
const tokenPattern = (): RegExp =>
  new RegExp(
    `${SKILL_OPEN}([^${SKILL_OPEN}${SKILL_CLOSE}]+)${SKILL_CLOSE}` +
      `|${FUNC_OPEN}([^${FUNC_OPEN}${FUNC_CLOSE}]+)${FUNC_CLOSE}` +
      `|${MCP_OPEN}([^${MCP_OPEN}${MCP_CLOSE}]+)${MCP_CLOSE}` +
      `|${PLUGIN_OPEN}([^${PLUGIN_OPEN}${PLUGIN_CLOSE}]+)${PLUGIN_CLOSE}` +
      `|${PLUGIN_COMMAND_OPEN}([^${PLUGIN_COMMAND_OPEN}${PLUGIN_COMMAND_CLOSE}]+)${PLUGIN_COMMAND_CLOSE}`,
    'g',
  )

export function tokenizeDraft(draft: string): DraftNode[] {
  const nodes: DraftNode[] = []
  const appendText = (value: string) => {
    if (!value) return
    const previous = nodes.at(-1)
    if (previous?.type === 'text') previous.value += value
    else nodes.push({ type: 'text', value })
  }
  const pattern = tokenPattern()
  let lastIndex = 0
  for (let match = pattern.exec(draft); match; match = pattern.exec(draft)) {
    if (match.index > lastIndex) {
      appendText(draft.slice(lastIndex, match.index))
    }
    if (match[1] !== undefined) {
      nodes.push({ type: 'skill', name: match[1] })
    } else if (match[2] !== undefined) {
      nodes.push({ type: 'function', name: match[2] })
    } else if (match[3] !== undefined) {
      nodes.push({ type: 'mcp', name: match[3] })
    } else if (match[4] !== undefined) {
      const tuple = decodeTuple(match[4], 3)
      if (tuple) {
        nodes.push({
          type: 'plugin',
          pluginId: tuple[0],
          name: tuple[1],
          expectedDigest: tuple[2],
        })
      } else {
        appendText(match[0])
      }
    } else {
      const tuple = decodeTuple(match[5], 5)
      if (tuple) {
        nodes.push({
          type: 'plugin_command',
          pluginId: tuple[0],
          pluginName: tuple[1],
          commandId: tuple[2],
          title: tuple[3],
          expectedDigest: tuple[4],
        })
      } else {
        appendText(match[0])
      }
    }
    lastIndex = match.index + match[0].length
  }
  if (lastIndex < draft.length) {
    appendText(draft.slice(lastIndex))
  }
  return nodes
}

/**
 * Resolve a raw draft into what the user/agent should actually see:
 * - `text`: user-authored task text. Plugin selectors are metadata and do
 *   not become prompt text; Skill/MCP mentions retain their legacy display.
 * - `skills`: skill names in first-seen order, de-duplicated (used to force
 *   per-run skills + the skill.use directive prefix).
 */
export function parseSkillDraft(
  draft: string,
): {
  text: string
  skills: string[]
  functions: string[]
  mcps: string[]
  plugins: PluginDraftReference[]
  pluginCommand?: PluginCommandDraftReference
} {
  const skills: string[] = []
  const functions: string[] = []
  const mcps: string[] = []
  const plugins: PluginDraftReference[] = []
  const skillNames = new Set<string>()
  const functionNames = new Set<string>()
  const mcpNames = new Set<string>()
  const pluginIDs = new Set<string>()
  let pluginCommand: PluginCommandDraftReference | undefined
  let text = ''
  for (const node of tokenizeDraft(draft)) {
    if (node.type === 'text') {
      text += node.value
    } else if (node.type === 'skill') {
      text += `@${node.name}`
      if (!skillNames.has(node.name)) {
        skillNames.add(node.name)
        skills.push(node.name)
      }
    } else if (node.type === 'function') {
      // Function tokens are mode markers, not literal text: they add no text
      // (the per-function directive is injected by the send path).
      if (!functionNames.has(node.name)) {
        functionNames.add(node.name)
        functions.push(node.name)
      }
    } else if (node.type === 'mcp') {
      // MCP tokens render as @mcp:name so the chat bubble shows the user's
      // intent verbatim. The actual server allowlist override + 'prefer
      // these tools' directive get injected by the send path.
      text += `@mcp:${node.name}`
      if (!mcpNames.has(node.name)) {
        mcpNames.add(node.name)
        mcps.push(node.name)
      }
    } else if (node.type === 'plugin') {
      if (!pluginIDs.has(node.pluginId)) {
        pluginIDs.add(node.pluginId)
        plugins.push({
          pluginId: node.pluginId,
          name: node.name,
          expectedDigest: node.expectedDigest,
        })
      }
    } else {
      pluginCommand = {
        pluginId: node.pluginId,
        pluginName: node.pluginName,
        commandId: node.commandId,
        title: node.title,
        expectedDigest: node.expectedDigest,
      }
    }
  }
  return { text, skills, functions, mcps, plugins, ...(pluginCommand ? { pluginCommand } : {}) }
}
