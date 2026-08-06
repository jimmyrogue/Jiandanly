import { useI18n } from '@/shared/i18n/i18n'
import { appLogoURL } from '@/shared/assets/logo'
import type { ChatMessage } from '@/shared/local-data/types'

const THINKING_STATUSES = new Set<ChatMessage['status']>(['pending', 'streaming'])

/**
 * Tiny left-aligned brand cue shown below the latest assistant message
 * while the model is thinking. No elapsed time, no token counter — just
 * the v4 prototype's quiet "round mark breathing" state.
 *
 * Paused (waiting_permission / waiting_input) doesn't render anything
 * here because PendingApprovalBar / PendingQuestionBar take over the
 * UI focus during those states.
 */
export function ThinkingIndicator({ message }: { message: ChatMessage }) {
  const { t } = useI18n()
  const isAssistant = message.role === 'assistant'
  const hasVisibleProcess = Boolean(
    message.presentation
    && ((message.presentation.snapshot.items?.length ?? 0) > 0
      || Object.values(message.presentation.drafts).some(Boolean)),
  )
  const active = isAssistant
    && THINKING_STATUSES.has(message.status)
    && !hasVisibleProcess
    && !message.content.trim()
  if (!active) {
    return null
  }
  return (
    <div className="thinking-indicator" role="status" aria-label={t('agent.thinking')}>
      <img src={appLogoURL} alt="" className="thinking-mark thinking-pulse" aria-hidden="true" />
      <span className="thinking-label">{t('agent.thinkingStreaming')}</span>
    </div>
  )
}
