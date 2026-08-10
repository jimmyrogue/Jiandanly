import { createTranslator, type Translator } from '@/shared/i18n/i18n'
import type { AgentTimelineItem, ChatMessage } from '@/shared/local-data/types'
import {
  ACTIVE_RUN_STATUSES,
  OPERATION_TYPES,
  activeLabel,
  defaultWorkingLabel,
  deriveProgressDetail,
  historicalProgressStages,
  isHandoffLedgerWarning,
  stripKnownPrefix,
  summaryHeadline,
  type AgentProgressStage,
  type ProgressTone,
} from './agentProgressActivity'
import {
  failureActionCTA,
  failureGuidance,
  failureMessage,
  failureTitle,
  type FailureActionCTA,
} from './agentFailureState'

export {
  ACTIVE_RUN_STATUSES,
  OPERATION_TYPES,
  deriveProgressDetail,
  historicalProgressStages,
  summaryHeadline,
}
export type { AgentProgressStage }
export { failureActionKindLabelKey } from './agentFailureState'
export {
  collectInFlightTaskRequests,
  subagentStatusLabel,
  subagentUsageLabel,
  truncateTaskDesc,
} from './agentSubagentState'


interface PendingPermission {
  requestID: string
  tool: string
}

export interface AgentProgressState {
  tone: ProgressTone
  label: string
  detail?: string
  failureMessage?: string
  failureAction?: FailureActionCTA
  failureActionKind?: AgentTimelineItem['failureActionKind']
  pendingPermission?: PendingPermission
  sourcesCount: number
  artifactsCount: number
  latestArtifactID?: string
}


export function deriveAgentProgress(message: ChatMessage, t: Translator = createTranslator('zh')): AgentProgressState | null {
  const events = message.agentEvents ?? []
  const pendingPermission = findPendingPermission(events, t)
  const sourcesCount = uniqueCount(events, (event) => (event.type === 'source.collected' ? event.sourceUrl || event.sourceTitle || event.eventId : undefined))
  const artifacts = uniqueValues(events, (event) => event.artifactId)
  const latestArtifactID = [...events].reverse().find((event) => event.artifactId)?.artifactId
  if (!events.length && !message.runId) {
    return null
  }

  const handoffWarning = latestHandoffWarningEvent(events)
  if (handoffWarning && ACTIVE_RUN_STATUSES.has(message.status)) {
    return {
      tone: 'working',
      label: activeLabel(handoffWarning, t),
      detail: activeDetail(handoffWarning, sourcesCount, t),
      sourcesCount,
      artifactsCount: artifacts.length,
      latestArtifactID,
    }
  }

  if (pendingPermission) {
    return {
      tone: 'permission',
      label: t('agent.waitingApproval', { tool: pendingPermission.tool }),
      detail: t('agent.permissionDetail'),
      pendingPermission,
      sourcesCount,
      artifactsCount: artifacts.length,
      latestArtifactID,
    }
  }

  const isActive = ACTIVE_RUN_STATUSES.has(message.status)
  const latestRunFailure = [...events].reverse().find(
    (event) => event.type === 'run.failed' || event.type === 'run.cleanup_required',
  )
  const latestStatusFailure = message.status === 'error'
    ? [...events].reverse().find((event) => event.type === 'run.failed' || event.type === 'run.cleanup_required' || event.type === 'tool.failed' || event.verificationStatus === 'failed')
    : undefined
  const latestVerificationFailure = !isActive
    ? [...events].reverse().find((event) => event.verificationStatus === 'failed')
    : undefined
  const latestFailure = latestRunFailure || latestStatusFailure || latestVerificationFailure
  if (latestFailure || message.status === 'error') {
    return {
      tone: 'failed',
      label: failureTitle(latestFailure, t),
      failureMessage: failureMessage(latestFailure, message, t),
      detail: failureGuidance(latestFailure, t)
        || (sourcesCount || artifacts.length ? t('agent.failedDetail') : undefined),
      failureAction: failureActionCTA(latestFailure, t),
      failureActionKind: latestFailure?.failureActionKind,
      sourcesCount,
      artifactsCount: artifacts.length,
      latestArtifactID,
    }
  }

  const latestCompletion = [...events].reverse().find((event) => event.type === 'run.completed' || event.type === 'run.canceled')
  if (latestCompletion || message.status === 'done') {
    const canceled = latestCompletion?.type === 'run.canceled'
    return {
      tone: canceled ? 'idle' : 'done',
      label: canceled ? (latestCompletion?.label || t('agent.completed')) : t('agent.completed'),
      detail: completionDetail(sourcesCount, artifacts.length, t),
      sourcesCount,
      artifactsCount: artifacts.length,
      latestArtifactID,
    }
  }

  const latestActive = [...events].reverse().find((event) => isProgressEvent(event))
  return {
    tone: 'working',
    label: latestActive ? activeLabel(latestActive, t) : defaultWorkingLabel(message, t),
    detail: activeDetail(latestActive, sourcesCount, t),
    sourcesCount,
    artifactsCount: artifacts.length,
    latestArtifactID,
  }
}

function findPendingPermission(events: AgentTimelineItem[], t: Translator): PendingPermission | undefined {
  const resolved = new Set<string>()
  for (const event of events) {
    if ((event.type === 'permission.resolved' || event.type === 'permission.auto_approved') && event.permissionRequestId) {
      resolved.add(event.permissionRequestId)
    }
  }
  for (const event of [...events].reverse()) {
    if (event.type === 'permission.required' && event.permissionRequestId && !resolved.has(event.permissionRequestId)) {
      return {
        requestID: event.permissionRequestId,
        tool: event.permissionTool || stripKnownPrefix(event.label, ['需要权限：', 'Permission required: ']) || t('agent.localAction'),
      }
    }
  }
  return undefined
}

function uniqueCount(events: AgentTimelineItem[], select: (event: AgentTimelineItem) => string | undefined): number {
  return uniqueValues(events, select).length
}

function uniqueValues(events: AgentTimelineItem[], select: (event: AgentTimelineItem) => string | undefined): string[] {
  const values = new Set<string>()
  for (const event of events) {
    const value = select(event)
    if (value) {
      values.add(value)
    }
  }
  return [...values]
}

function isProgressEvent(event: AgentTimelineItem): boolean {
  return [
    'skill.selected',
    'tool.requested',
    'tool.progress',
    'tool.started',
    'tool.completed',
    'tool.failed',
    'browser.observed',
    'source.collected',
    'ui.action.requested',
    'ui.action.completed',
    'repair.workflow',
    'run.waiting',
    'verification.completed',
    'run.budget_warning',
    'checkpoint.resumed',
  ].includes(event.type)
}

function activeDetail(event: AgentTimelineItem | undefined, sourcesCount: number, t: Translator): string | undefined {
  if (!event) {
    return undefined
  }
  if (sourcesCount > 0 && ['source.collected', 'browser.observed', 'tool.completed'].includes(event.type)) {
    return t('agent.detail.hasSources')
  }
  if (event.type === 'run.budget_warning') {
    return t('agent.detail.longRunning')
  }
  if (event.type === 'run.waiting') {
    return handoffLedgerDetail(event, t)
  }
  return undefined
}

function completionDetail(sourcesCount: number, artifactsCount: number, t: Translator): string | undefined {
  if (sourcesCount > 0) {
    return t('agent.detail.completedWithSources', { count: sourcesCount })
  }
  if (artifactsCount > 0) {
    return t('agent.detail.completedWithArtifacts', { count: artifactsCount })
  }
  return undefined
}

export function latestHandoffWarningEvent(events: AgentTimelineItem[]): AgentTimelineItem | undefined {
  return [...events].reverse().find((event) => event.type === 'run.waiting' && isHandoffLedgerWarning(event))
}


function handoffLedgerDetail(event: AgentTimelineItem, t: Translator): string | undefined {
  if (event.handoffLedgerState === 'missing') {
    return t('agent.handoffLedgerMissingDetail')
  }
  if (event.handoffLedgerState === 'stale') {
    return t('agent.handoffLedgerStaleDetail')
  }
  return undefined
}
