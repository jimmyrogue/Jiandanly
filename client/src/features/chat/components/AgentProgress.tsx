import { useState } from 'react'
import { IconAlertCircle, IconChevronDown, IconChevronRight, IconFolderPlus, IconInfoCircle, IconReload, IconStethoscope, IconTool, IconWorld } from '@tabler/icons-react'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'
import { createTranslator, useI18n, type Translator } from '@/shared/i18n/i18n'
import type { AgentSubagentProjection, AgentTimelineItem, AgentToolDetail, ChatMessage } from '@/shared/local-data/types'
import { withStableTimelineKeys } from '../timelineKeys'
import { failureRecoveryAction, type AgentFailureAction } from '../recovery'

type ProgressTone = 'working' | 'permission' | 'done' | 'failed' | 'idle'
export type { AgentFailureAction } from '../recovery'

interface PendingPermission {
  requestID: string
  tool: string
}

interface FailureActionCTA {
  action: AgentFailureAction
  label: string
}

interface AgentProgressState {
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

interface AgentProgressStage {
  id: string
  tone: Extract<ProgressTone, 'done' | 'failed'> | 'conflict'
  label: string
  detail?: AgentToolDetail
  events: AgentTimelineItem[]
  count: number
}

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

// skill.selected is emitted on every run as housekeeping — it is NOT a
// user-facing operation, so it must not make the progress row appear for a
// plain direct answer, nor become the headline.
const OPERATION_TYPES = new Set([
  'tool.requested',
  'tool.progress',
  'tool.started',
  'tool.completed',
  'tool.failed',
  'browser.observed',
  'source.collected',
  'artifact.created',
  'verification.completed',
  'ui.action.requested',
  'ui.action.completed',
  'repair.workflow',
  'run.waiting',
  'run.failed',
  'run.cleanup_required',
  'subagent.spawned',
  'subagent.started',
  'subagent.waiting',
  'subagent.completed',
  'subagent.failed',
  'subagent.canceled',
  'subagent.outcome_unknown',
])

const ACTIVITY_TYPES = new Set([
  'tool.requested',
  'tool.progress',
  'tool.started',
  'tool.completed',
  'tool.failed',
  'browser.observed',
  'verification.completed',
  'ui.action.requested',
  'ui.action.completed',
  'repair.workflow',
  'run.waiting',
  'run.failed',
  'run.cleanup_required',
  'subagent.spawned',
  'subagent.started',
  'subagent.waiting',
  'subagent.completed',
  'subagent.failed',
  'subagent.canceled',
  'subagent.outcome_unknown',
])

const ACTIVE_RUN_STATUSES = new Set<ChatMessage['status']>([
  'pending',
  'streaming',
  'waiting_permission',
  'waiting_input',
])

function historicalProgressStages(
  events: AgentTimelineItem[],
  message: ChatMessage,
  t: Translator,
): AgentProgressStage[] {
  // ponytail: timelines stay small; index groups only if real runs make this scan measurable.
  const groups: Array<{ id: string; events: AgentTimelineItem[] }> = []
  const toolCalls = new Map<string, number>()

  const append = (groupIndex: number, event: AgentTimelineItem) => {
    groups[groupIndex]?.events.push(event)
  }
  const push = (id: string, event: AgentTimelineItem) => {
    groups.push({ id, events: [event] })
    return groups.length - 1
  }
  const latestGroup = (matches: (event: AgentTimelineItem) => boolean) => {
    for (let index = groups.length - 1; index >= 0; index -= 1) {
      if (groups[index].events.some(matches)) {
        return index
      }
    }
    return -1
  }

  for (let index = 0; index < events.length; index += 1) {
    const event = events[index]
    if (event.type === 'tool.requested') {
      if (event.tool === 'task') {
        continue
      }
      const existingGroup = event.toolCallId ? toolCalls.get(event.toolCallId) : undefined
      if (existingGroup !== undefined) {
        append(existingGroup, event)
      } else {
        const groupIndex = push(event.toolCallId || `tool-${index}`, event)
        if (event.toolCallId) {
          toolCalls.set(event.toolCallId, groupIndex)
        }
      }
      continue
    }
    if (['tool.progress', 'tool.started', 'tool.completed', 'tool.failed'].includes(event.type)) {
      if (event.tool === 'task') {
        continue
      }
      const groupIndex = event.toolCallId && toolCalls.has(event.toolCallId)
        ? toolCalls.get(event.toolCallId)!
        : latestGroup((candidate) => candidate.tool === event.tool)
      if (groupIndex >= 0) {
        append(groupIndex, event)
      } else {
        push(event.toolCallId || `tool-${index}`, event)
      }
      continue
    }
    if (event.type === 'repair.workflow') {
      const groupIndex = latestGroup((candidate) =>
        candidate.type === 'repair.workflow' && candidate.repairAttempt === event.repairAttempt,
      )
      if (groupIndex >= 0) {
        append(groupIndex, event)
      } else {
        push(`repair-${event.repairAttempt ?? index}`, event)
      }
      continue
    }
    if (event.type === 'ui.action.requested') {
      push(`ui-${index}`, event)
      continue
    }
    if (event.type === 'ui.action.completed') {
      const groupIndex = latestGroup((candidate) => candidate.type === 'ui.action.requested')
      if (groupIndex >= 0) {
        append(groupIndex, event)
      } else {
        push(`ui-${index}`, event)
      }
      continue
    }
    if (event.type === 'browser.observed' || event.type === 'verification.completed') {
      push(`${event.type}-${index}`, event)
    }
  }

  const visibleGroups = ACTIVE_RUN_STATUSES.has(message.status) ? groups.slice(0, -1) : groups
  const stages = visibleGroups.map((group) => {
    const latest = group.events[group.events.length - 1]
    const detailSource = [...group.events].reverse().find((event) => event.toolDetail || event.target)
    const tone: AgentProgressStage['tone'] = latest.errorCode === 'file_exists'
      ? 'conflict'
      : latest.type === 'tool.failed' || latest.verificationStatus === 'failed' || latest.repairWorkflowStatus === 'failed'
        ? 'failed'
        : 'done'
    return {
      id: group.id,
      tone,
      label: activeLabel(latest, t),
      detail: detailSource?.toolDetail ?? (detailSource?.target ? { kind: 'text', text: detailSource.target } : undefined),
      events: group.events.filter((event) => Boolean(event.label)),
      count: 1,
    }
  })

  // Consecutive successful calls with the same visible action and target are
  // one user-facing activity. Their original events remain available in the
  // nested disclosure for audit and debugging.
  return stages.reduce<AgentProgressStage[]>((summary, stage) => {
    const previous = summary.at(-1)
    if (
      stage.tone === 'done' &&
      previous?.tone === 'done' &&
      previous.label === stage.label &&
      previous.detail?.kind === stage.detail?.kind &&
      previous.detail?.text === stage.detail?.text
    ) {
      previous.count += stage.count
      previous.events.push(...stage.events)
      return summary
    }
    summary.push(stage)
    return summary
  }, [])
}

/** Strip whichever of the known "前缀X" markers prefix the event label so
 *  the tool name comes out clean, then re-wrap it as "正在 X" — the
 *  in-progress framing the user expects in the AgentProgress headline.
 *  Used during active runs to relabel completed/failed events too. */
const TOOL_PHASE_PREFIXES = [
  '调用工具：',
  '工具开始：',
  '工具完成：',
  '工具失败：',
  '验证失败：',
  'Tool started: ',
  'Tool completed: ',
  'Tool failed: ',
]

function asInProgressToolLabel(event: AgentTimelineItem, t: Translator): string {
  return t('agent.toolRunning', { tool: stripKnownPrefix(event.label, TOOL_PHASE_PREFIXES) })
}

/** Build the live-action headline.
 *
 *  Earlier iterations of this got two things wrong in sequence:
 *  1. We surfaced `tool.completed` as the headline ("已完成 X") which
 *     read as "the whole task is done" while the run kept going.
 *  2. We then fell back to a generic "正在思考" between tool calls —
 *     but the ThinkingIndicator above already says "正在思考", so users
 *     saw two duplicate labels.
 *
 *  Current rule, during active runs:
 *    a. Prefer a tool that has more dispatches than completions
 *       (genuinely in flight). Frame as "正在 X".
 *    b. Otherwise re-frame the most recent tool event (even if
 *       completed) as "正在 X" — the agent is still working on that
 *       tool's results until the next tool kicks off.
 *    c. Otherwise fall through to other activity events (browser
 *       observed / verification / UI action) with their natural label.
 *    d. Otherwise the generic working label.
 *  Once the run actually finishes, the inactive branch uses natural
 *  framing so the final headline can read as completed.
 */
/** Result of headline scoring — the verb-only label PLUS the event we
 *  derived it from, so the renderer can pull `event.toolDetail` to
 *  build a richer per-tool subtitle ("搜索 · 普吉岛雨季天气"). */
interface CurrentActivity {
  label: string
  source?: AgentTimelineItem
}

function currentActivityLabel(events: AgentTimelineItem[], message: ChatMessage, t: Translator): CurrentActivity {
  const isActive = ACTIVE_RUN_STATUSES.has(message.status)
  if (isActive) {
    // Per-tool tally: positive ⇒ at least one call still in flight.
    // Counted by tool name (not eventId) so parallel dispatches of
    // the same tool collapse correctly.
    const pending = new Map<string, number>()
    for (const event of events) {
      if (!event.tool) {
        continue
      }
      if (event.type === 'tool.requested' || event.type === 'tool.started') {
        pending.set(event.tool, (pending.get(event.tool) ?? 0) + 1)
      } else if (event.type === 'tool.completed' || event.type === 'tool.failed') {
        pending.set(event.tool, (pending.get(event.tool) ?? 0) - 1)
      }
    }
    // (a) latest in-flight tool
    for (let index = events.length - 1; index >= 0; index -= 1) {
      const event = events[index]
      if (
        event.type !== 'tool.requested' &&
        event.type !== 'tool.started' &&
        event.type !== 'tool.progress'
      ) {
        continue
      }
      if (!event.tool || (pending.get(event.tool) ?? 0) <= 0) {
        continue
      }
      return { label: asInProgressToolLabel(event, t), source: event }
    }
    // (b) latest tool event of any phase, reframed as "正在 X"
    for (let index = events.length - 1; index >= 0; index -= 1) {
      const event = events[index]
      if (!event.tool) {
        continue
      }
      if (
        event.type === 'tool.requested' ||
        event.type === 'tool.started' ||
        event.type === 'tool.progress' ||
        event.type === 'tool.completed' ||
        event.type === 'tool.failed'
      ) {
        return { label: asInProgressToolLabel(event, t), source: event }
      }
    }
    // (c) non-tool activity events
    for (let index = events.length - 1; index >= 0; index -= 1) {
      const event = events[index]
      if (ACTIVITY_TYPES.has(event.type)) {
        return { label: operationLabel(event, t), source: event }
      }
    }
    // (d) generic
    return { label: defaultWorkingLabel(message, t) }
  }
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index]
    if (ACTIVITY_TYPES.has(event.type)) {
      return { label: operationLabel(event, t), source: event }
    }
  }
  return { label: defaultWorkingLabel(message, t) }
}

/**
 * Claude Code-style headline: while the run is active show the current action
 * and its concrete target; once finished show the aggregated tally of what was
 * done. Falls back to the latest activity when no completed tools were tallied.
 * Returns `{label, source?}` so the renderer can read `source.toolDetail`
 * to draw a richer subtitle ("搜索 · query") next to the verb.
 */
function summaryHeadline(events: AgentTimelineItem[], message: ChatMessage, t: Translator): CurrentActivity {
  if (ACTIVE_RUN_STATUSES.has(message.status)) {
    return currentActivityLabel(events, message, t)
  }
  // Inactive: prefer the aggregated count label (no source — it's a
  // tally across many tools, not a single event). Fall back to the
  // latest activity when no tools matched the bucket list.
  const aggregated = operationCountsLabel(events, t)
  if (aggregated) {
    return { label: aggregated }
  }
  return currentActivityLabel(events, message, t)
}

function operationLabel(event: AgentTimelineItem, t: Translator): string {
  if (event.type === 'tool.failed') {
    return t('agent.toolRunning', {
      tool: stripKnownPrefix(event.label, ['工具失败：', '验证失败：', 'Tool failed: ']),
    })
  }
  if (event.type === 'verification.completed') {
    return t('agent.verifying')
  }
  return activeLabel(event, t)
}

const COUNT_BUCKETS: Array<{ key: Parameters<Translator>[0]; tools: Set<string> }> = [
  { key: 'agent.count.filesRead', tools: new Set(['fs.read', 'file.read', 'read_file']) },
  { key: 'agent.count.filesWritten', tools: new Set(['fs.write', 'file.write', 'write_file', 'edit_file']) },
  { key: 'agent.count.commands', tools: new Set(['shell.run', 'execute']) },
  { key: 'agent.count.pages', tools: new Set(['browser.open', 'web.fetch']) },
  { key: 'agent.count.searches', tools: new Set(['web.search', 'browser.search']) },
]

function operationCountsLabel(events: AgentTimelineItem[], t: Translator): string {
  const tallies = new Map<string, number>()
  let other = 0
  for (const event of events) {
    if (event.type !== 'tool.completed' || !event.tool) {
      continue
    }
    const bucket = COUNT_BUCKETS.find((entry) => entry.tools.has(event.tool as string))
    if (bucket) {
      tallies.set(bucket.key, (tallies.get(bucket.key) ?? 0) + 1)
    } else {
      other += 1
    }
  }
  const parts: string[] = []
  for (const bucket of COUNT_BUCKETS) {
    const count = tallies.get(bucket.key)
    if (count) {
      parts.push(t(bucket.key, { count }))
    }
  }
  if (other > 0) {
    parts.push(t('agent.count.operations', { count: other }))
  }
  return parts.join(' · ')
}

function deriveAgentProgress(message: ChatMessage, t: Translator = createTranslator('zh')): AgentProgressState | null {
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

function failureTitle(event: AgentTimelineItem | undefined, t: Translator): string {
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

function failureMessage(
  event: AgentTimelineItem | undefined,
  message: ChatMessage,
  t: Translator,
): string | undefined {
  if (event?.errorCode === 'model_call_budget_exhausted') {
    return undefined
  }
  const value = (event?.label || message.content || '').trim()
  if (!value) {
    return undefined
  }
  const cleaned = stripKnownSuffix(value, failureActionKindSuffixes(t)).trim()
  return cleaned || undefined
}

function failureGuidance(
  event: AgentTimelineItem | undefined,
  t: Translator,
): string | undefined {
  if (!event) {
    return undefined
  }
  if (event.errorCode === 'model_call_budget_exhausted') {
    return t('agent.failure.modelCallBudget.guidance')
  }
  if (event.failureCategory) {
    const localized = t(failureCategoryActionKey(event.failureCategory))
    if (localized) {
      return localized
    }
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

function failureActionCTA(
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

function failureCategoryLabelKey(category: string): Parameters<Translator>[0] {
  switch (category) {
    case 'transient':
      return 'diagnostics.failureCategory.transient'
    case 'auth':
      return 'diagnostics.failureCategory.auth'
    case 'budget':
      return 'diagnostics.failureCategory.budget'
    case 'quota':
      return 'diagnostics.failureCategory.quota'
    case 'permission':
      return 'diagnostics.failureCategory.permission'
    case 'configuration':
      return 'diagnostics.failureCategory.configuration'
    case 'workspace':
      return 'diagnostics.failureCategory.workspace'
    case 'validation':
      return 'diagnostics.failureCategory.validation'
    case 'fatal':
      return 'diagnostics.failureCategory.fatal'
    case 'execution_lease_expired':
    case 'execution_cleanup_unconfirmed':
      return 'diagnostics.failureCategory.cleanup'
    default:
      return 'diagnostics.failureCategory.unknown'
  }
}

function failureActionKindLabelKey(actionKind: NonNullable<AgentTimelineItem['failureActionKind']>): Parameters<Translator>[0] {
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
    case 'transient':
      return 'diagnostics.failureAction.transient'
    case 'auth':
      return 'diagnostics.failureAction.auth'
    case 'budget':
      return 'diagnostics.failureAction.budget'
    case 'quota':
      return 'diagnostics.failureAction.quota'
    case 'permission':
      return 'diagnostics.failureAction.permission'
    case 'configuration':
      return 'diagnostics.failureAction.configuration'
    case 'workspace':
      return 'diagnostics.failureAction.workspace'
    case 'validation':
      return 'diagnostics.failureAction.validation'
    case 'fatal':
      return 'diagnostics.failureAction.fatal'
    case 'execution_lease_expired':
    case 'execution_cleanup_unconfirmed':
      return 'diagnostics.failureAction.cleanup'
    default:
      return 'diagnostics.failureAction.unknown'
  }
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

function activeLabel(event: AgentTimelineItem, t: Translator): string {
  if (
    event.type === 'tool.requested' ||
    event.type === 'tool.started' ||
    event.type === 'tool.progress'
  ) {
    return t('agent.toolRunning', { tool: stripKnownPrefix(event.label, ['调用工具：', '工具开始：', 'Tool started: ']) })
  }
  if (event.type === 'tool.completed') {
    return t('agent.toolCompleted', { tool: stripKnownPrefix(event.label, ['工具完成：', 'Tool completed: ']) })
  }
  if (event.type === 'browser.observed') {
    return t('agent.browserObserved', { target: stripKnownPrefix(event.label, ['观察网页：', 'Observed page: ']) })
  }
  if (event.type === 'source.collected') {
    return t('agent.organizingSources')
  }
  if (event.type === 'verification.completed') {
    return event.verificationStatus === 'failed' ? event.label : t('agent.verifying')
  }
  if (event.type === 'skill.selected') {
    return t('agent.selectingSkill')
  }
  if (event.type === 'ui.action.requested') {
    return t('agent.uiPreparing', { action: stripKnownPrefix(event.label, ['请求操作：', 'Action requested: ']) })
  }
  if (event.type === 'ui.action.completed') {
    return t('agent.uiCompleted', { action: stripKnownPrefix(event.label, ['操作完成：', 'Action completed: ']) })
  }
  if (event.type === 'run.waiting' && isHandoffLedgerWarning(event)) {
    return t('agent.handoffWarning')
  }
  return event.label || t('agent.processingFallback')
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

function defaultWorkingLabel(message: ChatMessage, t: Translator): string {
  if (message.status === 'waiting_permission') {
    return t('agent.waitingLocalAction')
  }
  if (message.status === 'waiting_input') {
    return t('agent.waitingInput')
  }
  return t('agent.working')
}

function latestHandoffWarningEvent(events: AgentTimelineItem[]): AgentTimelineItem | undefined {
  return [...events].reverse().find((event) => event.type === 'run.waiting' && isHandoffLedgerWarning(event))
}

function isHandoffLedgerWarning(event: AgentTimelineItem): boolean {
  return event.handoffLedgerState === 'missing' || event.handoffLedgerState === 'stale'
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

function stripKnownPrefix(value: string, prefixes: string[]): string {
  for (const prefix of prefixes) {
    if (value.startsWith(prefix)) {
      return value.slice(prefix.length)
    }
  }
  return value
}

function stripKnownSuffix(value: string, suffixes: string[]): string {
  for (const suffix of suffixes) {
    if (value.endsWith(suffix)) {
      return value.slice(0, -suffix.length)
    }
  }
  return value
}

/** Compute the AgentToolDetail to display alongside the verb.
 *
 *  Base case: the source event already carries a per-tool `toolDetail`
 *  (built by chatStore.toolDetail at event time), or — for older
 *  persisted events — a legacy single-string `target`.
 *
 *  Special case for the `task` (subagent dispatcher) tool when ≥2
 *  dispatches are in flight: the headline detail collapses to just the
 *  count ("4 个子任务进行中") — descriptions move out of this single line
 *  and into the per-task list rendered below the header by
 *  `inFlightTasks`. */
function deriveProgressDetail(
  source: AgentTimelineItem | undefined,
  events: AgentTimelineItem[],
  message: ChatMessage,
  t: Translator,
): AgentToolDetail | undefined {
  const base: AgentToolDetail | undefined =
    source?.toolDetail ?? (source?.target ? { kind: 'text', text: source.target } : undefined)

  // Once Runtime supplies lifecycle state, it is authoritative. Generic task
  // requests may remain in the timeline for audit but cannot keep a terminal
  // operation visually active.
  if (message.subagents?.length) {
    return base
  }
  if (source?.tool !== 'task' || !ACTIVE_RUN_STATUSES.has(message.status)) {
    return base
  }
  const inFlight = collectInFlightTaskRequests(events)
  if (inFlight.length < 2) {
    return base
  }
  // The descriptions render in the list below; up here we only carry
  // the count so the headline reads as a clean "派发 · 4 个子任务进行中".
  return { kind: 'text', text: t('agent.task.inFlight', { count: inFlight.length }) }
}

/** Cap each per-task description to a fixed width so the list rows
 *  stay short and visually scannable regardless of viewport width.
 *  The user asked for "大概10-20个文字" — 22 leaves a touch of headroom
 *  for Chinese sentences that end with a particle or punctuation mark
 *  that we don't want chopped off mid-character. CSS ellipsis on the
 *  containing span still kicks in as a safety net if a row gets
 *  squeezed by a narrow viewport.
 *
 *  We do NOT append a static `…` to truncated strings — the CSS
 *  ::after on `.agent-progress-task-desc` paints an animated
 *  `./../...` instead. The animation doubles as both an "in progress"
 *  cue and a "there's more in the tooltip" hint, so a single visual
 *  element carries both signals. Hover the row to see the full text
 *  via title="". */
const TASK_DESC_MAX = 22
function truncateTaskDesc(value: string): string {
  if (!value) {
    return ''
  }
  return value.length > TASK_DESC_MAX ? value.slice(0, TASK_DESC_MAX) : value
}

function subagentStatusLabel(item: AgentSubagentProjection, t: Translator): string {
  switch (item.status) {
    case 'queued': return t('agent.subagent.status.queued')
    case 'running': return t('agent.subagent.status.running')
    case 'waiting': return t('agent.subagent.status.waiting')
    case 'completed': return t('agent.subagent.status.completed')
    case 'failed': return t('agent.subagent.status.failed')
    case 'canceled': return t('agent.subagent.status.canceled')
    case 'unknown': return t('agent.subagent.status.unknown')
  }
}

function subagentUsageLabel(item: AgentSubagentProjection, t: Translator): string {
  const parts = [t('agent.subagent.usage', {
    calls: item.usage.modelCalls,
    tokens: item.usage.inputTokens + item.usage.outputTokens,
  })]
  if (item.usage.unmeteredCalls > 0) {
    parts.push(t('agent.subagent.usageUnmetered', { count: item.usage.unmeteredCalls }))
  }
  if (item.usage.outcomeUnknownCalls > 0) {
    parts.push(t('agent.subagent.usageUnknown', { count: item.usage.outcomeUnknownCalls }))
  }
  return parts.join(' · ')
}

/** Return the list of `tool.requested` events for the `task` tool that
 *  have NOT yet been matched by a corresponding `tool.completed` or
 *  `tool.failed`, identified by `tool_call_id`. Falls back to skipping
 *  events that lack a `toolCallId` (very old persisted events from
 *  before the tool_call_id-based aggregation shipped — they simply
 *  don't participate in the parallel grouping). */
function collectInFlightTaskRequests(events: AgentTimelineItem[]): AgentTimelineItem[] {
  const completedCallIds = new Set<string>()
  for (const event of events) {
    if (event.tool !== 'task' || !event.toolCallId) {
      continue
    }
    if (event.type === 'tool.completed' || event.type === 'tool.failed') {
      completedCallIds.add(event.toolCallId)
    }
  }
  const seen = new Set<string>()
  const result: AgentTimelineItem[] = []
  for (const event of events) {
    if (event.tool !== 'task' || event.type !== 'tool.requested') {
      continue
    }
    const id = event.toolCallId
    if (!id || completedCallIds.has(id) || seen.has(id)) {
      continue
    }
    seen.add(id)
    result.push(event)
  }
  return result
}
