import type { Translator } from '../../shared/i18n/i18n'
import type {
  AgentPlanTodo,
  AgentQuestionItem,
  AgentTimelineItem,
} from '../../shared/local-data/types'
import { stringValue, toolActionLabel } from './chatToolPresentation'

export function decisionTimelineItem(
  eventType: string,
  payload: Record<string, unknown>,
  eventId: string | undefined,
  t: Translator,
): AgentTimelineItem | undefined {
  switch (eventType) {
    case 'permission.required': {
      const tool = stringValue(payload.tool)
      return {
        type: eventType,
        label: t('chat.timeline.permissionRequired', { tool: toolActionLabel(tool, t) }),
        eventId,
        permissionRequestId: stringValue(payload.request_id),
        permissionTool: toolActionLabel(tool, t),
        permissionToolName: tool,
        permissionSource: approvalSource(payload.review_source),
        permissionReason: stringValue(payload.review_reason),
        permissionArguments:
          payload.arguments && typeof payload.arguments === 'object' && !Array.isArray(payload.arguments)
            ? payload.arguments as Record<string, unknown>
            : {},
        permissionCanGrantForRun: payload.allow_run_scope === true,
      }
    }
    case 'permission.resolved': {
      const tool = stringValue(payload.tool)
      const decision = payload.decision === 'approve' || payload.decision === 'edit' ? payload.decision : 'deny'
      const scope = payload.scope === 'run' ? 'run' : 'once'
      const approvedLabel = scope === 'run' ? t('chat.timeline.permissionApprovedRun') : t('chat.timeline.permissionApprovedOnce')
      return {
        type: eventType,
        label: `${decision === 'approve' || decision === 'edit' ? approvedLabel : t('chat.timeline.permissionDenied')}${t('chat.timeline.labelJoiner')}${toolActionLabel(tool, t)}`,
        eventId,
        permissionRequestId: stringValue(payload.request_id),
        permissionTool: toolActionLabel(tool, t),
        permissionDecision: decision,
        permissionScope: scope,
      }
    }
    case 'tool.reconciliation_required': {
      const tool = stringValue(payload.tool_name)
      return {
        type: eventType,
        label: t('chat.timeline.toolReconciliationRequired', { tool: toolActionLabel(tool, t) }),
        eventId,
        permissionRequestId: stringValue(payload.request_id),
        permissionTool: toolActionLabel(tool, t),
        permissionToolName: tool,
      }
    }
    case 'tool.reconciliation_resolved': {
      const decision = payload.decision === 'confirmed_completed' || payload.decision === 'retry_not_executed'
        ? payload.decision
        : 'abort'
      return {
        type: eventType,
        label: t(`chat.timeline.toolReconciliation.${decision}`),
        eventId,
        permissionRequestId: stringValue(payload.request_id),
        reconciliationDecision: decision,
      }
    }
    case 'permission.auto_approved': {
      const tool = stringValue(payload.tool)
      const source = approvalSource(payload.source)
      const labelKey = source === 'llm'
        ? 'chat.timeline.permissionAutoApprovedLlm'
        : source === 'rule'
          ? 'chat.timeline.permissionAutoApprovedRule'
          : 'chat.timeline.permissionAutoApproved'
      return {
        type: eventType,
        label: t(labelKey, { tool: toolActionLabel(tool, t) }),
        eventId,
        permissionRequestId: stringValue(payload.request_id),
        permissionTool: toolActionLabel(tool, t),
        permissionDecision: 'approve',
        permissionScope: 'run',
        permissionSource: source,
        permissionReason: stringValue(payload.reason),
      }
    }
    case 'question.asked': {
      const questions = parseQuestionPayload(payload.questions)
      return {
        type: eventType,
        label: t('chat.timeline.questionAsked'),
        eventId,
        questionRequestId: stringValue(payload.request_id),
        questions,
      }
    }
    case 'question.answered': {
      return {
        type: eventType,
        label: t('chat.timeline.questionAnswered'),
        eventId,
        questionRequestId: stringValue(payload.request_id),
        questionAnswers: parseAnswerPayload(payload.answers),
      }
    }
    case 'plan.approval_required': {
      return {
        type: eventType,
        label: t('chat.timeline.planApprovalRequired'),
        eventId,
        planApprovalRequestId: stringValue(payload.request_id),
        planTodos: parsePlanTodos(payload.todos),
      }
    }
    case 'plan.approval_resolved': {
      const decision = planApprovalDecision(payload.decision)
      const labelKey =
        decision === 'approve'
          ? 'chat.timeline.planApproved'
          : decision === 'modify'
            ? 'chat.timeline.planModified'
            : 'chat.timeline.planRejected'
      return {
        type: eventType,
        label: t(labelKey),
        eventId,
        planApprovalRequestId: stringValue(payload.request_id),
        planApprovalDecision: decision,
      }
    }
  }
  return undefined
}

function approvalSource(value: unknown): AgentTimelineItem['permissionSource'] {
  return value === 'rule' || value === 'llm' || value === 'fallback' || value === 'run_grant'
    ? value
    : undefined
}

function parseAnswerPayload(value: unknown): Record<string, string[]> | undefined {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    return undefined
  }
  const result: Record<string, string[]> = {}
  for (const [question, picks] of Object.entries(value as Record<string, unknown>)) {
    if (!Array.isArray(picks)) {
      continue
    }
    const labels = picks.filter((entry): entry is string => typeof entry === 'string' && entry.trim().length > 0)
    if (labels.length > 0) {
      result[question] = labels
    }
  }
  return Object.keys(result).length > 0 ? result : undefined
}

function parseQuestionPayload(value: unknown): AgentQuestionItem[] {
  if (!Array.isArray(value)) {
    return []
  }
  const questions: AgentQuestionItem[] = []
  for (const raw of value) {
    if (!raw || typeof raw !== 'object') {
      continue
    }
    const item = raw as Record<string, unknown>
    const question = stringValue(item.question)
    const header = stringValue(item.header)
    const rawOptions = Array.isArray(item.options) ? item.options : []
    // Accept BOTH shapes:
    //   - { label, description? }  — the documented AgentQuestionChoice
    //   - string                   — what the runtime used to emit (and
    //                                still might if it comes from a
    //                                third-party SSE producer)
    // The runtime now normalizes to the object form at its boundary
    // (see runs.py:_normalize_question_options), but client tolerance
    // is cheap defense in depth so the UI never silently drops options
    // again.
    const options = rawOptions
      .map((option) => {
        if (typeof option === 'string') {
          const label = option.trim()
          return label ? { label } : undefined
        }
        if (!option || typeof option !== 'object') {
          return undefined
        }
        const label = stringValue((option as Record<string, unknown>).label)
        if (!label) {
          return undefined
        }
        const description = stringValue((option as Record<string, unknown>).description)
        return description ? { label, description } : { label }
      })
      .filter((option): option is { label: string; description?: string } => Boolean(option))
    // A user.ask question may intentionally be free-form. The question bar
    // always renders its text input, so an empty options list is valid and
    // must not make the Runtime-owned wait state disappear from Client.
    if (!question) {
      continue
    }
    questions.push({ question, header, multiSelect: item.multiSelect === true, options })
  }
  return questions
}

function parsePlanTodos(value: unknown): AgentPlanTodo[] {
  if (!Array.isArray(value)) {
    return []
  }
  const todos: AgentPlanTodo[] = []
  for (const raw of value) {
    if (!raw || typeof raw !== 'object') {
      continue
    }
    const item = raw as Record<string, unknown>
    const content = stringValue(item.content).trim()
    if (!content) {
      continue
    }
    todos.push({
      content,
      status: planTodoStatus(item.status),
    })
  }
  return todos
}

function planTodoStatus(value: unknown): AgentPlanTodo['status'] {
  switch (value) {
    case 'in_progress':
    case 'completed':
      return value
    default:
      return 'pending'
  }
}

function planApprovalDecision(value: unknown): NonNullable<AgentTimelineItem['planApprovalDecision']> {
  switch (value) {
    case 'modify':
    case 'reject':
      return value
    default:
      return 'approve'
  }
}
