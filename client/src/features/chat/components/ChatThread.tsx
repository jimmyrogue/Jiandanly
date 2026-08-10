import { Fragment, useCallback, useEffect, useRef } from 'react'
import { AgentProgress, type AgentFailureAction } from './progress/AgentProgress'
import { AnsweredQuestions } from './AnsweredQuestions'
import { MessageBubble } from './MessageBubble'
import { RunProcess } from './RunProcess'
import { ThinkingIndicator } from './ThinkingIndicator'
import { IconArrowDown, IconCalendar, IconFileText, IconMessage } from '@tabler/icons-react'
import type { ChatMessage, Conversation, LocalFileRef } from '@/shared/local-data/types'
import { appLogoURL } from '@/shared/assets/logo'
import { useI18n } from '@/shared/i18n/i18n'
import { useSmartAutoScroll } from '@/shared/streaming/useSmartAutoScroll'

export function ChatThread({
  conversation,
  workspaceRoot,
  onOpenArtifact,
  onLoadArtifactContent,
  onOpenDiagnostics,
  onPreviewLocalFile,
  onLocalFileContextMenu,
  onPickSuggestion,
  onRegenerateMessage,
  onEditResendMessage,
  onDeleteMessage,
  onFailureAction,
}: {
  conversation?: Conversation
  /** Currently effective authorized workspace. App supplies this explicitly
   *  so a project picked before the first message is usable while the first
   *  Runtime projection is still converging. */
  workspaceRoot?: string
  onOpenArtifact: (artifactID: string) => void
  onLoadArtifactContent?: (artifactID: string) => Promise<Blob>
  onOpenDiagnostics: (runID: string) => void
  /** Open supported local files in the right-side preview. */
  onPreviewLocalFile?: (ref: LocalFileRef) => void
  onLocalFileContextMenu?: (ref: LocalFileRef) => void
  /** Welcome-screen suggestion tiles: prefill the composer with a concrete
   *  example prompt (the user edits/sends). Wired from App.tsx → setDraft. */
  onPickSuggestion?: (prompt: string) => void
  /** Re-run an assistant turn (drop it + everything after, re-issue the
   *  originating user message). Wired from App.tsx. */
  onRegenerateMessage?: (messageID: string) => void
  /** Edit a user message and resend (truncate + fresh run). */
  onEditResendMessage?: (messageID: string, newText: string) => void
  /** Delete a message (user msg drops its paired reply too). */
  onDeleteMessage?: (messageID: string) => void
  /** Action button surfaced from a failed assistant run. */
  onFailureAction?: (action: AgentFailureAction, messageID: string) => void
}) {
  // Conversations bound to a project workspace carry the absolute path;
  // MessageBubble uses it to resolve relative office-file refs surfaced
  // by the agent's fs.list / ls output.
  const effectiveWorkspaceRoot = workspaceRoot ?? conversation?.workspace?.path
  const runActive =
    conversation?.messages.some(
      (message) =>
        message.status === 'streaming' ||
        message.status === 'pending' ||
        message.status === 'waiting_permission' ||
        message.status === 'waiting_input',
    ) ?? false
  const { t } = useI18n()
  // Time-of-day greeting for the empty state — quiet and personal, matching
  // the v4 prototype ("下午好。") rather than a marketing headline.
  const hour = new Date().getHours()
  const greetingKey =
    hour < 6
      ? 'welcome.greeting.night'
      : hour < 12
        ? 'welcome.greeting.morning'
        : hour < 18
          ? 'welcome.greeting.afternoon'
          : 'welcome.greeting.evening'
  const streamDisplayCacheRef = useRef<Map<string, string>>(new Map())
  const welcomeSuggestions = [
    {
      Icon: IconMessage,
      title: t('welcome.unread.title'),
      description: t('welcome.unread.description'),
      prompt: t('welcome.unread.prompt'),
    },
    {
      Icon: IconFileText,
      title: t('welcome.minutes.title'),
      description: t('welcome.minutes.description'),
      prompt: t('welcome.minutes.prompt'),
    },
    {
      Icon: IconCalendar,
      title: t('welcome.today.title'),
      description: t('welcome.today.description'),
      prompt: t('welcome.today.prompt'),
    },
  ]
  const messageCount = conversation?.messages.length ?? 0
  const lastMessage = conversation?.messages.at(-1)
  const lastMessageContent = lastMessage?.content ?? ''
  const presentationRevision = lastMessage?.presentation?.snapshot.event_high_watermark ?? 0
  const presentationDraftLength = Object.values(lastMessage?.presentation?.drafts ?? {})
    .reduce((total, draft) => total + draft.length, 0)
  const { containerRef, isPinnedToBottom, scrollToBottom } = useSmartAutoScroll<HTMLDivElement>(
    [messageCount, lastMessageContent.length, presentationRevision, presentationDraftLength],
    { bottomThreshold: 120, runActive, resetKey: conversation?.id },
  )
  const handleStreamTextCommit = useCallback((messageID: string, displayedText: string) => {
    streamDisplayCacheRef.current.set(messageID, displayedText)
  }, [])

  useEffect(() => {
    for (const message of conversation?.messages ?? []) {
      if (message.status !== 'streaming') {
        streamDisplayCacheRef.current.delete(message.id)
      }
    }
  }, [conversation])
  return (
    <section className="chat-surface">
      {conversation?.messages.length ? (
        <>
          <div className="messages" ref={containerRef}>
            {conversation.messages.map((message, index) => (
              <Fragment key={message.id}>
                {/* Stone-dot divider between a user turn and the assistant's
                    reply — the brand's signature separator (三颗渐变小石点),
                    echoing the rich-text <hr>. Only between user → assistant. */}
                {message.role === 'assistant' && conversation.messages[index - 1]?.role === 'user' ? (
                  <StoneDots />
                ) : null}
                <AnsweredQuestions message={message} />
                <MessageBubble
                  message={message}
                  initialStreamText={message.status === 'streaming' ? streamDisplayCacheRef.current.get(message.id) : undefined}
                  onStreamTextCommit={handleStreamTextCommit}
                  workspaceRoot={effectiveWorkspaceRoot}
                  onPreviewLocalFile={onPreviewLocalFile}
                  onLocalFileContextMenu={onLocalFileContextMenu}
                  onRegenerate={onRegenerateMessage}
                  onEditResend={onEditResendMessage}
                  onDelete={onDeleteMessage}
                  onOpenDiagnostics={onOpenDiagnostics}
                  onLoadArtifactContent={onLoadArtifactContent}
                  runActive={runActive}
                >
                  <MessageProcess
                    message={message}
                    onOpenArtifact={onOpenArtifact}
                    onFailureAction={index === conversation.messages.length - 1
                      ? (action, failedMessage) => onFailureAction?.(action, failedMessage.id)
                      : undefined}
                  />
                </MessageBubble>
              </Fragment>
            ))}
            {conversation.messages.at(-1) ? (
              <ThinkingIndicator message={conversation.messages[conversation.messages.length - 1]} />
            ) : null}
          </div>
          {/* Scroll-to-bottom affordance (Codex / Claude Code pattern):
              appears only while the user has scrolled away from the latest
              content; click returns to the newest message. */}
          {!isPinnedToBottom ? (
            <button
              type="button"
              className="scroll-to-bottom"
              onClick={scrollToBottom}
              aria-label={t('chat.scrollToBottom')}
              title={t('chat.scrollToBottom')}
            >
              <IconArrowDown size={16} stroke={2} aria-hidden="true" />
            </button>
          ) : null}
        </>
      ) : (
        <div className="empty-state welcome-body">
          <div className="logo" aria-hidden="true">
            <img src={appLogoURL} alt="" />
          </div>
          <h1>{t(greetingKey)}</h1>
          <p>{t('welcome.subtitle')}</p>
          <div className="suggest-grid" aria-label={t('welcome.suggestions')}>
            {welcomeSuggestions.map(({ Icon, title, description, prompt }) => (
              <button className="suggest-tile" type="button" onClick={() => onPickSuggestion?.(prompt)} key={title}>
                <Icon className="suggest-tile-icon" size={17} stroke={1.8} aria-hidden="true" />
                <span className="suggest-title">{title}</span>
                <span className="suggest-description">{description}</span>
              </button>
            ))}
          </div>
        </div>
      )}
    </section>
  )
}

function MessageProcess({
  message,
  onOpenArtifact,
  onFailureAction,
}: {
  message: ChatMessage
  onOpenArtifact: (artifactID: string) => void
  onFailureAction?: (action: AgentFailureAction, message: ChatMessage) => void
}) {
  const { t } = useI18n()
  const hasPresentationProcess = Boolean(
    Object.values(message.presentation?.drafts ?? {}).some(Boolean)
    || message.presentation?.snapshot.items?.some((item) => item.kind !== 'final_answer'),
  )
  const recoveryMessage = message.presentation && message.status === 'error'
    ? {
        ...message,
        subagents: undefined,
        agentEvents: (message.agentEvents ?? [])
          .filter((event) => event.type === 'run.failed' || event.type === 'run.cleanup_required')
          .map((event) => ({
            ...event,
            label: event.type === 'run.failed'
              ? t('chat.timeline.runFailed')
              : t('chat.timeline.runCleanupRequired'),
          })),
      }
    : message
  return (
    <>
      {hasPresentationProcess ? (
        <RunProcess message={message} onOpenArtifact={onOpenArtifact} />
      ) : null}
      {!hasPresentationProcess || message.status === 'error' ? (
        <AgentProgress
          message={recoveryMessage}
          onOpenArtifact={onOpenArtifact}
          onFailureAction={onFailureAction}
        />
      ) : null}
    </>
  )
}

/** The brand's signature turn separator: three small graduated "stones"
 *  (3 / 4.5 / 3 px) centered on the chat column. Decorative only. */
function StoneDots() {
  return (
    <div className="stone-dots" aria-hidden="true">
      <span />
      <span />
      <span />
    </div>
  )
}
