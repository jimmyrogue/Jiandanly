import type { Translator } from '@/shared/i18n/i18n'
import type { AgentTimelineItem, AgentToolDetail, ChatMessage } from '@/shared/local-data/types'
import { collectInFlightTaskRequests } from './agentSubagentState'

export type ProgressTone = 'working' | 'permission' | 'done' | 'failed' | 'idle'

export interface AgentProgressStage {
  id: string
  tone: Extract<ProgressTone, 'done' | 'failed'> | 'conflict'
  label: string
  detail?: AgentToolDetail
  events: AgentTimelineItem[]
  count: number
}

// skill.selected is emitted on every run as housekeeping — it is NOT a
// user-facing operation, so it must not make the progress row appear for a
// plain direct answer, nor become the headline.
export const OPERATION_TYPES = new Set([
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

export const ACTIVE_RUN_STATUSES = new Set<ChatMessage['status']>([
  'pending',
  'streaming',
  'waiting_permission',
  'waiting_input',
])

export function historicalProgressStages(
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
export function summaryHeadline(events: AgentTimelineItem[], message: ChatMessage, t: Translator): CurrentActivity {
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

export function activeLabel(event: AgentTimelineItem, t: Translator): string {
  if (
    event.type === 'tool.requested'
    || event.type === 'tool.started'
    || event.type === 'tool.progress'
  ) {
    return t('agent.toolRunning', {
      tool: stripKnownPrefix(event.label, ['调用工具：', '工具开始：', 'Tool started: ']),
    })
  }
  if (event.type === 'tool.completed') {
    return t('agent.toolCompleted', {
      tool: stripKnownPrefix(event.label, ['工具完成：', 'Tool completed: ']),
    })
  }
  if (event.type === 'browser.observed') {
    return t('agent.browserObserved', {
      target: stripKnownPrefix(event.label, ['观察网页：', 'Observed page: ']),
    })
  }
  if (event.type === 'source.collected') return t('agent.organizingSources')
  if (event.type === 'verification.completed') {
    return event.verificationStatus === 'failed' ? event.label : t('agent.verifying')
  }
  if (event.type === 'skill.selected') return t('agent.selectingSkill')
  if (event.type === 'ui.action.requested') {
    return t('agent.uiPreparing', {
      action: stripKnownPrefix(event.label, ['请求操作：', 'Action requested: ']),
    })
  }
  if (event.type === 'ui.action.completed') {
    return t('agent.uiCompleted', {
      action: stripKnownPrefix(event.label, ['操作完成：', 'Action completed: ']),
    })
  }
  if (event.type === 'run.waiting' && isHandoffLedgerWarning(event)) {
    return t('agent.handoffWarning')
  }
  return event.label || t('agent.processingFallback')
}

export function defaultWorkingLabel(message: ChatMessage, t: Translator): string {
  if (message.status === 'waiting_permission') return t('agent.waitingLocalAction')
  if (message.status === 'waiting_input') return t('agent.waitingInput')
  return t('agent.working')
}

export function isHandoffLedgerWarning(event: AgentTimelineItem): boolean {
  return event.handoffLedgerState === 'missing' || event.handoffLedgerState === 'stale'
}


export function stripKnownPrefix(value: string, prefixes: string[]): string {
  for (const prefix of prefixes) {
    if (value.startsWith(prefix)) {
      return value.slice(prefix.length)
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
export function deriveProgressDetail(
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
