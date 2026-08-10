import { useState } from 'react'
import { IconAlertCircle, IconChevronDown, IconChevronRight, IconFolderPlus, IconInfoCircle, IconReload, IconStethoscope, IconTool, IconWorld } from '@tabler/icons-react'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'
import { useI18n } from '@/shared/i18n/i18n'
import type { AgentToolDetail, ChatMessage } from '@/shared/local-data/types'
import { withStableTimelineKeys } from '../../timelineKeys'
import type { AgentFailureAction } from '../../recovery'
import {
  ACTIVE_RUN_STATUSES,
  OPERATION_TYPES,
  collectInFlightTaskRequests,
  deriveAgentProgress,
  deriveProgressDetail,
  failureActionKindLabelKey,
  historicalProgressStages,
  latestHandoffWarningEvent,
  subagentStatusLabel,
  subagentUsageLabel,
  summaryHeadline,
  truncateTaskDesc,
  type AgentProgressStage,
  type AgentProgressState,
} from './agentProgressState'

export type { AgentFailureAction } from '../../recovery'

export function AgentProgress({
  message,
  onFailureAction,
}: {
  message: ChatMessage
  /** Kept in the prop signature for backwards compatibility with the
   *  call site in App.tsx — the artifact preview link was removed from
   *  this component as part of the timeline cleanup (users said the
   *  expanded view was too noisy and they only wanted diagnostics). */
  onOpenArtifact?: (artifactID: string) => void
  /** Diagnostics now live in MessageBubble's persistent footer. Retained
   *  temporarily so older callers do not break while they migrate. */
  onOpenDiagnostics?: (runID: string) => void
  onFailureAction?: (action: AgentFailureAction, message: ChatMessage) => void
}) {
  const { t } = useI18n()
  const [expanded, setExpanded] = useState(false)
  const progress = deriveAgentProgress(message, t)
  // Permission prompts are surfaced once in the approval bar above the
  // composer. Other work stays available in one expandable activity history.
  if (
    !progress ||
    progress.tone === 'permission' ||
    progress.tone === 'done' ||
    (progress.tone === 'working' && message.content.trim())
  ) {
    return null
  }

  const events = message.agentEvents ?? []
  const subagents = message.subagents ?? []
  // Only surface the phase timeline for real tool/operation activity; a plain
  // direct answer needs no progress chrome.
  if (!subagents.length && !events.some((event) => OPERATION_TYPES.has(event.type))) {
    return null
  }
  const stageHistory = historicalProgressStages(events, message, t)
  const bodyId = `agent-progress-body-${message.id}`
  // While the run is active: the current action + its concrete target
  // ("正在打开 weather.com"). Failures keep their terminal headline.
  const headline = progress.tone === 'failed'
    ? { label: progress.label }
    : summaryHeadline(events, message, t)
  // Prefer the rich `toolDetail` shape when present (set by
  // chatStore.timelineItem when the runtime's tool.requested event
  // surfaces real args). Fall back to the legacy `target` string for
  // older persisted events / replayed conversations from before the
  // tool.requested flow shipped. The `task` (subagent dispatcher) tool
  // gets a special path when 2+ dispatches are in flight — the headline
  // detail shows only the count ("4 个子任务进行中") and the descriptions
  // render as a per-task list below the header (see inFlightTasks).
  const activeSubagents = subagents.filter(
    (item) => item.status === 'queued' || item.status === 'running' || item.status === 'waiting',
  )
  const detail = activeSubagents.length
    ? { kind: 'text' as const, text: t('agent.task.inFlight', { count: activeSubagents.length }) }
    : deriveProgressDetail(headline.source, events, message, t)
  // Durable lifecycle rows are always shown so status and usage survive a
  // reconnect. The task-event heuristic remains only for older Runtime
  // versions and keeps its previous ≥2 threshold.
  const inFlightTasks =
    headline.source?.tool === 'task' && ACTIVE_RUN_STATUSES.has(message.status)
      ? collectInFlightTaskRequests(events)
      : []
  const showTaskList = subagents.length > 0 || inFlightTasks.length >= 2
  const failureAction = progress.failureAction?.action === 'diagnostics' ? undefined : progress.failureAction
  const isHandoffWarning = Boolean(latestHandoffWarningEvent(events) && ACTIVE_RUN_STATUSES.has(message.status))
  const isNoticeCard = isHandoffWarning || progress.tone === 'failed'
  const hasNoticeBody = isNoticeCard && Boolean(
    detail?.text ||
    progress.failureMessage ||
    progress.detail,
  )
  const headerCanToggle = hasNoticeBody || stageHistory.length > 0
  const headerDetail = detail
  const NoticeTitleIcon = isNoticeCard
    ? progress.tone === 'failed' ? IconAlertCircle : IconInfoCircle
    : undefined

  // The leading status dot we used to show next to the headline was
  // pure ornament — the tone is already reflected in the label
  // (e.g. "已完成 …" vs "搜索网页"). Dropped per UX feedback. The
  // `working` state instead surfaces "ongoingness" via the CSS-animated
  // trailing dots on `.agent-progress-summary` (see styles.css).
  //
  // When the source event has a `toolDetail`, we draw a "· {detail}"
  // segment after the verb — host with a globe icon for web tools,
  // basename for filesystem tools, query / prompt for search-style.
  // The animated trailing dots stay attached to the verb only, so the
  // target text doesn't visually shake.
  const summaryInner = (
    <>
      {!isNoticeCard ? <span className="agent-progress-status-dot" aria-hidden="true" /> : null}
      {NoticeTitleIcon ? (
        <NoticeTitleIcon className="agent-progress-notice-title-icon" size={14} aria-hidden="true" />
      ) : null}
      <span className="name" key={headline.label}>{headline.label}</span>
      {headerDetail ? (
        <>
          {!isNoticeCard ? <span className="agent-progress-sep" aria-hidden="true">·</span> : null}
          {headerDetail.showWebIcon ? (
            <IconWorld className="agent-progress-target-icon" size={12} aria-hidden="true" />
          ) : null}
          <span className="agent-progress-target" title={headerDetail.tooltip ?? headerDetail.text}>
            {headerDetail.text}
          </span>
        </>
      ) : null}
      {headerCanToggle ? (
        expanded ? (
          <IconChevronDown className="tool-card-caret" aria-hidden="true" />
        ) : (
          <IconChevronRight className="tool-card-caret" aria-hidden="true" />
        )
      ) : null}
    </>
  )

  return (
    <div className="agent-progress-stages mt-4">
      <div
        className={cn(
          'tool-card agent-progress agent-progress-stage',
          `agent-progress-${progress.tone}`,
          isNoticeCard ? 'agent-progress-notice-card' : 'agent-progress-tool-card',
        )}
        data-state={progress.tone}
        data-expanded={expanded}
      >
      {headerCanToggle ? (
        <button
          type="button"
          className="tool-card-header agent-progress-summary"
          aria-expanded={expanded}
          aria-controls={headerCanToggle ? bodyId : undefined}
          aria-label={
            isNoticeCard
              ? expanded ? t('agent.collapseDetails') : t('agent.expandDetails')
              : expanded ? t('agent.collapseSteps') : t('agent.expandSteps')
          }
          onClick={() => setExpanded((value) => !value)}
        >
          {summaryInner}
        </button>
      ) : (
        // No diagnostics → no point in being expandable. Render the
        // headline as a passive row so users don't see a chevron that
        // opens an empty drawer.
        <div className="tool-card-header agent-progress-summary agent-progress-summary-static">
          {summaryInner}
        </div>
      )}

      {expanded ? (
        <div id={bodyId} className="agent-progress-expanded-body">
          {isNoticeCard ? (
            <AgentProgressNoticeBody
              progress={progress}
              targetDetail={detail}
            />
          ) : null}
          {stageHistory.length > 0 ? <AgentProgressHistory stages={stageHistory} /> : null}
        </div>
      ) : null}

      {/* Runtime-owned lifecycle rows show identity-safe status and usage.
       * Older Runtime versions fall back to unmatched task tool requests.
       * The list stays outside the disclosure so parallel work remains
       * visible at a glance; there are intentionally no child controls. */}
      {!isNoticeCard && showTaskList ? (
        <ul
          className="agent-progress-tasks"
          aria-label={subagents.length
            ? t('agent.subagent.list')
            : t('agent.task.inFlight', { count: inFlightTasks.length })}
        >
          {subagents.length ? subagents.map((subagent) => {
            const usage = subagentUsageLabel(subagent, t)
            return (
              <li
                key={subagent.operationId}
                className="agent-progress-task-item"
                data-status={subagent.status}
              >
                <span className="agent-progress-task-label">
                  {subagent.subagentType || t('agent.subagent.defaultType')}
                </span>
                <span className="agent-progress-task-desc" title={subagent.description}>
                  {truncateTaskDesc(subagent.description)}
                </span>
                <span className="agent-progress-task-status">
                  {subagentStatusLabel(subagent, t)}
                </span>
                <span className="agent-progress-task-usage">{usage}</span>
              </li>
            )
          }) : inFlightTasks.map((task, idx) => {
            const fullText = task.toolDetail?.text ?? task.target ?? ''
            const tooltip = task.toolDetail?.tooltip ?? fullText
            return (
              <li key={task.toolCallId} className="agent-progress-task-item" data-status="running">
                <span className="agent-progress-task-label">
                  {t('agent.task.itemLabel', { index: idx + 1 })}
                </span>
                <span className="agent-progress-task-desc" title={tooltip}>
                  {truncateTaskDesc(fullText)}
                </span>
              </li>
            )
          })}
        </ul>
      ) : null}

      {!isNoticeCard && progress.detail ? (
        <p className="agent-progress-detail">{progress.detail}</p>
      ) : null}

      </div>

      {failureAction && onFailureAction ? (
        <div className="agent-progress-user-action" role="alert">
          <span>
            {progress.failureActionKind && progress.failureActionKind !== 'user_action'
              ? t(failureActionKindLabelKey(progress.failureActionKind))
              : t('sidebar.status.needsAttention')}
          </span>
          <Button
            className="agent-progress-action"
            size="sm"
            variant="outline"
            onClick={() => onFailureAction(failureAction.action, message)}
          >
            {failureActionIcon(failureAction.action)}
            {failureAction.label}
          </Button>
        </div>
      ) : null}
    </div>
  )
}

function AgentProgressHistory({ stages }: { stages: AgentProgressStage[] }) {
  return (
    <div className="agent-progress-history">
      {stages.map((stage) => (
        <AgentProgressStageRow key={stage.id} stage={stage} />
      ))}
    </div>
  )
}

function AgentProgressStageRow({ stage }: { stage: AgentProgressStage }) {
  const label = stage.count > 1 ? `${stage.label} × ${stage.count}` : stage.label

  return (
    <details
      className="agent-progress-history-group"
      data-state={stage.tone}
    >
      <summary className="agent-progress-history-summary">
        <span className="agent-progress-status-dot" aria-hidden="true" />
        <span className="name">{label}</span>
        {stage.detail ? (
          <>
            <span className="agent-progress-sep" aria-hidden="true">·</span>
            {stage.detail.showWebIcon ? (
              <IconWorld className="agent-progress-target-icon" size={12} aria-hidden="true" />
            ) : null}
            <span className="agent-progress-target" title={stage.detail.tooltip ?? stage.detail.text}>
              {stage.detail.text}
            </span>
          </>
        ) : null}
        <IconChevronRight className="tool-card-caret" aria-hidden="true" />
      </summary>
      <div className="tool-card-results agent-progress-stage-body">
        {withStableTimelineKeys(stage.events).map(({ item: event, key }) => (
          <div className="agent-progress-stage-event" key={`${stage.id}:${key}`}>
            {event.label}
          </div>
        ))}
      </div>
    </details>
  )
}

function AgentProgressNoticeBody({
  progress,
  targetDetail,
}: {
  progress: AgentProgressState
  targetDetail?: AgentToolDetail
}) {
  if (!targetDetail?.text && !progress.failureMessage && !progress.detail) {
    return null
  }

  return (
    <div className="agent-progress-notice-body">
      {targetDetail?.text ? (
        <div className="agent-progress-notice-target-full" title={targetDetail.tooltip ?? targetDetail.text}>
          {targetDetail.text}
        </div>
      ) : null}

      {progress.failureMessage ? (
        <div className="agent-progress-notice-raw">
          {progress.failureMessage}
        </div>
      ) : null}

      {progress.detail ? (
        <div className="agent-progress-notice-line">
          <span>{progress.detail}</span>
        </div>
      ) : null}

    </div>
  )
}

function failureActionIcon(action: AgentFailureAction) {
  switch (action) {
    case 'retry':
      return <IconReload size={13} aria-hidden="true" />
    case 'repair':
      return <IconTool size={13} aria-hidden="true" />
    case 'workspace':
      return <IconFolderPlus size={13} aria-hidden="true" />
    case 'diagnostics':
      return <IconStethoscope size={13} aria-hidden="true" />
  }
}
