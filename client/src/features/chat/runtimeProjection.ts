import {
  applyRunPresentationChange,
  createRunPresentationState,
  isSubagentLifecycleEvent,
  type AgentRunEvent,
  type RunPresentationItem,
  type RunPresentationState,
  type SubagentLifecyclePayload,
} from '@shejane/runtime-sdk'
import { createTranslator, type Translator } from '../../shared/i18n/i18n'
import type {
  AgentSubagentProjection,
  AgentSubagentReceiptStatus,
  AgentSubagentStatus,
  ChatMessage,
  Conversation,
  AgentTimelineItem,
  LocalFileRef,
  MessageStatus,
} from '../../shared/local-data/types'
import {
  listLocalRunEvents,
  parseRuntimeModelSpec,
  type LocalRun,
  type LocalThreadItem,
  type LocalThreadSnapshot,
  type RuntimeConnection,
} from '../../runtime/client'
import { projectTransientAssistantText, timelineItem } from './chatStore'

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
          snapshot.presentations,
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

/** Tracks assembled tool args for a single stream session.
 *  Keeps completed rows human-readable with their original request payload.
 */
export type ToolArgsByCallId = Map<string, Record<string, unknown>>

export async function projectRuntimeThreadCache(
  snapshot: LocalThreadSnapshot,
  existing: Conversation | undefined,
  config: RuntimeConnection,
  t: Translator,
): Promise<Conversation> {
  const conversation = projectRuntimeThread(snapshot, existing, t)
  if (!snapshot.events_truncated) return conversation

  for (const message of conversation.messages) {
    if (
      message.role !== 'assistant'
      || !message.runId
      || (message.status !== 'waiting_permission' && message.status !== 'waiting_input')
    ) {
      continue
    }
    const events = await listLocalRunEvents(message.runId, message.lastEventSeq ?? 0, config)
    const seenEventIDs = new Set(
      (message.agentEvents ?? []).flatMap((event) => event.eventId ? [event.eventId] : []),
    )
    const toolArgsByCallId: ToolArgsByCallId = new Map()
    for (const event of events) {
      recordLocalEventCursor(message, event)
      processStreamEvent(message, event, seenEventIDs, toolArgsByCallId, t)
    }
    finalizeLocalRunStatus(message)
  }
  return conversation
}

/** Process one Runtime stream event into the disposable Client message
 *  projection. Single owner of the event-processing rules shared by live
 *  streaming (`runStreaming`) and truncated-thread replay
 *  (`projectRuntimeThreadCache`): presentation sync, transient text, token
 *  accumulation, tool-arg cache, subagent lifecycle, timeline append, and
 *  office-file edit detection. `onOfficeFileOpened` is optional because the
 *  replay path has no live document to open.
 */
export function processStreamEvent(
  message: ChatMessage,
  event: AgentRunEvent,
  seenEventIDs: Set<string>,
  toolArgsByCallId: ToolArgsByCallId,
  t: Translator,
  onOfficeFileOpened?: (ref: LocalFileRef) => void,
) {
  if (event.event_type === 'llm.delta') {
    const presentationOwnsDelta = Boolean(
      message.runId
      && (event.presentation_change || event.presentation_changes?.length),
    )
    if (presentationOwnsDelta) {
      if (event.id && seenEventIDs.has(event.id)) return
      if (event.id) seenEventIDs.add(event.id)
      applyRunPresentationEvent(message, event)
    }
    return
  }
  applyRunPresentationEvent(message, event)
  if (!message.presentation) {
    message.content = projectTransientAssistantText(message.content, event)
  }
  if (event.event_type === 'llm.usage') {
    if (event.id && seenEventIDs.has(event.id)) {
      return
    }
    if (event.id) {
      seenEventIDs.add(event.id)
    }
    const payload = event.payload ?? {}
    const input = Number(payload.input_tokens) || 0
    const output = Number(payload.output_tokens) || 0
    if (input > 0 || output > 0) {
      message.tokens = (message.tokens ?? 0) + input + output
    }
    return
  }

  const alreadySeen = Boolean(event.id && seenEventIDs.has(event.id))
  if (event.id) {
    seenEventIDs.add(event.id)
  }
  if (event.event_type === 'run.completed') {
    const payload = event.payload ?? {}
    const input = Number(payload.input_tokens) || 0
    const output = Number(payload.output_tokens) || 0
    if (input > 0 || output > 0) {
      message.tokens = input + output
    }
  }
  if (event.event_type === 'model.selected') {
    const payload = event.payload ?? {}
    message.runMode = {
      resolved: String(payload.label ?? payload.resolved_model_id ?? ''),
      reason: String(payload.reason ?? ''),
    }
  }

  if (event.event_type === 'tool.requested') {
    const payload = event.payload ?? {}
    const id = String(payload.tool_call_id ?? '')
    const args = payload.arguments
    if (id && args && typeof args === 'object' && !Array.isArray(args)) {
      toolArgsByCallId.set(id, args as Record<string, unknown>)
    }
  } else if (event.event_type === 'tool.completed' || event.event_type === 'tool.failed') {
    const payload = event.payload ?? {}
    const id = String(payload.tool_call_id ?? '')
    const cached = id ? toolArgsByCallId.get(id) : undefined
    if (cached && !payload.arguments) {
      event = { ...event, payload: { ...payload, arguments: cached } }
    }
    if (event.event_type === 'tool.completed' && !alreadySeen) {
      const editedFile = detectOfficeFileEdited(event.payload)
      if (editedFile) onOfficeFileOpened?.(editedFile)
    }
  }

  if (!alreadySeen) {
    const subagents = applySubagentLifecycleEvent(message.subagents, event)
    if (subagents.length) {
      message.subagents = subagents
    }
  }

  const item = timelineItem(event, t)
  if (item) {
    if (!alreadySeen) {
      message.agentEvents = [...(message.agentEvents ?? []), item]
    }
  }
}

const officeWriteToolNames = new Set<string>([
  'office.find_replace',
  'office.insert_paragraph',
  'office.update_paragraph',
  'office.delete_paragraph',
  'office.apply_style',
  'office.set_cells',
  'office.set_formula',
  'office.set_cell_format',
  'office.merge_cells',
  'office.add_row',
  'office.create_pptx',
  'office.add_slide',
  'office.update_slide',
  'office.delete_slide',
  'office.reorder_slides',
  'office.set_slide_title',
  'office.set_slide_bullets',
  'office.set_slide_notes',
  'office.add_image_to_slide',
])

function detectOfficeFileEdited(payload: AgentRunEvent['payload']): LocalFileRef | null {
  if (!payload) return null
  const toolName = String((payload as Record<string, unknown>).tool ?? (payload as Record<string, unknown>).name ?? '')
  if (!officeWriteToolNames.has(toolName)) return null
  const result = (payload as Record<string, unknown>).result
  const resultObj = result && typeof result === 'object' && !Array.isArray(result)
    ? result as Record<string, unknown>
    : null
  if (!resultObj || String(resultObj.ok ?? '') !== 'true') return null
  const editedPath = String(resultObj.edited_path ?? '')
  if (!editedPath) return null
  const kindRaw = String(resultObj.kind ?? '')
  const lower = editedPath.toLowerCase()
  const kind: LocalFileRef['kind'] = kindRaw === 'word' || kindRaw === 'excel' || kindRaw === 'powerpoint'
    ? kindRaw
    : lower.endsWith('.xlsx')
      ? 'excel'
      : lower.endsWith('.pptx')
        ? 'powerpoint'
        : 'word'
  const name = editedPath.split(/[\\/]/).pop() || editedPath
  return { path: editedPath, kind, name }
}

export function recordLocalEventCursor(message: ChatMessage, event: AgentRunEvent) {
  if (Number.isSafeInteger(event.seq) && Number(event.seq) >= 0) {
    message.lastEventSeq = Math.max(message.lastEventSeq ?? 0, Number(event.seq))
  }
}

/** Fold one transient delta into the message's streaming text buffer. */
export function appendLocalDelta(message: ChatMessage, delta: string, event: AgentRunEvent, seenEventIDs: Set<string>) {
  const presentationOwnsDelta = Boolean(
    message.runId
    && (event.presentation_change || event.presentation_changes?.length),
  )
  if (presentationOwnsDelta || (event.id && seenEventIDs.has(event.id))) return
  if (event.id) seenEventIDs.add(event.id)
  message.content = projectTransientAssistantText(message.content, {
    ...event,
    payload: { ...(event.payload ?? {}), content: delta },
  })
}

export function finalizeLocalRunStatus(message: ChatMessage) {
  const events = message.agentEvents ?? []
  if (events.some((event) => event.type === 'run.failed' || event.type === 'run.cleanup_required')) {
    message.status = 'error'
    if (!message.content.trim()) {
      message.content = latestRunFailedLabel(message)
    }
    return
  }
  if (events.some((event) => event.type === 'run.completed')) {
    message.status = 'done'
    return
  }
  if (hasPendingPermission(events)) {
    message.status = 'waiting_permission'
    return
  }
  if (hasPendingPlanApproval(events)) {
    message.status = 'waiting_input'
    return
  }
  message.status = hasPendingQuestion(events) ? 'waiting_input' : 'done'
}

function hasPendingPermission(events: AgentTimelineItem[]): boolean {
  const pending = new Set<string>()
  for (const event of events) {
    if (event.type === 'permission.required' && event.permissionRequestId) {
      pending.add(event.permissionRequestId)
    }
    if (event.type === 'tool.reconciliation_required' && event.permissionRequestId) {
      pending.add(event.permissionRequestId)
    }
    if (event.type === 'tool.reconciliation_resolved' && event.permissionRequestId) {
      pending.delete(event.permissionRequestId)
    }
    if (event.type === 'permission.resolved' && event.permissionRequestId) {
      pending.delete(event.permissionRequestId)
    }
  }
  return pending.size > 0
}

function hasPendingQuestion(events: AgentTimelineItem[]): boolean {
  const pending = new Set<string>()
  for (const event of events) {
    if (event.type === 'question.asked' && event.questionRequestId) {
      pending.add(event.questionRequestId)
    }
    if (event.type === 'question.answered' && event.questionRequestId) {
      pending.delete(event.questionRequestId)
    }
  }
  return pending.size > 0
}

function hasPendingPlanApproval(events: AgentTimelineItem[]): boolean {
  const pending = new Set<string>()
  for (const event of events) {
    if (event.type === 'plan.approval_required' && event.planApprovalRequestId) {
      pending.add(event.planApprovalRequestId)
    }
    if (event.type === 'plan.approval_resolved' && event.planApprovalRequestId) {
      pending.delete(event.planApprovalRequestId)
    }
  }
  return pending.size > 0
}

export function latestRunFailedLabel(message: ChatMessage): string {
  return [...(message.agentEvents ?? [])].reverse().find(
    (event) => event.type === 'run.failed' || event.type === 'run.cleanup_required',
  )?.label ?? ''
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
  presentations: LocalThreadSnapshot['presentations'] | undefined,
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
  const presentationSnapshot = item.run_id ? presentations?.[item.run_id] : undefined
  const presentation = presentationSnapshot
    ? createRunPresentationState(presentationSnapshot)
    : presentations === undefined && item.run_id
      ? projectLegacyRunPresentation(item.run_id, item, runEvents)
      : undefined
  const finalAnswer = presentation?.snapshot.items?.find(
    (presentationItem) => presentationItem.kind === 'final_answer',
  )
  const fallback = [...agentEvents].reverse().find(
    (event) => event.type === 'run.failed' || event.type === 'run.cleanup_required',
  )?.label
  return {
    ...(existing ?? {}),
    id,
    role: 'assistant',
    content: finalAnswer?.content || item.content || (status === 'error' ? fallback ?? '' : ''),
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
    agentEvents: agentEvents.length ? agentEvents : undefined,
    presentation,
    // This is a rebuild from Runtime truth. Explicitly clear an older Client
    // projection when Runtime now reports no subagent invocations.
    subagents: subagents.length ? subagents : undefined,
  }
}

/** Compatibility Adapter for Runtime builds that predate `presentations`. */
export function projectLegacyRunPresentation(
  runID: string,
  assistantItem: LocalThreadItem,
  events: AgentRunEvent[],
): RunPresentationState | undefined {
  const items = new Map<string, RunPresentationItem>()
  const toolIDs = new Map<string, string>()
  const decisions = new Map<string, string>()
  for (const event of events) {
    const payload = event.payload ?? {}
    const seq = Number(event.seq)
    if (!Number.isSafeInteger(seq) || seq < 1) continue
    const createdAt = event.created_at ?? assistantItem.created_at
    const toolCallID = String(payload.tool_call_id ?? '')
    if (event.event_type === 'tool.requested' && toolCallID) {
      const id = `tool-call:${toolCallID}`
      toolIDs.set(toolCallID, id)
      items.set(id, {
        id,
        kind: 'tool',
        status: 'in_progress',
        order: { event_seq: seq, slot: 0 },
        revision: seq,
        source: { kind: 'run_event', id: event.id ?? id },
        tool_call_id: toolCallID,
        tool_name: String(payload.name ?? payload.tool ?? 'tool'),
        risk: 'unknown',
        created_at: createdAt,
        updated_at: createdAt,
        completed_at: null,
      })
      continue
    }
    if (
      (event.event_type === 'tool.completed' || event.event_type === 'tool.failed')
      && toolCallID
    ) {
      const id = toolIDs.get(toolCallID) ?? `tool-call:${toolCallID}`
      const current = items.get(id)
      if (current?.kind === 'tool') {
        items.set(id, {
          ...current,
          status: event.event_type === 'tool.completed' ? 'completed' : 'failed',
          revision: seq,
          updated_at: createdAt,
          completed_at: createdAt,
        })
      }
      continue
    }
    const decisionKind = legacyDecisionKind(event.event_type)
    const requestID = String(payload.request_id ?? '')
    const decisionStarted = event.event_type.endsWith('.required')
      || event.event_type === 'question.asked'
    if (decisionKind && requestID && decisionStarted) {
      const id = `wait:${requestID}`
      decisions.set(requestID, id)
      items.set(id, {
        id,
        kind: decisionKind,
        status: 'waiting',
        order: { event_seq: seq, slot: 0 },
        revision: seq,
        source: { kind: 'run_event', id: event.id ?? id },
        request_id: requestID,
        summary: String(payload.summary ?? payload.tool ?? decisionKind),
        created_at: createdAt,
        updated_at: createdAt,
        completed_at: null,
      })
      continue
    }
    if (requestID && decisions.has(requestID)) {
      const id = decisions.get(requestID)!
      const current = items.get(id)
      if (current && 'request_id' in current) {
        items.set(id, {
          ...current,
          status: 'completed',
          revision: seq,
          updated_at: createdAt,
          completed_at: createdAt,
        })
      }
      continue
    }
    if (event.event_type === 'artifact.created' && payload.artifact_id) {
      const artifactID = String(payload.artifact_id)
      items.set(`artifact:${artifactID}`, {
        id: `artifact:${artifactID}`,
        kind: 'artifact',
        status: 'completed',
        order: { event_seq: seq, slot: 0 },
        revision: seq,
        source: { kind: 'run_event', id: event.id ?? artifactID },
        artifact_id: artifactID,
        title: String(payload.title ?? artifactID),
        content_type: String(payload.media_type ?? 'application/octet-stream'),
        created_at: createdAt,
      })
      continue
    }
    if (event.event_type === 'run.failed' || event.event_type === 'run.canceled') {
      items.set(`notice:${event.id ?? seq}`, {
        id: `notice:${event.id ?? seq}`,
        kind: 'notice',
        status: event.event_type === 'run.failed' ? 'failed' : 'canceled',
        order: { event_seq: seq, slot: 0 },
        revision: seq,
        source: { kind: 'run_event', id: event.id ?? String(seq) },
        severity: event.event_type === 'run.failed' ? 'error' : 'warning',
        message: event.event_type === 'run.failed' ? 'Run failed' : 'Run canceled',
        created_at: createdAt,
      })
      continue
    }
    if (event.event_type === 'run.completed' && assistantItem.content) {
      items.set(`answer:${assistantItem.id}`, {
        id: `answer:${assistantItem.id}`,
        kind: 'final_answer',
        status: 'completed',
        order: { event_seq: seq, slot: 0 },
        revision: seq,
        source: { kind: 'thread_item', id: assistantItem.id },
        content: assistantItem.content,
        created_at: assistantItem.created_at,
        completed_at: assistantItem.completed_at ?? createdAt,
      })
    }
  }
  if (!items.size) return undefined
  return createRunPresentationState({
    schema_version: 1,
    run_id: runID,
    items: [...items.values()].sort((left, right) => (
      left.order.event_seq - right.order.event_seq
      || left.order.slot - right.order.slot
      || left.id.localeCompare(right.id)
    )),
    event_high_watermark: Math.max(0, ...events.map((event) => Number(event.seq) || 0)),
  })
}

function legacyDecisionKind(eventType: string): 'approval' | 'question' | 'plan' | 'reconciliation' | undefined {
  if (eventType.startsWith('permission.')) return 'approval'
  if (eventType.startsWith('question.')) return 'question'
  if (eventType.startsWith('plan.')) return 'plan'
  if (eventType.startsWith('tool.reconciliation')) return 'reconciliation'
  return undefined
}

export function applyRunPresentationEvent(message: ChatMessage, event: AgentRunEvent): void {
  const changes = event.presentation_changes
    ?? (event.presentation_change ? [event.presentation_change] : [])
  if (!changes.length || !message.runId) return
  let current = message.presentation ?? createRunPresentationState({
    schema_version: 1,
    run_id: message.runId,
    items: [],
    event_high_watermark: 0,
  })
  for (const change of changes) {
    current = applyRunPresentationChange(current, change)
    if (change.kind === 'item.upsert' && change.item.kind === 'final_answer') {
      message.content = change.item.content
    }
  }
  message.presentation = current
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
