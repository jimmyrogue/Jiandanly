import type { Translator } from '@/shared/i18n/i18n'
import type { AgentTimelineItem, ChatMessage } from '@/shared/local-data/types'
import { failureRecoveryAction, type AgentFailureAction } from '../../recovery'

export interface FailureActionCTA {
  action: AgentFailureAction
  label: string
}

export function failureTitle(
  event: AgentTimelineItem | undefined,
  t: Translator,
): string {
  if (event?.errorCode === 'model_call_budget_exhausted') {
    return t('agent.failure.modelCallBudget.title')
  }
  if (event?.failureCategory) {
    return t(failureCategoryLabelKey(event.failureCategory))
  }
  if (event?.failureActionKind) {
    return t(failureActionKindLabelKey(event.failureActionKind))
  }
  return t('agent.failed')
}

export function failureMessage(
  event: AgentTimelineItem | undefined,
  message: ChatMessage,
  t: Translator,
): string | undefined {
  if (event?.errorCode === 'model_call_budget_exhausted') return undefined
  const value = (event?.label || message.content || '').trim()
  if (!value) return undefined
  const cleaned = stripKnownSuffix(value, failureActionKindSuffixes(t)).trim()
  return cleaned || undefined
}

export function failureGuidance(
  event: AgentTimelineItem | undefined,
  t: Translator,
): string | undefined {
  if (!event) return undefined
  if (event.errorCode === 'model_call_budget_exhausted') {
    return t('agent.failure.modelCallBudget.guidance')
  }
  if (event.failureCategory) {
    const localized = t(failureCategoryActionKey(event.failureCategory))
    if (localized) return localized
  }
  if (event.failureActionKind === 'operator_action') {
    return t('diagnostics.failureAction.fatal')
  }
  if (event.failureActionKind === 'repair') {
    return t('diagnostics.failureAction.validation')
  }
  if (event.failureActionKind === 'retry') {
    return t('diagnostics.failureAction.transient')
  }
  if (event.failureActionKind === 'inspect') {
    return t('diagnostics.failureAction.unknown')
  }
  return event.failureSuggestedAction
}

export function failureActionCTA(
  event: AgentTimelineItem | undefined,
  t: Translator,
): FailureActionCTA | undefined {
  const action = failureRecoveryAction(event)
  return action ? { action, label: failureActionLabel(action, t) } : undefined
}

function failureActionLabel(action: AgentFailureAction, t: Translator): string {
  switch (action) {
    case 'retry':
      return t('agent.failureAction.retry')
    case 'repair':
      return t('agent.failureAction.repair')
    case 'workspace':
      return t('agent.failureAction.chooseWorkspace')
    case 'diagnostics':
      return t('agent.failureAction.openDiagnostics')
  }
}

function failureCategoryLabelKey(category: string): Parameters<Translator>[0] {
  switch (category) {
    case 'transient': return 'diagnostics.failureCategory.transient'
    case 'auth': return 'diagnostics.failureCategory.auth'
    case 'budget': return 'diagnostics.failureCategory.budget'
    case 'quota': return 'diagnostics.failureCategory.quota'
    case 'permission': return 'diagnostics.failureCategory.permission'
    case 'configuration': return 'diagnostics.failureCategory.configuration'
    case 'workspace': return 'diagnostics.failureCategory.workspace'
    case 'validation': return 'diagnostics.failureCategory.validation'
    case 'fatal': return 'diagnostics.failureCategory.fatal'
    case 'execution_lease_expired':
    case 'execution_cleanup_unconfirmed':
      return 'diagnostics.failureCategory.cleanup'
    default:
      return 'diagnostics.failureCategory.unknown'
  }
}

export function failureActionKindLabelKey(
  actionKind: NonNullable<AgentTimelineItem['failureActionKind']>,
): Parameters<Translator>[0] {
  switch (actionKind) {
    case 'retry': return 'diagnostics.failureActionKind.retry'
    case 'user_action': return 'diagnostics.failureActionKind.user_action'
    case 'repair': return 'diagnostics.failureActionKind.repair'
    case 'operator_action': return 'diagnostics.failureActionKind.operator_action'
    case 'inspect': return 'diagnostics.failureActionKind.inspect'
  }
}

function failureActionKindSuffixes(t: Translator): string[] {
  return [
    t('diagnostics.failureActionKind.retry'),
    t('diagnostics.failureActionKind.user_action'),
    t('diagnostics.failureActionKind.repair'),
    t('diagnostics.failureActionKind.operator_action'),
    t('diagnostics.failureActionKind.inspect'),
  ].map((label) => ` · ${label}`)
}

function failureCategoryActionKey(category: string): Parameters<Translator>[0] {
  switch (category) {
    case 'transient': return 'diagnostics.failureAction.transient'
    case 'auth': return 'diagnostics.failureAction.auth'
    case 'budget': return 'diagnostics.failureAction.budget'
    case 'quota': return 'diagnostics.failureAction.quota'
    case 'permission': return 'diagnostics.failureAction.permission'
    case 'configuration': return 'diagnostics.failureAction.configuration'
    case 'workspace': return 'diagnostics.failureAction.workspace'
    case 'validation': return 'diagnostics.failureAction.validation'
    case 'fatal': return 'diagnostics.failureAction.fatal'
    case 'execution_lease_expired':
    case 'execution_cleanup_unconfirmed':
      return 'diagnostics.failureAction.cleanup'
    default:
      return 'diagnostics.failureAction.unknown'
  }
}

function stripKnownSuffix(value: string, suffixes: string[]): string {
  for (const suffix of suffixes) {
    if (value.endsWith(suffix)) return value.slice(0, -suffix.length)
  }
  return value
}
