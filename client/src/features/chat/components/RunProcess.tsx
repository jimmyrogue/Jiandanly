import { useState } from 'react'
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
} from '@tabler/icons-react'
import type { RunPresentationItem } from '@shejane/runtime-sdk'
import { useI18n } from '@/shared/i18n/i18n'
import type { ChatMessage } from '@/shared/local-data/types'

export function RunProcess({
  message,
  onOpenArtifact,
}: {
  message: ChatMessage
  onOpenArtifact?: (artifactID: string) => void
}) {
  const { t } = useI18n()
  const [expanded, setExpanded] = useState(false)
  const presentation = message.presentation
  if (!presentation) return null

  const items = (presentation.snapshot.items ?? []).filter((item) => item.kind !== 'final_answer')
  const drafts = Object.values(presentation.drafts).filter(Boolean)
  if (!items.length && !drafts.length) return null

  const completed = message.status === 'done' || message.status === 'error'
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
          <ProcessItems id={bodyId} items={items} drafts={[]} onOpenArtifact={onOpenArtifact} />
        ) : null}
      </section>
    )
  }

  return (
    <section className="run-process run-process-active" aria-live="polite">
      <div className="run-process-title">
        <IconLoader2 className="run-process-spinner" size={14} aria-hidden="true" />
        <span>{t('agent.process.running')}</span>
      </div>
      <ProcessItems
        id={bodyId}
        items={items}
        drafts={drafts}
        onOpenArtifact={onOpenArtifact}
      />
    </section>
  )
}

function ProcessItems({
  id,
  items,
  drafts,
  onOpenArtifact,
}: {
  id: string
  items: RunPresentationItem[]
  drafts: string[]
  onOpenArtifact?: (artifactID: string) => void
}) {
  return (
    <div id={id} className="run-process-items">
      {items.map((item) => (
        <ProcessItem key={item.id} item={item} onOpenArtifact={onOpenArtifact} />
      ))}
      {drafts.map((draft, index) => (
        <div className="run-process-item run-process-narrative is-active" key={`draft-${index}`}>
          <span className="run-process-item-marker" aria-hidden="true" />
          <span>{draft}</span>
        </div>
      ))}
    </div>
  )
}

function ProcessItem({
  item,
  onOpenArtifact,
}: {
  item: RunPresentationItem
  onOpenArtifact?: (artifactID: string) => void
}) {
  const { t } = useI18n()
  if (item.kind === 'progress') {
    return (
      <div className="run-process-item run-process-narrative">
        <span className="run-process-item-marker" aria-hidden="true" />
        <span>{item.text}</span>
      </div>
    )
  }
  if (item.kind === 'tool') {
    const active = item.status === 'pending' || item.status === 'in_progress' || item.status === 'waiting'
    return (
      <div className="run-process-item run-process-activity" data-status={item.status}>
        {active
          ? <IconLoader2 className="run-process-spinner" size={13} aria-hidden="true" />
          : item.status === 'completed'
            ? <IconCheck size={13} aria-hidden="true" />
            : <IconTool size={13} aria-hidden="true" />}
        <span className="run-process-tool-name">{item.tool_name}</span>
        <span className="run-process-status">{t(`agent.process.status.${item.status}`)}</span>
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
