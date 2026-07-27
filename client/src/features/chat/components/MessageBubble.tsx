import { Children, cloneElement, isValidElement, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import remarkBreaks from 'remark-breaks'
import remarkNormalizeHeadings from 'remark-normalize-headings'
import { IconBox, IconCheck, IconCommand, IconCopy, IconPencil, IconRefresh, IconSparkles, IconStethoscope, IconTrash } from '@tabler/icons-react'
import { ChatImage } from './ChatImage'
import { CodeBlock } from './CodeBlock'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { cn } from '@/lib/utils'
import { formatMessageTime, useI18n, type Translator } from '@/shared/i18n/i18n'
import { FileTypeIcon } from '@/shared/files/FileTypeIcon'
import { filePreviewKind } from '@/shared/files/filePreview'
import type { AgentTimelineItem, ChatMessage, LocalFileRef } from '@/shared/local-data/types'
import { useSmoothTextStream } from '@/shared/streaming/useSmoothTextStream'
import { completePartialMarkdown } from '@/shared/streaming/completePartialMarkdown'

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
        {isAssistant && message.reasoning?.trim() ? (
          <ReasoningDisclosure reasoning={message.reasoning} active={message.status === 'streaming'} />
        ) : null}
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

function ReasoningDisclosure({ reasoning, active }: { reasoning: string; active: boolean }) {
  const { t } = useI18n()
  return (
    <details className="message-reasoning">
      <summary aria-live={active ? 'polite' : undefined}>
        {active ? t('agent.thinkingStreaming') : t('message.reasoningTitle')}
      </summary>
      <div className="message-reasoning-body">{reasoning}</div>
    </details>
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

function MarkdownContent({
  content,
  generatedArtifactImageRefs,
  normalizeHeadings = false,
  workspaceRoot,
  onPreviewLocalFile,
  onLocalFileContextMenu,
}: {
  content: string
  generatedArtifactImageRefs?: ReadonlySet<string>
  normalizeHeadings?: boolean
  workspaceRoot?: string
  onPreviewLocalFile?: (ref: LocalFileRef) => void
  onLocalFileContextMenu?: (ref: LocalFileRef) => void
}) {
  if (!content) {
    return null
  }
  // remark-breaks: a single newline becomes a line break (LLMs emit single
  // newlines as paragraph separators; CommonMark would otherwise merge them).
  // remark-normalize-headings: rebalance ad-hoc heading levels — finished
  // content only, so streaming headings don't jump as more arrive.
  const remarkPlugins = normalizeHeadings
    ? [remarkGfm, remarkBreaks, remarkNormalizeHeadings]
    : [remarkGfm, remarkBreaks]
  // Click-to-preview is only enabled when (a) the parent gave us a
  // callback AND (b) we know which workspace to resolve relative paths
  // against. Without `workspaceRoot` we'd be guessing — the absolute
  // path case still works because the regex captures it whole.
  const previewEnabled = Boolean(onPreviewLocalFile)
  const previewClick = onPreviewLocalFile
  // react-markdown doesn't expose a `text`-node component override (the
  // `components` map only takes HTML element names), so we walk the
  // rendered children of common text containers and replace recognized
  // office filenames inside any string descendant. This catches refs in
  // paragraphs, list items, table cells, headings, blockquotes — without
  // needing a custom remark/rehype plugin.
  const renderChildren = (children: React.ReactNode): React.ReactNode => {
    if (!previewEnabled || !previewClick) return children
    return processChildren(children, workspaceRoot, previewClick, onLocalFileContextMenu)
  }
  return (
    <ReactMarkdown
      remarkPlugins={remarkPlugins}
      components={{
        a: ({ node: _node, ...props }) => <a {...props} target="_blank" rel="noreferrer" />,
        img: ({ node: _node, src, alt }) => {
          const source = typeof src === 'string' ? src : undefined
          const filename = source ? pathBasename(source.split(/[?#]/, 1)[0]) : ''
          return filename && generatedArtifactImageRefs?.has(filename)
            ? null
            : <ChatImage src={source} alt={alt} />
        },
        p: ({ children }) => <p>{renderChildren(children)}</p>,
        li: ({ children }) => <li>{renderChildren(children)}</li>,
        td: ({ children }) => <td>{renderChildren(children)}</td>,
        th: ({ children }) => <th>{renderChildren(children)}</th>,
        h1: ({ children }) => <h1>{renderChildren(children)}</h1>,
        h2: ({ children }) => <h2>{renderChildren(children)}</h2>,
        h3: ({ children }) => <h3>{renderChildren(children)}</h3>,
        h4: ({ children }) => <h4>{renderChildren(children)}</h4>,
        h5: ({ children }) => <h5>{renderChildren(children)}</h5>,
        h6: ({ children }) => <h6>{renderChildren(children)}</h6>,
        blockquote: ({ children }) => <blockquote>{renderChildren(children)}</blockquote>,
        // Fenced code blocks get syntax highlighting + a per-block copy
        // button (CodeBlock). Inline code keeps the plain chip styling.
        // react-markdown v9 dropped the `inline` prop, so detect a block by
        // a `language-*` class or a multi-line body.
        code: ({ node: _node, className, children, ...rest }) => {
          const text = childrenToText(children)
          const match = /language-(\w+)/.exec(className || '')
          if (match || text.includes('\n')) {
            return <CodeBlock language={match?.[1]} code={text.replace(/\n$/, '')} />
          }
          const display = text.trim()
          const kind = filePreviewKind(display)
          const path = resolveLocalPath(display, workspaceRoot)
          if (previewClick && kind && path) {
            return (
              <code className={className} {...rest}>
                <LocalFileLink
                  path={path}
                  kind={kind}
                  name={pathBasename(display) || display}
                  display={display}
                  onClick={previewClick}
                  onContextMenu={onLocalFileContextMenu}
                />
              </code>
            )
          }
          return (
            <code className={className} {...rest}>
              {children}
            </code>
          )
        },
        // CodeBlock supplies its own <pre>; unwrap react-markdown's so we
        // don't nest <pre><div><pre>.
        pre: ({ children }) => <>{children}</>,
      }}
    >
      {content}
    </ReactMarkdown>
  )
}

/** Flatten a react-markdown children tree to its raw text — used to recover
 *  the verbatim source of a fenced code block for highlighting + copy. */
function childrenToText(node: React.ReactNode): string {
  if (node == null || node === false || node === true) {
    return ''
  }
  if (typeof node === 'string' || typeof node === 'number') {
    return String(node)
  }
  if (Array.isArray(node)) {
    return node.map(childrenToText).join('')
  }
  if (typeof node === 'object' && 'props' in node) {
    return childrenToText((node as { props?: { children?: React.ReactNode } }).props?.children)
  }
  return ''
}

/** Element types we deliberately don't crack open. `a` and `button`
 *  would produce invalid nested-interactive markup if we put an
 *  LocalFileLink (button) inside them; LocalFileLink itself never
 *  contains file refs to find. */
const NON_RECURSIVE_INLINE_TYPES = new Set<unknown>(['a', 'button', 'code'])

/** Walk a ReactNode tree, replacing any string descendants with a
 *  fragment that has recognized local filenames wrapped in clickable
 *  buttons. Non-string nodes recurse into their `children` prop so
 *  refs nested inside `<code>` / `<strong>` / `<em>` / etc. get
 *  picked up too (LLMs commonly format filenames as bold inline code,
 *  which we'd otherwise miss). */
function processChildren(
  children: React.ReactNode,
  workspaceRoot: string | undefined,
  onPreviewLocalFile: (ref: LocalFileRef) => void,
  onLocalFileContextMenu?: (ref: LocalFileRef) => void,
): React.ReactNode {
  if (typeof children === 'string') {
    return renderTextWithLocalFileLinks(children, workspaceRoot, onPreviewLocalFile, onLocalFileContextMenu)
  }
  if (Array.isArray(children)) {
    return Children.map(children, (child) =>
      processChildren(child, workspaceRoot, onPreviewLocalFile, onLocalFileContextMenu))
  }
  if (isValidElement(children)) {
    const props = (children.props ?? {}) as {
      children?: React.ReactNode
      node?: { tagName?: string }
    }
    if (
      NON_RECURSIVE_INLINE_TYPES.has(children.type) ||
      props.node?.tagName === 'code' ||
      children.type === LocalFileLink
    ) {
      return children
    }
    if (props.children === undefined) {
      return children
    }
    return cloneElement(
      children,
      undefined,
      processChildren(props.children, workspaceRoot, onPreviewLocalFile, onLocalFileContextMenu),
    )
  }
  // null / boolean / number — leave alone.
  return children
}

/** Regex that captures a chunk of "looks like a path" text ending in
 *  a short extension. The deterministic extension map decides whether
 *  the result is actually previewable. Matches:
 *    foo.docx
 *    sub/foo.docx
 *    /Users/me/project/foo.docx
 *    "my report.pdf"
 *  Doesn't try to be exhaustive — we stop at whitespace, quotes, or
 *  common markdown delimiters (backticks, parens) so we don't swallow
 *  surrounding punctuation. Case-insensitive on the extension only.
 */
const LOCAL_FILE_RE = /(["'])([^"'`\n]+?\.[A-Za-z0-9]{1,12})\1|([^\s"'`(){}\[\]<>]+\.[A-Za-z0-9]{1,12})/g

/** Cross-platform basename. Mirror of the helper in App.tsx. */
function pathBasename(path: string): string {
  const trimmed = path.replace(/[/\\]+$/, '')
  const idx = Math.max(trimmed.lastIndexOf('/'), trimmed.lastIndexOf('\\'))
  return idx >= 0 ? trimmed.slice(idx + 1) : trimmed
}

/** Returns true if `path` looks absolute (POSIX or Windows). Used to
 *  decide whether we need to prepend `workspaceRoot`. */
function isAbsolutePath(path: string): boolean {
  return path.startsWith('/') || /^[A-Za-z]:[\\/]/.test(path)
}

/** Join two path segments with the system's preferred separator. We
 *  default to "/" since the runtime runs on the user's machine and
 *  Windows tolerates forward slashes everywhere. */
function joinPath(root: string, rel: string): string {
  const cleanedRoot = root.replace(/[/\\]+$/, '')
  const cleanedRel = rel.replace(/^[/\\]+/, '')
  return `${cleanedRoot}/${cleanedRel}`
}

function resolveLocalPath(path: string, workspaceRoot: string | undefined): string | null {
  // The renderer cannot deterministically expand HOME. Keeping a tilde path
  // clickable only guarantees a Runtime 404, so leave it as honest prose.
  if (path.startsWith('~')) return null
  if (isAbsolutePath(path)) return path
  return workspaceRoot ? joinPath(workspaceRoot, path) : null
}

/** Scan a text node for office file references, returning a React
 *  fragment with the recognized refs replaced by clickable buttons.
 *  Non-match characters pass through verbatim, so this is safe to use
 *  on arbitrary agent prose.
 */
function renderTextWithLocalFileLinks(
  text: string,
  workspaceRoot: string | undefined,
  onPreviewLocalFile: (ref: LocalFileRef) => void,
  onLocalFileContextMenu?: (ref: LocalFileRef) => void,
): React.ReactNode {
  const parts: React.ReactNode[] = []
  let lastIndex = 0
  // The regex has `g` flag; we reset `lastIndex` to 0 on entry by using
  // `matchAll` (which constructs a fresh internal cursor each call).
  const matches = Array.from(text.matchAll(LOCAL_FILE_RE))
  if (matches.length === 0) {
    // Return the bare string — fragment-wrapping is invisible to React
    // but causes Testing Library's `findByText` to occasionally fail
    // when comparing normalized text content.
    return text
  }
  for (const match of matches) {
    const matchedText = match[2] ?? match[3]
    const fullMatch = match[0]
    const quoted = Boolean(match[2])
    const fullStart = match.index ?? 0
    const start = fullStart + (quoted ? 1 : 0)
    if (start > lastIndex) {
      parts.push(text.slice(lastIndex, start))
    }
    const kind = filePreviewKind(matchedText)
    const absolutePath = resolveLocalPath(matchedText, workspaceRoot)
    const prefix = text.slice(0, start)
    const truncatedAbsolutePath = !quoted && /(?:^|\s)[^\s]*[/\\][^\s]*\s$/.test(prefix)
    const ambiguousRelativePath = !quoted
      && !isAbsolutePath(matchedText)
      && /[/\\]/.test(matchedText)
      // One leading prose token ("Open docs/report.pdf") is unambiguous;
      // two or more may be a truncated space-containing path, so require
      // quotes/inline code rather than inventing a boundary.
      && prefix.trim().split(/\s+/).filter(Boolean).length >= 2
    if (!kind || !absolutePath || truncatedAbsolutePath || ambiguousRelativePath) {
      // Either the extension isn't one we preview, or we have no
      // workspace root to resolve against — render as plain text.
      parts.push(matchedText)
    } else {
      parts.push(
        <LocalFileLink
          key={`${start}-${matchedText}`}
          path={absolutePath}
          kind={kind}
          name={pathBasename(matchedText) || matchedText}
          display={matchedText}
          onClick={onPreviewLocalFile}
          onContextMenu={onLocalFileContextMenu}
        />,
      )
    }
    lastIndex = fullStart + fullMatch.length - (quoted ? 1 : 0)
  }
  if (lastIndex < text.length) {
    parts.push(text.slice(lastIndex))
  }
  return <>{parts}</>
}

function LocalFileLink({
  path,
  kind,
  name,
  display,
  onClick,
  onContextMenu,
}: {
  path: string
  kind: NonNullable<LocalFileRef['kind']>
  name: string
  display: string
  onClick: (ref: LocalFileRef) => void
  onContextMenu?: (ref: LocalFileRef) => void
}) {
  const ref = { path, kind, name }
  return (
    <button
      type="button"
      className="message-office-link"
      title={path}
      onClick={(event) => {
        event.preventDefault()
        onClick(ref)
      }}
      onContextMenu={(event) => {
        if (!onContextMenu) return
        event.preventDefault()
        onContextMenu(ref)
      }}
    >
      {display}
    </button>
  )
}

/**
 * Pulls all base64-encoded image/png payloads out of an assistant
 * message's `tool.completed` events for `code.execute` and renders
 * them as inline `<img>` elements. Used to surface matplotlib/PIL
 * figures so the user actually sees what the agent generated, rather
 * than relying on the LLM's text description (which often hallucinates
 * markdown image links pointing at fake URLs).
 *
 * Returns `null` when there are no images — the surrounding bubble
 * keeps its layout clean for text-only replies.
 */
function CodeExecutionImages({ events }: { events?: AgentTimelineItem[] }) {
  if (!events || events.length === 0) return null
  const images: string[] = []
  const seen = new Set<string>()
  for (const item of events) {
    if (item.type !== 'tool.completed' || item.tool !== 'code.execute') continue
    for (const img of item.codeExecImages ?? []) {
      if (!img || seen.has(img)) continue
      seen.add(img)
      images.push(img)
    }
  }
  if (images.length === 0) return null
  return (
    <div className="message-code-images">
      {images.map((b64) => (
        <img
          key={b64}
          src={`data:image/png;base64,${b64}`}
          alt=""
          className="message-code-image"
          loading="lazy"
        />
      ))}
    </div>
  )
}

function GeneratedArtifactImages({
  events,
  onLoadArtifactContent,
}: {
  events?: AgentTimelineItem[]
  onLoadArtifactContent?: (artifactID: string) => Promise<Blob>
}) {
  const [images, setImages] = useState<Array<{ id: string; title: string; url: string }>>([])
  const loaderRef = useRef(onLoadArtifactContent)
  loaderRef.current = onLoadArtifactContent

  useEffect(() => {
    const loader = loaderRef.current
    const artifacts = (events ?? []).filter((item) => (
      item.type === 'artifact.created'
      && (item.artifactTool === 'image.generate' || item.artifactTool === 'image.edit')
      && item.artifactMediaType?.startsWith('image/')
      && item.artifactId
    ))
    if (!loader || artifacts.length === 0) {
      setImages([])
      return
    }
    let disposed = false
    const createdURLs: string[] = []
    void Promise.all(artifacts.map(async (artifact) => {
      try {
        const blob = await loader(artifact.artifactId!)
        const url = URL.createObjectURL(blob)
        createdURLs.push(url)
        return {
          id: artifact.artifactId!,
          title: artifact.artifactTitle || artifact.artifactId!,
          url,
        }
      } catch {
        return undefined
      }
    })).then((loaded) => {
      if (disposed) {
        createdURLs.forEach((url) => URL.revokeObjectURL(url))
        return
      }
      setImages(loaded.filter((item): item is NonNullable<typeof item> => Boolean(item)))
    })
    return () => {
      disposed = true
      createdURLs.forEach((url) => URL.revokeObjectURL(url))
    }
  }, [events, Boolean(onLoadArtifactContent)])

  if (images.length === 0) return null
  return (
    <div className="message-code-images">
      {images.map((image) => (
        <ChatImage key={image.id} src={image.url} alt={image.title} />
      ))}
    </div>
  )
}
