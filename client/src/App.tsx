import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { toast } from 'sonner'
import { Toaster } from '@/components/ui/sonner'
import type { AgentFailureAction } from './features/chat/recovery'
import { findConversationPendingApproval } from './features/chat/pendingApproval'
import { findConversationPendingPlanApproval } from './features/chat/pendingPlanApproval'
import { findConversationPendingQuestion } from './features/chat/pendingQuestion'
import { useConversationDataActions } from './features/app/useConversationDataActions'
import {
  useConversationProject,
  type ConversationRenderContext,
} from './features/app/useConversationProject'
import { useRuntimeCommands } from './features/app/useRuntimeCommands'
import { useRuntimeDelivery } from './features/app/useRuntimeDelivery'
import { useRunDecisionGuards } from './features/app/useRunDecisionGuards'
import { useRuntimeObservers } from './features/app/useRuntimeObservers'
import { useMessageRecoveryActions } from './features/app/useMessageRecoveryActions'
import { useAppShortcuts } from './features/app/useAppShortcuts'
import {
  executeLocalHarnessMessage,
  type LocalHarnessRunOptions,
} from './features/app/runExecution'
import {
  loadRuntimeThreadIDs,
  readChatMode,
  storeRuntimeThreadIDs,
} from './features/app/appStorage'
import { useRuntimeModelSettings } from './features/app/useRuntimeModelSettings'
import { useLocalDocuments } from './features/app/useLocalDocuments'
import { findWorkspaceByPath, useWorkspaceActions } from './features/app/useWorkspaceActions'
import { useSidebarLayout } from './features/app/useSidebarLayout'
import { AppShell } from './features/app/AppShell'
import { runtimeCommandErrorMessage } from './features/app/runtimeCommandError'
import { runtimeStoreActions } from './features/app/state/runtimeStore'
import { workspaceStore, workspaceStoreActions } from './features/app/state/workspaceStore'
import { useStore } from './features/app/state/store'
import { I18nProvider } from './shared/i18n/I18nProvider'
import { useI18n } from './shared/i18n/i18n'
import { LocalConversationStore } from './shared/local-data/localConversations'
import type { ChatMode, Conversation, LocalAttachmentRef } from './shared/local-data/types'
import type { PluginsHubTab } from './features/plugins/PluginsHub'
import {
  advanceLocalPluginSetupCommand,
  clearLocalMemory,
  cleanupLocalRuntimeAssetStorage,
  createLocalSkill,
  createMcpServer,
  deleteLocalSkill,
  deleteMcpServer,
  getLocalRunDiagnostics,
  getLocalArtifactContent,
  getLocalFixedRuntimeAssetStatus,
  getLocalRuntimeAssetStorage,
  getLocalPlugin,
  getLocalPluginReadiness,
  getLocalSkillFile,
  listInstalledSkills,
  listLocalPlugins,
  listMcpServers,
  prepareLocalFixedRuntimeAsset,
  removeLocalFixedRuntimeAsset,
  updateLocalSkill,
  updateMcpServer,
  type AgentSettings,
  type FixedRuntimeAssetPluginID,
  type PermissionMode,
} from './runtime/client'
import {
  finalizeLocalRunStatus,
  projectRuntimeThreadCache,
} from './features/chat/runtimeProjection'
import {
  downloadLocalRunDiagnostics,
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
type NoticeOptions = Omit<NonNullable<Parameters<typeof toast.message>[1]>, 'id'>

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
  const sendingOperationRef = useRef(0)
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

  const [draft, setDraft] = useState('')
  const [permissionMode, setPermissionMode] = useState<PermissionMode>('auto')
  const [isSending, setIsSending] = useState(false)
  const [pendingDeleteMessageID, setPendingDeleteMessageID] = useState<string>()
  const [pendingDiagnosticsRunID, setPendingDiagnosticsRunID] = useState<string>()
  const [mainView, setMainView] = useState<'chat' | 'plugins' | 'settings'>('chat')
  const [pluginsTab, setPluginsTab] = useState<PluginsHubTab>('plugins')
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
    setActiveID,
    refreshConversations,
    refreshConversationsAfterStream,
    createConversationRenderContext,
    scheduleConversationRender,
    syncRuntimeThreadCache,
    setActiveConversationID,
    saveActiveConversationWorkspace,
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
    deleteConversationMessage,
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
  const {
    submittedPermissionRequestIDs,
    handlePermissionDecision,
    handleToolReconciliation,
    handleQuestionAnswer,
    handlePlanApprovalDecision,
  } = useRunDecisionGuards({
    handlePermissionDecisionOnce,
    handleToolReconciliationOnce,
    handleQuestionAnswerOnce,
    handlePlanApprovalDecisionOnce,
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
  useRuntimeObservers({ runtime, runtimeConnection, t })

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
  const {
    conversationSidebarRef,
    keyboardHelpOpen,
    setKeyboardHelpOpen,
    shortcutRows,
  } = useAppShortcuts({
    cancelActiveRun,
    expandSidebar,
    hasActiveRun,
    isSending,
    setMainView,
    startNewConversation,
    t,
  })
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
    handleRegenerateMessage,
    recoveryTargetFor,
    repairRecoveryTarget,
    retryRecoveryTarget,
  } = useMessageRecoveryActions({
    activeConversation,
    localData,
    resendFromUserMessage,
    setNotice,
    t,
  })
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

  function sendLocalHarnessMessage(
    content: string,
    context: ConversationRenderContext,
    settingsOverride?: Required<AgentSettings>,
    runOptions?: LocalHarnessRunOptions,
    targetConversationID = activeIDRef.current,
    attachments: LocalAttachmentRef[] = [],
  ): Promise<Conversation> {
    return executeLocalHarnessMessage({
      activeAgentSettings: agentSettings,
      attachments,
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
      runtimeThreadIDs: runtimeThreadIDsRef.current,
      scheduleConversationRender,
      settingsOverride,
      settleDeliveredLocalRunCommand,
      suppressRuntimeCommandFailureNotice,
      t,
      targetConversationID,
    })
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

  // The renderer is always hosted by Electron; Runtime is its only execution backend.
  const shellClassName = isDesktop ? 'app-window-shell electron-window-shell' : 'app-window-shell'
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
      handleDeleteMessage: deleteConversationMessage,
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
