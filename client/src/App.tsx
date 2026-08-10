import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { toast } from 'sonner'
import { Toaster } from '@/components/ui/sonner'
import { deriveAgentHistory } from './features/chat/conversationHistory'
import {
  beginRecoveryAction,
  createRecoveryState,
  endRecoveryAction,
  latestRunFailureEvent,
  nextRepairAttempt,
  nextRetryAttempt,
  recoveryTargetKey,
  type AgentFailureAction,
  type RecoveryTarget,
} from './features/chat/recovery'
import { parseSkillDraft } from './features/chat/skillDraft'
import { findConversationPendingApproval } from './features/chat/pendingApproval'
import { findConversationPendingPlanApproval } from './features/chat/pendingPlanApproval'
import { findConversationPendingQuestion } from './features/chat/pendingQuestion'
import { useConversationDataActions } from './features/app/useConversationDataActions'
import { useConversationProject } from './features/app/useConversationProject'
import { useRuntimeCommands } from './features/app/useRuntimeCommands'
import { useRuntimeDelivery } from './features/app/useRuntimeDelivery'
import {
  loadRuntimeThreadIDs,
  readChatMode,
  storeRuntimeThreadIDs,
  writeChatMode,
} from './features/app/appStorage'
import { useRuntimeModelSettings } from './features/app/useRuntimeModelSettings'
import { useLocalDocuments } from './features/app/useLocalDocuments'
import { findWorkspaceByPath, useWorkspaceActions } from './features/app/useWorkspaceActions'
import { useSidebarLayout } from './features/app/useSidebarLayout'
import { AppShell } from './features/app/AppShell'
import {
  cloneConversation,
  sortConversationsForSidebar,
  upsertConversation,
} from './features/app/conversationState'
import { runtimeCommandErrorMessage } from './features/app/runtimeCommandError'
import { runtimeStoreActions } from './features/app/state/runtimeStore'
import { workspaceStore, workspaceStoreActions } from './features/app/state/workspaceStore'
import { useStore } from './features/app/state/store'
import { I18nProvider } from './shared/i18n/I18nProvider'
import { useI18n } from './shared/i18n/i18n'
import { createLocalID, LocalConversationStore } from './shared/local-data/localConversations'
import type { AgentTimelineItem, ChatMessage, ChatMode, Conversation, ConversationWorkspace, LocalAttachmentRef } from './shared/local-data/types'
import type { ConversationSidebarHandle } from './features/chat/components/ConversationSidebar'
import type { PluginsHubTab } from './features/plugins/PluginsHub'
import {
  advanceLocalPluginSetupCommand,
  clearLocalMemory,
  cleanupLocalRuntimeAssetStorage,
  createLocalSkill,
  createLocalRun,
  createMcpServer,
  deleteLocalSkill,
  deleteMcpServer,
  getLocalRunDiagnostics,
  getLocalThreadSnapshot,
  getRuntimeConnection,
  hasRuntimeAuthorization,
  getLocalArtifactContent,
  getLocalFixedRuntimeAssetStatus,
  getLocalRuntimeAssetStorage,
  getLocalPlugin,
  getLocalPluginReadiness,
  getLocalSkillFile,
  listAuthorizedWorkspaces,
  listInstalledSkills,
  listLocalPlugins,
  listLocalRuns,
  listLocalSchedules,
  listMcpServers,
  markLocalScheduleNotified,
  parseRuntimeModelSpec,
  prepareLocalFixedRuntimeAsset,
  removeLocalFixedRuntimeAsset,
  probeRuntime,
  updateLocalSkill,
  updateMcpServer,
  type AgentSettings,
  type CreateLocalRunInput,
  type FixedRuntimeAssetPluginID,
  type LocalToolReconciliationDecision,
  type LocalPlanApprovalDecision,
  type LocalPermissionScope,
  type PendingRunStartCommand,
  type PermissionMode,
  type LocalRun as LocalHarnessRun,
  type LocalRunMetadata,
  type LocalScheduledRun,
} from './runtime/client'
import {
  finalizeLocalRunStatus,
  projectRuntimeThreadCache,
} from './features/chat/runtimeProjection'
import {
  downloadLocalRunDiagnostics,
  notifyAgentCompleted,
  notifyAgentFailed,
  notifyScheduledRun,
  streamLocalMessage,
 } from './features/app/runStreaming'

const appNoticeToastID = 'shejane-app-notice'

function setNotice(message: string, options: NoticeOptions = {}) {
  if (!message.trim()) {
    toast.dismiss(appNoticeToastID)
    return
  }
  toast.dismiss(appNoticeToastID)
  toast.message(message, {
    duration: 3200,
    ...options,
    id: appNoticeToastID,
  })
}
const scheduledRunNotificationPollMs = 30_000
const runtimeHealthPollMs = 2_000
interface LocalHarnessRunOptions {
  parentRunId?: string
  metadata?: LocalRunMetadata
  initialAgentEvents?: AgentTimelineItem[]
  replaceFromClientId?: string
  hideUserMessage?: boolean
  pluginReferences?: NonNullable<ChatMessage['pluginReferences']>
  pluginCommand?: NonNullable<ChatMessage['pluginCommand']>
}

type NoticeOptions = Omit<NonNullable<Parameters<typeof toast.message>[1]>, 'id'>

interface ConversationRenderContext {
  navigationVersionAtStart: number
}

export function App() {
  return (
    <I18nProvider>
      <AppContent />
      <Toaster position="top-center" offset={52} duration={3200} visibleToasts={1} />
    </I18nProvider>
  )
}

function useAppContentViewModel() {
  const { t, locale } = useI18n()
  const isDesktop = Boolean(window.shejaneClient)
  const localData = useMemo(() => new LocalConversationStore('shejane-local:runtime:local-owner'), [])
  const navigationVersionRef = useRef(0)
  const recoveryStateRef = useRef<ReturnType<typeof createRecoveryState> | null>(null)
  if (recoveryStateRef.current === null) {
    recoveryStateRef.current = createRecoveryState()
  }
  const recoveryState = recoveryStateRef.current
  const runtimeCommandFailureNoticeSuppressionRef = useRef(new Map<string, string>())
  const suppressRuntimeCommandFailureNotice = useCallback((commandId: string, message: string) => {
    runtimeCommandFailureNoticeSuppressionRef.current.set(commandId, message)
  }, [])
  const consumeRuntimeCommandFailureNotice = useCallback((commandId: string, message: string): boolean => {
    if (runtimeCommandFailureNoticeSuppressionRef.current.get(commandId) !== message) {
      return false
    }
    runtimeCommandFailureNoticeSuppressionRef.current.delete(commandId)
    return true
  }, [])

  const clearRuntimeCommandFailureNotice = useCallback((commandId: string) => {
    runtimeCommandFailureNoticeSuppressionRef.current.delete(commandId)
  }, [])
  const runtimeThreadCursorRef = useRef(0)
  const runtimeThreadIDsRef = useRef(new Set<string>())
  const questionAnswersInFlightRef = useRef(new Set<string>())
  const permissionDecisionsInFlightRef = useRef(new Set<string>())
  const planDecisionsInFlightRef = useRef(new Set<string>())
  const toolReconciliationsInFlightRef = useRef(new Set<string>())
  const sendingOperationRef = useRef(0)
  const conversationSidebarRef = useRef<ConversationSidebarHandle>(null)
  const {
    appShellStyle,
    beginSidebarResize,
    collapseSidebar,
    expandSidebar,
    handleSidebarResizeKeyDown,
    isResizingSidebar,
    sidebarCollapsed,
    sidebarMotion,
    sidebarWidth,
  } = useSidebarLayout()
  const {
    agentSettings,
    changeAgentSettings,
    changeImageMode,
    imageMode,
    imageModels,
    mode,
    models,
    refreshCurrentModel,
    runtime,
    runtimeConnection,
    runtimeSettingsConfig,
    setMode,
  } = useRuntimeModelSettings({ t, setNotice })

  const [submittedPermissionRequestIDs, setSubmittedPermissionRequestIDs] = useState<ReadonlySet<string>>(
    () => new Set(),
  )
  const [draft, setDraft] = useState('')
  const [permissionMode, setPermissionMode] = useState<PermissionMode>('auto')
  const [isSending, setIsSending] = useState(false)
  const [pendingDeleteMessageID, setPendingDeleteMessageID] = useState<string>()
  const [pendingDiagnosticsRunID, setPendingDiagnosticsRunID] = useState<string>()
  const [mainView, setMainView] = useState<'chat' | 'plugins' | 'settings'>('chat')
  const [pluginsTab, setPluginsTab] = useState<PluginsHubTab>('plugins')
  const [keyboardHelpOpen, setKeyboardHelpOpen] = useState(false)
  const [modelRequiredOpen, setModelRequiredOpen] = useState(false)
  const [modelServiceAddRequested, setModelServiceAddRequested] = useState(false)
  const listInstalledSkillsForView = useCallback(
    () => runtimeConnection
      ? listInstalledSkills(runtimeConnection)
      : Promise.resolve({ skills: [], roots: [] }),
    [runtimeConnection],
  )
  const listPluginsForView = useCallback(
    () => runtimeConnection ? listLocalPlugins(runtimeConnection) : Promise.resolve([]),
    [runtimeConnection],
  )
  const listMcpServersForView = useCallback(
    () => runtimeConnection
      ? listMcpServers(runtimeConnection)
      : Promise.resolve({ servers: [], sources_scanned: [] }),
    [runtimeConnection],
  )
  // Composer-bound selection and Runtime workspace authorization state.
  // Shared with the feature hooks through the workspace store. The pending
  // project (= workspace) slot mirrors `pendingWorkspace` — they're set
  // together when the picker resolves, since "project" in this product
  // means "this chat is bound to that workspace directory".
  const {
    pendingWorkspace,
    pendingProject,
    pendingAttachments,
    authorizedWorkspaces,
    localRuns,
    pendingCommandDeliveryVersion,
  } = useStore(workspaceStore)
  const [pluginCatalogVersion, setPluginCatalogVersion] = useState(0)
  const scheduledNotificationIDs = useRef(new Set<string>())
  const {
    activeDocument,
    artifactPreview,
    docPreviewRefreshKey,
    openLocalArtifact,
    openLocalDocument,
    setActiveDocument,
    setArtifactPreview,
    showLocalFileContextMenu,
  } = useLocalDocuments({
    runtimeConnection,
    t,
    setNotice,
  })

  const {
    activeConversation,
    activeID,
    activeIDRef,
    conversations,
    setConversations,
    setActiveID,
    refreshConversations,
    refreshConversationsAfterStream,
    createConversationRenderContext,
    scheduleConversationRender,
    syncRuntimeThreadCache,
    setActiveConversationID,
    startNewConversation,
    selectConversation,
  } = useConversationProject({
    localData,
    isDesktop,
    t,
    setNotice,
    setMainView,
    setDraft,
    setMode,
    readChatMode,
    navigationVersionRef,
    runtimeThreadCursorRef,
    runtimeThreadIDsRef,
    runtimeThreadStorageLoad: loadRuntimeThreadIDs,
    runtimeThreadStorageSave: storeRuntimeThreadIDs,
    detachVisibleSend,
  })
  const {
    deleteConversationData,
    exportConversationData,
    exportLocalData,
    importLocalData,
    renameConversation,
    togglePinConversation,
    updateConversationMetadata,
  } = useConversationDataActions({
    activeIDRef,
    agentSettings,
    localData,
    locale,
    mode,
    refreshConversations,
    runtimeConnection,
    runtimeThreadIDsRef,
    setNotice,
    t,
  })

  function changeMode(next: ChatMode): void {
    setMode(next)
    if (activeIDRef.current) {
      void updateConversationMetadata(
        activeIDRef.current,
        (conversation) => {
          conversation.model = next
        },
        { touch: false },
      )
    }
  }

  /** Global app shortcuts. Bypass browser/OS defaults only for app-level
   *  actions that are already visible in the shell. */
  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      const mod = event.metaKey || event.ctrlKey
      const key = event.key.toLowerCase()
      if (mod && !event.shiftKey && !event.altKey && key === 'n') {
        event.preventDefault()
        startNewConversation()
        return
      }
      if (mod && !event.shiftKey && !event.altKey && key === 'k') {
        event.preventDefault()
        expandSidebar()
        setMainView('chat')
        conversationSidebarRef.current?.openSearch()
        return
      }
      if (!mod && !event.altKey && event.key === '?' && !isEditableKeyboardTarget(event.target)) {
        event.preventDefault()
        setKeyboardHelpOpen(true)
      }
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [])

  /** Listen for the tray's "New Chat" menu item — the main process
   *  sends `shejane:new-chat` after bringing the window forward. */
  useEffect(() => {
    const unsubscribe = window.shejaneClient?.onNewChatRequest?.(() => {
      startNewConversation()
    })
    return unsubscribe
  }, [])

  const {
    settleDeliveredLocalRunCommand,
    settleRejectedPendingRuntimeCommand,
    submitPluginCommand,
    sendMessage,
    resendFromUserMessage,
    cancelActiveRun,
    appendInstructionToActiveRun,
    handlePermissionDecisionOnce,
    handleToolReconciliationOnce,
    handleQuestionAnswerOnce,
    handlePlanApprovalDecisionOnce,
  } = useRuntimeCommands({
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
    scheduleConversationRender,
    sendLocalHarnessMessage,
    consumeRuntimeCommandFailureNotice,
    suppressRuntimeCommandFailureNotice,
    clearRuntimeCommandFailureNotice,
    storeRuntimeThreadIDs,
    openLocalDocument,
    streamLocalMessage,
    projectRuntimeThreadCache,
    finalizeLocalRunStatus,
  })

  useRuntimeDelivery({
    localData,
    isDesktop,
    settleDeliveredLocalRunCommand,
    settleRejectedPendingRuntimeCommand,
    setNotice,
    consumeRuntimeCommandFailureNotice,
    t,
    retryDelayMs: 2000,
  })

  useEffect(() => {
    const clientBridge = window.shejaneClient
    const config = getRuntimeConnection()
    if (!config) {
      return
    }
    runtimeStoreActions.setConnection(config)
    let disposed = false
    let polling = false
    let catalogLoaded = false
    if (clientBridge?.runtime?.ready === false) {
      runtimeStoreActions.setRuntime({ online: false })
    }

    const loadRuntimeCatalog = async () => {
      if (catalogLoaded || !hasRuntimeAuthorization(config)) return
      catalogLoaded = true
      try {
        const [workspaces, runs] = await Promise.all([
          listAuthorizedWorkspaces(config),
          listLocalRuns(config),
        ])
        if (!disposed) {
          workspaceStoreActions.setAuthorizedWorkspaces(workspaces)
          workspaceStoreActions.setLocalRuns(runs)
        }
      } catch {
        catalogLoaded = false
      }
    }

    const poll = async () => {
      if (polling) return
      polling = true
      try {
        const probe = await probeRuntime(config.baseURL)
        if (disposed) return
        runtimeStoreActions.setRuntime(probe)
        if (probe.online) {
          await loadRuntimeCatalog()
        } else {
          catalogLoaded = false
        }
      } finally {
        polling = false
      }
    }

    void poll()
    const interval = window.setInterval(() => void poll(), runtimeHealthPollMs)
    return () => {
      disposed = true
      window.clearInterval(interval)
    }
  }, [])


  useEffect(() => {
    if (!runtime?.online || !hasRuntimeAuthorization(runtimeConnection)) {
      return
    }
    let disposed = false
    const config = runtimeConnection
    const poll = async () => {
      try {
        const schedules = await listLocalSchedules(config, { notifyPending: true })
        if (disposed || schedules.length === 0) {
          return
        }
        const unnotified = schedules.filter(
          (schedule) => !scheduledNotificationIDs.current.has(schedule.id),
        )
        for (const schedule of unnotified) {
          scheduledNotificationIDs.current.add(schedule.id)
          notifyScheduledRun(schedule, t)
        }
        await Promise.all(
          unnotified.map((schedule) => markLocalScheduleNotified(schedule.id, config)),
        )
        const freshRuns = await listLocalRuns(config)
        if (!disposed) {
          workspaceStoreActions.setLocalRuns(freshRuns)
        }
      } catch {
        // Best-effort observer; the next poll will retry.
      }
    }
    void poll()
    const interval = window.setInterval(() => void poll(), scheduledRunNotificationPollMs)
    return () => {
      disposed = true
      window.clearInterval(interval)
    }
  }, [runtime?.online, runtimeConnection, t])

  // A Runtime run can stay cancelable after `isSending` flips false because
  // HITL permission/question pauses block the SSE stream while the run lives.
  const hasActiveRun = Boolean(
    activeConversation?.messages.some(
      (msg) =>
        msg.role === 'assistant' &&
        Boolean(msg.runId) &&
        (msg.status === 'streaming' || msg.status === 'waiting_permission' || msg.status === 'waiting_input'),
    ),
  )
  const pendingApproval = findConversationPendingApproval(
    activeConversation,
    t,
    submittedPermissionRequestIDs,
  )
  const pendingPlanApproval = pendingApproval ? null : findConversationPendingPlanApproval(activeConversation)
  const pendingQuestion = pendingApproval || pendingPlanApproval ? null : findConversationPendingQuestion(activeConversation)
  const activeWorkspace = activeConversation?.workspace ?? pendingWorkspace
  const selectedWorkspace = activeWorkspace ? findWorkspaceByPath(authorizedWorkspaces, activeWorkspace.path) : undefined
  const localProject = activeWorkspace
    ? {
        label: selectedWorkspace?.label ?? activeWorkspace.label,
        path: activeWorkspace.path,
        authorized: Boolean(selectedWorkspace || activeWorkspace.authorized),
      }
    : undefined
  const {
    authorizeWorkspace,
    diagnoseWorkspace,
    dropAttachments,
    removeAttachment,
    removeProjectFromActiveConversation,
    selectAttachments,
    selectProjectForActiveConversation,
  } = useWorkspaceActions({
    activeIDRef,
    hasActiveRun,
    isSending,
    retryRecoveryTarget,
    runtimeConnection,
    saveActiveConversationWorkspace,
    setNotice,
    t,
    updateConversationMetadata,
  })

  function beginVisibleSend(): number {
    const operation = sendingOperationRef.current + 1
    sendingOperationRef.current = operation
    setIsSending(true)
    return operation
  }

  function finishVisibleSend(operation: number): void {
    if (sendingOperationRef.current === operation) setIsSending(false)
  }

  function detachVisibleSend(): void {
    sendingOperationRef.current += 1
    setIsSending(false)
  }

  function recoveryTargetFor(assistantMessageID: string): RecoveryTarget | undefined {
    if (!activeConversation) {
      return undefined
    }
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
    if (!target) {
      return
    }
    void retryRecoveryTarget(target)
  }

  async function regenerateMessageInConversation(conversationID: string, assistantMessageID: string) {
    const conversation = await localData.get(conversationID)
    if (!conversation) {
      return
    }
    const messages = conversation.messages
    const assistantIndex = messages.findIndex((message) => message.id === assistantMessageID)
    if (assistantIndex < 0) {
      return
    }
    // The user turn that produced this reply is the nearest preceding user message.
    let userIndex = -1
    for (let i = assistantIndex - 1; i >= 0; i--) {
      if (messages[i].role === 'user') {
        userIndex = i
        break
      }
    }
    if (userIndex < 0) {
      return
    }
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
    if (!failure) {
      return undefined
    }
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
      initialAgentEvents: [
        {
          type: 'ui.action.requested',
          label: t('agent.uiActionRequestedLabel', { action: retryAction }),
          retryAttempt: attempt,
          retrySourceRunId: assistantMessage.runId,
          retrySourceMessageId: assistantMessage.id,
        },
      ],
    }
  }

  async function repairRecoveryTarget(target: RecoveryTarget) {
    if (!beginRecoveryAction(recoveryState, 'repair', target)) {
      setNotice(t('app.notice.recoveryRetryAlreadyRunning'))
      return
    }
    try {
      const conversation = await localData.get(target.conversationID)
      if (!conversation) {
        return
      }
      const messages = conversation.messages
      const assistantIndex = messages.findIndex((message) => message.id === target.assistantMessageID)
      if (assistantIndex < 0) {
        return
      }
      let userIndex = -1
      for (let i = assistantIndex - 1; i >= 0; i--) {
        if (messages[i].role === 'user') {
          userIndex = i
          break
        }
      }
      if (userIndex < 0) {
        return
      }
      const assistantMessage = messages[assistantIndex]
      const userMessage = messages[userIndex]
      const failure = latestRunFailureEvent(assistantMessage)
      const attempt = nextRepairAttempt(assistantMessage)
      const repairAction = t('agent.repairAttemptLabel', { attempt })
      const initialAgentEvents: AgentTimelineItem[] = [
        {
          type: 'ui.action.requested',
          label: t('agent.uiActionRequestedLabel', { action: repairAction }),
          repairAttempt: attempt,
          repairSourceRunId: assistantMessage.runId,
          repairSourceMessageId: assistantMessage.id,
        },
      ]
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

  function handleAgentFailureAction(action: AgentFailureAction, assistantMessageID: string) {
    const recoveryTarget = recoveryTargetFor(assistantMessageID)
    if (!recoveryTarget) {
      return
    }
    if (action === 'retry') {
      void retryRecoveryTarget(recoveryTarget)
      return
    }
    if (action === 'repair') {
      void repairRecoveryTarget(recoveryTarget)
      return
    }
    if (action === 'workspace') {
      void selectProjectForActiveConversation(recoveryTarget)
      return
    }
    if (action === 'diagnostics') {
      const runID = activeConversation?.messages.find((message) => message.id === assistantMessageID)?.runId
      if (runID) {
        setPendingDiagnosticsRunID(runID)
      }
    }
  }

  function handleEditResendMessage(userMessageID: string, newText: string) {
    if (!activeConversation) {
      return
    }
    void resendFromUserMessage(userMessageID, newText, true)
  }

  async function handleDeleteMessage(messageID: string) {
    if (!activeID) {
      return
    }
    const conversation = await localData.get(activeID)
    const message = conversation?.messages.find((item) => item.id === messageID)
    if (!conversation) {
      return
    }
    // Don't mutate a conversation with an in-flight run: the streaming send
    // holds its own conversation snapshot and would re-save (un-delete) on
    // completion. The delete button is already disabled while runActive; this
    // guards the case where the confirm dialog was opened before a run began.
    if (conversation.messages.some((message) => message.status === 'streaming' || message.status === 'pending')) {
      return
    }
    const index = conversation.messages.findIndex((message) => message.id === messageID)
    if (index < 0) {
      return
    }
    const target = conversation.messages[index]
    // Deleting a user message also drops its paired assistant reply (keeps
    // turns coherent); deleting an assistant message drops just it.
    const removeCount =
      target.role === 'user' && conversation.messages[index + 1]?.role === 'assistant' ? 2 : 1
    conversation.messages = [
      ...conversation.messages.slice(0, index),
      ...conversation.messages.slice(index + removeCount),
    ]
    conversation.updatedAt = new Date().toISOString()
    await localData.save(conversation)
    await refreshConversations(activeID)
  }

  async function sendLocalHarnessMessage(
    content: string,
    context: ConversationRenderContext,
    settingsOverride?: Required<AgentSettings>,
    runOptions?: LocalHarnessRunOptions,
    targetConversationID = activeIDRef.current,
    attachments: LocalAttachmentRef[] = [],
  ): Promise<Conversation> {
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
    let effectiveSettings: Required<AgentSettings> = settingsOverride ?? agentSettings
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
      history: runtimeThreadIDsRef.current.has(conversation.id)
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

  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      if (event.defaultPrevented || event.key !== 'Escape') {
        return
      }
      if (keyboardHelpOpen) {
        event.preventDefault()
        setKeyboardHelpOpen(false)
        return
      }
      if (isSending || hasActiveRun) {
        event.preventDefault()
        void cancelActiveRun()
      }
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [cancelActiveRun, keyboardHelpOpen, isSending, hasActiveRun])

  async function handlePermissionDecision(
    messageID: string,
    requestID: string,
    decision: 'approve' | 'edit' | 'deny',
    scope: LocalPermissionScope = 'once',
    editedAction?: { name: string, args: Record<string, unknown> },
  ) {
    if (permissionDecisionsInFlightRef.current.has(requestID)) return false
    permissionDecisionsInFlightRef.current.add(requestID)
    setSubmittedPermissionRequestIDs((current) => new Set(current).add(requestID))
    let commandAccepted = false
    try {
      commandAccepted = await handlePermissionDecisionOnce(
        messageID,
        requestID,
        decision,
        scope,
        editedAction,
      )
    } finally {
      permissionDecisionsInFlightRef.current.delete(requestID)
      if (!commandAccepted) {
        setSubmittedPermissionRequestIDs((current) => {
          const next = new Set(current)
          next.delete(requestID)
          return next
        })
      }
    }
    return commandAccepted
  }

  async function handleToolReconciliation(
    messageID: string,
    requestID: string,
    decision: LocalToolReconciliationDecision,
  ) {
    if (toolReconciliationsInFlightRef.current.has(requestID)) return
    toolReconciliationsInFlightRef.current.add(requestID)
    try {
      await handleToolReconciliationOnce(messageID, requestID, decision)
    } finally {
      toolReconciliationsInFlightRef.current.delete(requestID)
    }
  }

  async function handleQuestionAnswer(
    messageID: string,
    requestID: string,
    answers: Record<string, string[]>,
  ) {
    if (questionAnswersInFlightRef.current.has(requestID)) return
    questionAnswersInFlightRef.current.add(requestID)
    try {
      await handleQuestionAnswerOnce(messageID, requestID, answers)
    } finally {
      questionAnswersInFlightRef.current.delete(requestID)
    }
  }

  async function handlePlanApprovalDecision(
    messageID: string,
    requestID: string,
    decision: LocalPlanApprovalDecision,
    instructions?: string,
  ) {
    if (planDecisionsInFlightRef.current.has(requestID)) return
    planDecisionsInFlightRef.current.add(requestID)
    try {
      await handlePlanApprovalDecisionOnce(
        messageID,
        requestID,
        decision,
        instructions,
      )
    } finally {
      planDecisionsInFlightRef.current.delete(requestID)
    }
  }

  async function recoverLocalRun(run: LocalHarnessRun) {
    if (!runtimeConnection) {
      setNotice(t('app.notice.runtimeDisconnected'))
      return
    }
    const timestamp = new Date().toISOString()
    const conversation = createConversation(run.goal, timestamp, t('chat.newConversation'))
    const userMessage: ChatMessage = {
      id: createLocalID('msg'),
      role: 'user',
      content: t('app.notice.recoverLocalRun', { goal: run.goal }),
      createdAt: timestamp,
      status: 'done',
    }
    const assistantMessage: ChatMessage = {
      id: createLocalID('msg'),
      role: 'assistant',
      content: '',
      createdAt: timestamp,
      status: 'streaming',
      runId: run.id,
      agentEvents: [],
    }
    conversation.messages = [userMessage, assistantMessage]
    await localData.save(conversation)
    const renderContext = createConversationRenderContext()
    scheduleConversationRender(conversation, renderContext)
    setNotice('')
    try {
      await streamLocalMessage(
        run.id,
        runtimeConnection,
        conversation,
        assistantMessage,
        t,
        openLocalDocument,
        () => scheduleConversationRender(conversation, renderContext),
      )
      finalizeLocalRunStatus(assistantMessage)
      scheduleConversationRender(conversation, renderContext)
      const freshRuns = await listLocalRuns(runtimeConnection)
      workspaceStoreActions.setLocalRuns(freshRuns)
    } catch (error) {
      assistantMessage.status = 'error'
      assistantMessage.content = error instanceof Error ? error.message : t('app.notice.recoverLocalRunFailed')
      setNotice(assistantMessage.content)
      scheduleConversationRender(conversation, renderContext)
    } finally {
      conversation.updatedAt = new Date().toISOString()
      await localData.save(conversation)
      await refreshConversationsAfterStream(conversation.id, renderContext)
    }
  }

  async function exportLocalRunDiagnostics(runID: string) {
    if (!runtimeConnection) {
      setNotice(t('app.notice.runtimeDisconnected'))
      return
    }
    try {
      const diagnostics = await getLocalRunDiagnostics(runID, runtimeConnection)
      downloadLocalRunDiagnostics(diagnostics)
      setNotice(t('app.notice.diagnosticsExported', { id: diagnostics.run.id }))
    } catch (error) {
      setNotice(error instanceof Error ? error.message : t('app.notice.diagnosticsExportFailed'))
    }
  }

  async function saveActiveConversationWorkspace(workspace: ConversationWorkspace | undefined) {
    if (!activeID) {
      workspaceStoreActions.setPendingWorkspace(workspace)
      return
    }
    const timestamp = new Date().toISOString()
    const conversation = (await localData.get(activeID)) ?? createConversation(t('chat.newConversation'), timestamp, t('chat.newConversation'))
    if (workspace) {
      conversation.workspace = workspace
    } else {
      delete conversation.workspace
    }
    conversation.updatedAt = timestamp
    await localData.save(conversation)
    setActiveConversationID(conversation.id)
    setConversations((items) => sortConversationsForSidebar(
      upsertConversation(items, cloneConversation(conversation)),
    ))
  }

  // The renderer is always hosted by Electron; Runtime is its only execution backend.
  const shellClassName = isDesktop ? 'app-window-shell electron-window-shell' : 'app-window-shell'
  const shortcutModifier = keyboardShortcutModifier()
  const shortcutRows = [
    { label: t('shortcuts.newChat'), keys: [`${shortcutModifier}N`] },
    { label: t('shortcuts.searchChats'), keys: [`${shortcutModifier}K`] },
    { label: t('shortcuts.stopRun'), keys: ['Esc'] },
    { label: t('shortcuts.help'), keys: ['?'] },
  ]

  return {
    shell: {
      activeID,
      appShellStyle,
      beginSidebarResize,
      collapseSidebar,
      conversations,
      conversationSidebarRef,
      deleteConversationData,
      expandSidebar,
      exportConversationData,
      exportLocalData,
      handleSidebarResizeKeyDown,
      importLocalData,
      isResizingSidebar,
      keyboardHelpOpen,
      mainView,
      pluginsTab,
      renameConversation,
      selectConversation,
      setKeyboardHelpOpen,
      setPluginsTab,
      shellClassName,
      shortcutRows,
      sidebarCollapsed,
      sidebarMotion,
      sidebarWidth,
      startNewConversation,
      togglePinConversation,
    },
    chat: {
      activeConversation,
      activeDocument,
      activeWorkspace,
      appendInstructionToActiveRun,
      artifactPreview,
      cancelActiveRun,
      changeImageMode,
      changeMode,
      docPreviewRefreshKey,
      draft,
      dropAttachments,
      exportLocalRunDiagnostics,
      handleAgentFailureAction,
      handleDeleteMessage,
      handleEditResendMessage,
      handlePermissionDecision,
      handlePlanApprovalDecision,
      handleQuestionAnswer,
      handleRegenerateMessage,
      handleToolReconciliation,
      hasActiveRun,
      isSending,
      imageMode,
      imageModels,
      mode,
      modelRequiredOpen,
      models,
      openLocalArtifact,
      openLocalDocument,
      pendingApproval,
      pendingAttachments,
      pendingDeleteMessageID,
      pendingDiagnosticsRunID,
      pendingPlanApproval,
      pendingProject,
      pendingQuestion,
      permissionMode,
      removeAttachment,
      removeProjectFromActiveConversation,
      refreshCurrentModel,
      selectAttachments,
      selectProjectForActiveConversation,
      sendMessage,
      setActiveDocument,
      setArtifactPreview,
      setDraft,
      setModelRequiredOpen,
      setPendingDeleteMessageID,
      setPendingDiagnosticsRunID,
      setPermissionMode,
      showLocalFileContextMenu,
    },
    plugins: {
      agentSettings,
      changeAgentSettings,
      listInstalledSkillsForView,
      listMcpServersForView,
      listPluginsForView,
      pluginCatalogVersion,
      runtimeSettingsConfig,
      setModelCatalogVersion: () => runtimeStoreActions.bumpCatalogVersion(),
      submitPluginCommand,
    },
    common: {
      isDesktop,
      runtime,
      runtimeConnection,
      t,
      setMainView,
      modelServiceAddRequested,
      setModelServiceAddRequested,
    },
  }
}

type AppContentViewModel = ReturnType<typeof useAppContentViewModel>

function AppContent() {
  const view = useAppContentViewModel()
  return <AppShell shell={view.shell} chat={view.chat} plugins={view.plugins} common={view.common} />
}

function isEditableKeyboardTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) {
    return false
  }
  const tagName = target.tagName.toLowerCase()
  return (
    target.isContentEditable ||
    tagName === 'input' ||
    tagName === 'textarea' ||
    tagName === 'select'
  )
}

function keyboardShortcutModifier(): string {
  if (typeof navigator === 'undefined') {
    return 'Ctrl+'
  }
  return /Mac|iPhone|iPad|iPod/.test(navigator.platform) ? '⌘' : 'Ctrl+'
}

function upsertLocalRun(items: LocalHarnessRun[], run: LocalHarnessRun): LocalHarnessRun[] {
  return [run, ...items.filter((item) => item.id !== run.id)]
}

function createConversation(firstMessage: string, timestamp: string, fallbackTitle: string): Conversation {
  return {
    id: createLocalID('conv'),
    title: firstMessage.slice(0, 24) || fallbackTitle,
    archived: false,
    createdAt: timestamp,
    updatedAt: timestamp,
    messages: [],
  }
}
