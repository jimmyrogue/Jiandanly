import { useRef } from 'react'
import {
  beginRecoveryAction,
  createRecoveryState,
  endRecoveryAction,
  latestRunFailureEvent,
  nextRepairAttempt,
  nextRetryAttempt,
  type RecoveryTarget,
} from '@/features/chat/recovery'
import type { Translator } from '@/shared/i18n/i18n'
import { LocalConversationStore } from '@/shared/local-data/localConversations'
import type { AgentTimelineItem, ChatMessage, Conversation } from '@/shared/local-data/types'
import type { LocalHarnessRunOptions } from './runExecution'
import type { RuntimeCommandHandlers } from './useRuntimeCommands'

export function useMessageRecoveryActions({
  activeConversation,
  localData,
  resendFromUserMessage,
  setNotice,
  t,
}: {
  activeConversation?: Conversation
  localData: LocalConversationStore
  resendFromUserMessage: RuntimeCommandHandlers['resendFromUserMessage']
  setNotice: (message: string) => void
  t: Translator
}) {
  const recoveryStateRef = useRef<ReturnType<typeof createRecoveryState> | null>(null)
  if (recoveryStateRef.current === null) recoveryStateRef.current = createRecoveryState()
  const recoveryState = recoveryStateRef.current

  function recoveryTargetFor(assistantMessageID: string): RecoveryTarget | undefined {
    if (!activeConversation) return undefined
    return { conversationID: activeConversation.id, assistantMessageID }
  }

  async function retryRecoveryTarget(target: RecoveryTarget) {
    if (!beginRecoveryAction(recoveryState, 'retry', target)) {
      setNotice(t('app.notice.recoveryRetryAlreadyRunning'))
      return
    }
    try {
      await regenerateMessageInConversation(target.conversationID, target.assistantMessageID)
    } finally {
      endRecoveryAction(recoveryState, 'retry', target)
    }
  }

  function handleRegenerateMessage(assistantMessageID: string) {
    const target = recoveryTargetFor(assistantMessageID)
    if (target) void retryRecoveryTarget(target)
  }

  async function regenerateMessageInConversation(
    conversationID: string,
    assistantMessageID: string,
  ) {
    const conversation = await localData.get(conversationID)
    if (!conversation) return
    const messages = conversation.messages
    const assistantIndex = messages.findIndex((message) => message.id === assistantMessageID)
    if (assistantIndex < 0) return
    let userIndex = -1
    for (let index = assistantIndex - 1; index >= 0; index -= 1) {
      if (messages[index].role === 'user') {
        userIndex = index
        break
      }
    }
    if (userIndex < 0) return
    const userMessage = messages[userIndex]
    const assistantMessage = messages[assistantIndex]
    const retryRunOptions = retryRunOptionsFor(assistantMessage)
    void resendFromUserMessage(
      userMessage.id,
      userMessage.content,
      true,
      retryRunOptions,
      conversationID,
      Boolean(retryRunOptions),
    )
  }

  function retryRunOptionsFor(assistantMessage: ChatMessage): LocalHarnessRunOptions | undefined {
    const failure = latestRunFailureEvent(assistantMessage)
    if (!failure) return undefined
    const attempt = nextRetryAttempt(assistantMessage)
    const retryAction = t('agent.retryAttemptLabel', { attempt })
    return {
      parentRunId: assistantMessage.runId,
      metadata: {
        intent: 'retry',
        source_run_id: assistantMessage.runId,
        source_message_id: assistantMessage.id,
        attempt,
        failure_category: failure.failureCategory,
        failure_action_kind: failure.failureActionKind,
      },
      initialAgentEvents: [{
        type: 'ui.action.requested',
        label: t('agent.uiActionRequestedLabel', { action: retryAction }),
        retryAttempt: attempt,
        retrySourceRunId: assistantMessage.runId,
        retrySourceMessageId: assistantMessage.id,
      }],
    }
  }

  async function repairRecoveryTarget(target: RecoveryTarget) {
    if (!beginRecoveryAction(recoveryState, 'repair', target)) {
      setNotice(t('app.notice.recoveryRetryAlreadyRunning'))
      return
    }
    try {
      const conversation = await localData.get(target.conversationID)
      if (!conversation) return
      const messages = conversation.messages
      const assistantIndex = messages.findIndex(
        (message) => message.id === target.assistantMessageID,
      )
      if (assistantIndex < 0) return
      let userIndex = -1
      for (let index = assistantIndex - 1; index >= 0; index -= 1) {
        if (messages[index].role === 'user') {
          userIndex = index
          break
        }
      }
      if (userIndex < 0) return
      const assistantMessage = messages[assistantIndex]
      const userMessage = messages[userIndex]
      const failure = latestRunFailureEvent(assistantMessage)
      const attempt = nextRepairAttempt(assistantMessage)
      const repairAction = t('agent.repairAttemptLabel', { attempt })
      const initialAgentEvents: AgentTimelineItem[] = [{
        type: 'ui.action.requested',
        label: t('agent.uiActionRequestedLabel', { action: repairAction }),
        repairAttempt: attempt,
        repairSourceRunId: assistantMessage.runId,
        repairSourceMessageId: assistantMessage.id,
      }]
      await resendFromUserMessage(
        userMessage.id,
        userMessage.content,
        true,
        {
          parentRunId: assistantMessage.runId,
          metadata: {
            intent: 'repair',
            source_run_id: assistantMessage.runId,
            source_message_id: assistantMessage.id,
            attempt,
            failure_category: failure?.failureCategory,
            failure_action_kind: failure?.failureActionKind,
          },
          initialAgentEvents,
        },
        target.conversationID,
        true,
      )
    } finally {
      endRecoveryAction(recoveryState, 'repair', target)
    }
  }

  return {
    handleRegenerateMessage,
    recoveryTargetFor,
    repairRecoveryTarget,
    retryRecoveryTarget,
  }
}
