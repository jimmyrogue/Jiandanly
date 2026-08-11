import {
  createRunPresentationState,
  type AgentRunEvent,
  type RunPresentationItem,
  type RunPresentationState,
} from '@shejane/runtime-sdk'
import type { LocalThreadItem } from '../../../runtime/client'
import { stringValue, toolDetail, truncate } from './chatToolPresentation'

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
      const toolName = String(payload.name ?? payload.tool ?? 'tool')
      const displayDetail = safeLegacyToolDetail(payload, toolName)
      toolIDs.set(toolCallID, id)
      items.set(id, {
        id,
        kind: 'tool',
        status: 'in_progress',
        order: { event_seq: seq, slot: 0 },
        revision: seq,
        source: { kind: 'run_event', id: event.id ?? id },
        tool_call_id: toolCallID,
        tool_name: toolName,
        risk: 'unknown',
        display_target: displayDetail?.text,
        display_target_kind: displayDetail?.kind,
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
          failure_detail: event.event_type === 'tool.failed'
            ? safeLegacyFailureDetail(payload)
            : null,
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

function safeLegacyToolDetail(payload: Record<string, unknown>, toolName: string) {
  const detail = toolDetail(payload, toolName)
  if (!detail || detail.kind === 'host' || detail.kind === 'count') return detail
  const args = payload.arguments
  if (!args || typeof args !== 'object' || Array.isArray(args)) return undefined
  const values = args as Record<string, unknown>
  return stringValue(values.path) || stringValue(values.file_path) ? detail : undefined
}

function safeLegacyFailureDetail(payload: Record<string, unknown>): string | null {
  const status = stringValue(payload.message).match(/\b([1-5]\d{2})\b/)?.[1]
  return status ? truncate(status, 160) : null
}
