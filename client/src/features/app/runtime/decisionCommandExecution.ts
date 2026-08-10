import type { toast } from 'sonner'
import type { Translator } from '@/shared/i18n/i18n'
import type { LocalConversationStore } from '@/shared/local-data/localConversations'
import type { ChatMessage, Conversation, LocalFileRef } from '@/shared/local-data/types'
import {
  deliverPendingRuntimeCommands,
  type PendingPermissionResolveCommand,
  type PendingPlanResolveCommand,
  type PendingQuestionAnswerCommand,
  type PendingToolReconcileCommand,
  type RuntimeConnection,
} from '@/runtime/client'
import type { ConversationRenderContext } from '../useConversationProject'
import { workspaceStoreActions } from '../state/workspaceStore'
import { runtimeCommandErrorMessage } from './runtimeCommandError'

type NoticeOptions = Omit<NonNullable<Parameters<typeof toast.message>[1]>, 'id'>
type RuntimeLocalMessageStreamer = typeof import('./runStreaming').streamLocalMessage
type PendingDecisionCommand =
  | PendingPermissionResolveCommand
  | PendingPlanResolveCommand
  | PendingQuestionAnswerCommand
  | PendingToolReconcileCommand

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

async function deliverDecisionCommand(
  command: PendingDecisionCommand,
  localData: LocalConversationStore,
  runtimeConnection: RuntimeConnection,
): Promise<void> {
  const report = await deliverPendingRuntimeCommands(
    [command],
    runtimeConnection,
    async (deliveredCommand) => {
      await localData.deletePendingRuntimeCommand(deliveredCommand.commandId)
    },
  )
  const failure = report.failures[0]
  if (!failure) return
  workspaceStoreActions.setPendingCommandDeliveryVersion((version) => version + 1)
  throw failure.error
}

export async function executeDecisionCommand<Command extends PendingDecisionCommand>({
  context,
  runtimeConnection,
  conversation,
  message,
  runId,
  renderContext,
  contentBeforeDecision,
  waitingStatus,
  loadCommand,
  onAccepted,
  onRejected,
}: {
  context: RunDecisionCommandContext
  runtimeConnection: RuntimeConnection
  conversation: Conversation
  message: ChatMessage
  runId: string
  renderContext: ConversationRenderContext
  contentBeforeDecision: string
  waitingStatus: ChatMessage['status']
  loadCommand: () => Promise<Command>
  onAccepted?: (command: Command) => void
  onRejected?: () => void
}): Promise<boolean> {
  const {
    localData,
    t,
    setNotice,
    scheduleConversationRender,
    suppressRuntimeCommandFailureNotice,
    openLocalDocument,
    streamLocalMessage,
    finalizeLocalRunStatus,
    refreshConversationsAfterStream,
  } = context
  let command: Command | undefined
  let commandAccepted = false
  try {
    command = await loadCommand()
    await deliverDecisionCommand(command, localData, runtimeConnection)
    commandAccepted = true
    onAccepted?.(command)
    await streamLocalMessage(
      runId,
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
    message.status = commandAccepted ? 'streaming' : waitingStatus
    if (!commandAccepted) {
      message.content = contentBeforeDecision
      onRejected?.()
    }
    const errorMessage = runtimeCommandErrorMessage(error, t)
    if (command) suppressRuntimeCommandFailureNotice(command.commandId, errorMessage)
    setNotice(errorMessage)
    scheduleConversationRender(conversation, renderContext)
  } finally {
    conversation.updatedAt = new Date().toISOString()
    await localData.save(conversation)
    await refreshConversationsAfterStream(conversation.id, renderContext)
  }
  return commandAccepted
}
