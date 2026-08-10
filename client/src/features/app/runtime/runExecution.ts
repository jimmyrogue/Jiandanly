import type { Translator } from '@/shared/i18n/i18n'
import {
  createLocalID,
  type LocalConversationStore,
} from '@/shared/local-data/localConversations'
import type {
  AgentTimelineItem,
  ChatMessage,
  ChatMode,
  Conversation,
  ConversationProject,
  ConversationWorkspace,
  LocalAttachmentRef,
  LocalFileRef,
} from '@/shared/local-data/types'
import {
  createLocalRun,
  getLocalThreadSnapshot,
  getRuntimeConnection,
  parseRuntimeModelSpec,
  type AgentSettings,
  type CreateLocalRunInput,
  type LocalRun,
  type LocalRunMetadata,
  type PendingRunStartCommand,
  type PermissionMode,
  type RuntimeConnection,
} from '@/runtime/client'
import { deriveAgentHistory } from '@/features/chat/conversationHistory'
import {
  finalizeLocalRunStatus,
  projectRuntimeThreadCache,
} from '@/features/chat/projection/runtimeProjection'
import { parseSkillDraft } from '@/features/chat/skillDraft'
import { runtimeCommandErrorMessage } from './runtimeCommandError'
import { runtimeStoreActions } from '../state/runtimeStore'
import { workspaceStoreActions } from '../state/workspaceStore'
import { writeChatMode } from '../appStorage'
import {
  notifyAgentCompleted,
  notifyAgentFailed,
  streamLocalMessage,
} from './runStreaming'
import type { ConversationRenderContext } from '../useConversationProject'

export interface LocalHarnessRunOptions {
  parentRunId?: string
  metadata?: LocalRunMetadata
  initialAgentEvents?: AgentTimelineItem[]
  replaceFromClientId?: string
  hideUserMessage?: boolean
  pluginReferences?: NonNullable<ChatMessage['pluginReferences']>
  pluginCommand?: NonNullable<ChatMessage['pluginCommand']>
}

interface LocalRunExecutionContext {
  activeAgentSettings: Required<AgentSettings>
  attachments?: LocalAttachmentRef[]
  content: string
  context: ConversationRenderContext
  localData: LocalConversationStore
  mode: ChatMode
  models: ReadonlyArray<{ id: string }>
  openLocalDocument: (ref: LocalFileRef) => void
  pendingProject: ConversationProject | undefined
  pendingWorkspace: ConversationWorkspace | undefined
  permissionMode: PermissionMode
  runOptions?: LocalHarnessRunOptions
  runtimeConnection: RuntimeConnection | null | undefined
  runtimeThreadIDs: ReadonlySet<string>
  settingsOverride?: Required<AgentSettings>
  scheduleConversationRender: (
    conversation: Conversation,
    context: ConversationRenderContext,
  ) => void
  settleDeliveredLocalRunCommand: (
    command: PendingRunStartCommand,
    run: LocalRun,
    config: RuntimeConnection,
  ) => Promise<boolean>
  suppressRuntimeCommandFailureNotice: (commandId: string, message: string) => void
  t: Translator
  targetConversationID?: string
}

export async function executeLocalHarnessMessage({
  activeAgentSettings,
  attachments = [],
  content,
  context,
  localData,
  mode,
  models,
  openLocalDocument,
  pendingProject,
  pendingWorkspace,
  permissionMode,
  runOptions,
  runtimeConnection,
  runtimeThreadIDs,
  scheduleConversationRender,
  settingsOverride,
  settleDeliveredLocalRunCommand,
  suppressRuntimeCommandFailureNotice,
  t,
  targetConversationID,
}: LocalRunExecutionContext): Promise<Conversation> {
  const runRuntimeConnection = runtimeConnection ?? getRuntimeConnection()
  const commandId = createLocalID('cmd')
  if (!runRuntimeConnection) {
    throw new Error(t('app.notice.runtimeDisconnected'))
  }
  if (!runtimeConnection) {
    runtimeStoreActions.setConnection(runRuntimeConnection)
  }
  const selectedMode = parseRuntimeModelSpec(mode)
  if (!selectedMode || !models.some((model) => model.id === selectedMode)) {
    throw new Error(t('app.notice.localModelUnavailable'))
  }
  const {
    text: parsedText,
    skills: draftSkills,
    functions: draftFunctions,
    mcps: draftMcps,
    plugins: draftPlugins,
    pluginCommand: draftPluginCommand,
  } = parseSkillDraft(content)
  const selectedPlugins = runOptions?.pluginReferences
    ? runOptions.pluginReferences.map((plugin) => ({
      pluginId: plugin.pluginId,
      name: plugin.name,
      expectedDigest: plugin.digest,
    }))
    : draftPlugins
  const selectedPluginCommand = runOptions?.pluginCommand
    ? {
      pluginId: runOptions.pluginCommand.pluginId,
      pluginName: runOptions.pluginCommand.pluginName,
      commandId: runOptions.pluginCommand.commandId,
      title: runOptions.pluginCommand.title,
      expectedDigest: runOptions.pluginCommand.digest,
    }
    : draftPluginCommand
  const text = parsedText.trim()
  if (!text) {
    throw new Error(t('app.notice.emptyMessage'))
  }

  const timestamp = new Date().toISOString()
  const conversation = (targetConversationID ? await localData.get(targetConversationID) : undefined) ?? createConversation(text, timestamp, t('chat.newConversation'))
  // Composer's project picker can run before the first message, in
  // which case the workspace + project sit in pending* slots until
  // we materialize the conversation here.
  if (!conversation.workspace && pendingWorkspace) {
    conversation.workspace = { ...pendingWorkspace }
  }
  if (!conversation.project && pendingProject) {
    conversation.project = { ...pendingProject }
  }
  const userMessage: ChatMessage = {
    id: createLocalID('msg'),
    commandId,
    role: 'user',
    content: text,
    createdAt: timestamp,
    status: 'done',
    attachments: attachments.length ? attachments : undefined,
    pluginReferences: selectedPlugins.length
      ? selectedPlugins.map((plugin) => ({
        pluginId: plugin.pluginId,
        name: plugin.name,
        digest: plugin.expectedDigest,
      }))
      : undefined,
    pluginCommand: selectedPluginCommand
      ? {
        pluginId: selectedPluginCommand.pluginId,
        pluginName: selectedPluginCommand.pluginName,
        commandId: selectedPluginCommand.commandId,
        title: selectedPluginCommand.title,
        digest: selectedPluginCommand.expectedDigest,
      }
      : undefined,
  }
  const assistantMessage: ChatMessage = {
    id: createLocalID('msg'),
    role: 'assistant',
    content: '',
    createdAt: timestamp,
    status: 'pending',
    agentEvents: runOptions?.initialAgentEvents ? [...runOptions.initialAgentEvents] : [],
  }

  const priorMessages = conversation.messages
  conversation.messages = [
    ...priorMessages,
    ...(runOptions?.hideUserMessage ? [] : [userMessage]),
    assistantMessage,
  ]
  conversation.updatedAt = timestamp
  scheduleConversationRender(conversation, context)

  const parentRunId = runOptions?.parentRunId ?? [...priorMessages]
    .reverse()
    .find((message) => message.role === 'assistant' && Boolean(message.runId))?.runId

  const skillsForRun = !settingsOverride ? draftSkills : []
  const functionsForRun = !settingsOverride ? draftFunctions : []
  const mcpsForRun = !settingsOverride ? draftMcps : []
  const directives: string[] = []
  if (skillsForRun.length > 0) {
    directives.push(t('skills.useDirective', { names: skillsForRun.join('、') }))
  }
  if (mcpsForRun.length > 0) {
    directives.push(t('mcp.useDirective', { names: mcpsForRun.join('、') }))
  }
  const goal = directives.length > 0 ? `${directives.join('\n\n')}\n\n${text}` : text
  // Layered settings overrides — later wins. settingsOverride is used
  // for things like the auto-retry path that wants the user's bare
  // settings without slash-injected forcing.
  let effectiveSettings: Required<AgentSettings> = settingsOverride ?? activeAgentSettings
  if (skillsForRun.length > 0) {
    effectiveSettings = { ...effectiveSettings, skills: 'on' as const }
  }
  if (mcpsForRun.length > 0) {
    // Force MCP on AND make sure none of the explicitly referenced
    // servers are in the disabled list (the user just asked for
    // them by typing /name — the previous "off" state is overridden
    // for THIS run only; the persistent toggle on the MCP tab
    // stays untouched).
    const requested = new Set(mcpsForRun)
    effectiveSettings = {
      ...effectiveSettings,
      mcp: 'on' as const,
      mcpDisabled: effectiveSettings.mcpDisabled.filter((name) => !requested.has(name)),
    }
  }

  const runInput: CreateLocalRunInput = {
    commandId,
    clientMessageId: userMessage.id,
    threadId: conversation.id,
    assistantMessageId: assistantMessage.id,
    userInput: text,
    threadTitle: conversation.title,
    threadMetadata: {
      archived: conversation.archived,
      pinned: conversation.pinned ?? false,
      model: selectedMode,
      project: conversation.project,
      workspace: conversation.workspace,
    },
    userItemMetadata: attachments.length || runOptions?.hideUserMessage
      ? {
        ...(attachments.length ? { attachments } : {}),
        ...(runOptions?.hideUserMessage ? { hidden_from_transcript: true } : {}),
      }
      : undefined,
    replaceFromClientId: runOptions?.replaceFromClientId,
    goal,
    workspacePath: conversation.workspace?.path.trim() || undefined,
    attachmentPaths: attachments.map((attachment) => attachment.path),
    requiredTools: functionsForRun.includes('image') ? ['image.generate'] : undefined,
    history: runtimeThreadIDs.has(conversation.id)
      ? undefined
      : deriveAgentHistory(priorMessages),
    parentRunId,
    settings: effectiveSettings,
    metadata: runOptions?.metadata,
    mode: selectedMode,
    permissionMode,
    pluginRefs: selectedPlugins.map((plugin) => ({
      pluginId: plugin.pluginId,
      expectedDigest: plugin.expectedDigest,
    })),
    pluginCommand: selectedPluginCommand
      ? {
        pluginId: selectedPluginCommand.pluginId,
        commandId: selectedPluginCommand.commandId,
        expectedDigest: selectedPluginCommand.expectedDigest,
      }
      : undefined,
  }
  const pendingCommand: PendingRunStartCommand = {
    type: 'run.start',
    commandId,
    createdAt: timestamp,
    input: runInput,
  }
  let pendingCommandSaved = false
  await localData.saveWithPendingRuntimeCommand(conversation, pendingCommand)

  let keepConversation = true
  pendingCommandSaved = true
  try {
    const run = await createLocalRun(runInput, runRuntimeConnection)
    Object.assign(assistantMessage, { runId: run.id, status: 'streaming' as const })
    const runInputs = run.inputs
    const runInputsByIndex = new Map(runInputs.map((input) => [input.client_index, input]))
    if (attachments.length && (
      runInputs.length !== attachments.length
      || attachments.some((_, index) => !runInputsByIndex.has(index))
    )) {
      throw new Error('Runtime returned invalid attachment input references')
    }
    if (attachments.length) {
      userMessage.attachments = attachments.map((attachment, index) => ({
        ...attachment,
        runId: run.id,
        inputId: runInputsByIndex.get(index)!.input_id,
        mediaType: runInputsByIndex.get(index)!.media_type,
        bytes: runInputsByIndex.get(index)!.bytes,
      }))
    }
    keepConversation = await settleDeliveredLocalRunCommand(pendingCommand, run, runRuntimeConnection)
    if (!keepConversation) return conversation
    workspaceStoreActions.setLocalRuns((items) => upsertLocalRun(items, run))
    scheduleConversationRender(conversation, context)
    await streamLocalMessage(
      run.id,
      runRuntimeConnection,
      conversation,
      assistantMessage,
      t,
      openLocalDocument,
      () => scheduleConversationRender(conversation, context),
    )
    finalizeLocalRunStatus(assistantMessage)
    conversation.model = selectedMode
    if (assistantMessage.status === 'done') {
      writeChatMode(selectedMode)
    }
    scheduleConversationRender(conversation, context)
    // OS-level notification when the user has switched away — the
    // main process suppresses it if the window is still focused, so
    // we can call unconditionally on every terminal state.
    if (assistantMessage.status === 'done') {
      notifyAgentCompleted(assistantMessage, t)
    } else if (assistantMessage.status === 'error') {
      notifyAgentFailed(assistantMessage, t)
    }
  } catch (error) {
    workspaceStoreActions.setPendingCommandDeliveryVersion((version) => version + 1)
    const message = runtimeCommandErrorMessage(error, t)
    if (pendingCommandSaved) {
      suppressRuntimeCommandFailureNotice(commandId, message)
    }
    assistantMessage.status = assistantMessage.runId ? 'streaming' : 'pending'
    throw error
  } finally {
    if (keepConversation && await localData.get(conversation.id)) {
      try {
        const snapshot = await getLocalThreadSnapshot(conversation.id, runRuntimeConnection)
        Object.assign(
          conversation,
          await projectRuntimeThreadCache(snapshot, conversation, runRuntimeConnection, t),
        )
      } catch {
        conversation.updatedAt = new Date().toISOString()
      }
      if (await localData.saveRuntimeProjection(conversation)) {
        scheduleConversationRender(conversation, context)
      }
    }
  }

  return conversation
}

function upsertLocalRun(items: LocalRun[], run: LocalRun): LocalRun[] {
  return [run, ...items.filter((item) => item.id !== run.id)]
}

export function createConversation(
  firstMessage: string,
  timestamp: string,
  fallbackTitle: string,
): Conversation {
  return {
    id: createLocalID('conv'),
    title: firstMessage.slice(0, 24) || fallbackTitle,
    archived: false,
    createdAt: timestamp,
    updatedAt: timestamp,
    messages: [],
  }
}
