import { useCallback, type MutableRefObject, type Dispatch, type SetStateAction } from 'react'
import { toast } from 'sonner'
import { createLocalID } from '@/shared/local-data/localConversations'
import type { Translator } from '@/shared/i18n/i18n'
import { mergeAttachments } from './conversationState'
import { runtimeCommandErrorMessage } from './runtimeCommandError'
import { conversationStore } from './state/conversationStore'
import { runtimeStore } from './state/runtimeStore'
import { workspaceStoreActions } from './state/workspaceStore'
import { useStore } from './state/store'
import type {
  ChatMode,
  Conversation,
  LocalAttachmentRef,
} from '@/shared/local-data/types'
import type {
  AgentSettings,
  PendingRuntimeCommand,
  PendingRuntimeCommandFailure,
  PendingRunCancelCommand,
  PendingRunInjectCommand,
  RuntimeCommandResult,
  RuntimeConnection,
} from '@/runtime/client'
import {
  cancelLocalRunCommand,
  hasRuntimeAuthorization,
  injectLocalRunInstruction,
  parseRuntimeModelSpec,
} from '@/runtime/client'
import type { ConversationRenderContext } from './useConversationProject'
import type { LocalHarnessRunOptions } from './runExecution'
import {
  useRunDecisionCommands,
  type RunDecisionCommandContext,
  type RunDecisionCommandHandlers,
} from './useRunDecisionCommands'
import {
  useRuntimeCommandSettlement,
  type PendingPluginCommand,
  type ProjectRuntimeThreadCache,
} from './useRuntimeCommandSettlement'

interface RuntimeCommandContext extends RunDecisionCommandContext {
  draft: string
  pendingAttachments: LocalAttachmentRef[]
  mode: ChatMode
  agentSettings: Required<AgentSettings>
  navigationVersionRef: MutableRefObject<number>
  runtimeThreadIDsRef: MutableRefObject<Set<string>>
  setDraft: Dispatch<SetStateAction<string>>
  setPluginCatalogVersion: Dispatch<SetStateAction<number>>
  setActiveConversationID: (nextActiveID: string | undefined) => void
  setMainView: (view: 'chat' | 'plugins' | 'settings') => void
  beginVisibleSend: () => number
  finishVisibleSend: (operation: number) => void
  detachVisibleSend: () => void
  syncRuntimeThreadCache: (config: RuntimeConnection) => Promise<Conversation[]>
  refreshConversations: (nextActiveID?: string, options?: { preserveEmptyActive?: boolean }) => Promise<void>
  sendLocalHarnessMessage: (
    content: string,
    context: ConversationRenderContext,
    settingsOverride?: Required<AgentSettings>,
    runOptions?: LocalHarnessRunOptions,
    targetConversationID?: string,
    attachments?: LocalAttachmentRef[],
  ) => Promise<Conversation>
  consumeRuntimeCommandFailureNotice: (commandId: string, message: string) => boolean
  clearRuntimeCommandFailureNotice: (commandId: string) => void
  storeRuntimeThreadIDs: (ids: Set<string>) => void
  projectRuntimeThreadCache: ProjectRuntimeThreadCache
}

export interface RuntimeCommandHandlers extends RunDecisionCommandHandlers {
  sendMessage: () => Promise<void>
  resendFromUserMessage: (
    userMessageID: string,
    text: string,
    preferLocal: boolean,
    localRunOptions?: LocalHarnessRunOptions,
    targetConversationID?: string,
    preservePriorTurns?: boolean,
  ) => Promise<void>
  cancelActiveRun: () => Promise<void>
  appendInstructionToActiveRun: () => Promise<void>
  submitPluginCommand: (command: PendingPluginCommand) => Promise<RuntimeCommandResult>
  settleDeliveredLocalRunCommand: (
    command: PendingRuntimeCommand,
    result: RuntimeCommandResult,
    config: RuntimeConnection,
  ) => Promise<boolean>
  settleRejectedPendingRuntimeCommand: (
    failure: PendingRuntimeCommandFailure,
    config: RuntimeConnection,
  ) => Promise<void>
}

export function useRuntimeCommands(context: RuntimeCommandContext): RuntimeCommandHandlers {
  const {
    localData,
    draft,
    pendingAttachments,
    mode,
    agentSettings,
    t,
    navigationVersionRef,
    runtimeThreadIDsRef,
    setNotice,
    setDraft,
    setPluginCatalogVersion,
    setActiveConversationID,
    setMainView,
    beginVisibleSend,
    finishVisibleSend,
    detachVisibleSend,
    createConversationRenderContext,
    syncRuntimeThreadCache,
    refreshConversations,
    refreshConversationsAfterStream,
    sendLocalHarnessMessage,
    consumeRuntimeCommandFailureNotice,
    suppressRuntimeCommandFailureNotice,
    clearRuntimeCommandFailureNotice,
    storeRuntimeThreadIDs,
    projectRuntimeThreadCache,
  } = context
  const { runtime, connection: runtimeConnection, models } = useStore(runtimeStore)
  const { conversations, activeID } = useStore(conversationStore)
  const activeConversation = conversations.find((conversation) => conversation.id === activeID)
  const decisionHandlers = useRunDecisionCommands(context)
  const {
    settleDeliveredLocalRunCommand,
    settleRejectedPendingRuntimeCommand,
    submitPluginCommand,
  } = useRuntimeCommandSettlement({
    localData,
    projectRuntimeThreadCache,
    runtimeConnection,
    runtimeThreadIDsRef,
    setPluginCatalogVersion,
    storeRuntimeThreadIDs,
    syncRuntimeThreadCache,
    suppressRuntimeCommandFailureNotice,
    clearRuntimeCommandFailureNotice,
    t,
  })

  const sendMessage = useCallback(async () => {
    const content = draft
    const attachments = pendingAttachments
    const sendingOperation = beginVisibleSend()
    setNotice('')
    setDraft('')
    workspaceStoreActions.setPendingAttachments([])
    const renderContext = createConversationRenderContext()
    try {
      if (!runtime?.online || !hasRuntimeAuthorization(runtimeConnection)) {
        throw new Error(t('app.notice.runtimeDisconnected'))
      }
      const selectedMode = parseRuntimeModelSpec(mode)
      if (!selectedMode || !models.some((model) => model.id === selectedMode)) {
        throw new Error(t('app.notice.localModelUnavailable'))
      }
      const conversation = await sendLocalHarnessMessage(
        content,
        renderContext,
        undefined,
        undefined,
        conversationStore.getState().activeID,
        attachments,
      )
      await refreshConversationsAfterStream(conversation.id, renderContext)
    } catch (error) {
      setDraft((current) => current || content)
      if (navigationVersionRef.current === renderContext.navigationVersionAtStart) {
        workspaceStoreActions.setPendingAttachments((current) => mergeAttachments(current, attachments))
      }
      setNotice(error instanceof Error ? error.message : t('app.notice.sendFailed'))
      const userNavigatedWhileStreaming = navigationVersionRef.current !== renderContext.navigationVersionAtStart
      await refreshConversations(userNavigatedWhileStreaming ? conversationStore.getState().activeID : activeID, {
        preserveEmptyActive: userNavigatedWhileStreaming && !conversationStore.getState().activeID,
      })
    } finally {
      finishVisibleSend(sendingOperation)
    }
  }, [
    activeID,
    conversationStore,
    beginVisibleSend,
    draft,
    finishVisibleSend,
    models,
    navigationVersionRef,
    pendingAttachments,
    refreshConversations,
    refreshConversationsAfterStream,
    runtime,
    runtimeConnection,
    sendLocalHarnessMessage,
    setDraft,
    setNotice,
    t,
  ])

  const resendFromUserMessage = useCallback(async (
    userMessageID: string,
    text: string,
    preferLocal: boolean,
    localRunOptions?: LocalHarnessRunOptions,
    targetConversationID = conversationStore.getState().activeID,
    preservePriorTurns = false,
  ) => {
    if (!targetConversationID) {
      return
    }
    const conversation = await localData.get(targetConversationID)
    if (!conversation) {
      return
    }
    const index = conversation.messages.findIndex((message) => message.id === userMessageID)
    if (index < 0) {
      return
    }
    const sourceMessage = conversation.messages[index]
    const attachments = sourceMessage.attachments ?? []
    if (!preservePriorTurns) {
      conversation.messages = conversation.messages.slice(0, index)
      conversation.updatedAt = new Date().toISOString()
      await localData.save(conversation)
      if (conversationStore.getState().activeID !== targetConversationID) {
        detachVisibleSend()
        navigationVersionRef.current += 1
        workspaceStoreActions.setPendingWorkspace(undefined)
        workspaceStoreActions.setPendingProject(undefined)
        setActiveConversationID(targetConversationID)
        setMainView('chat')
      }
      await refreshConversations(targetConversationID)
    }

    const renderContext = createConversationRenderContext()
    const sendingOperation = beginVisibleSend()
    setNotice('')
    try {
      const next = await sendLocalHarnessMessage(
        text,
        renderContext,
        agentSettings,
        {
          ...localRunOptions,
          replaceFromClientId: preservePriorTurns ? undefined : userMessageID,
          hideUserMessage: preservePriorTurns,
          ...(sourceMessage.pluginReferences
            ? { pluginReferences: sourceMessage.pluginReferences }
            : {}),
          ...(sourceMessage.pluginCommand ? { pluginCommand: sourceMessage.pluginCommand } : {}),
        },
        targetConversationID,
        attachments,
      )
      await refreshConversationsAfterStream(next.id, renderContext)
    } catch (error) {
      setNotice(error instanceof Error ? error.message : t('app.notice.sendFailed'))
      await refreshConversations(targetConversationID)
    } finally {
      finishVisibleSend(sendingOperation)
    }
  }, [
    conversationStore,
    agentSettings,
    beginVisibleSend,
    detachVisibleSend,
    finishVisibleSend,
    localData,
    navigationVersionRef,
    refreshConversations,
    refreshConversationsAfterStream,
    sendLocalHarnessMessage,
    setActiveConversationID,
    setMainView,
    setNotice,
  ])

  const cancelActiveRun = useCallback(async () => {
    if (!activeConversation) {
      return
    }
    const activeMessage = [...activeConversation.messages]
      .reverse()
      .find(
        (msg) =>
          msg.role === 'assistant' &&
          Boolean(msg.runId) &&
          (msg.status === 'streaming' || msg.status === 'waiting_permission' || msg.status === 'waiting_input'),
      )
    if (!activeMessage?.runId) {
      return
    }
    let command: PendingRunCancelCommand | undefined
    try {
      if (!runtimeConnection) {
        return
      }
      const existing = (await localData.listPendingRuntimeCommands()).find(
        (command): command is PendingRunCancelCommand =>
          command.type === 'run.cancel' && command.input.runId === activeMessage.runId,
      )
      command = existing ?? {
        type: 'run.cancel' as const,
        commandId: `cancel_${activeMessage.runId}`,
        createdAt: new Date().toISOString(),
        input: { runId: activeMessage.runId, threadId: activeConversation.id },
      }
      if (!existing) await localData.savePendingRuntimeCommand(command)
      try {
        const result = await cancelLocalRunCommand(command.commandId, command.input.runId, runtimeConnection)
        await settleDeliveredLocalRunCommand(command, result, runtimeConnection)
      } catch (error) {
        workspaceStoreActions.setPendingCommandDeliveryVersion((version) => version + 1)
        throw error
      }
    } catch (error) {
      const commandErrorMessage = runtimeCommandErrorMessage(error, t)
      if (command?.commandId) {
        suppressRuntimeCommandFailureNotice(command.commandId, commandErrorMessage)
      }
      setNotice(commandErrorMessage)
    }
  }, [
    activeConversation,
    localData,
    runtimeConnection,
    setNotice,
    settleDeliveredLocalRunCommand,
    suppressRuntimeCommandFailureNotice,
    t,
  ])

  const appendInstructionToActiveRun = useCallback(async () => {
    const content = draft.trim()
    if (!content) {
      setNotice(t('app.notice.emptyMessage'))
      return
    }
    if (!activeConversation || !runtimeConnection) {
      setNotice(t('app.notice.runtimeDisconnected'))
      return
    }
    const activeMessage = [...activeConversation.messages]
      .reverse()
      .find(
        (msg) =>
          msg.role === 'assistant' &&
          Boolean(msg.runId) &&
          (msg.status === 'streaming' || msg.status === 'waiting_permission' || msg.status === 'waiting_input'),
      )
    if (!activeMessage?.runId) {
      setNotice(t('app.notice.missingLocalTask'))
      return
    }

    setNotice('')
    setDraft('')
    let command: PendingRunInjectCommand | undefined
    try {
      const existing = (await localData.listPendingRuntimeCommands()).find(
        (command): command is PendingRunInjectCommand =>
          command.type === 'run.inject' &&
          command.input.runId === activeMessage.runId &&
          command.input.content === content,
      )
      command = existing ?? {
        type: 'run.inject' as const,
        commandId: createLocalID('inject'),
        createdAt: new Date().toISOString(),
        input: { runId: activeMessage.runId, threadId: activeConversation.id, content },
      }
      if (!existing) await localData.savePendingRuntimeCommand(command)
      const result = await injectLocalRunInstruction(
        command.commandId,
        command.input.runId,
        command.input.content,
        runtimeConnection,
      )
      await settleDeliveredLocalRunCommand(command, result, runtimeConnection)
      toast.success(t('app.notice.steeringQueued'), { id: 'steering-queued', duration: 2200 })
    } catch (error) {
      workspaceStoreActions.setPendingCommandDeliveryVersion((version) => version + 1)
      setDraft((current) => current || content)
      const commandErrorMessage = runtimeCommandErrorMessage(error, t)
      if (command?.commandId) {
        suppressRuntimeCommandFailureNotice(command.commandId, commandErrorMessage)
      }
      setNotice(commandErrorMessage)
    }
  }, [
    activeConversation,
    createLocalID,
    draft,
    localData,
    runtimeConnection,
    setDraft,
    setNotice,
    settleDeliveredLocalRunCommand,
    suppressRuntimeCommandFailureNotice,
    t,
  ])

  return {
    sendMessage,
    resendFromUserMessage,
    cancelActiveRun,
    appendInstructionToActiveRun,
    ...decisionHandlers,
    submitPluginCommand,
    settleDeliveredLocalRunCommand,
    settleRejectedPendingRuntimeCommand,
  }
}
