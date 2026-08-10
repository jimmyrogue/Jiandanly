import { useCallback, type MutableRefObject, type Dispatch, type SetStateAction } from 'react'
import { toast } from 'sonner'
import { createLocalID } from '@/shared/local-data/localConversations'
import type { Translator } from '@/shared/i18n/i18n'
import { mergeAttachments, upsertConversation } from './conversationState'
import { runtimeCommandErrorMessage } from './runtimeCommandError'
import { conversationStore, conversationStoreActions } from './state/conversationStore'
import { runtimeStore } from './state/runtimeStore'
import { workspaceStoreActions } from './state/workspaceStore'
import { useStore } from './state/store'
import type {
  AgentTimelineItem,
  ChatMode,
  ChatMessage,
  Conversation,
  ConversationProject,
  ConversationWorkspace,
  LocalAttachmentRef,
} from '@/shared/local-data/types'
import type {
  AgentSettings,
  LocalRunMetadata,
  LocalThreadSnapshot,
  PendingPluginInstallCommand,
  PendingPluginModelBindCommand,
  PendingPluginRemoveCommand,
  PendingPluginRollbackCommand,
  PendingPluginStateCommand,
  PendingPluginUpdateCommand,
  PendingRuntimeAssetInstallCommand,
  PendingRuntimeCommand,
  PendingRuntimeCommandFailure,
  PendingRunCancelCommand,
  PendingRunInjectCommand,
  RuntimeCommandResult,
  RuntimeConnection,
  LocalRun,
} from '@/runtime/client'
import {
  cancelLocalRunCommand,
  deleteLocalThread,
  deliverPendingRuntimeCommands,
  getLocalThreadSnapshot,
  hasRuntimeAuthorization,
  injectLocalRunInstruction,
  parseRuntimeModelSpec,
  streamLocalRun,
} from '@/runtime/client'
import type { ConversationRenderContext } from './useConversationProject'
import {
  useRunDecisionCommands,
  type RunDecisionCommandContext,
  type RunDecisionCommandHandlers,
} from './useRunDecisionCommands'

interface LocalHarnessRunOptions {
  parentRunId?: string
  metadata?: LocalRunMetadata
  initialAgentEvents?: AgentTimelineItem[]
  replaceFromClientId?: string
  hideUserMessage?: boolean
  pluginReferences?: NonNullable<ChatMessage['pluginReferences']>
  pluginCommand?: NonNullable<ChatMessage['pluginCommand']>
}

type PendingPluginCommand =
  | PendingPluginInstallCommand
  | PendingRuntimeAssetInstallCommand
  | PendingPluginModelBindCommand
  | PendingPluginStateCommand
  | PendingPluginUpdateCommand
  | PendingPluginRollbackCommand
  | PendingPluginRemoveCommand

type ProjectRuntimeThreadCache = (
  snapshot: LocalThreadSnapshot,
  existing: Conversation | undefined,
  config: RuntimeConnection,
  t: Translator,
) => Promise<Conversation>

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

interface RuntimeCommandHandlers extends RunDecisionCommandHandlers {
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

function isPendingPluginCommand(command: PendingRuntimeCommand): command is PendingPluginCommand {
  return command.type.startsWith('plugin.')
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

  const settleDeliveredLocalRunCommand = useCallback(async (
    command: PendingRuntimeCommand,
    result: RuntimeCommandResult,
    config: RuntimeConnection,
  ): Promise<boolean> => {
    clearRuntimeCommandFailureNotice(command.commandId)
    if (isPendingPluginCommand(command)) {
      await localData.deletePendingRuntimeCommand(command.commandId)
      setPluginCatalogVersion((version) => version + 1)
      return true
    }
    if (
      command.type === 'question.answer' ||
      command.type === 'permission.resolve' ||
      command.type === 'plan.resolve' ||
      command.type === 'tool.reconcile' ||
      command.type === 'run.cancel' ||
      command.type === 'run.inject'
    ) {
      await localData.deletePendingRuntimeCommand(command.commandId)
      const projected = await syncRuntimeThreadCache(config)
      conversationStoreActions.setConversations((items) => projected.reduce((next, conversation) => upsertConversation(next, conversation), items))
      return true
    }
    const run = result as LocalRun
    const threadID = command.input.threadId
    if (threadID) {
      const nextRuntimeThreadIDs = new Set(runtimeThreadIDsRef.current).add(threadID)
      storeRuntimeThreadIDs(nextRuntimeThreadIDs)
      runtimeThreadIDsRef.current = nextRuntimeThreadIDs
    }
    const [pending, conversation] = await Promise.all([
      localData.getPendingRuntimeCommand(command.commandId),
      threadID ? localData.get(threadID) : Promise.resolve(undefined),
    ])
    if (pending?.canceledAt || (threadID && !conversation)) {
      if (threadID) {
        await cancelLocalRunCommand(`cancel_${run.id}`, run.id, config)
        await streamLocalRun(run.id, config, { onDelta: () => undefined, onEvent: () => undefined })
        await deleteLocalThread(threadID, config)
        const nextRuntimeThreadIDs = new Set(runtimeThreadIDsRef.current)
        nextRuntimeThreadIDs.delete(threadID)
        storeRuntimeThreadIDs(nextRuntimeThreadIDs)
        runtimeThreadIDsRef.current = nextRuntimeThreadIDs
      }
      if (threadID) {
        await localData.settleCanceledLocalRunCommand(threadID, command.commandId)
      } else {
        await localData.deletePendingRuntimeCommand(command.commandId)
      }
      return false
    }
    await localData.deletePendingRuntimeCommand(command.commandId)
    return true
  }, [
    localData,
    runtimeThreadIDsRef,
    conversationStoreActions,
    setPluginCatalogVersion,
    clearRuntimeCommandFailureNotice,
    storeRuntimeThreadIDs,
    syncRuntimeThreadCache,
  ])

  const settleRejectedPendingRuntimeCommand = useCallback(async (
    failure: PendingRuntimeCommandFailure,
    config: RuntimeConnection,
  ): Promise<void> => {
    const { command } = failure
    if (isPendingPluginCommand(command)) {
      await localData.deletePendingRuntimeCommand(command.commandId)
      setPluginCatalogVersion((version) => version + 1)
      return
    }
    if (command.type !== 'run.start' && command.type !== 'run.fork') {
      const existing = await localData.get(command.input.threadId)
      let projected: Conversation | undefined
      try {
        const snapshot = await getLocalThreadSnapshot(command.input.threadId, config)
        projected = await projectRuntimeThreadCache(snapshot, existing, config, t)
      } catch {
        // A rejected command may refer to a thread that no longer exists.
      }
      await localData.settleRejectedRuntimeCommand(command.commandId, projected)
      if (projected) conversationStoreActions.setConversations((items) => upsertConversation(items, projected))
      return
    }
    const threadID = command.input.threadId
    const conversation = threadID ? await localData.get(threadID) : undefined
    const assistantID = command.input.assistantMessageId
    const message = conversation?.messages.find((item) => item.id === assistantID)
    if (conversation && message) {
      message.status = 'error'
      message.agentEvents = [
        ...(message.agentEvents ?? []),
        { type: 'ui.command_rejected', label: runtimeCommandErrorMessage(failure.error, t) },
      ]
      conversation.updatedAt = new Date().toISOString()
    }
    await localData.settleRejectedRuntimeCommand(command.commandId, conversation)
    if (conversation) conversationStoreActions.setConversations((items) => upsertConversation(items, conversation))
  }, [
    localData,
    projectRuntimeThreadCache,
    conversationStoreActions,
    setPluginCatalogVersion,
    t,
  ])

  const submitPluginCommand = useCallback(async (
    command: PendingPluginCommand,
  ): Promise<RuntimeCommandResult> => {
    if (!hasRuntimeAuthorization(runtimeConnection)) {
      throw new Error(t('app.notice.runtimeDisconnected'))
    }
    const config = runtimeConnection
    await localData.savePendingRuntimeCommand(command)
    let result: RuntimeCommandResult | undefined
    const report = await deliverPendingRuntimeCommands(
      [command],
      config,
      async (_deliveredCommand, deliveredResult) => {
        result = deliveredResult
        await localData.deletePendingRuntimeCommand(command.commandId)
        setPluginCatalogVersion((version) => version + 1)
      },
    )
    const failure = report.failures[0]
    if (failure) {
      const commandErrorMessage = runtimeCommandErrorMessage(failure.error, t)
      suppressRuntimeCommandFailureNotice(command.commandId, commandErrorMessage)
      if (failure.retryable) {
        workspaceStoreActions.setPendingCommandDeliveryVersion((version) => version + 1)
      } else {
        await localData.deletePendingRuntimeCommand(command.commandId)
      }
      throw failure.error
    }
    if (!result) throw new Error('plugin command completed without a receipt')
    return result
  }, [
    localData,
    runtimeConnection,
    setPluginCatalogVersion,
    suppressRuntimeCommandFailureNotice,
    t,
  ])

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
