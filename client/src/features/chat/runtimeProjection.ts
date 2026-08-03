import {
  isSubagentLifecycleEvent,
  type AgentRunEvent,
  type SubagentLifecyclePayload,
} from '@shejane/runtime-sdk'
import { createTranslator, type Translator } from '../../shared/i18n/i18n'
import type {
  AgentSubagentProjection,
  AgentSubagentReceiptStatus,
  AgentSubagentStatus,
  ChatMessage,
  Conversation,
  MessageStatus,
} from '../../shared/local-data/types'
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
          snapshot.events_truncated,
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
  eventsTruncated: boolean,
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
  const subagents = projectSubagents(run, runEvents)
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
      ? {
          lastEventSeq: eventsTruncated
            ? eventHighWatermarks[item.run_id]
            : Math.max(existing?.lastEventSeq ?? 0, eventHighWatermarks[item.run_id]),
        }
      : {}),
    ...(run?.command_id ? { commandId: run.command_id } : {}),
    ...(agentEvents.length ? { agentEvents } : {}),
    // This is a rebuild from Runtime truth. Explicitly clear an older Client
    // projection when Runtime now reports no subagent invocations.
    subagents: subagents.length ? subagents : undefined,
  }
}

/** Advance the disposable Client projection from one Runtime event.
 *
 * Runtime's durable `operation_id` is the identity. Generic `task` tool
 * events are accepted only as a compatibility projection for older Runtime
 * builds; the first real lifecycle event replaces that temporary row. */
export function applySubagentLifecycleEvent(
  current: AgentSubagentProjection[] | undefined,
  event: AgentRunEvent,
): AgentSubagentProjection[] {
  const items = current ?? []
  if (isSubagentLifecycleEvent(event)) {
    return replaceSubagentProjection(items, projectLifecyclePayload(event.payload))
  }
  if (isLegacySubagentTerminalEvent(event.event_type)) {
    return applyLegacySubagentTerminalEvent(items, event)
  }
  return applyLegacyTaskEvent(items, event)
}

function projectSubagents(run: LocalRun | undefined, events: AgentRunEvent[]): AgentSubagentProjection[] {
  // Presence of the field means this Runtime supports the complete P4
  // current-state projection. Even an empty array is authoritative: replaying
  // older events here could resurrect an operation Runtime intentionally no
  // longer reports. Events remain the rebuild source only for older Runtime
  // versions that omit the field entirely.
  if (Array.isArray(run?.subagent_invocations)) {
    return projectSnapshotSubagents(run)
  }
  return events.reduce<AgentSubagentProjection[]>(applySubagentLifecycleEvent, [])
}

function replaceSubagentProjection(
  current: AgentSubagentProjection[],
  next: AgentSubagentProjection,
): AgentSubagentProjection[] {
  const withoutMatchingLegacy = current.filter((item) =>
    !(isLegacyProjection(item)
      && item.operationId !== next.operationId
      && item.toolCallId === next.toolCallId),
  )
  const index = withoutMatchingLegacy.findIndex((item) => item.operationId === next.operationId)
  if (index < 0) return [...withoutMatchingLegacy, next]
  const updated = [...withoutMatchingLegacy]
  updated[index] = next
  return updated
}

function isLegacyProjection(item: AgentSubagentProjection): boolean {
  return item.operationId.startsWith('legacy-task:')
}

function projectLifecyclePayload(payload: SubagentLifecyclePayload): AgentSubagentProjection {
  return {
    operationId: payload.operation_id,
    parentRunId: payload.parent_run_id,
    ...(payload.parent_operation_id ? { parentOperationId: payload.parent_operation_id } : {}),
    toolCallId: payload.tool_call_id,
    subagentType: payload.subagent_type,
    description: payload.description,
    status: publicSubagentStatus(payload.status, payload.receipt_status),
    receiptStatus: payload.receipt_status,
    attemptCount: payload.attempt_count,
    usage: projectSubagentUsage(payload.usage),
    ...(payload.error_type ? { errorType: payload.error_type } : {}),
    createdAt: payload.created_at,
    ...(payload.started_at ? { startedAt: payload.started_at } : {}),
    ...(payload.completed_at ? { completedAt: payload.completed_at } : {}),
    updatedAt: payload.updated_at,
  }
}

function publicSubagentStatus(
  status: AgentSubagentStatus,
  receiptStatus: AgentSubagentReceiptStatus,
): AgentSubagentStatus {
  return receiptStatus === 'outcome_unknown' ? 'unknown' : status
}

function isLegacySubagentTerminalEvent(eventType: string): boolean {
  return eventType === 'subagent.completed'
    || eventType === 'subagent.failed'
    || eventType === 'subagent.canceled'
    || eventType === 'subagent.outcome_unknown'
}

/** Old Runtime builds emitted an incomplete inferred terminal event instead
 * of generic `tool.completed` / `tool.failed`. It may settle only an existing
 * synthetic task row; all non-terminal state still comes from task events. */
function applyLegacySubagentTerminalEvent(
  current: AgentSubagentProjection[],
  event: AgentRunEvent,
): AgentSubagentProjection[] {
  const payload = objectValue(event.payload)
  const toolCallId = stringValue(payload.tool_call_id)
  const previous = current.find((item) => toolCallId && item.toolCallId === toolCallId)
  if (!previous) return current

  const outcomeUnknown = event.event_type === 'subagent.outcome_unknown'
    || payload.status === 'unknown'
    || payload.receipt_status === 'outcome_unknown'
  const canceled = event.event_type === 'subagent.canceled'
  const failed = !outcomeUnknown
    && !canceled
    && (event.event_type === 'subagent.failed' || payload.status === 'error' || payload.status === 'failed')
  const status: AgentSubagentStatus = outcomeUnknown
    ? 'unknown'
    : canceled
      ? 'canceled'
      : failed
        ? 'failed'
        : 'completed'
  const receiptStatus: AgentSubagentReceiptStatus = outcomeUnknown
    ? 'outcome_unknown'
    : canceled
      ? 'canceled'
      : failed
        ? 'failed'
        : 'completed'
  const operationId = stringValue(payload.operation_id) || previous.operationId
  const completedAt = stringValue(payload.completed_at) || event.created_at || previous.completedAt
  const next: AgentSubagentProjection = {
    ...previous,
    operationId,
    status,
    receiptStatus,
    subagentType: stringValue(payload.subagent_type) || previous.subagentType,
    description: stringValue(payload.description) || previous.description,
    ...(stringValue(payload.error_type) ? { errorType: stringValue(payload.error_type) } : {}),
    ...(completedAt ? { completedAt } : {}),
    updatedAt: stringValue(payload.updated_at) || event.created_at || previous.updatedAt,
  }
  return replaceSubagentProjection(current, next)
}

function applyLegacyTaskEvent(
  current: AgentSubagentProjection[],
  event: AgentRunEvent,
): AgentSubagentProjection[] {
  const payload = objectValue(event.payload)
  if (stringValue(payload.tool) !== 'task') return current
  const toolCallId = stringValue(payload.tool_call_id)
  const parentRunId = event.run_id || ''
  if (!toolCallId || !parentRunId) return current
  const operationId = `legacy-task:${parentRunId}:${toolCallId}`
  const previous = current.find((item) => item.operationId === operationId)
  // Once a durable operation occupies this tool call, generic tool events may
  // not overwrite its lifecycle state.
  if (!previous && current.some((item) => item.toolCallId === toolCallId)) return current

  if (event.event_type === 'tool.requested') {
    const args = objectValue(payload.arguments)
    const createdAt = event.created_at || ''
    if (!createdAt) return current
    return [
      ...current.filter((item) => item.operationId !== operationId),
      {
        operationId,
        parentRunId,
        toolCallId,
        subagentType: stringValue(args.subagent_type) || stringValue(args.subagent_name),
        description: stringValue(args.description) || stringValue(args.task_description),
        status: 'running',
        receiptStatus: 'running',
        attemptCount: 0,
        usage: emptySubagentUsage(),
        createdAt,
        startedAt: createdAt,
        updatedAt: createdAt,
      },
    ]
  }
  if (!previous || (event.event_type !== 'tool.completed' && event.event_type !== 'tool.failed')) {
    return current
  }
  const outcomeUnknown = event.event_type === 'tool.failed'
    && (
      payload.error_code === 'tool_outcome_unknown'
      || payload.receipt_status === 'outcome_unknown'
      || payload.status === 'unknown'
    )
  const failed = event.event_type === 'tool.failed' && !outcomeUnknown
  const completedAt = event.created_at || previous.updatedAt
  return current.map((item) => item.operationId === operationId
    ? {
        ...item,
        status: outcomeUnknown ? 'unknown' : failed ? 'failed' : 'completed',
        receiptStatus: outcomeUnknown ? 'outcome_unknown' : failed ? 'failed' : 'completed',
        completedAt,
        updatedAt: completedAt,
      }
    : item)
}

function emptySubagentUsage(): AgentSubagentProjection['usage'] {
  return {
    modelCalls: 0,
    inputTokens: 0,
    outputTokens: 0,
    unmeteredCalls: 0,
    outcomeUnknownCalls: 0,
  }
}

function projectSnapshotSubagents(run: LocalRun | undefined): AgentSubagentProjection[] {
  return (run?.subagent_invocations ?? []).map((item) => ({
    operationId: item.operation_id,
    parentRunId: item.parent_run_id,
    ...(item.parent_operation_id ? { parentOperationId: item.parent_operation_id } : {}),
    toolCallId: item.tool_call_id,
    subagentType: item.subagent_type,
    description: item.description,
    status: publicSubagentStatus(item.status, item.receipt_status),
    receiptStatus: item.receipt_status,
    attemptCount: item.attempt_count,
    usage: projectSubagentUsage(item.usage),
    ...(item.error_type ? { errorType: item.error_type } : {}),
    createdAt: item.created_at,
    ...(item.started_at ? { startedAt: item.started_at } : {}),
    ...(item.completed_at ? { completedAt: item.completed_at } : {}),
    updatedAt: item.updated_at,
  }))
}

function stringValue(value: unknown): string {
  return typeof value === 'string' ? value : ''
}

function projectSubagentUsage(
  usage: {
    model_calls: number
    input_tokens: number
    output_tokens: number
    unmetered_calls: number
    outcome_unknown_calls: number
  } | null | undefined,
): AgentSubagentProjection['usage'] {
  return {
    modelCalls: usage?.model_calls ?? 0,
    inputTokens: usage?.input_tokens ?? 0,
    outputTokens: usage?.output_tokens ?? 0,
    unmeteredCalls: usage?.unmetered_calls ?? 0,
    outcomeUnknownCalls: usage?.outcome_unknown_calls ?? 0,
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
