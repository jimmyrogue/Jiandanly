import { useCallback, useEffect, useRef, useState } from 'react'
import { IconBox, IconCheck, IconCommand, IconCopy, IconPencil, IconRefresh, IconSparkles, IconStethoscope, IconTrash } from '@tabler/icons-react'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { cn } from '@/lib/utils'
import { formatMessageTime, useI18n, type Translator } from '@/shared/i18n/i18n'
import { FileTypeIcon } from '@/shared/files/FileTypeIcon'
import { filePreviewKind } from '@/shared/files/filePreview'
import type { ChatMessage, LocalFileRef } from '@/shared/local-data/types'
import { useSmoothTextStream } from '@/shared/streaming/useSmoothTextStream'
import { completePartialMarkdown } from '@/shared/streaming/completePartialMarkdown'
import {
  CodeExecutionImages,
  GeneratedArtifactImages,
  MarkdownContent,
} from './MessageContent'

const zhUsageNumberFormatter = new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 2 })
const enUsageNumberFormatter = new Intl.NumberFormat('en-US', { maximumFractionDigits: 2 })

function useMessageBubbleViewModel({
  message,
  children,
  initialStreamText = '',
  onStreamTextCommit,
  workspaceRoot,
  onPreviewLocalFile,
  onLocalFileContextMenu,
  onRegenerate,
  onEditResend,
  onDelete,
  onOpenDiagnostics,
  onLoadArtifactContent,
  runActive = false,
}: {
  message: ChatMessage
  children?: React.ReactNode
  initialStreamText?: string
  onStreamTextCommit?: (messageID: string, displayedText: string) => void
  /** Re-run an assistant turn: drops it (and everything after) and
   *  re-issues the originating user message. Assistant messages only. */
  onRegenerate?: (messageID: string) => void
  /** Edit a user message and resend: drops it (and everything after) and
   *  starts a fresh run with the edited text. User messages only. */
  onEditResend?: (messageID: string, newText: string) => void
  /** Delete a message. Deleting a user message also drops its paired
   *  assistant reply; deleting an assistant message drops just it. */
  onDelete?: (messageID: string) => void
  /** Open diagnostics for this Runtime-backed assistant turn. */
  onOpenDiagnostics?: (runID: string) => void
  /** Load an authenticated Runtime Artifact body for inline image rendering. */
  onLoadArtifactContent?: (artifactID: string) => Promise<Blob>
  /** True while a run is streaming for this conversation — disables the
   *  retry/edit/delete actions so the user can't mutate mid-run. */
  runActive?: boolean
  /** Absolute path of the active conversation's workspace, used to
   *  resolve relative local-file refs in agent text. Undefined for
   *  chats without a project. */
  workspaceRoot?: string
  /** Callback fired when the user clicks a recognized local filename
   *  rendered inside agent markdown. Undefined disables the click. */
  onPreviewLocalFile?: (ref: LocalFileRef) => void
  /** Show the native attachment menu (preview/open/save/reveal). */
  onLocalFileContextMenu?: (ref: LocalFileRef) => void
}) {
  const { locale, t } = useI18n()
  const previousMessageIDRef = useRef(message.id)
  const previousContentRef = useRef('')
  const isAssistant = message.role === 'assistant'
  const commitStreamText = useCallback((displayedText: string) => {
    if (isAssistant && message.status === 'streaming') {
      onStreamTextCommit?.(message.id, displayedText)
    }
  }, [isAssistant, message.id, message.status, onStreamTextCommit])
  const stream = useSmoothTextStream({
    locale,
    segmentsPerTick: 3,
    tickMs: 22,
    onCommit: commitStreamText,
  })

  useEffect(() => {
    if (previousMessageIDRef.current !== message.id) {
      previousMessageIDRef.current = message.id
      previousContentRef.current = ''
      stream.cancel()
    }
    if (isAssistant && message.status === 'error') {
      previousContentRef.current = message.content
      if (stream.isStreaming || stream.text) {
        stream.cancel()
      }
      return
    }
    if (!isAssistant || message.status !== 'streaming') {
      if (stream.isStreaming) {
        // A terminal run can trigger an OS notification immediately. Flush
        // the buffered tail now so "reply complete" never precedes the UI.
        if (message.content.startsWith(previousContentRef.current)) {
          const delta = message.content.slice(previousContentRef.current.length)
          if (delta) {
            stream.pushChunk(delta)
          }
        } else {
          stream.pushChunk(message.content)
        }
        previousContentRef.current = message.content
        stream.finish()
      } else {
        previousContentRef.current = message.content
      }
      return
    }
    if (!stream.isStreaming) {
      const seedText = message.content.startsWith(initialStreamText) ? initialStreamText : ''
      stream.start(seedText)
      previousContentRef.current = seedText
    }
    if (message.content.startsWith(previousContentRef.current)) {
      const delta = message.content.slice(previousContentRef.current.length)
      if (delta) {
        stream.pushChunk(delta)
        previousContentRef.current = message.content
      }
    } else if (message.content) {
      stream.start(message.content)
      previousContentRef.current = message.content
    }
  }, [initialStreamText, isAssistant, message.content, message.id, message.status, stream])

  const [copied, setCopied] = useState(false)
  const [copyFailed, setCopyFailed] = useState(false)
  const copyResetRef = useRef<number | undefined>(undefined)
  useEffect(() => () => window.clearTimeout(copyResetRef.current), [])

  const handleCopy = async () => {
    const text = message.content.trim()
    if (!text) {
      return
    }
    try {
      if (!navigator.clipboard) throw new Error('clipboard unavailable')
      await navigator.clipboard.writeText(text)
      setCopyFailed(false)
      setCopied(true)
      window.clearTimeout(copyResetRef.current)
      copyResetRef.current = window.setTimeout(() => setCopied(false), 1500)
    } catch {
      setCopied(false)
      setCopyFailed(true)
      window.clearTimeout(copyResetRef.current)
      copyResetRef.current = window.setTimeout(() => setCopyFailed(false), 1500)
    }
  }

  const [editing, setEditing] = useState(false)
  const [editText, setEditText] = useState('')
  const startEdit = () => {
    setEditText(message.content)
    setEditing(true)
  }
  const cancelEdit = () => setEditing(false)
  const commitEdit = () => {
    const next = editText.trim()
    if (!next) {
      return
    }
    setEditing(false)
    onEditResend?.(message.id, next)
  }

  const waitingText = message.status === 'waiting_permission'
    ? t('message.waitingPermission')
    : message.status === 'waiting_input'
      ? t('message.waitingInput')
      : ''
  const content = message.content || waitingText
  const generatedArtifactImageRefs = new Set((message.agentEvents ?? []).flatMap((event) => (
    event.type === 'artifact.created'
    && (event.artifactTool === 'image.generate' || event.artifactTool === 'image.edit')
    && event.artifactMediaType?.startsWith('image/')
      ? [event.artifactTitle ?? '', event.artifactId ?? ''].filter(Boolean)
      : []
  )))
  const hideFailedAssistantContent = isFailureOnlyAssistantContent(message)
  // Action affordances appear on settled turns only (not mid-stream).
  const settled = message.status === 'done' || message.status === 'error'
  const latestCleanupState = [...(message.agentEvents ?? [])].reverse().find(
    (event) => event.type === 'run.cleanup_required' || event.type === 'run.failed',
  )
  const cleanupUnconfirmed = latestCleanupState?.type === 'run.cleanup_required'
  const canRegenerate = isAssistant && settled && !cleanupUnconfirmed && Boolean(onRegenerate)
  const canEdit = !isAssistant && Boolean(onEditResend)
  const canDelete = settled && Boolean(onDelete)

  // Per-turn Runtime usage comes from model token counts and completed tools.
  const toolCalls = (message.agentEvents ?? []).filter((event) => event.type === 'tool.completed' || event.type === 'tool.failed').length
  const usageParts = buildUsageParts(message, toolCalls, locale, t)
  const showUsage = isAssistant && settled && usageParts.length > 0
  const showStream = isAssistant && !hideFailedAssistantContent && (message.status === 'streaming' || stream.isStreaming)
  const messageTime = formatMessageTime(message.createdAt, locale, t)
  const attachmentCards = message.attachments?.length ? (
    <div className={cn('message-attachments', !isAssistant && 'message-attachments-detached')}>
      {message.attachments.map((attachment) => {
        const ref: LocalFileRef = {
          ...attachment,
          kind: filePreviewKind(attachment.name),
        }
        return onPreviewLocalFile ? (
          <button
            type="button"
            className="message-attachment"
            key={`${attachment.runId ?? ''}:${attachment.inputId ?? attachment.path}`}
            title={attachment.path}
            onClick={() => onPreviewLocalFile(ref)}
            onContextMenu={(event) => {
              if (!onLocalFileContextMenu) return
              event.preventDefault()
              onLocalFileContextMenu(ref)
            }}
          >
            <span className="message-attachment-glyph" aria-hidden="true">
              <FileTypeIcon name={attachment.name} />
            </span>
            <span className="message-attachment-name">{attachment.name}</span>
          </button>
        ) : (
          <span className="message-attachment" key={attachment.path} title={attachment.path}>
            <span className="message-attachment-glyph" aria-hidden="true">
              <FileTypeIcon name={attachment.name} />
            </span>
            <span className="message-attachment-name">{attachment.name}</span>
          </span>
        )
      })}
    </div>
  ) : null

  return { attachmentCards, canDelete, canEdit, canRegenerate, cancelEdit, children, commitEdit, content, copied, copyFailed, editText, editing, generatedArtifactImageRefs, handleCopy, hideFailedAssistantContent, isAssistant, message, messageTime, onDelete, onLoadArtifactContent, onLocalFileContextMenu, onOpenDiagnostics, onPreviewLocalFile, onRegenerate, runActive, setEditText, showStream, showUsage, startEdit, stream, t, usageParts, waitingText, workspaceRoot }
}

export function MessageBubble(props: Parameters<typeof useMessageBubbleViewModel>[0]) {
  return <MessageBubbleView view={useMessageBubbleViewModel(props)} />
}

function MessageBubbleView({ view }: { view: ReturnType<typeof useMessageBubbleViewModel> }) {
  const { attachmentCards, canDelete, canEdit, canRegenerate, cancelEdit, children, commitEdit, content, copied, copyFailed, editText, editing, generatedArtifactImageRefs, handleCopy, hideFailedAssistantContent, isAssistant, message, messageTime, onDelete, onLoadArtifactContent, onLocalFileContextMenu, onOpenDiagnostics, onPreviewLocalFile, onRegenerate, runActive, setEditText, showStream, showUsage, startEdit, stream, t, usageParts, waitingText, workspaceRoot } = view
  return (
    <article className={cn('message', message.role)}>
      {!isAssistant ? attachmentCards : null}
      <div className="message-bubble-inner">
        <div className="message-content">
          {!isAssistant && (message.pluginReferences?.length || message.pluginCommand) ? (
            <div className="message-attachments message-plugin-selection">
              {message.pluginReferences?.map((plugin) => (
                <span key={plugin.pluginId} title={`${plugin.pluginId}\n${plugin.digest}`}>
                  <IconBox size={14} aria-hidden="true" />
                  @{plugin.name}
                </span>
              ))}
              {message.pluginCommand ? (
                <span title={`${message.pluginCommand.pluginId}:${message.pluginCommand.commandId}\n${message.pluginCommand.digest}`}>
                  <IconCommand size={14} aria-hidden="true" />
                  /{message.pluginCommand.pluginName}: {message.pluginCommand.title}
                </span>
              ) : null}
            </div>
          ) : null}
          {isAssistant ? attachmentCards : null}
          {editing ? (
            <div className="message-edit">
              <textarea
                className="message-edit-input"
                value={editText}
                autoFocus
                rows={Math.min(10, Math.max(2, editText.split('\n').length))}
                aria-label={t('message.edit')}
                onChange={(event) => setEditText(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) {
                    event.preventDefault()
                    commitEdit()
                  } else if (event.key === 'Escape') {
                    event.preventDefault()
                    cancelEdit()
                  }
                }}
              />
              <div className="message-edit-actions">
                <button type="button" className="message-edit-cancel" onClick={cancelEdit}>
                  {t('message.editCancel')}
                </button>
                <button type="button" className="message-edit-save" onClick={commitEdit} disabled={!editText.trim()}>
                  {t('message.editSave')}
                </button>
              </div>
            </div>
          ) : showStream ? (
            stream.text ? (
              <MarkdownContent
                content={completePartialMarkdown(stream.text)}
                generatedArtifactImageRefs={generatedArtifactImageRefs}
                workspaceRoot={workspaceRoot}
                onPreviewLocalFile={onPreviewLocalFile}
                onLocalFileContextMenu={onLocalFileContextMenu}
              />
            ) : waitingText ? (
              <p className="whitespace-pre-wrap break-words">{waitingText}</p>
            ) : null
          ) : hideFailedAssistantContent ? null : (
            <MarkdownContent
              content={content}
              generatedArtifactImageRefs={generatedArtifactImageRefs}
              normalizeHeadings
              workspaceRoot={workspaceRoot}
              onPreviewLocalFile={onPreviewLocalFile}
              onLocalFileContextMenu={onLocalFileContextMenu}
            />
          )}
        </div>
        {/* Rich rendering for matplotlib/PIL figures from code.execute.
         *  We pull image/png base64 payloads out of every tool.completed
         *  event for code.execute and inline them — that way users see
         *  the actual chart instead of just LLM prose describing it (or,
         *  worse, the model's hallucinated `![](imgbb.com/…)` URL). */}
        {isAssistant ? <CodeExecutionImages events={message.agentEvents} /> : null}
        {isAssistant ? (
          <GeneratedArtifactImages
            events={message.agentEvents}
            onLoadArtifactContent={onLoadArtifactContent}
          />
        ) : null}
        {children}
      </div>
      <div className="message-meta">
        {message.content.trim() && !hideFailedAssistantContent ? (
          <button
            type="button"
            className="message-meta-action"
            onClick={() => void handleCopy()}
            title={copyFailed ? t('message.copyFailed') : copied ? t('message.copied') : t('message.copy')}
            aria-label={copyFailed ? t('message.copyFailed') : copied ? t('message.copied') : t('message.copy')}
          >
            {copied ? <IconCheck size={13} aria-hidden="true" /> : <IconCopy size={13} aria-hidden="true" />}
          </button>
        ) : null}
        {!editing && canRegenerate ? (
          <button
            type="button"
            className="message-meta-action"
            onClick={() => onRegenerate?.(message.id)}
            disabled={runActive}
            title={t('message.regenerate')}
            aria-label={t('message.regenerate')}
          >
            <IconRefresh size={13} aria-hidden="true" />
          </button>
        ) : null}
        {!editing && canEdit ? (
          <button
            type="button"
            className="message-meta-action"
            onClick={startEdit}
            disabled={runActive}
            title={t('message.edit')}
            aria-label={t('message.edit')}
          >
            <IconPencil size={13} aria-hidden="true" />
          </button>
        ) : null}
        {!editing && canDelete ? (
          <button
            type="button"
            className="message-meta-action message-meta-action-danger"
            onClick={() => onDelete?.(message.id)}
            disabled={runActive}
            title={t('message.delete')}
            aria-label={t('message.delete')}
          >
            <IconTrash size={13} aria-hidden="true" />
          </button>
        ) : null}
        {isAssistant ? <ModelModeBadge runMode={message.runMode} /> : null}
        {showUsage ? (
          <span className="message-meta-usage" title={t('agent.usageTooltip')}>
            {usageParts.join(' · ')}
          </span>
        ) : null}
        {showUsage && messageTime ? <span className="message-meta-dot" aria-hidden="true">·</span> : null}
        {messageTime ? <span className="message-meta-time">{messageTime}</span> : null}
        {isAssistant && message.runId && onOpenDiagnostics ? (
          <button
            type="button"
            className="message-meta-action"
            onClick={() => onOpenDiagnostics(message.runId!)}
            title={t('agent.viewDiagnostics', { id: message.runId })}
            aria-label={t('agent.diagnostics')}
          >
            <IconStethoscope size={13} aria-hidden="true" />
          </button>
        ) : null}
      </div>
    </article>
  )
}

function ModelModeBadge({ runMode }: { runMode?: ChatMessage['runMode'] }) {
  if (!runMode?.resolved?.trim()) {
    return null
  }
  const reason = runMode.reason?.trim()
  const label = runMode.resolved
  const badge = (
    <span className="message-meta-mode">
      <IconSparkles className="message-meta-mode-icon" size={11} aria-hidden="true" />
      <span className="message-meta-mode-label">{label}</span>
    </span>
  )
  if (!reason) {
    return badge
  }
  return (
    <Tooltip>
      <TooltipTrigger asChild>{badge}</TooltipTrigger>
      <TooltipContent side="top" sideOffset={4}>
        {reason}
      </TooltipContent>
    </Tooltip>
  )
}

function isFailureOnlyAssistantContent(message: ChatMessage): boolean {
  if (message.role !== 'assistant' || message.status !== 'error') {
    return false
  }
  const content = message.content.trim()
  if (!content) return false
  const failure = [...(message.agentEvents ?? [])].reverse().find(
    (event) => event.type === 'run.failed' || event.type === 'run.cleanup_required',
  )
  const label = failure?.label.trim() ?? ''
  return label === content || (content.length <= 200 && label.startsWith(`${content} ·`))
}

function buildUsageParts(
  message: ChatMessage,
  toolCalls: number,
  locale: string,
  t: Translator,
): string[] {
  const parts: string[] = []
  if (typeof message.tokens === 'number' && message.tokens > 0) {
    parts.push(t('agent.usageTokens', { count: formatUsageNumber(message.tokens, locale) }))
  }
  if (toolCalls > 0) {
    parts.push(t('agent.usageTools', { count: formatUsageNumber(toolCalls, locale) }))
  }
  return parts
}

function formatUsageNumber(value: number, locale: string): string {
  return (locale === 'zh' ? zhUsageNumberFormatter : enUsageNumberFormatter).format(value)
}
