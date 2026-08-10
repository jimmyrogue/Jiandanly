import type { Translator } from '@/shared/i18n/i18n'
import type { AgentSubagentProjection, AgentTimelineItem } from '@/shared/local-data/types'

const TASK_DESC_MAX = 22

/** Cap each per-task description to a fixed width; CSS supplies the animated ellipsis. */
export function truncateTaskDesc(value: string): string {
  if (!value) return ''
  return value.length > TASK_DESC_MAX ? value.slice(0, TASK_DESC_MAX) : value
}

export function subagentStatusLabel(item: AgentSubagentProjection, t: Translator): string {
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

export function subagentUsageLabel(item: AgentSubagentProjection, t: Translator): string {
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

/** Return unmatched `task` requests, grouped by tool_call_id. */
export function collectInFlightTaskRequests(events: AgentTimelineItem[]): AgentTimelineItem[] {
  const completedCallIds = new Set<string>()
  for (const event of events) {
    if (event.tool !== 'task' || !event.toolCallId) continue
    if (event.type === 'tool.completed' || event.type === 'tool.failed') {
      completedCallIds.add(event.toolCallId)
    }
  }
  const seen = new Set<string>()
  const result: AgentTimelineItem[] = []
  for (const event of events) {
    if (event.tool !== 'task' || event.type !== 'tool.requested') continue
    const id = event.toolCallId
    if (!id || completedCallIds.has(id) || seen.has(id)) continue
    seen.add(id)
    result.push(event)
  }
  return result
}
