import {
  isSubagentLifecycleEvent,
  SUBAGENT_LIFECYCLE_EVENT_TYPES,
  type AgentRunEvent,
  type SubagentLifecycleEventType,
  type SubagentLifecyclePayload,
} from '@shejane/runtime-sdk'
import { createTranslator, type TranslationKey, type Translator } from '../../shared/i18n/i18n'
import type {
  AgentSubagentStatus,
  AgentTimelineItem,
} from '../../shared/local-data/types'
import { decisionTimelineItem } from './chatDecisionTimeline'
import {
  stringValue,
  TOOL_TARGET_MAX,
  toolActionLabel,
  toolDetail,
  toolTarget,
  truncate,
} from './chatToolPresentation'

export { toolDetail } from './chatToolPresentation'

export function projectTransientAssistantText(current: string, event: AgentRunEvent): string {
  if (event.event_type === 'llm.delta') {
    return current + stringValue(event.payload?.content)
  }
  if (
    event.event_type === 'llm.round.started'
    || event.event_type === 'tool.requested'
    || event.event_type === 'question.asked'
  ) {
    return ''
  }
  return current
}

export function timelineItem(event: AgentRunEvent, t: Translator = createTranslator('zh')): AgentTimelineItem | null {
  if (event.event_type === 'llm.delta') {
    return null
  }
  const payload = event.payload ?? {}
  const eventId = event.id
    ?? (event.run_id && event.seq !== undefined ? `${event.run_id}:${event.seq}` : undefined)
  if (isSubagentLifecycleEvent(event)) {
    return subagentTimelineItem(event.event_type, event.payload, eventId, t)
  }
  // Older Runtime builds emitted incomplete inferred lifecycle payloads. The
  // state reducer correlates those with generic `task` events; do not surface
  // a raw protocol name in the user-facing timeline.
  if ((SUBAGENT_LIFECYCLE_EVENT_TYPES as readonly string[]).includes(event.event_type)) {
    return null
  }
  const decisionItem = decisionTimelineItem(event.event_type, payload, eventId, t)
  if (decisionItem) return decisionItem
  switch (event.event_type) {
    case 'llm.usage': {
      const input = Number(payload.input_tokens) || 0
      const output = Number(payload.output_tokens) || 0
      return { type: event.event_type, label: '', eventId, tokens: input + output }
    }
    case 'skill.selected':
      return { type: event.event_type, label: t('chat.timeline.skillSelected', { skill: stringValue(payload.skill) || 'direct-answer' }), eventId }
    case 'tool.requested': {
      const tool = stringValue(payload.tool)
      return {
        type: event.event_type,
        label: t('chat.timeline.toolRequested', { tool: toolActionLabel(tool, t) }),
        eventId,
        tool,
        toolCallId: stringValue(payload.tool_call_id) || undefined,
        target: toolTarget(payload, tool),
        toolDetail: toolDetail(payload, tool),
      }
    }
    case 'tool.progress': {
      const tool = stringValue(payload.tool)
      const message = stringValue(payload.message)
      const completed = Number(payload.completed)
      const total = Number(payload.total)
      const unit = stringValue(payload.unit)
      const amount = Number.isFinite(completed) && Number.isFinite(total) && total > 0
        ? `${completed}/${total}${unit ? ` ${unit}` : ''}`
        : ''
      const detail = [message, amount].filter(Boolean).join(' · ')
      return {
        type: event.event_type,
        label: t('chat.timeline.toolRequested', { tool: toolActionLabel(tool, t) }),
        eventId,
        tool,
        toolCallId: stringValue(payload.tool_call_id) || undefined,
        toolDetail: detail
          ? { kind: 'text', text: truncate(detail, TOOL_TARGET_MAX), tooltip: detail }
          : undefined,
      }
    }
    case 'tool.completed': {
      const tool = stringValue(payload.tool)
      const item: AgentTimelineItem = {
        type: event.event_type,
        label: t('chat.timeline.toolCompleted', { tool: toolActionLabel(tool, t) }),
        eventId,
        tool,
        toolCallId: stringValue(payload.tool_call_id) || undefined,
        target: toolTarget(payload, tool),
        toolDetail: toolDetail(payload, tool),
      }
      // For code.execute, extract any image/png payloads from the
      // tool result so MessageBubble can render them inline. Without
      // this the user only sees the LLM's text, which often makes up
      // bogus `![](https://imgbb.com/...)` URLs as placeholders for
      // charts it knows it produced but can't reference.
      if (tool === 'code.execute') {
        item.codeExecImages = extractCodeExecImages(payload)
      }
      return item
    }
    case 'tool.failed': {
      const tool = stringValue(payload.tool)
      const errorCode = stringValue(payload.error_code) || undefined
      const conflictPath = errorCode === 'file_exists' ? fileConflictPath(payload) : ''
      return {
        type: event.event_type,
        label: errorCode === 'file_exists'
          ? t('chat.timeline.fileConflict', { target: conflictPath || toolActionLabel(tool, t) })
          : t('chat.timeline.toolFailed', { tool: toolActionLabel(tool, t) }),
        eventId,
        tool,
        errorCode,
        toolCallId: stringValue(payload.tool_call_id) || undefined,
        target: conflictPath || toolTarget(payload, tool),
        toolDetail: conflictPath
          ? { kind: 'text', text: truncate(conflictPath, TOOL_TARGET_MAX), tooltip: conflictPath }
          : toolDetail(payload, tool),
      }
    }
    case 'steering.injected':
      return { type: event.event_type, label: t('chat.timeline.steeringInjected'), eventId }
    case 'artifact.created': {
      const title = stringValue(payload.title) || stringValue(payload.artifact_id)
      const tool = stringValue(payload.tool)
      return {
        type: event.event_type,
        label: t('chat.timeline.artifact', { title: title || tool }),
        eventId,
        artifactId: stringValue(payload.artifact_id),
        artifactTitle: title,
        artifactTool: tool,
        artifactMediaType: stringValue(payload.media_type),
      }
    }
    case 'verification.completed': {
      const status = payload.status === 'passed' ? 'passed' : 'failed'
      const tool = stringValue(payload.tool)
      return {
        type: event.event_type,
        label: `${status === 'passed' ? t('chat.timeline.verificationPassed') : t('chat.timeline.verificationFailed')}${t('chat.timeline.labelJoiner')}${toolActionLabel(tool, t)}`,
        eventId,
        verificationStatus: status,
      }
    }
    case 'browser.observed': {
      const title = stringValue(payload.title)
      const url = stringValue(payload.url)
      return { type: event.event_type, label: t('chat.timeline.browserObserved', { target: title || url || t('chat.timeline.currentPage') }), eventId, artifactId: stringValue(payload.artifact_id), target: toolTarget(payload) }
    }
    case 'source.collected': {
      const title = stringValue(payload.title)
      const url = stringValue(payload.url)
      return {
        type: event.event_type,
        label: t('chat.timeline.sourceCollected', { target: title || url || t('chat.timeline.webSource') }),
        eventId,
        artifactId: stringValue(payload.artifact_id),
        sourceTitle: title,
        sourceUrl: url,
      }
    }
    case 'environment.observed': {
      const app = stringValue(payload.foreground_app)
      const title = stringValue(payload.window_title)
      const platform = stringValue(payload.platform)
      const target = app && title ? `${app} - ${title}` : app || title || platform || t('chat.timeline.localEnvironment')
      return { type: event.event_type, label: t('chat.timeline.environmentObserved', { target }), eventId }
    }
    case 'ui.action.requested': {
      const tool = stringValue(payload.tool)
      return { type: event.event_type, label: t('chat.timeline.uiRequested', { tool: toolActionLabel(tool, t) }), eventId }
    }
    case 'ui.action.completed': {
      const tool = stringValue(payload.tool)
      return { type: event.event_type, label: t('chat.timeline.uiCompleted', { tool: toolActionLabel(tool, t) }), eventId, artifactId: stringValue(payload.artifact_id) }
    }
    case 'repair.workflow': {
      const attempt = numberValue(payload.attempt)
      const maxAttempts = numberValue(payload.max_attempts)
      const status = repairWorkflowStatus(payload.status)
      return {
        type: event.event_type,
        label: t(repairWorkflowLabelKey(status), {
          attempt: attempt ? String(attempt) : '?',
          max: maxAttempts ? String(maxAttempts) : '?',
        }),
        eventId,
        repairWorkflowStatus: status,
        repairAttempt: attempt || undefined,
        repairSourceRunId: stringValue(payload.source_run_id) || undefined,
        repairSourceMessageId: stringValue(payload.source_message_id) || undefined,
      }
    }
    case 'run.waiting': {
      const handoff = objectValue(payload.handoff)
      const handoffLedgerState = handoffLedgerStateValue(handoff?.ledger_state)
      const handoffLedgerMessage = stringValue(handoff?.ledger_message)
      return {
        type: event.event_type,
        label: t('chat.timeline.runWaiting'),
        eventId,
        ...(handoffLedgerState ? { handoffLedgerState } : {}),
        ...(handoffLedgerMessage ? { handoffLedgerMessage } : {}),
      }
    }
    case 'run.budget_warning': {
      const label = payload.reason === 'long_running' ? t('chat.timeline.budgetLong') : t('chat.timeline.budgetMax')
      return { type: event.event_type, label, eventId }
    }
    case 'run.completed':
      return { type: event.event_type, label: t('chat.timeline.runCompleted'), eventId }
    case 'run.failed':
      return runFailedTimelineItem(event.event_type, payload, eventId, t)
    case 'run.cleanup_required':
      return {
        type: event.event_type,
        label: stringValue(payload.error) || t('chat.timeline.runCleanupRequired'),
        eventId,
        failureCategory: stringValue(payload.category) || 'execution_cleanup_unconfirmed',
        failureRetryable: false,
      }
    case 'run.canceled':
      return { type: event.event_type, label: t('chat.timeline.runCanceled'), eventId }
    default:
      return { type: event.event_type, label: event.event_type, eventId }
  }
}

function subagentTimelineItem(
  eventType: SubagentLifecycleEventType,
  payload: SubagentLifecyclePayload,
  eventId: string | undefined,
  t: Translator,
): AgentTimelineItem {
  const status: AgentSubagentStatus = payload.receipt_status === 'outcome_unknown'
    ? 'unknown'
    : payload.status
  const subagentType = payload.subagent_type || t('agent.subagent.defaultType')
  const description = payload.description
  return {
    type: eventType,
    label: t(subagentTimelineLabelKey(status), { type: subagentType }),
    eventId,
    tool: 'task',
    toolCallId: payload.tool_call_id,
    subagentOperationId: payload.operation_id,
    subagentStatus: status,
    subagentReceiptStatus: payload.receipt_status,
    subagentType,
    subagentDescription: description,
    subagentUsage: {
      modelCalls: payload.usage.model_calls,
      inputTokens: payload.usage.input_tokens,
      outputTokens: payload.usage.output_tokens,
      unmeteredCalls: payload.usage.unmetered_calls,
      outcomeUnknownCalls: payload.usage.outcome_unknown_calls,
    },
    toolDetail: description
      ? { kind: 'text', text: truncate(description, TOOL_TARGET_MAX), tooltip: description }
      : undefined,
  }
}

function subagentTimelineLabelKey(status: AgentSubagentStatus): TranslationKey {
  if (status === 'unknown') return 'chat.timeline.subagentOutcomeUnknown'
  switch (status) {
    case 'queued': return 'chat.timeline.subagentSpawned'
    case 'running': return 'chat.timeline.subagentStarted'
    case 'waiting': return 'chat.timeline.subagentWaiting'
    case 'completed': return 'chat.timeline.subagentCompleted'
    case 'failed': return 'chat.timeline.subagentFailed'
    case 'canceled': return 'chat.timeline.subagentCanceled'
  }
}

function runFailedTimelineItem(
  type: string,
  payload: Record<string, unknown>,
  eventId: string | undefined,
  t: Translator,
): AgentTimelineItem {
  const failureActionKind = knownFailureActionKind(payload.action_kind)
  const failureRecoveryAction = knownFailureRecoveryAction(payload.recovery_action)
  const failureCategory = stringValue(payload.category)
  const failureSuggestedAction = stringValue(payload.suggested_action)
  const errorCode = stringValue(payload.error_code) || stringValue(payload.code)
  const rawRetryable = payload.retryable
  const failureRetryable = typeof rawRetryable === 'boolean' ? rawRetryable : undefined
  const baseLabel = stringValue(payload.message) || stringValue(payload.error) || t('chat.timeline.runFailed')
  const policyLabel = failureActionKind ? t(failureActionKindKey(failureActionKind)) : ''
  return {
    type,
    label: policyLabel ? `${baseLabel} · ${policyLabel}` : baseLabel,
    eventId,
    ...(failureCategory ? { failureCategory } : {}),
    ...(failureRetryable !== undefined ? { failureRetryable } : {}),
    ...(failureActionKind ? { failureActionKind } : {}),
    ...(failureRecoveryAction ? { failureRecoveryAction } : {}),
    ...(failureSuggestedAction ? { failureSuggestedAction } : {}),
    ...(errorCode ? { errorCode } : {}),
  }
}

function knownFailureRecoveryAction(value: unknown): AgentTimelineItem['failureRecoveryAction'] | undefined {
  switch (value) {
    case 'retry':
    case 'repair':
    case 'workspace':
    case 'diagnostics':
      return value
    default:
      return undefined
  }
}

function knownFailureActionKind(value: unknown): AgentTimelineItem['failureActionKind'] | undefined {
  switch (value) {
    case 'retry':
    case 'user_action':
    case 'repair':
    case 'operator_action':
    case 'inspect':
      return value
    default:
      return undefined
  }
}

function failureActionKindKey(actionKind: NonNullable<AgentTimelineItem['failureActionKind']>): TranslationKey {
  switch (actionKind) {
    case 'retry':
      return 'diagnostics.failureActionKind.retry'
    case 'user_action':
      return 'diagnostics.failureActionKind.user_action'
    case 'repair':
      return 'diagnostics.failureActionKind.repair'
    case 'operator_action':
      return 'diagnostics.failureActionKind.operator_action'
    case 'inspect':
      return 'diagnostics.failureActionKind.inspect'
  }
}

function fileConflictPath(payload: Record<string, unknown>): string {
  const content = stringValue(payload.content)
  if (!content) return ''
  try {
    const envelope = JSON.parse(content) as { path?: unknown }
    return stringValue(envelope.path)
  } catch {
    return ''
  }
}

function numberValue(value: unknown): number | undefined {
  if (typeof value === 'number' && Number.isFinite(value)) {
    return value
  }
  if (typeof value === 'string' && value.trim()) {
    const parsed = Number(value)
    return Number.isFinite(parsed) ? parsed : undefined
  }
  return undefined
}

function objectValue(value: unknown): Record<string, unknown> | undefined {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : undefined
}

function handoffLedgerStateValue(value: unknown): AgentTimelineItem['handoffLedgerState'] | undefined {
  switch (value) {
    case 'not_required':
    case 'missing':
    case 'fresh':
    case 'stale':
      return value
    default:
      return undefined
  }
}

function repairWorkflowStatus(value: unknown): NonNullable<AgentTimelineItem['repairWorkflowStatus']> {
  switch (value) {
    case 'completed':
    case 'failed':
    case 'rejected':
    case 'canceled':
      return value
    default:
      return 'started'
  }
}

function repairWorkflowLabelKey(
  status: NonNullable<AgentTimelineItem['repairWorkflowStatus']>,
): Parameters<Translator>[0] {
  switch (status) {
    case 'completed':
      return 'chat.timeline.repairCompleted'
    case 'failed':
      return 'chat.timeline.repairFailed'
    case 'rejected':
      return 'chat.timeline.repairRejected'
    case 'canceled':
      return 'chat.timeline.repairCanceled'
    default:
      return 'chat.timeline.repairStarted'
  }
}

/** Extract base64-encoded image payloads from a code.execute tool
 *  result payload. The wire envelope is set by
 *  Runtime code-execution events → JSON-encoded
 *  into `payload.content` AND mirrored on `payload.data`. We try both
 *  so older runtime builds still produce images; current runtimes
 *  populate `data.results[].data["image/png" | "image/jpeg" | "image/svg+xml"]`.
 *  Returns the unique image strings in document order. */
function extractCodeExecImages(payload: Record<string, unknown>): string[] {
  const out: string[] = []
  const seen = new Set<string>()
  const visit = (results: unknown) => {
    if (!Array.isArray(results)) return
    for (const entry of results) {
      if (!entry || typeof entry !== 'object') continue
      const data = (entry as { data?: unknown }).data
      if (!data || typeof data !== 'object') continue
      const dataMap = data as Record<string, unknown>
      for (const key of ['image/png', 'image/jpeg', 'image/svg+xml']) {
        const value = dataMap[key]
        if (typeof value === 'string' && value.length > 0 && !seen.has(value)) {
          seen.add(value)
          out.push(value)
        }
      }
    }
  }
  // Runtime path: payload.data.results.
  const data = payload.data
  if (data && typeof data === 'object') {
    visit((data as { results?: unknown }).results)
  }
  // Wire-envelope fallback: payload.content is a JSON string of the
  // full result envelope (see runtime code.py wrapper).
  if (out.length === 0 && typeof payload.content === 'string') {
    try {
      const parsed = JSON.parse(payload.content) as { data?: { results?: unknown } }
      visit(parsed?.data?.results)
    } catch {
      // Not JSON — ignore.
    }
  }
  return out
}
