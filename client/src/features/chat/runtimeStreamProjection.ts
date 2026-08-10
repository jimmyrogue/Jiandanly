import {
  applyRunPresentationChange,
  createRunPresentationState,
  type AgentRunEvent,
} from '@shejane/runtime-sdk'
import type { Translator } from '../../shared/i18n/i18n'
import type {
  AgentTimelineItem,
  ChatMessage,
  LocalFileRef,
} from '../../shared/local-data/types'
import { projectTransientAssistantText, timelineItem } from './chatStore'
import { applySubagentLifecycleEvent } from './runtimeSubagentProjection'

/** Tracks assembled tool args for a single stream session.
 *  Keeps completed rows human-readable with their original request payload.
 */
export type ToolArgsByCallId = Map<string, Record<string, unknown>>

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
    if (event.id && seenEventIDs.has(event.id)) return
    if (event.id) seenEventIDs.add(event.id)
    const payload = event.payload ?? {}
    const input = Number(payload.input_tokens) || 0
    const output = Number(payload.output_tokens) || 0
    if (input > 0 || output > 0) {
      message.tokens = (message.tokens ?? 0) + input + output
    }
    return
  }

  const alreadySeen = Boolean(event.id && seenEventIDs.has(event.id))
  if (event.id) seenEventIDs.add(event.id)
  if (event.event_type === 'run.completed') {
    const payload = event.payload ?? {}
    const input = Number(payload.input_tokens) || 0
    const output = Number(payload.output_tokens) || 0
    if (input > 0 || output > 0) message.tokens = input + output
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
    if (subagents.length) message.subagents = subagents
  }

  const item = timelineItem(event, t)
  if (item && !alreadySeen) {
    message.agentEvents = [...(message.agentEvents ?? []), item]
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
export function appendLocalDelta(
  message: ChatMessage,
  delta: string,
  event: AgentRunEvent,
  seenEventIDs: Set<string>,
) {
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
    if (!message.content.trim()) message.content = latestRunFailedLabel(message)
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
    if (
      (event.type === 'permission.required' || event.type === 'tool.reconciliation_required')
      && event.permissionRequestId
    ) {
      pending.add(event.permissionRequestId)
    }
    if (
      (event.type === 'permission.resolved' || event.type === 'tool.reconciliation_resolved')
      && event.permissionRequestId
    ) {
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
