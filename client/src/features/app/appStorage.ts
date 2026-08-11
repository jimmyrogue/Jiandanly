import type { ChatMode } from '@/shared/local-data/types'
import { parseRuntimeModelSpec, type AgentSettings, type ReasoningMode } from '@/runtime/client'

const runtimeThreadIDsStorageKey = 'shejane.runtime-thread-ids.v1'
const agentSettingsStorageKey = 'shejane.agentSettings.v9'
const legacyAgentSettingsStorageKey = 'shejane-agent-settings'
const chatModeStorageKey = 'shejane.chatMode.v2'
const reasoningModeStorageKey = 'shejane.reasoningModes.v2'
const legacyReasoningModeStorageKey = 'shejane.reasoningMode.v1'

const defaultAgentSettings: Required<AgentSettings> = {
  memory: 'on',
  skills: 'on',
  mcp: 'on',
  mcpDisabled: [],
  advanced: {},
}

export function readAgentSettings(): Required<AgentSettings> {
  if (typeof window === 'undefined') return { ...defaultAgentSettings }
  try {
    const canonicalRaw = window.localStorage.getItem(agentSettingsStorageKey)
    const legacyRaw = window.localStorage.getItem(legacyAgentSettingsStorageKey)
    const canonical = parseAgentSettings(canonicalRaw)
    const legacy = parseAgentSettings(legacyRaw)
    const settings: Required<AgentSettings> = {
      memory: canonical.memory === 'off' || (!canonicalRaw && legacy.memory === 'off') ? 'off' : 'on',
      skills: 'on',
      mcp: 'on',
      mcpDisabled: [...new Set(
        Array.isArray(legacy.mcpDisabled)
          ? storedMcpDisabled(legacy)
          : storedMcpDisabled(canonical),
      )],
      advanced: {},
    }
    if (legacyRaw !== null && writeAgentSettings(settings)) {
      try {
        window.localStorage.removeItem(legacyAgentSettingsStorageKey)
      } catch {
        // The canonical copy is durable even if legacy cleanup is unavailable.
      }
    }
    return settings
  } catch {
    return { ...defaultAgentSettings }
  }
}

function parseAgentSettings(raw: string | null): Partial<AgentSettings> {
  if (!raw) return {}
  try {
    const parsed = JSON.parse(raw)
    return parsed && typeof parsed === 'object' ? parsed as Partial<AgentSettings> : {}
  } catch {
    return {}
  }
}

function storedMcpDisabled(settings: Partial<AgentSettings>): string[] {
  return Array.isArray(settings.mcpDisabled)
    ? settings.mcpDisabled.filter((name): name is string => typeof name === 'string')
    : []
}

export function writeAgentSettings(settings: Required<AgentSettings>): boolean {
  try {
    window.localStorage.setItem(agentSettingsStorageKey, JSON.stringify({
      memory: settings.memory,
      mcpDisabled: settings.mcpDisabled,
    }))
    return true
  } catch {
    return false
  }
}

export function readChatMode(): ChatMode {
  if (typeof window === 'undefined') return ''
  try {
    const raw = window.localStorage.getItem(chatModeStorageKey)?.trim()
    if (raw) return parseRuntimeModelSpec(raw) ?? ''
  } catch {
    // Local storage can be unavailable in restricted browser contexts.
  }
  return ''
}

export function writeChatMode(mode: ChatMode) {
  try {
    window.localStorage.setItem(chatModeStorageKey, mode)
  } catch {
    // Local storage can be unavailable in restricted browser contexts.
  }
}

export function readReasoningMode(model: ChatMode): ReasoningMode | undefined {
  if (typeof window === 'undefined' || !model) return undefined
  try {
    const stored = JSON.parse(window.localStorage.getItem(reasoningModeStorageKey) ?? '{}')
    const mode = stored && typeof stored === 'object'
      ? reasoningMode((stored as Record<string, unknown>)[model])
      : undefined
    if (mode) return mode
    const legacyMode = reasoningMode(window.localStorage.getItem(legacyReasoningModeStorageKey))
    if (!legacyMode) return undefined
    writeReasoningMode(legacyMode, model)
    return legacyMode
  } catch {
    // Local storage can be unavailable in restricted browser contexts.
  }
  return undefined
}

export function writeReasoningMode(mode: ReasoningMode, model: ChatMode) {
  if (!model) return
  try {
    const stored = JSON.parse(window.localStorage.getItem(reasoningModeStorageKey) ?? '{}')
    const modes = stored && typeof stored === 'object' ? stored : {}
    window.localStorage.setItem(reasoningModeStorageKey, JSON.stringify({ ...modes, [model]: mode }))
    window.localStorage.removeItem(legacyReasoningModeStorageKey)
  } catch {
    // Local storage can be unavailable in restricted browser contexts.
  }
}

function reasoningMode(value: unknown): ReasoningMode | undefined {
  if (
    value === 'off'
    || value === 'minimal'
    || value === 'low'
    || value === 'medium'
    || value === 'high'
    || value === 'xhigh'
    || value === 'max'
  ) return value
  return undefined
}

export function loadRuntimeThreadIDs(): Set<string> {
  try {
    const value = JSON.parse(localStorage.getItem(runtimeThreadIDsStorageKey) ?? '[]')
    return new Set(
      Array.isArray(value) ? value.filter((id): id is string => typeof id === 'string') : [],
    )
  } catch {
    return new Set()
  }
}

export function storeRuntimeThreadIDs(ids: Set<string>) {
  localStorage.setItem(runtimeThreadIDsStorageKey, JSON.stringify([...ids]))
}
