import { useCallback } from 'react'
import { toast } from 'sonner'
import { conversationForQuestionAnswer } from '@/features/chat/pendingQuestion'
import type { Translator } from '@/shared/i18n/i18n'
import type { LocalConversationStore } from '@/shared/local-data/localConversations'
import type { ChatMessage, Conversation, LocalFileRef } from '@/shared/local-data/types'
import {
  answerLocalQuestionCommand,
  reconcileLocalToolCommand,
  resolveLocalPermissionCommand,
  resolveLocalPlanCommand,
  type LocalPermissionScope,
  type LocalPlanApprovalDecision,
  type LocalToolReconciliationDecision,
  type PendingPermissionResolveCommand,
  type PendingPlanResolveCommand,
  type PendingQuestionAnswerCommand,
  type PendingToolReconcileCommand,
} from '@/runtime/client'
import { cloneConversation } from './conversationState'
import { runtimeCommandErrorMessage } from './runtimeCommandError'
import { conversationStore } from './state/conversationStore'
import { runtimeStore } from './state/runtimeStore'
import { useStore } from './state/store'
import { workspaceStoreActions } from './state/workspaceStore'
import type { ConversationRenderContext } from './useConversationProject'

type NoticeOptions = Omit<NonNullable<Parameters<typeof toast.message>[1]>, 'id'>
type RuntimeLocalMessageStreamer = typeof import('./runStreaming').streamLocalMessage

export interface RunDecisionCommandContext {
  localData: LocalConversationStore
  t: Translator
  setNotice: (message: string, options?: NoticeOptions) => void
  createConversationRenderContext: () => ConversationRenderContext
  refreshConversationsAfterStream: (
    conversationID: string,
    context: ConversationRenderContext,
  ) => Promise<void>
  scheduleConversationRender: (
    conversation: Conversation,
    context: ConversationRenderContext,
  ) => void
  suppressRuntimeCommandFailureNotice: (commandId: string, message: string) => void
  openLocalDocument: (ref: LocalFileRef) => void
  streamLocalMessage: RuntimeLocalMessageStreamer
  finalizeLocalRunStatus: (message: ChatMessage) => void
}

export interface RunDecisionCommandHandlers {
  handlePermissionDecisionOnce: (
    messageID: string,
    requestID: string,
    decision: 'approve' | 'edit' | 'deny',
    scope: LocalPermissionScope,
    editedAction?: { name: string; args: Record<string, unknown> },
  ) => Promise<boolean>
  handleToolReconciliationOnce: (
    messageID: string,
    requestID: string,
    decision: LocalToolReconciliationDecision,
  ) => Promise<void>
  handleQuestionAnswerOnce: (
    messageID: string,
    requestID: string,
    answers: Record<string, string[]>,
  ) => Promise<void>
  handlePlanApprovalDecisionOnce: (
    messageID: string,
    requestID: string,
    decision: LocalPlanApprovalDecision,
    instructions?: string,
  ) => Promise<void>
}

export function useRunDecisionCommands(
  context: RunDecisionCommandContext,
): RunDecisionCommandHandlers {
  const {
    localData,
    t,
    setNotice,
    createConversationRenderContext,
    refreshConversationsAfterStream,
    scheduleConversationRender,
    suppressRuntimeCommandFailureNotice,
    openLocalDocument,
    streamLocalMessage,
    finalizeLocalRunStatus,
  } = context
  const { connection: runtimeConnection } = useStore(runtimeStore)
  const { conversations, activeID } = useStore(conversationStore)

  const handlePermissionDecisionOnce = useCallback(
    async (
      messageID: string,
      requestID: string,
      decision: 'approve' | 'edit' | 'deny',
      scope: LocalPermissionScope,
      editedAction?: { name: string; args: Record<string, unknown> },
    ): Promise<boolean> => {
      const conversationID = conversationStore.getState().activeID
      if (!conversationID || !runtimeConnection) {
        setNotice(t('app.notice.runtimeDisconnected'))
        return false
      }
      const persistedConversation = await localData.get(conversationID)
      const visibleConversation = conversations.find((item) => item.id === conversationID)
      const findPermissionMessage = (conversation: Conversation | undefined) =>
        conversation?.messages.find((item) => item.id === messageID) ??
        conversation?.messages.find((item) =>
          item.agentEvents?.some((event) => event.permissionRequestId === requestID),
        )
      const sourceConversation = [persistedConversation, visibleConversation].find((candidate) =>
        Boolean(findPermissionMessage(candidate)?.runId),
      )
      const conversation = sourceConversation ? cloneConversation(sourceConversation) : undefined
      const message = findPermissionMessage(conversation)
      if (!conversation || !message?.runId) {
        setNotice(t('app.notice.missingLocalTask'))
        return false
      }

      setNotice('')
      const contentBeforeDecision = message.content
      let commandAccepted = false
      message.status = 'streaming'
      const renderContext = createConversationRenderContext()
      message.agentEvents = [
        ...(message.agentEvents ?? []),
        {
          type: 'ui.permission_decision_pending',
          label: t(
            decision === 'deny'
              ? 'chat.timeline.permissionDenied'
              : scope === 'run'
                ? 'chat.timeline.permissionApprovedRun'
                : 'chat.timeline.permissionApprovedOnce',
          ),
          permissionRequestId: requestID,
        },
      ]
      scheduleConversationRender(conversation, renderContext)
      let command: PendingPermissionResolveCommand | undefined
      try {
        const existing = (await localData.listPendingRuntimeCommands()).find(
          (command): command is PendingPermissionResolveCommand =>
            command.type === 'permission.resolve' && command.input.permissionId === requestID,
        )
        command = existing ?? {
          type: 'permission.resolve' as const,
          commandId: `resolve_${requestID}`,
          createdAt: new Date().toISOString(),
          input: {
            permissionId: requestID,
            decision,
            scope,
            editedAction,
            runId: message.runId,
            threadId: conversation.id,
          },
        }
        if (!existing) await localData.savePendingRuntimeCommand(command)
        try {
          await resolveLocalPermissionCommand(
            command.commandId,
            command.input.permissionId,
            command.input.decision,
            { scope: command.input.scope, editedAction: command.input.editedAction },
            runtimeConnection,
          )
          commandAccepted = true
          await localData.deletePendingRuntimeCommand(command.commandId)
        } catch (error) {
          workspaceStoreActions.setPendingCommandDeliveryVersion((version) => version + 1)
          throw error
        }
        toast.success(
          command.input.decision === 'approve' || command.input.decision === 'edit'
            ? t(
                command.input.scope === 'run'
                  ? 'app.notice.permissionRunApproved'
                  : 'app.notice.permissionApproved',
              )
            : t('app.notice.permissionDenied'),
          { id: 'permission-decision', duration: 2000 },
        )
        await streamLocalMessage(
          message.runId,
          runtimeConnection,
          conversation,
          message,
          t,
          openLocalDocument,
          () => scheduleConversationRender(conversation, renderContext),
        )
        finalizeLocalRunStatus(message)
        scheduleConversationRender(conversation, renderContext)
      } catch (error) {
        message.status = commandAccepted ? 'streaming' : 'waiting_permission'
        if (!commandAccepted) {
          message.content = contentBeforeDecision
          message.agentEvents = (message.agentEvents ?? []).filter(
            (event) =>
              !(
                event.type === 'ui.permission_decision_pending' &&
                event.permissionRequestId === requestID
              ),
          )
        }
        const commandErrorMessage = runtimeCommandErrorMessage(error, t)
        if (command?.commandId) {
          suppressRuntimeCommandFailureNotice(command.commandId, commandErrorMessage)
        }
        setNotice(commandErrorMessage)
        scheduleConversationRender(conversation, renderContext)
      } finally {
        conversation.updatedAt = new Date().toISOString()
        await localData.save(conversation)
        await refreshConversationsAfterStream(conversation.id, renderContext)
      }
      return commandAccepted
    },
    [
      conversations,
      createConversationRenderContext,
      finalizeLocalRunStatus,
      localData,
      openLocalDocument,
      refreshConversationsAfterStream,
      runtimeConnection,
      scheduleConversationRender,
      setNotice,
      streamLocalMessage,
      suppressRuntimeCommandFailureNotice,
      t,
    ],
  )

  const handleToolReconciliationOnce = useCallback(
    async (
      messageID: string,
      requestID: string,
      decision: LocalToolReconciliationDecision,
    ) => {
      if (!activeID || !runtimeConnection) {
        setNotice(t('app.notice.runtimeDisconnected'))
        return
      }
      const conversation = await localData.get(activeID)
      const message = conversation?.messages.find((item) => item.id === messageID)
      if (!conversation || !message?.runId) {
        setNotice(t('app.notice.missingLocalTask'))
        return
      }
      setNotice('')
      const contentBeforeDecision = message.content
      let commandAccepted = false
      message.status = 'streaming'
      const renderContext = createConversationRenderContext()
      let command: PendingToolReconcileCommand | undefined
      try {
        const existing = (await localData.listPendingRuntimeCommands()).find(
          (command): command is PendingToolReconcileCommand =>
            command.type === 'tool.reconcile' && command.input.operationId === requestID,
        )
        command = existing ?? {
          type: 'tool.reconcile' as const,
          commandId: `reconcile_${requestID}`,
          createdAt: new Date().toISOString(),
          input: {
            operationId: requestID,
            decision,
            runId: message.runId,
            threadId: conversation.id,
          },
        }
        if (!existing) await localData.savePendingRuntimeCommand(command)
        try {
          await reconcileLocalToolCommand(
            command.commandId,
            command.input.operationId,
            command.input.decision,
            runtimeConnection,
          )
          commandAccepted = true
          await localData.deletePendingRuntimeCommand(command.commandId)
        } catch (error) {
          workspaceStoreActions.setPendingCommandDeliveryVersion((version) => version + 1)
          throw error
        }
        await streamLocalMessage(
          message.runId,
          runtimeConnection,
          conversation,
          message,
          t,
          openLocalDocument,
          () => scheduleConversationRender(conversation, renderContext),
        )
        finalizeLocalRunStatus(message)
        scheduleConversationRender(conversation, renderContext)
      } catch (error) {
        message.status = commandAccepted ? 'streaming' : 'waiting_permission'
        if (!commandAccepted) message.content = contentBeforeDecision
        const commandErrorMessage = runtimeCommandErrorMessage(error, t)
        if (command?.commandId) {
          suppressRuntimeCommandFailureNotice(command.commandId, commandErrorMessage)
        }
        setNotice(commandErrorMessage)
        scheduleConversationRender(conversation, renderContext)
      } finally {
        conversation.updatedAt = new Date().toISOString()
        await localData.save(conversation)
        await refreshConversationsAfterStream(conversation.id, renderContext)
      }
    },
    [
      activeID,
      createConversationRenderContext,
      finalizeLocalRunStatus,
      localData,
      openLocalDocument,
      refreshConversationsAfterStream,
      runtimeConnection,
      scheduleConversationRender,
      setNotice,
      streamLocalMessage,
      suppressRuntimeCommandFailureNotice,
      t,
    ],
  )

  const handleQuestionAnswerOnce = useCallback(
    async (messageID: string, requestID: string, answers: Record<string, string[]>) => {
      const conversationID = conversationStore.getState().activeID
      if (!conversationID) {
        setNotice(t('app.notice.missingLocalTask'))
        return
      }
      const persistedConversation = await localData.get(conversationID)
      const visibleConversation = conversations.find((item) => item.id === conversationID)
      const selectedConversation = conversationForQuestionAnswer(
        persistedConversation,
        visibleConversation,
        messageID,
      )
      const conversation =
        selectedConversation === visibleConversation && visibleConversation
          ? cloneConversation(visibleConversation)
          : selectedConversation
      const message = conversation?.messages.find((item) => item.id === messageID)
      if (!runtimeConnection) {
        setNotice(t('app.notice.runtimeDisconnected'))
        return
      }
      if (!conversation || !message?.runId) {
        setNotice(t('app.notice.missingLocalTask'))
        return
      }

      setNotice('')
      const contentBeforeAnswer = message.content
      let commandAccepted = false
      message.status = 'streaming'
      const renderContext = createConversationRenderContext()
      let command: PendingQuestionAnswerCommand | undefined
      try {
        const existing = (await localData.listPendingRuntimeCommands()).find(
          (command): command is PendingQuestionAnswerCommand =>
            command.type === 'question.answer' && command.input.questionId === requestID,
        )
        command = existing ?? {
          type: 'question.answer' as const,
          commandId: `answer_${requestID}`,
          createdAt: new Date().toISOString(),
          input: {
            questionId: requestID,
            answers,
            runId: message.runId,
            threadId: conversation.id,
          },
        }
        if (!existing) await localData.savePendingRuntimeCommand(command)
        try {
          await answerLocalQuestionCommand(
            command.commandId,
            command.input.questionId,
            command.input.answers,
            runtimeConnection,
          )
          commandAccepted = true
          await localData.deletePendingRuntimeCommand(command.commandId)
        } catch (error) {
          workspaceStoreActions.setPendingCommandDeliveryVersion((version) => version + 1)
          throw error
        }
        await streamLocalMessage(
          message.runId,
          runtimeConnection,
          conversation,
          message,
          t,
          openLocalDocument,
          () => scheduleConversationRender(conversation, renderContext),
        )
        finalizeLocalRunStatus(message)
        scheduleConversationRender(conversation, renderContext)
      } catch (error) {
        message.status = commandAccepted ? 'streaming' : 'waiting_input'
        if (!commandAccepted) message.content = contentBeforeAnswer
        const commandErrorMessage = runtimeCommandErrorMessage(error, t)
        if (command?.commandId) {
          suppressRuntimeCommandFailureNotice(command.commandId, commandErrorMessage)
        }
        setNotice(commandErrorMessage)
        scheduleConversationRender(conversation, renderContext)
      } finally {
        conversation.updatedAt = new Date().toISOString()
        await localData.save(conversation)
        await refreshConversationsAfterStream(conversation.id, renderContext)
      }
    },
    [
      conversations,
      createConversationRenderContext,
      finalizeLocalRunStatus,
      localData,
      openLocalDocument,
      refreshConversationsAfterStream,
      runtimeConnection,
      scheduleConversationRender,
      setNotice,
      streamLocalMessage,
      suppressRuntimeCommandFailureNotice,
      t,
    ],
  )

  const handlePlanApprovalDecisionOnce = useCallback(
    async (
      messageID: string,
      requestID: string,
      decision: LocalPlanApprovalDecision,
      instructions?: string,
    ) => {
      if (!activeID || !runtimeConnection) {
        setNotice(t('app.notice.runtimeDisconnected'))
        return
      }
      const conversation = await localData.get(activeID)
      const message = conversation?.messages.find((item) => item.id === messageID)
      if (!conversation || !message?.runId) {
        setNotice(t('app.notice.missingLocalTask'))
        return
      }

      setNotice('')
      const contentBeforeDecision = message.content
      let commandAccepted = false
      message.status = 'streaming'
      const renderContext = createConversationRenderContext()
      let command: PendingPlanResolveCommand | undefined
      try {
        const existing = (await localData.listPendingRuntimeCommands()).find(
          (command): command is PendingPlanResolveCommand =>
            command.type === 'plan.resolve' && command.input.approvalId === requestID,
        )
        command = existing ?? {
          type: 'plan.resolve' as const,
          commandId: `resolve_plan_${requestID}`,
          createdAt: new Date().toISOString(),
          input: {
            approvalId: requestID,
            decision,
            instructions: instructions?.trim() || undefined,
            runId: message.runId,
            threadId: conversation.id,
          },
        }
        if (!existing) await localData.savePendingRuntimeCommand(command)
        try {
          await resolveLocalPlanCommand(
            command.commandId,
            command.input.approvalId,
            command.input.decision,
            command.input.instructions,
            runtimeConnection,
          )
          commandAccepted = true
          await localData.deletePendingRuntimeCommand(command.commandId)
        } catch (error) {
          workspaceStoreActions.setPendingCommandDeliveryVersion((version) => version + 1)
          throw error
        }
        const noticeKey =
          command.input.decision === 'approve'
            ? 'app.notice.planApproved'
            : command.input.decision === 'modify'
              ? 'app.notice.planModified'
              : 'app.notice.planRejected'
        toast.success(t(noticeKey), { id: 'plan-approval-decision', duration: 2000 })
        await streamLocalMessage(
          message.runId,
          runtimeConnection,
          conversation,
          message,
          t,
          openLocalDocument,
          () => scheduleConversationRender(conversation, renderContext),
        )
        finalizeLocalRunStatus(message)
        scheduleConversationRender(conversation, renderContext)
      } catch (error) {
        message.status = commandAccepted ? 'streaming' : 'waiting_input'
        if (!commandAccepted) message.content = contentBeforeDecision
        const commandErrorMessage = runtimeCommandErrorMessage(error, t)
        if (command?.commandId) {
          suppressRuntimeCommandFailureNotice(command.commandId, commandErrorMessage)
        }
        setNotice(commandErrorMessage)
        scheduleConversationRender(conversation, renderContext)
      } finally {
        conversation.updatedAt = new Date().toISOString()
        await localData.save(conversation)
        await refreshConversationsAfterStream(conversation.id, renderContext)
      }
    },
    [
      activeID,
      createConversationRenderContext,
      finalizeLocalRunStatus,
      localData,
      openLocalDocument,
      refreshConversationsAfterStream,
      runtimeConnection,
      scheduleConversationRender,
      setNotice,
      streamLocalMessage,
      suppressRuntimeCommandFailureNotice,
      t,
    ],
  )

  return {
    handlePermissionDecisionOnce,
    handleToolReconciliationOnce,
    handleQuestionAnswerOnce,
    handlePlanApprovalDecisionOnce,
  }
}
