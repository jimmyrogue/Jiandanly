import type { AgentRunEvent } from '@shejane/runtime-sdk'
import { createTranslator, type Translator } from '../../shared/i18n/i18n'
import type { ChatMessage, Conversation, MessageStatus } from '../../shared/local-data/types'
import { parseRuntimeModelSpec, type LocalRun, type LocalThreadItem, type LocalThreadSnapshot } from '../../runtime/client'
import { timelineItem } from './chatStore'

/** Build the disposable Electron cache from one authoritative Runtime thread. */
export function projectRuntimeThread(
  snapshot: LocalThreadSnapshot,
  existing?: Conversation,
  t: Translator = createTranslator('zh'),
): Conversation {
  const runs = new Map(snapshot.runs.map((run) => [run.id, run]))
  const eventsByRun = new Map<string, AgentRunEvent[]>()
  for (const event of snapshot.events) {
    const items = eventsByRun.get(event.run_id) ?? []
    items.push({
      id: event.id,
      run_id: event.run_id,
      seq: event.seq,
      event_type: event.event_type,
      payload: event.payload,
      created_at: event.created_at,
    })
    eventsByRun.set(event.run_id, items)
  }

  const existingByID = new Map((existing?.messages ?? []).map((message) => [message.id, message]))
  const messages = [...snapshot.items]
    .sort((left, right) => left.position - right.position || left.id.localeCompare(right.id))
    .flatMap((item) => isHiddenTranscriptItem(item)
      ? []
      : [projectRuntimeItem(
          item,
          runs.get(item.run_id ?? ''),
          eventsByRun,
          snapshot.event_high_watermarks ?? {},
          existingByID,
          t,
        )])

  const metadata = objectValue(snapshot.thread.metadata)
  const model = parseRuntimeModelSpec(String(metadata.model ?? ''))
  return {
    id: snapshot.thread.id,
    title: snapshot.thread.title,
    archived: Boolean(snapshot.thread.archived_at || metadata.archived),
    ...(typeof metadata.pinned === 'boolean' ? { pinned: metadata.pinned } : {}),
    createdAt: snapshot.thread.created_at,
    updatedAt: snapshot.thread.updated_at,
    ...(model ? { model } : existing?.model ? { model: existing.model } : {}),
    ...(projectValue(metadata.project) ? { project: projectValue(metadata.project) } : {}),
    ...(workspaceValue(metadata.workspace) ? { workspace: workspaceValue(metadata.workspace) } : {}),
    messages,
  }
}

function isHiddenTranscriptItem(item: LocalThreadItem): boolean {
  return item.item_type === 'user_message'
    && objectValue(item.metadata).hidden_from_transcript === true
}

function projectRuntimeItem(
  item: LocalThreadItem,
  run: LocalRun | undefined,
  eventsByRun: Map<string, AgentRunEvent[]>,
  eventHighWatermarks: Record<string, number>,
  existingByID: Map<string, ChatMessage>,
  t: Translator,
): ChatMessage {
  const id = item.client_id || item.id
  const existing = existingByID.get(id)
  if (item.item_type === 'user_message') {
    const attachments = attachmentValues(item.metadata, run)
    const pluginSelection = pluginSelectionValue(item.metadata)
    return {
      ...(existing ?? {}),
      id,
      role: 'user',
      content: item.content,
      createdAt: item.created_at,
      status: 'done',
      ...(item.run_id ? { runId: item.run_id } : {}),
      ...(attachments.length ? { attachments } : {}),
      pluginReferences: pluginSelection.references.length ? pluginSelection.references : undefined,
      pluginCommand: pluginSelection.command,
    }
  }

  const runEvents = item.run_id ? eventsByRun.get(item.run_id) ?? [] : []
  const agentEvents = runEvents
    .map((event) => timelineItem(event, t))
    .filter((event): event is NonNullable<typeof event> => event !== null)
  const status = assistantStatus(item.status, run?.status)
  const fallback = [...agentEvents].reverse().find(
    (event) => event.type === 'run.failed' || event.type === 'run.cleanup_required',
  )?.label
  return {
    ...(existing ?? {}),
    id,
    role: 'assistant',
    content: item.content || (status === 'error' ? fallback ?? '' : ''),
    createdAt: item.created_at,
    status,
    ...(item.run_id ? { runId: item.run_id } : {}),
    ...(item.run_id && eventHighWatermarks[item.run_id] !== undefined
      ? { lastEventSeq: Math.max(existing?.lastEventSeq ?? 0, eventHighWatermarks[item.run_id]) }
      : {}),
    ...(run?.command_id ? { commandId: run.command_id } : {}),
    ...(agentEvents.length ? { agentEvents } : {}),
  }
}

function pluginSelectionValue(value: unknown): {
  references: NonNullable<ChatMessage['pluginReferences']>
  command?: NonNullable<ChatMessage['pluginCommand']>
} {
  const selection = objectValue(objectValue(value).plugin_selection)
  const references = Array.isArray(selection.references)
    ? selection.references.flatMap((value) => {
      const item = objectValue(value)
      return typeof item.plugin_id === 'string' && item.plugin_id
        && typeof item.name === 'string' && item.name
        && typeof item.digest === 'string' && item.digest
        ? [{ pluginId: item.plugin_id, name: item.name, digest: item.digest }]
        : []
    }).slice(0, 32)
    : []
  const item = objectValue(selection.command)
  const command = typeof item.plugin_id === 'string' && item.plugin_id
    && typeof item.plugin_name === 'string' && item.plugin_name
    && typeof item.command_id === 'string' && item.command_id
    && typeof item.title === 'string' && item.title
    && typeof item.digest === 'string' && item.digest
    ? {
      pluginId: item.plugin_id,
      pluginName: item.plugin_name,
      commandId: item.command_id,
      title: item.title,
      digest: item.digest,
    }
    : undefined
  return { references, ...(command ? { command } : {}) }
}

function attachmentValues(
  value: unknown,
  run?: LocalRun,
): NonNullable<ChatMessage['attachments']> {
  const attachments = objectValue(value).attachments
  if (!Array.isArray(attachments)) return []
  const inputsByIndex = new Map((run?.inputs ?? []).map((input) => [input.client_index, input]))
  const valid = attachments.flatMap((value, index) => {
    const item = objectValue(value)
    const input = inputsByIndex.get(index)
    return typeof item.path === 'string' && item.path && typeof item.name === 'string' && item.name
      ? [{
        path: item.path,
        name: item.name,
        ...(input
          ? { inputId: input.input_id, mediaType: input.media_type, bytes: input.bytes }
          : typeof item.input_id === 'string' && item.input_id
            ? {
              inputId: item.input_id,
              ...(typeof item.media_type === 'string' && item.media_type ? { mediaType: item.media_type } : {}),
              ...(typeof item.bytes === 'number' && item.bytes >= 0 ? { bytes: item.bytes } : {}),
            }
            : {}),
      }]
      : []
  }).slice(0, 10)
  return valid.map((attachment) => ({
    ...attachment,
    ...(run?.id && attachment.inputId ? {
      runId: run.id,
    } : {}),
  }))
}

function assistantStatus(itemStatus: string, runStatus?: LocalRun['status']): MessageStatus {
  if (itemStatus === 'completed') return 'done'
  if (itemStatus === 'failed' || itemStatus === 'cleanup_required') return 'error'
  if (itemStatus === 'canceled') return 'done'
  if (runStatus === 'waiting_permission') return 'waiting_permission'
  if (runStatus === 'waiting_input') return 'waiting_input'
  return 'streaming'
}

function objectValue(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {}
}

function projectValue(value: unknown): Conversation['project'] | undefined {
  const item = objectValue(value)
  return typeof item.name === 'string' && item.name ? { name: item.name } : undefined
}

function workspaceValue(value: unknown): Conversation['workspace'] | undefined {
  const item = objectValue(value)
  if (typeof item.path !== 'string' || !item.path || typeof item.label !== 'string') return undefined
  return {
    path: item.path,
    label: item.label,
    authorized: Boolean(item.authorized),
  }
}
