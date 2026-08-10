import { useCallback, type Dispatch, type MutableRefObject, type SetStateAction } from 'react'
import type { Translator } from '@/shared/i18n/i18n'
import type { LocalConversationStore } from '@/shared/local-data/localConversations'
import type { Conversation } from '@/shared/local-data/types'
import {
  cancelLocalRunCommand,
  deleteLocalThread,
  deliverPendingRuntimeCommands,
  getLocalThreadSnapshot,
  hasRuntimeAuthorization,
  streamLocalRun,
  type LocalRun,
  type LocalThreadSnapshot,
  type PendingPluginInstallCommand,
  type PendingPluginModelBindCommand,
  type PendingPluginRemoveCommand,
  type PendingPluginRollbackCommand,
  type PendingPluginStateCommand,
  type PendingPluginUpdateCommand,
  type PendingRuntimeAssetInstallCommand,
  type PendingRuntimeCommand,
  type PendingRuntimeCommandFailure,
  type RuntimeCommandResult,
  type RuntimeConnection,
} from '@/runtime/client'
import { upsertConversation } from './conversationState'
import { runtimeCommandErrorMessage } from './runtimeCommandError'
import { conversationStoreActions } from './state/conversationStore'
import { workspaceStoreActions } from './state/workspaceStore'

export type PendingPluginCommand =
  | PendingPluginInstallCommand
  | PendingRuntimeAssetInstallCommand
  | PendingPluginModelBindCommand
  | PendingPluginStateCommand
  | PendingPluginUpdateCommand
  | PendingPluginRollbackCommand
  | PendingPluginRemoveCommand

export type ProjectRuntimeThreadCache = (
  snapshot: LocalThreadSnapshot,
  existing: Conversation | undefined,
  config: RuntimeConnection,
  t: Translator,
) => Promise<Conversation>

function isPendingPluginCommand(command: PendingRuntimeCommand): command is PendingPluginCommand {
  return command.type.startsWith('plugin.')
}

export function useRuntimeCommandSettlement({
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
}: {
  localData: LocalConversationStore
  projectRuntimeThreadCache: ProjectRuntimeThreadCache
  runtimeConnection: RuntimeConnection | null
  runtimeThreadIDsRef: MutableRefObject<Set<string>>
  setPluginCatalogVersion: Dispatch<SetStateAction<number>>
  storeRuntimeThreadIDs: (ids: Set<string>) => void
  syncRuntimeThreadCache: (config: RuntimeConnection) => Promise<Conversation[]>
  suppressRuntimeCommandFailureNotice: (commandId: string, message: string) => void
  clearRuntimeCommandFailureNotice: (commandId: string) => void
  t: Translator
}) {
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
      conversationStoreActions.setConversations((items) => projected.reduce(
        (next, conversation) => upsertConversation(next, conversation),
        items,
      ))
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
        await localData.settleCanceledLocalRunCommand(threadID, command.commandId)
      } else {
        await localData.deletePendingRuntimeCommand(command.commandId)
      }
      return false
    }
    await localData.deletePendingRuntimeCommand(command.commandId)
    return true
  }, [
    clearRuntimeCommandFailureNotice,
    localData,
    runtimeThreadIDsRef,
    setPluginCatalogVersion,
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
      if (projected) {
        conversationStoreActions.setConversations((items) => upsertConversation(items, projected))
      }
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
    if (conversation) {
      conversationStoreActions.setConversations((items) => upsertConversation(items, conversation))
    }
  }, [localData, projectRuntimeThreadCache, setPluginCatalogVersion, t])

  const submitPluginCommand = useCallback(async (
    command: PendingPluginCommand,
  ): Promise<RuntimeCommandResult> => {
    if (!hasRuntimeAuthorization(runtimeConnection)) {
      throw new Error(t('app.notice.runtimeDisconnected'))
    }
    await localData.savePendingRuntimeCommand(command)
    let result: RuntimeCommandResult | undefined
    const report = await deliverPendingRuntimeCommands(
      [command],
      runtimeConnection,
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

  return {
    settleDeliveredLocalRunCommand,
    settleRejectedPendingRuntimeCommand,
    submitPluginCommand,
  }
}
