import { useCallback, useMemo } from 'react'
import { toast } from 'sonner'
import { conversationForQuestionAnswer } from '@/features/chat/pendingQuestion'
import type { Conversation } from '@/shared/local-data/types'
import {
  type LocalPermissionScope,
  type LocalPlanApprovalDecision,
  type LocalToolReconciliationDecision,
  type PendingPermissionResolveCommand,
  type PendingPlanResolveCommand,
  type PendingQuestionAnswerCommand,
  type PendingToolReconcileCommand,
} from '@/runtime/client'
import { cloneConversation } from '../conversationState'
import { conversationStore } from '../state/conversationStore'
import { runtimeStore } from '../state/runtimeStore'
import { useStore } from '../state/store'
import {
  executeDecisionCommand,
  type RunDecisionCommandContext,
} from './decisionCommandExecution'

export type { RunDecisionCommandContext } from './decisionCommandExecution'

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
  const executionContext = useMemo<RunDecisionCommandContext>(
    () => ({
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
    }),
    [
      createConversationRenderContext,
      finalizeLocalRunStatus,
      localData,
      openLocalDocument,
      refreshConversationsAfterStream,
      scheduleConversationRender,
      setNotice,
      streamLocalMessage,
      suppressRuntimeCommandFailureNotice,
      t,
    ],
  )
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
      const runID = message.runId

      setNotice('')
      const contentBeforeDecision = message.content
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
      return executeDecisionCommand({
        context: executionContext,
        runtimeConnection,
        conversation,
        message,
        runId: runID,
        renderContext,
        contentBeforeDecision,
        waitingStatus: 'waiting_permission',
        loadCommand: async () => {
          const existing = (await localData.listPendingRuntimeCommands()).find(
            (command): command is PendingPermissionResolveCommand =>
              command.type === 'permission.resolve' && command.input.permissionId === requestID,
          )
          const command: PendingPermissionResolveCommand = existing ?? {
            type: 'permission.resolve' as const,
            commandId: `resolve_${requestID}`,
            createdAt: new Date().toISOString(),
            input: {
              permissionId: requestID,
              decision,
              scope,
              editedAction,
              runId: runID,
              threadId: conversation.id,
            },
          }
          if (!existing) await localData.savePendingRuntimeCommand(command)
          return command
        },
        onAccepted: (command) => toast.success(
          command.input.decision === 'approve' || command.input.decision === 'edit'
            ? t(
                command.input.scope === 'run'
                  ? 'app.notice.permissionRunApproved'
                  : 'app.notice.permissionApproved',
              )
            : t('app.notice.permissionDenied'),
          { id: 'permission-decision', duration: 2000 },
        ),
        onRejected: () => {
          message.agentEvents = (message.agentEvents ?? []).filter(
            (event) =>
              !(
                event.type === 'ui.permission_decision_pending' &&
                event.permissionRequestId === requestID
              ),
          )
        },
      })
    },
    [
      executionContext,
      conversations,
      createConversationRenderContext,
      localData,
      runtimeConnection,
      scheduleConversationRender,
      setNotice,
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
      const runID = message.runId
      setNotice('')
      const contentBeforeDecision = message.content
      message.status = 'streaming'
      const renderContext = createConversationRenderContext()
      await executeDecisionCommand({
        context: executionContext,
        runtimeConnection,
        conversation,
        message,
        runId: runID,
        renderContext,
        contentBeforeDecision,
        waitingStatus: 'waiting_permission',
        loadCommand: async () => {
          const existing = (await localData.listPendingRuntimeCommands()).find(
            (command): command is PendingToolReconcileCommand =>
              command.type === 'tool.reconcile' && command.input.operationId === requestID,
          )
          const command: PendingToolReconcileCommand = existing ?? {
            type: 'tool.reconcile' as const,
            commandId: `reconcile_${requestID}`,
            createdAt: new Date().toISOString(),
            input: {
              operationId: requestID,
              decision,
              runId: runID,
              threadId: conversation.id,
            },
          }
          if (!existing) await localData.savePendingRuntimeCommand(command)
          return command
        },
      })
    },
    [
      activeID,
      executionContext,
      createConversationRenderContext,
      localData,
      runtimeConnection,
      setNotice,
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
      const runID = message.runId

      setNotice('')
      const contentBeforeAnswer = message.content
      message.status = 'streaming'
      const renderContext = createConversationRenderContext()
      await executeDecisionCommand({
        context: executionContext,
        runtimeConnection,
        conversation,
        message,
        runId: runID,
        renderContext,
        contentBeforeDecision: contentBeforeAnswer,
        waitingStatus: 'waiting_input',
        loadCommand: async () => {
          const existing = (await localData.listPendingRuntimeCommands()).find(
            (command): command is PendingQuestionAnswerCommand =>
              command.type === 'question.answer' && command.input.questionId === requestID,
          )
          const command: PendingQuestionAnswerCommand = existing ?? {
            type: 'question.answer' as const,
            commandId: `answer_${requestID}`,
            createdAt: new Date().toISOString(),
            input: {
              questionId: requestID,
              answers,
              runId: runID,
              threadId: conversation.id,
            },
          }
          if (!existing) await localData.savePendingRuntimeCommand(command)
          return command
        },
      })
    },
    [
      executionContext,
      conversations,
      createConversationRenderContext,
      localData,
      runtimeConnection,
      setNotice,
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
      const runID = message.runId

      setNotice('')
      const contentBeforeDecision = message.content
      message.status = 'streaming'
      const renderContext = createConversationRenderContext()
      await executeDecisionCommand({
        context: executionContext,
        runtimeConnection,
        conversation,
        message,
        runId: runID,
        renderContext,
        contentBeforeDecision,
        waitingStatus: 'waiting_input',
        loadCommand: async () => {
          const existing = (await localData.listPendingRuntimeCommands()).find(
            (command): command is PendingPlanResolveCommand =>
              command.type === 'plan.resolve' && command.input.approvalId === requestID,
          )
          const command: PendingPlanResolveCommand = existing ?? {
            type: 'plan.resolve' as const,
            commandId: `resolve_plan_${requestID}`,
            createdAt: new Date().toISOString(),
            input: {
              approvalId: requestID,
              decision,
              instructions: instructions?.trim() || undefined,
              runId: runID,
              threadId: conversation.id,
            },
          }
          if (!existing) await localData.savePendingRuntimeCommand(command)
          return command
        },
        onAccepted: (command) => {
          const noticeKey =
            command.input.decision === 'approve'
              ? 'app.notice.planApproved'
              : command.input.decision === 'modify'
                ? 'app.notice.planModified'
                : 'app.notice.planRejected'
          toast.success(t(noticeKey), { id: 'plan-approval-decision', duration: 2000 })
        },
      })
    },
    [
      activeID,
      executionContext,
      createConversationRenderContext,
      localData,
      runtimeConnection,
      setNotice,
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
