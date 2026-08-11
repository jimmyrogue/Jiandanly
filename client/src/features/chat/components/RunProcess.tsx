import { useEffect, useState } from 'react'
import {
  IconAlertCircle,
  IconBox,
  IconCheck,
  IconChevronDown,
  IconChevronRight,
  IconLoader2,
  IconMessageQuestion,
  IconSearch,
  IconTool,
  IconUsers,
  IconWorld,
} from '@tabler/icons-react'
import type { RunPresentationItem } from '@shejane/runtime-sdk'
import { toolActionLabel, truncate } from '@/features/chat/projection/chatToolPresentation'
import { useModelPhaseText } from '@/features/chat/projection/modelPhasePresentation'
import { useI18n } from '@/shared/i18n/i18n'
import type { ChatMessage } from '@/shared/local-data/types'

const NARRATIVE_PREVIEW_MAX = 120
type ToolItem = Extract<RunPresentationItem, { kind: 'tool' }>

export function RunProcess({
  message,
  onOpenArtifact,
}: {
  message: ChatMessage
  onOpenArtifact?: (artifactID: string) => void
}) {
  const { t } = useI18n()
  const presentation = message.presentation
  const items = (presentation?.snapshot.items ?? []).filter((item) => item.kind !== 'final_answer')
  const drafts = Object.values(presentation?.drafts ?? {}).filter(Boolean)
  const [expanded, setExpanded] = useState(message.status === 'error')
  useEffect(() => {
    if (message.status === 'done') setExpanded(false)
    if (message.status === 'error') setExpanded(true)
  }, [message.status])
  const completed = message.status === 'done' || message.status === 'error'
  const currentTool = currentToolForActiveRun(items)
  const phaseActive = !completed
    && !currentTool
    && Boolean(message.modelPhase && message.modelPhase !== 'completed')
  const phaseText = useModelPhaseText(
    message.modelPhase,
    message.modelPhaseStartedAt,
    phaseActive,
    t,
  )
  if (!presentation) return null
  if (!items.length && !drafts.length) return null

  const tools = items.filter((item) => item.kind === 'tool').length
  const summary = t('agent.process.summary', { steps: items.length, tools })
  const bodyId = `run-process-${message.id}`

  if (completed) {
    return (
      <section className="run-process run-process-completed">
        <button
          type="button"
          className="run-process-summary"
          aria-expanded={expanded}
          aria-controls={bodyId}
          onClick={() => setExpanded((value) => !value)}
        >
          <span>{summary}</span>
          {expanded
            ? <IconChevronDown size={14} aria-hidden="true" />
            : <IconChevronRight size={14} aria-hidden="true" />}
        </button>
        {expanded ? (
          <ProcessItems
            id={bodyId}
            items={items}
            drafts={[]}
            compactNarratives={false}
            onOpenArtifact={onOpenArtifact}
          />
        ) : null}
      </section>
    )
  }

  const currentToolAction = currentTool ? toolActionLabel(currentTool.tool_name, t) : ''
  const currentActivity = currentTool
    ? [
        currentToolAction === currentTool.tool_name ? t('chat.tool.fallback') : currentToolAction,
        currentTool.display_target,
      ].filter(Boolean).join(' · ')
    : message.modelPhase && message.modelPhase !== 'completed'
      ? phaseText
      : t('agent.process.running')
  const historicalItems = currentTool
    ? items.filter((item) => item.id !== currentTool.id)
    : items

  return (
    <section className="run-process run-process-active" aria-live="polite">
      <div className="run-process-title">
        <IconLoader2 className="run-process-spinner" size={14} aria-hidden="true" />
        <span>{currentActivity}</span>
      </div>
      <ProcessItems
        id={bodyId}
        items={historicalItems}
        drafts={drafts}
        compactNarratives
        onOpenArtifact={onOpenArtifact}
      />
    </section>
  )
}

function ProcessItems({
  id,
  items,
  drafts,
  compactNarratives,
  onOpenArtifact,
}: {
  id: string
  items: RunPresentationItem[]
  drafts: string[]
  compactNarratives: boolean
  onOpenArtifact?: (artifactID: string) => void
}) {
  const groupedItems = groupCompletedTools(items)
  return (
    <div id={id} className="run-process-items">
      {groupedItems.map(({ item, count }) => (
        <ProcessItem
          key={item.id}
          item={item}
          count={count}
          compactNarrative={compactNarratives}
          onOpenArtifact={onOpenArtifact}
        />
      ))}
      {drafts.map((draft, index) => (
        <ProcessNarrative
          key={`draft-${index}`}
          text={draft}
          active
          compact={compactNarratives}
        />
      ))}
    </div>
  )
}

function ProcessItem({
  item,
  count,
  compactNarrative,
  onOpenArtifact,
}: {
  item: RunPresentationItem
  count: number
  compactNarrative: boolean
  onOpenArtifact?: (artifactID: string) => void
}) {
  const { t } = useI18n()
  if (item.kind === 'progress') {
    return <ProcessNarrative text={item.text} compact={compactNarrative} />
  }
  if (item.kind === 'tool') {
    const active = item.status === 'pending' || item.status === 'in_progress' || item.status === 'waiting'
    const mappedAction = toolActionLabel(item.tool_name, t)
    const action = mappedAction === item.tool_name ? t('chat.tool.fallback') : mappedAction
    const status = item.status === 'completed'
      ? ''
      : t(`agent.process.status.${item.status}`)
    const statusDetail = [status, item.failure_detail].filter(Boolean).join(' · ')
    return (
      <div className="run-process-item run-process-activity" data-status={item.status}>
        {active
          ? <IconLoader2 className="run-process-spinner" size={13} aria-hidden="true" />
          : item.status === 'completed'
            ? <IconCheck size={13} aria-hidden="true" />
            : <IconTool size={13} aria-hidden="true" />}
        <span className="run-process-tool-name" title={mappedAction === item.tool_name ? item.tool_name : undefined}>
          {action}
        </span>
        {item.display_target ? (
          <>
            <span className="run-process-separator" aria-hidden="true">·</span>
            {item.display_target_kind === 'host'
              ? <IconWorld className="run-process-target-icon" size={12} aria-hidden="true" />
              : null}
            <span className="run-process-target" title={item.display_target}>
              {item.display_target}
            </span>
          </>
        ) : null}
        {count > 1 ? <span className="run-process-count">× {count}</span> : null}
        {statusDetail ? <span className="run-process-status">{statusDetail}</span> : null}
      </div>
    )
  }
  if (item.kind === 'reasoning_summary') {
    return (
      <details className="run-process-reasoning">
        <summary>{t('agent.process.reasoning')}</summary>
        <p>{item.summary}</p>
      </details>
    )
  }
  if (item.kind === 'subagent' || item.kind === 'verification') {
    const label = item.kind === 'subagent'
      ? item.description || item.subagent_type || t('agent.process.subagent')
      : t('agent.process.verification')
    return (
      <div className="run-process-item run-process-activity" data-status={item.status}>
        {item.kind === 'subagent'
          ? <IconUsers size={13} aria-hidden="true" />
          : <IconSearch size={13} aria-hidden="true" />}
        <span className="run-process-tool-name">{label}</span>
        <span className="run-process-status">{t(`agent.process.status.${item.status}`)}</span>
      </div>
    )
  }
  if (item.kind === 'artifact') {
    const content = (
      <>
        <IconBox size={13} aria-hidden="true" />
        <span className="run-process-tool-name">{item.title}</span>
        <span className="run-process-status">{t('agent.process.status.completed')}</span>
      </>
    )
    return onOpenArtifact ? (
      <button
        type="button"
        className="run-process-item run-process-activity run-process-artifact"
        onClick={() => onOpenArtifact(item.artifact_id)}
      >
        {content}
      </button>
    ) : (
      <div className="run-process-item run-process-activity">{content}</div>
    )
  }
  if (
    item.kind === 'approval'
    || item.kind === 'question'
    || item.kind === 'plan'
    || item.kind === 'reconciliation'
  ) {
    return (
      <div className="run-process-item run-process-decision" data-status={item.status}>
        <IconMessageQuestion size={13} aria-hidden="true" />
        <span className="run-process-tool-name">{item.summary}</span>
        <span className="run-process-status">{t(`agent.process.status.${item.status}`)}</span>
      </div>
    )
  }
  if (item.kind === 'notice') {
    const message = item.status === 'canceled'
      ? t('agent.process.notice.canceled')
      : item.status === 'unknown'
        ? t('agent.process.notice.unknown')
        : t('agent.process.notice.failed')
    return (
      <div className="run-process-item run-process-notice" role="status">
        <IconAlertCircle size={13} aria-hidden="true" />
        <span>{message}</span>
      </div>
    )
  }
  return null
}

function ProcessNarrative({
  text,
  active = false,
  compact,
}: {
  text: string
  active?: boolean
  compact: boolean
}) {
  const preview = text.replace(/\s+/g, ' ').trim()
  const shouldCollapse = compact && preview.length > NARRATIVE_PREVIEW_MAX
  if (!shouldCollapse) {
    return (
      <div className={`run-process-item run-process-narrative${active ? ' is-active' : ''}`}>
        <span className="run-process-item-marker" aria-hidden="true" />
        <span>{text}</span>
      </div>
    )
  }
  return (
    <details className={`run-process-narrative-details${active ? ' is-active' : ''}`}>
      <summary className="run-process-item run-process-narrative">
        <span className="run-process-item-marker" aria-hidden="true" />
        <span>{truncate(preview, NARRATIVE_PREVIEW_MAX)}</span>
        <IconChevronRight className="run-process-narrative-caret" size={13} aria-hidden="true" />
      </summary>
      <p>{text}</p>
    </details>
  )
}

function groupCompletedTools(
  items: RunPresentationItem[],
): Array<{ item: RunPresentationItem; count: number }> {
  return items.reduce<Array<{ item: RunPresentationItem; count: number }>>((groups, item) => {
    const previous = groups.at(-1)
    if (
      item.kind === 'tool'
      && item.status === 'completed'
      && previous?.item.kind === 'tool'
      && previous.item.status === 'completed'
      && sameToolPresentation(previous.item, item)
    ) {
      previous.count += 1
      return groups
    }
    groups.push({ item, count: 1 })
    return groups
  }, [])
}

function sameToolPresentation(left: ToolItem, right: ToolItem): boolean {
  return left.tool_name === right.tool_name
    && left.display_target === right.display_target
    && left.display_target_kind === right.display_target_kind
}

function currentToolForActiveRun(items: RunPresentationItem[]): ToolItem | undefined {
  for (let index = items.length - 1; index >= 0; index -= 1) {
    const item = items[index]
    if (
      item.kind === 'tool'
      && (item.status === 'pending' || item.status === 'in_progress')
    ) {
      return item
    }
  }
  return undefined
}
