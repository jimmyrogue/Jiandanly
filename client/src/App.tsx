import { IconDownload, IconLayoutSidebarLeftExpand, IconSparkles, IconTrash, IconX } from '@tabler/icons-react'
import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState, type CSSProperties, type KeyboardEvent as ReactKeyboardEvent, type PointerEvent as ReactPointerEvent } from 'react'
import { toast } from 'sonner'
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogMedia,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Toaster } from '@/components/ui/sonner'
import { TooltipProvider } from '@/components/ui/tooltip'
import { projectTransientAssistantText, timelineItem } from './features/chat/chatStore'
import { ChatThread } from './features/chat/components/ChatThread'
import { Composer } from './features/chat/components/Composer'
import type { ModelOption } from './features/chat/components/ModeSelector'
import { deriveAgentHistory } from './features/chat/conversationHistory'
import { recentRecoverableFailures } from './features/chat/recoverableFailures'
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
import { ConversationSidebar, type ConversationSidebarHandle } from './features/chat/components/ConversationSidebar'
import { PendingApprovalBar } from './features/chat/components/PendingApprovalBar'
import { PendingPlanApprovalBar } from './features/chat/components/PendingPlanApprovalBar'
import { PendingQuestionBar } from './features/chat/components/PendingQuestionBar'
import { PluginsHub, type PluginsHubTab } from './features/plugins/PluginsHub'
import { advancedSettingsFromRuntime, advancedSettingsPatchToRuntime } from './features/settings/runtimeSettings'
import { findConversationPendingApproval } from './features/chat/pendingApproval'
import { findConversationPendingPlanApproval } from './features/chat/pendingPlanApproval'
import {
  conversationForQuestionAnswer,
  findConversationPendingQuestion,
} from './features/chat/pendingQuestion'
import type { AgentRunEvent } from '@shejane/runtime-sdk'
import { I18nProvider } from './shared/i18n/I18nProvider'
import { useI18n, type Translator } from './shared/i18n/i18n'
import { createLocalID, LocalConversationStore } from './shared/local-data/localConversations'
import type { AgentTimelineItem, ChatMessage, ChatMode, Conversation, ConversationProject, ConversationWorkspace, ExportedModelService, LocalAttachmentRef, LocalFileRef, OpenDocument } from './shared/local-data/types'
import {
  authorizeLocalWorkspace,
  advanceLocalPluginSetupCommand,
  answerLocalQuestionCommand,
  cancelLocalRunCommand,
  clearLocalMemory,
  cleanupLocalRuntimeAssetStorage,
  createLocalSkill,
  createLocalRun,
  deliverPendingRuntimeCommands,
  createMcpServer,
  deleteLocalSkill,
  deleteLocalThread,
  deleteMcpServer,
  diagnoseLocalWorkspace,
  fetchWorkspaceFile,
  fetchRunInput,
  getLocalRunDiagnostics,
  getRuntimeSettings,
  getLocalThreadSnapshot,
  getRuntimeConnection,
  hasRuntimeAuthorization,
  getLocalArtifact,
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
  listLocalRunEvents,
  listLocalThreads,
  listLocalThreadChanges,
  listLocalRuntimeModels,
  listModelCapabilityBindings,
  listModelServices,
  listLocalSchedules,
  listMcpServers,
  LocalStreamCursorResetRequiredError,
  markLocalScheduleNotified,
  importModelService,
  injectLocalRunInstruction,
  parseRuntimeModelSpec,
  prepareLocalFixedRuntimeAsset,
  removeLocalFixedRuntimeAsset,
  probeRuntime,
  resolveLocalPlanCommand,
  resolveLocalPermissionCommand,
  reconcileLocalToolCommand,
  refreshModelService,
  setModelCapabilityBinding,
  streamLocalRun,
  updateLocalSkill,
  updateLocalThread,
  updateMcpServer,
  updateRuntimeSettings,
  type AgentSettings,
  type CreateLocalRunInput,
  type FixedRuntimeAssetPluginID,
  type LocalArtifact,
  type RuntimeConnection,
  type LocalToolReconciliationDecision,
  type RuntimeProbe,
  type LocalPlanApprovalDecision,
  type LocalPermissionScope,
  type PendingRunStartCommand,
  type PendingRunCancelCommand,
  type PendingRunInjectCommand,
  type PendingQuestionAnswerCommand,
  type PendingPermissionResolveCommand,
  type PendingPlanResolveCommand,
  type PendingToolReconcileCommand,
  type PendingPluginInstallCommand,
  type PendingRuntimeAssetInstallCommand,
  type PendingPluginModelBindCommand,
  type PendingPluginStateCommand,
  type PendingPluginUpdateCommand,
  type PendingPluginRollbackCommand,
  type PendingPluginRemoveCommand,
  type PermissionMode,
  type PendingRuntimeCommand,
  type PendingRuntimeCommandFailure,
  type RuntimeCommandResult,
  type LocalRun as LocalHarnessRun,
  type LocalRunDiagnostics,
  type LocalRunMetadata,
  type LocalScheduledRun,
  type LocalThreadSnapshot,
  type LocalWorkspaceDiagnosis,
  type LocalWorkspaceAuthorization,
} from './runtime/client'
import { filePreviewKind } from './shared/files/filePreview'
import { downloadFile } from './shared/files/downloadFile'
import {
  applyRunPresentationEvent,
  applySubagentLifecycleEvent,
  projectRuntimeThread,
} from './features/chat/runtimeProjection'

const ArtifactPanel = lazy(() => import('./features/chat/components/ArtifactPanel').then((module) => ({ default: module.ArtifactPanel })))
const DocPreviewPanel = lazy(() => import('./features/chat/components/DocPreviewPanel').then((module) => ({ default: module.DocPreviewPanel })))
const MCPView = lazy(() => import('./features/mcp/MCPView').then((module) => ({ default: module.MCPView })))
const PluginsView = lazy(() => import('./features/plugins/PluginsView').then((module) => ({ default: module.PluginsView })))
const SettingsView = lazy(() => import('./features/settings/SettingsView').then((module) => ({ default: module.SettingsView })))
const SkillsView = lazy(() => import('./features/skills/SkillsView').then((module) => ({ default: module.SkillsView })))

const appNoticeToastID = 'shejane-app-notice'

function runtimeCommandErrorMessage(error: unknown, t: Translator): string {
  return error instanceof Error ? error.message : t('app.notice.sendFailed')
}

async function chooseWorkspaceDirectory(): Promise<string | undefined> {
  const selectedPath = await window.shejaneClient?.selectWorkspaceDirectory?.()
  return selectedPath || undefined
}

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
const sidebarWidthStorageKey = 'shejane.sidebar.width.v2'
const sidebarCollapsedStorageKey = 'shejane.sidebar.collapsed.v1'
const runtimeThreadIDsStorageKey = 'shejane.runtime-thread-ids.v1'
const scheduledRunNotificationPollMs = 30_000
const pendingCommandRetryMs = 2_000
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

// Skill and MCP discovery are always available. Client stores only the user
// choices that still exist: memory and individually disabled MCP servers.
const agentSettingsStorageKey = 'shejane.agentSettings.v9'
// Concrete Runtime model selection. Stale values are reconciled against the
// Runtime catalog after connection.
const chatModeStorageKey = 'shejane.chatMode.v2'
const defaultAgentSettings: Required<AgentSettings> = {
  memory: 'on',
  skills: 'on',
  mcp: 'on',
  mcpDisabled: [],
  // Empty = every advanced knob inherits the runtime's own default. The user
  // only ever populates the fields they explicitly change in the panel.
  advanced: {},
}
const defaultChatMode: ChatMode = ''
const defaultSidebarWidth = 252
const minSidebarWidth = 190
const maxSidebarWidth = 340
const sidebarKeyboardStep = 12
const sidebarMotionMs = 220
type NoticeOptions = Omit<NonNullable<Parameters<typeof toast.message>[1]>, 'id'>

interface ConversationRenderContext {
  navigationVersionAtStart: number
}

interface PendingConversationRender {
  conversation: Conversation
  context: ConversationRenderContext
}

type PendingPluginCommand =
  | PendingPluginInstallCommand
  | PendingRuntimeAssetInstallCommand
  | PendingPluginModelBindCommand
  | PendingPluginStateCommand
  | PendingPluginUpdateCommand
  | PendingPluginRollbackCommand
  | PendingPluginRemoveCommand

function isPendingPluginCommand(command: PendingRuntimeCommand): command is PendingPluginCommand {
  return command.type.startsWith('plugin.')
}

function clampSidebarWidth(width: number): number {
  return Math.min(maxSidebarWidth, Math.max(minSidebarWidth, Math.round(width)))
}

function readSidebarWidth(): number {
  if (typeof window === 'undefined') {
    return defaultSidebarWidth
  }
  try {
    const rawWidth = window.localStorage.getItem(sidebarWidthStorageKey)
    if (!rawWidth) {
      return defaultSidebarWidth
    }
    const parsedWidth = Number(rawWidth)
    return Number.isFinite(parsedWidth) ? clampSidebarWidth(parsedWidth) : defaultSidebarWidth
  } catch {
    return defaultSidebarWidth
  }
}

function readSidebarCollapsed(): boolean {
  if (typeof window === 'undefined') {
    return false
  }
  try {
    return window.localStorage.getItem(sidebarCollapsedStorageKey) === '1'
  } catch {
    return false
  }
}

function writeSidebarCollapsed(collapsed: boolean) {
  try {
    window.localStorage.setItem(sidebarCollapsedStorageKey, collapsed ? '1' : '0')
  } catch {
    // Local storage can be unavailable in restricted browser contexts.
  }
}

function writeSidebarWidth(width: number) {
  try {
    window.localStorage.setItem(sidebarWidthStorageKey, String(clampSidebarWidth(width)))
  } catch {
    // Local storage can be unavailable in restricted browser contexts.
  }
}

function readAgentSettings(): Required<AgentSettings> {
  if (typeof window === 'undefined') {
    return { ...defaultAgentSettings }
  }
  try {
    const raw = window.localStorage.getItem(agentSettingsStorageKey)
    if (!raw) {
      return { ...defaultAgentSettings }
    }
    const parsed = JSON.parse(raw) as Partial<AgentSettings>
    return {
      memory: parsed.memory === 'off' ? 'off' : 'on',
      skills: 'on',
      mcp: 'on',
      // Defensive: anything non-string in the persisted list gets
      // dropped. Empty array if missing.
      mcpDisabled: Array.isArray(parsed.mcpDisabled)
        ? parsed.mcpDisabled.filter((name): name is string => typeof name === 'string')
        : [],
      advanced: {},
    }
  } catch {
    return { ...defaultAgentSettings }
  }
}

function writeAgentSettings(settings: Required<AgentSettings>) {
  try {
    window.localStorage.setItem(agentSettingsStorageKey, JSON.stringify({
      memory: settings.memory,
      mcpDisabled: settings.mcpDisabled,
    }))
  } catch {
    // Local storage can be unavailable in restricted browser contexts.
  }
}

function readChatMode(): ChatMode {
  if (typeof window === 'undefined') {
    return defaultChatMode
  }
  try {
    const raw = window.localStorage.getItem(chatModeStorageKey)?.trim()
    // The Runtime catalog reconciles selections that are no longer available.
    if (raw) return parseRuntimeModelSpec(raw) ?? defaultChatMode
  } catch {
    // Local storage can be unavailable in restricted browser contexts.
  }
  return defaultChatMode
}

function writeChatMode(mode: ChatMode) {
  try {
    window.localStorage.setItem(chatModeStorageKey, mode)
  } catch {
    // Local storage can be unavailable in restricted browser contexts.
  }
}

function chooseAvailableMode(models: ModelOption[], ...candidates: ChatMode[]): ChatMode {
  return candidates.find((candidate) => models.some((model) => model.id === candidate))
    ?? models.find((model) => model.recommended)?.id
    ?? models[0]?.id
    ?? ''
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
  const pendingConversationRendersRef = useRef<Map<string, PendingConversationRender>>(new Map())
  const liveRenderTimerRef = useRef<number>()
  const activeIDRef = useRef<string | undefined>()
  const navigationVersionRef = useRef(0)
  const conversationInitializationCompleteRef = useRef(false)
  const recoveryStateRef = useRef<ReturnType<typeof createRecoveryState> | null>(null)
  if (recoveryStateRef.current === null) {
    recoveryStateRef.current = createRecoveryState()
  }
  const recoveryState = recoveryStateRef.current
  const startupRecoveryNoticeShownRef = useRef(false)
  const sidebarResizeStateRef = useRef<{ startX: number, startWidth: number } | null>(null)
  const sidebarMotionTimerRef = useRef<number>()
  const runtimeThreadCursorRef = useRef(0)
  const runtimeThreadIDsRef = useRef(new Set<string>())
  const questionAnswersInFlightRef = useRef(new Set<string>())
  const permissionDecisionsInFlightRef = useRef(new Set<string>())
  const planDecisionsInFlightRef = useRef(new Set<string>())
  const toolReconciliationsInFlightRef = useRef(new Set<string>())
  const sendingOperationRef = useRef(0)
  const conversationSidebarRef = useRef<ConversationSidebarHandle>(null)

  const [conversations, setConversations] = useState<Conversation[]>([])
  const [submittedPermissionRequestIDs, setSubmittedPermissionRequestIDs] = useState<ReadonlySet<string>>(
    () => new Set(),
  )
  const [activeID, setActiveID] = useState<string>()
  const [draft, setDraft] = useState('')
  // Visible selection. The first available model seeds the cross-conversation default.
  const [mode, setMode] = useState<ChatMode>(readChatMode)
  const [permissionMode, setPermissionMode] = useState<PermissionMode>('auto')
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
  const [isSending, setIsSending] = useState(false)
  const [pendingDeleteMessageID, setPendingDeleteMessageID] = useState<string>()
  const [pendingDiagnosticsRunID, setPendingDiagnosticsRunID] = useState<string>()
  const [sidebarWidth, setSidebarWidth] = useState(readSidebarWidth)
  const [sidebarCollapsed, setSidebarCollapsed] = useState(readSidebarCollapsed)
  const [sidebarMotion, setSidebarMotion] = useState<'idle' | 'closing' | 'opening'>('idle')
  const [agentSettings, setAgentSettings] = useState<Required<AgentSettings>>(readAgentSettings)
  const [runtimeSettingsConfig, setRuntimeSettingsConfig] = useState<RuntimeConnection | null>(null)
  const [mainView, setMainView] = useState<'chat' | 'plugins' | 'settings'>('chat')
  const [pluginsTab, setPluginsTab] = useState<PluginsHubTab>('plugins')
  const [isResizingSidebar, setIsResizingSidebar] = useState(false)
  const [keyboardHelpOpen, setKeyboardHelpOpen] = useState(false)
  const [modelRequiredOpen, setModelRequiredOpen] = useState(false)
  const [modelServiceAddRequested, setModelServiceAddRequested] = useState(false)
  // Runtime model catalog feeding the composer picker.
  const [models, setModels] = useState<ModelOption[]>([])
  const modelsRef = useRef<ModelOption[]>([])
  const [imageMode, setImageMode] = useState<ChatMode>()
  const [imageModels, setImageModels] = useState<ModelOption[]>([])
  const [modelCatalogVersion, setModelCatalogVersion] = useState(0)
  const [runtime, setRuntime] = useState<RuntimeProbe | null>(null)
  const [runtimeConnection, setRuntimeConnection] = useState<RuntimeConnection | null>(null)
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
  const [pendingWorkspace, setPendingWorkspace] = useState<ConversationWorkspace | undefined>()
  /** Project (= workspace) the user picked in the composer before a
   *  conversation existed. Drained when `sendMessage` creates the
   *  first conversation; cleared on new-chat / select. Mirrors the
   *  `pendingWorkspace` slot — they're set together when the picker
   *  resolves, since "project" in this product means "this chat is
   *  bound to that workspace directory". */
  const [pendingProject, setPendingProject] = useState<ConversationProject | undefined>()
  const [pendingAttachments, setPendingAttachments] = useState<LocalAttachmentRef[]>([])
  const [authorizedWorkspaces, setAuthorizedWorkspaces] = useState<LocalWorkspaceAuthorization[]>([])
  const [localRuns, setLocalRuns] = useState<LocalHarnessRun[]>([])
  const [pendingCommandDeliveryVersion, setPendingCommandDeliveryVersion] = useState(0)
  const [pluginCatalogVersion, setPluginCatalogVersion] = useState(0)
  const scheduledNotificationIDs = useRef(new Set<string>())
  const runtimeSettingsWriteRef = useRef<Promise<void> | null>(null)
  if (runtimeSettingsWriteRef.current === null) {
    runtimeSettingsWriteRef.current = Promise.resolve()
  }
  const runtimeConnectionRef = useRef<RuntimeConnection | null>(null)
  const [artifactPreview, setArtifactPreview] = useState<LocalArtifact | null>(null)
  const [activeDocument, setActiveDocument] = useState<OpenDocument | null>(null)
  // Bumped on `doc.changed` (Phase 2 territory) to force the renderer to
  // re-fetch the file bytes. Phase 1 only needs the initial open path.
  const [docPreviewRefreshKey, setDocPreviewRefreshKey] = useState(0)
  function loadLocalFileBytes(ref: LocalFileRef): Promise<ArrayBuffer> {
    if (!runtimeConnection) {
      return Promise.reject(new Error(t('app.notice.runtimeDisconnected')))
    }
    if (ref.runId && ref.inputId) {
      return fetchRunInput(ref.runId, ref.inputId, runtimeConnection)
    }
    return fetchWorkspaceFile(ref.path, runtimeConnection)
  }

  /** Open supported files in the right panel; external-only files use their OS app. */
  function openLocalDocument(ref: LocalFileRef) {
    const kind = ref.kind ?? filePreviewKind(ref.name)
    if (!kind) {
      void openLocalFileNatively(ref)
      return
    }
    setActiveDocument({
      sourceKey: ref.runId && ref.inputId
        ? `run-input:${ref.runId}:${ref.inputId}`
        : `local:${ref.path}`,
      kind,
      name: ref.name,
      tooltip: ref.path,
      loadBytes: () => loadLocalFileBytes(ref),
      localPath: ref.path,
      runId: ref.runId,
      inputId: ref.inputId,
    })
    setDocPreviewRefreshKey((k) => k + 1)
  }

  async function openLocalFileNatively(ref: LocalFileRef) {
    try {
      const error = ref.runId && ref.inputId
        ? await window.shejaneClient?.openFileSnapshot?.({
          name: ref.name,
          bytes: new Uint8Array(await loadLocalFileBytes(ref)),
          action: 'open',
        })
        : await window.shejaneClient?.openFileWithDefaultApp?.(ref.path)
      if (error) setNotice(error)
    } catch (error) {
      setNotice(error instanceof Error ? error.message : String(error))
    }
  }

  async function revealLocalFile(ref: LocalFileRef) {
    try {
      if (ref.runId && ref.inputId) {
        const error = await window.shejaneClient?.openFileSnapshot?.({
          name: ref.name,
          bytes: new Uint8Array(await loadLocalFileBytes(ref)),
          action: 'reveal',
        })
        if (error) setNotice(error)
        return
      }
      await window.shejaneClient?.revealFileInFolder?.(ref.path)
    } catch (error) {
      setNotice(error instanceof Error ? error.message : String(error))
    }
  }

  async function downloadLocalFile(ref: LocalFileRef) {
    try {
      await downloadFile(ref.name, () => loadLocalFileBytes(ref))
    } catch (error) {
      setNotice(error instanceof Error ? error.message : String(error))
    }
  }

  async function showLocalFileContextMenu(ref: LocalFileRef) {
    const action = await window.shejaneClient?.showFileContextMenu?.({
      canPreview: Boolean(ref.kind ?? filePreviewKind(ref.name)),
    })
    if (action === 'preview') openLocalDocument(ref)
    if (action === 'open') await openLocalFileNatively(ref)
    if (action === 'save') await downloadLocalFile(ref)
    if (action === 'reveal') await revealLocalFile(ref)
  }

  function changeAgentSettings(next: Required<AgentSettings>) {
    const normalized = { ...next, skills: 'on' as const, mcp: 'on' as const }
    const runtimePatch = advancedSettingsPatchToRuntime(agentSettings.advanced, normalized.advanced)
    const runtimeSettingsReady = runtimeSettingsConfig === runtimeConnection && Boolean(runtime?.online)
    setAgentSettings(normalized)
    writeAgentSettings(normalized)
    if (!runtimeConnection || !runtimeSettingsReady || Object.keys(runtimePatch).length === 0) return

    const config = runtimeConnection
    runtimeSettingsWriteRef.current = runtimeSettingsWriteRef.current!
      .catch(() => undefined)
      .then(async () => {
        const settings = await updateRuntimeSettings(
          runtimePatch,
          config,
        )
        if (runtimeConnectionRef.current === config) {
          setAgentSettings((current) => ({
            ...current,
            advanced: advancedSettingsFromRuntime(settings),
          }))
        }
      })
      .catch(async (error) => {
        setNotice(error instanceof Error ? error.message : String(error))
        try {
          const settings = await getRuntimeSettings(config)
          if (runtimeConnectionRef.current === config) {
            setAgentSettings((current) => ({
              ...current,
              advanced: advancedSettingsFromRuntime(settings),
            }))
            setRuntimeSettingsConfig(config)
          }
        } catch {
          if (runtimeConnectionRef.current === config) {
            setRuntimeSettingsConfig(null)
          }
        }
      })
  }

  useEffect(() => {
    runtimeConnectionRef.current = runtimeConnection
  }, [runtimeConnection])

  // Runtime owns the complete BYOK model catalog.
  useEffect(() => {
    if (!runtimeConnection) {
      modelsRef.current = []
      setModels([])
      setImageMode(undefined)
      setImageModels([])
      return
    }
    let cancelled = false
    void listLocalRuntimeModels(runtimeConnection).then(async (localCatalog) => {
      if (cancelled) return
      const catalog: ModelOption[] = localCatalog.flatMap((model) => {
        const spec = parseRuntimeModelSpec(model.spec)
        if (!model.available || !spec) return []
        return [{
          id: spec,
          label: model.display_name,
          imageInputs: Boolean(model.image_inputs),
          description: t('settings.modelServices.localDescription'),
          vendor: model.service_name,
          vendor_info: t('settings.modelServices.localVendorInfo'),
          recommended: model.recommended,
        }]
      })
      const savedMode = readChatMode()
      const defaultMode = chooseAvailableMode(catalog, savedMode)
      modelsRef.current = catalog
      setModels(catalog)
      if (defaultMode && defaultMode !== savedMode) writeChatMode(defaultMode)
      setMode((current) => chooseAvailableMode(catalog, current, defaultMode))

      try {
        const [capabilityBindings, modelServices] = await Promise.all([
          listModelCapabilityBindings(runtimeConnection),
          listModelServices(runtimeConnection),
        ])
        if (cancelled) return
        const configuredConnections = new Set(
          modelServices.filter((service) => service.credential_configured).map((service) => service.id),
        )
        const imageCatalog: ModelOption[] = localCatalog.flatMap((model) => {
          const spec = parseRuntimeModelSpec(model.spec)
          const imageCapability = model.capabilities.find(
            (capability) => capability.capability === 'image_generation'
              && capability.verification === 'verified',
          )
          if (!spec || !imageCapability || !configuredConnections.has(model.connection_id)) return []
          return [{
            id: spec,
            label: model.display_name,
            imageInputs: false,
            description: t('composer.mode.imageGeneration'),
            vendor: model.service_name,
            vendor_info: t('settings.modelServices.localVendorInfo'),
            recommended: model.recommended,
          }]
        })
        const imageBinding = capabilityBindings.find(
          (binding) => binding.capability === 'image_generation' && binding.status === 'ready',
        )
        const boundImageMode = imageBinding
          ? parseRuntimeModelSpec(imageBinding.model_spec)
          : undefined
        setImageModels(imageCatalog)
        setImageMode(
          boundImageMode && imageCatalog.some((model) => model.id === boundImageMode)
            ? boundImageMode
            : undefined,
        )
      } catch {
        if (!cancelled) {
          setImageModels([])
          setImageMode(undefined)
        }
      }
    }).catch(() => {
      setModels([])
      setImageModels([])
      setImageMode(undefined)
    })
    return () => {
      cancelled = true
    }
  }, [runtimeConnection, modelCatalogVersion, t])

  async function changeImageMode(next: ChatMode): Promise<void> {
    if (!runtimeConnection) return
    setNotice('')
    try {
      const binding = await setModelCapabilityBinding(
        'image_generation',
        { model_spec: next },
        runtimeConnection,
      )
      setImageMode(parseRuntimeModelSpec(binding.model_spec))
    } catch (error) {
      setNotice(error instanceof Error ? error.message : String(error))
    }
  }

  // Runtime owns advanced defaults. Client only projects them into the form;
  // localStorage is intentionally not a competing source of truth.
  useEffect(() => {
    setRuntimeSettingsConfig(null)
    if (!runtime?.online || !runtimeConnection || !hasRuntimeAuthorization(runtimeConnection)) return
    let cancelled = false
    void getRuntimeSettings(runtimeConnection)
      .then((settings) => {
        if (!cancelled) {
          setAgentSettings((current) => ({
            ...current,
            advanced: advancedSettingsFromRuntime(settings),
          }))
          setRuntimeSettingsConfig(runtimeConnection)
        }
      })
      .catch((error) => {
        if (!cancelled) setNotice(error instanceof Error ? error.message : String(error))
      })
    return () => {
      cancelled = true
    }
  }, [runtime?.online, runtimeConnection])

  useEffect(() => {
    writeSidebarWidth(sidebarWidth)
  }, [sidebarWidth])

  useEffect(() => {
    writeSidebarCollapsed(sidebarCollapsed)
  }, [sidebarCollapsed])

  useEffect(() => {
    return () => {
      if (sidebarMotionTimerRef.current) {
        window.clearTimeout(sidebarMotionTimerRef.current)
      }
    }
  }, [])

  /** Mirror the visible sidebar width onto `:root` so the sonner
   *  toaster — which portals to <body> and therefore can't inherit the
   *  `--sidebar-width` set on `.app-shell` — can offset its
   *  horizontal centering to land over the chat area, not the whole
   *  viewport. Collapsed sidebar → 0px; expanded → the same
   *  clamp(190, sidebarWidth, 340) used in styles.css. */
  useEffect(() => {
    const visible = sidebarCollapsed ? 0 : Math.min(maxSidebarWidth, Math.max(minSidebarWidth, sidebarWidth))
    document.documentElement.style.setProperty('--toast-center-offset', `${visible / 2}px`)
  }, [sidebarWidth, sidebarCollapsed])

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
      detachVisibleSend()
      navigationVersionRef.current += 1
      setActiveConversationID(undefined)
      setPendingWorkspace(undefined)
      setPendingProject(undefined)
      setDraft('')
      setMainView('chat')
    })
    return unsubscribe
  }, [])

  useEffect(() => {
    if (!isResizingSidebar) {
      return undefined
    }

    document.body.classList.add('sidebar-resizing')

    function handlePointerMove(event: PointerEvent) {
      const resizeState = sidebarResizeStateRef.current
      if (!resizeState || !Number.isFinite(event.clientX)) {
        return
      }
      const nextWidth = clampSidebarWidth(resizeState.startWidth + event.clientX - resizeState.startX)
      setSidebarWidth(nextWidth)
    }

    function finishResize() {
      sidebarResizeStateRef.current = null
      setIsResizingSidebar(false)
    }

    window.addEventListener('pointermove', handlePointerMove)
    window.addEventListener('pointerup', finishResize)
    window.addEventListener('pointercancel', finishResize)

    return () => {
      document.body.classList.remove('sidebar-resizing')
      window.removeEventListener('pointermove', handlePointerMove)
      window.removeEventListener('pointerup', finishResize)
      window.removeEventListener('pointercancel', finishResize)
    }
  }, [isResizingSidebar])

  useEffect(() => {
    return () => {
      if (liveRenderTimerRef.current !== undefined) {
        window.clearTimeout(liveRenderTimerRef.current)
      }
      pendingConversationRendersRef.current.clear()
    }
  }, [])

  useEffect(() => {
    activeIDRef.current = activeID
  }, [activeID])

  const syncRuntimeThreadCache = useCallback(async (config: RuntimeConnection): Promise<Conversation[]> => {
    const { threads, cursor } = await listLocalThreads(config)
    const nextThreadIDs = new Set(threads.map((thread) => thread.id))
    const removedThreadIDs = [...loadRuntimeThreadIDs()].filter((id) => !nextThreadIDs.has(id))
    await Promise.all(removedThreadIDs.map((id) => localData.delete(id)))
    const existing = new Map((await localData.list()).map((item) => [item.id, item]))
    const snapshots = await mapWithConcurrency(
      threads,
      4,
      (thread) => getLocalThreadSnapshot(thread.id, config),
    )
    const projected = await mapWithConcurrency(
      snapshots,
      4,
      (snapshot) => projectRuntimeThreadCache(
        snapshot,
        existing.get(snapshot.thread.id),
        config,
        t,
      ),
    )
    const saved = await Promise.all(
      projected.map((conversation) => localData.saveRuntimeProjection(conversation)),
    )
    const visibleProjected = projected.filter((_conversation, index) => saved[index])
    storeRuntimeThreadIDs(nextThreadIDs)
    runtimeThreadIDsRef.current = nextThreadIDs
    runtimeThreadCursorRef.current = Math.max(runtimeThreadCursorRef.current, cursor)
    return visibleProjected
  }, [localData, t])

  const settleDeliveredLocalRunCommand = useCallback(async (
    command: PendingRuntimeCommand,
    result: RuntimeCommandResult,
    config: RuntimeConnection,
  ): Promise<boolean> => {
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
      setConversations((items) =>
        projected.reduce((next, conversation) => upsertConversation(next, conversation), items),
      )
      return true
    }
    const run = result as LocalHarnessRun
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
  }, [localData, syncRuntimeThreadCache])

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
      if (projected) setConversations((items) => upsertConversation(items, projected))
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
      setConversations((items) => upsertConversation(items, conversation))
    }
  }, [localData, t])

  useEffect(() => {
    let disposed = false
    const navigationVersion = navigationVersionRef.current
    const maySelectInitialConversation = !conversationInitializationCompleteRef.current
    void localData.list().then((items) => {
      if (disposed) {
        return
      }
      conversationInitializationCompleteRef.current = true
      setConversations((current) => {
        if (!isDesktop) {
          return items
        }
        const merged = new Map(current.map((item) => [item.id, item]))
        for (const item of items) {
          const existing = merged.get(item.id)
          if (!existing || item.updatedAt > existing.updatedAt) {
            merged.set(item.id, item)
          }
        }
        return sortConversationsForSidebar(Array.from(merged.values()))
      })
      if (
        maySelectInitialConversation &&
        navigationVersionRef.current === navigationVersion
      ) {
        setActiveConversationID(items[0]?.id)
      }
      const [failure] = !startupRecoveryNoticeShownRef.current
        ? recentRecoverableFailures(items, 1)
        : []
      if (failure) {
        startupRecoveryNoticeShownRef.current = true
        setNotice(t('app.notice.recoverableFailureAfterRestart'), {
          duration: 8000,
          action: {
            label: t('agent.failureAction.openChat'),
            onClick: () => {
              setActiveConversationID(failure.target.conversationID)
              setMainView('chat')
            },
          },
        })
      }
    })
    return () => {
      disposed = true
    }
  }, [isDesktop, localData, t])

  useEffect(() => {
    const clientBridge = window.shejaneClient
    const config = getRuntimeConnection()
    if (!config) {
      return
    }
    setRuntimeConnection(config)
    let disposed = false
    let polling = false
    let catalogLoaded = false
    if (clientBridge?.runtime?.ready === false) {
      setRuntime({ online: false })
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
          setAuthorizedWorkspaces(workspaces)
          setLocalRuns(runs)
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
        setRuntime(probe)
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
    if (!isDesktop || !runtime?.online || !hasRuntimeAuthorization(runtimeConnection)) {
      return
    }
    let disposed = false
    let retryTimer: number | undefined
    const config = runtimeConnection
    const deliver = async () => {
      try {
        const commands = await localData.listPendingRuntimeCommands()
        if (disposed || commands.length === 0) return
        const report = await deliverPendingRuntimeCommands(
          commands,
          config,
          (command, run) => settleDeliveredLocalRunCommand(command, run, config).then(() => undefined),
        )
        const rejectedSettlements: Promise<void>[] = []
        for (const failure of report.failures) {
          if (!failure.retryable) {
            rejectedSettlements.push(settleRejectedPendingRuntimeCommand(failure, config))
          }
        }
        await Promise.all(rejectedSettlements)
        const blocked = report.failures.find((item) => !item.retryable)
        if (!disposed && blocked) {
          setNotice(runtimeCommandErrorMessage(blocked.error, t))
        }
        if (!disposed && report.failures.some((item) => item.retryable)) {
          retryTimer = window.setTimeout(() => void deliver(), pendingCommandRetryMs)
        }
      } catch {
        if (!disposed) {
          retryTimer = window.setTimeout(() => void deliver(), pendingCommandRetryMs)
        }
      }
    }
    void deliver()
    return () => {
      disposed = true
      if (retryTimer !== undefined) window.clearTimeout(retryTimer)
    }
  }, [
    isDesktop,
    localData,
    pendingCommandDeliveryVersion,
    runtime?.online,
    runtimeConnection,
    settleDeliveredLocalRunCommand,
    settleRejectedPendingRuntimeCommand,
    t,
  ])

  useEffect(() => {
    if (!isDesktop || !runtime?.online || !hasRuntimeAuthorization(runtimeConnection)) {
      return
    }
    let disposed = false
    let polling = false
    let interval: number | undefined
    const applyProjected = (projected: Conversation[], deleted = new Set<string>()) => {
      if (disposed || (projected.length === 0 && deleted.size === 0)) return
      setConversations((current) => {
        const merged = new Map<string, Conversation>()
        for (const item of current) {
          if (!deleted.has(item.id)) merged.set(item.id, item)
        }
        for (const item of projected) merged.set(item.id, item)
        return sortConversationsForSidebar(Array.from(merged.values()))
      })
    }
    const pollChanges = async () => {
      if (polling) return
      polling = true
      try {
        const result = await listLocalThreadChanges(runtimeThreadCursorRef.current, runtimeConnection)
        if (disposed) return
        if (result.resetRequired) {
          const previousThreadIDs = new Set(runtimeThreadIDsRef.current)
          const projected = await syncRuntimeThreadCache(runtimeConnection)
          const deleted = new Set(
            [...previousThreadIDs].filter((threadID) => !runtimeThreadIDsRef.current.has(threadID)),
          )
          applyProjected(projected, deleted)
          return
        }
        const latest = new Map(result.changes.map((change) => [change.thread_id, change]))
        const deleted = new Set(
          [...latest.values()].flatMap((change) =>
            change.change_type === 'thread.deleted' ? [change.thread_id] : []),
        )
        await Promise.all([...deleted].map((threadID) => localData.delete(threadID)))
        const existing = new Map((await localData.list()).map((item) => [item.id, item]))
        const snapshots = await mapWithConcurrency(
          [...latest.keys()].filter((threadID) => !deleted.has(threadID)),
          4,
          (threadID) => getLocalThreadSnapshot(threadID, runtimeConnection),
        )
        const projected = await mapWithConcurrency(
          snapshots,
          4,
          (snapshot) => projectRuntimeThreadCache(
            snapshot,
            existing.get(snapshot.thread.id),
            runtimeConnection,
            t,
          ),
        )
        const saved = await Promise.all(
          projected.map((conversation) => localData.saveRuntimeProjection(conversation)),
        )
        const visibleProjected = projected.filter((_conversation, index) => saved[index])
        const nextRuntimeThreadIDs = new Set(runtimeThreadIDsRef.current)
        for (const threadID of latest.keys()) nextRuntimeThreadIDs.add(threadID)
        for (const threadID of deleted) nextRuntimeThreadIDs.delete(threadID)
        storeRuntimeThreadIDs(nextRuntimeThreadIDs)
        runtimeThreadIDsRef.current = nextRuntimeThreadIDs
        runtimeThreadCursorRef.current = Math.max(runtimeThreadCursorRef.current, result.cursor)
        applyProjected(visibleProjected, deleted)
      } catch {
        // Cursor polling is a cache refresh. The next pass retries from the
        // last committed cursor; it never changes Runtime truth.
      } finally {
        polling = false
      }
    }
    void syncRuntimeThreadCache(runtimeConnection)
      .then((projected) => {
        applyProjected(projected)
        if (!disposed) {
          interval = window.setInterval(() => void pollChanges(), 2000)
        }
      })
      .catch(() => {
        if (!disposed) {
          interval = window.setInterval(() => void pollChanges(), 2000)
        }
      })
    return () => {
      disposed = true
      if (interval !== undefined) window.clearInterval(interval)
    }
  }, [isDesktop, runtime?.online, runtimeConnection, syncRuntimeThreadCache, t])

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
          setLocalRuns(freshRuns)
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

  const activeConversation = conversations.find((conversation) => conversation.id === activeID)
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

  async function refreshConversations(nextActiveID?: string, options: { preserveEmptyActive?: boolean } = {}) {
    const items = await localData.list()
    setConversations(items)
    setActiveConversationID(nextActiveID ?? (options.preserveEmptyActive ? undefined : items[0]?.id))
  }

  async function submitPluginCommand(command: PendingPluginCommand): Promise<RuntimeCommandResult> {
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
      if (failure.retryable) {
        setPendingCommandDeliveryVersion((version) => version + 1)
      } else {
        await localData.deletePendingRuntimeCommand(command.commandId)
      }
      throw failure.error
    }
    if (!result) throw new Error('plugin command completed without a receipt')
    return result
  }

  function startNewConversation() {
    detachVisibleSend()
    navigationVersionRef.current += 1
    setActiveConversationID(undefined)
    setPendingWorkspace(undefined)
    setPendingProject(undefined)
    setPendingAttachments([])
    setDraft('')
    setMainView('chat')
  }

  function selectConversation(id: string) {
    detachVisibleSend()
    navigationVersionRef.current += 1
    setPendingWorkspace(undefined)
    setPendingProject(undefined)
    setPendingAttachments([])
    setActiveConversationID(id)
    setMainView('chat')
  }

  function setActiveConversationID(nextActiveID: string | undefined) {
    activeIDRef.current = nextActiveID
    setActiveID(nextActiveID)
    if (!nextActiveID) {
      setMode(chooseAvailableMode(modelsRef.current, readChatMode()))
      return
    }
    void localData.get(nextActiveID).then((conversation) => {
      if (activeIDRef.current === nextActiveID) {
        setMode(chooseAvailableMode(modelsRef.current, conversation?.model ?? '', readChatMode()))
      }
    })
  }

  function createConversationRenderContext(): ConversationRenderContext {
    return { navigationVersionAtStart: navigationVersionRef.current }
  }

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

  async function refreshConversationsAfterStream(conversationID: string, context: ConversationRenderContext) {
    const userNavigatedWhileStreaming = navigationVersionRef.current !== context.navigationVersionAtStart
    await refreshConversations(userNavigatedWhileStreaming ? activeIDRef.current : conversationID, {
      preserveEmptyActive: userNavigatedWhileStreaming && !activeIDRef.current,
    })
  }

  function scheduleConversationRender(conversation: Conversation, context: ConversationRenderContext) {
    pendingConversationRendersRef.current.set(conversation.id, {
      conversation: cloneConversation(conversation),
      context,
    })
    if (liveRenderTimerRef.current !== undefined) {
      return
    }
    liveRenderTimerRef.current = window.setTimeout(() => {
      liveRenderTimerRef.current = undefined
      const pending = Array.from(pendingConversationRendersRef.current.values())
      pendingConversationRendersRef.current.clear()
      if (!pending.length) {
        return
      }
      setConversations((items) => pending.reduce((nextItems, item) => upsertConversation(nextItems, item.conversation), items))
      const focusTarget = pending.find(
        (item) =>
          activeIDRef.current === item.conversation.id ||
          navigationVersionRef.current === item.context.navigationVersionAtStart,
      )
      if (focusTarget) {
        setActiveConversationID(focusTarget.conversation.id)
      }
    }, 33)
  }

  async function sendMessage() {
    const content = draft
    const attachments = pendingAttachments
    const sendingOperation = beginVisibleSend()
    setNotice('')
    setDraft('')
    setPendingAttachments([])
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
        activeIDRef.current,
        attachments,
      )
      await refreshConversationsAfterStream(conversation.id, renderContext)
    } catch (error) {
      setDraft((current) => current || content)
      if (navigationVersionRef.current === renderContext.navigationVersionAtStart) {
        setPendingAttachments((current) => mergeAttachments(current, attachments))
      }
      setNotice(error instanceof Error ? error.message : t('app.notice.sendFailed'))
      const userNavigatedWhileStreaming = navigationVersionRef.current !== renderContext.navigationVersionAtStart
      await refreshConversations(userNavigatedWhileStreaming ? activeIDRef.current : activeID, {
        preserveEmptyActive: userNavigatedWhileStreaming && !activeIDRef.current,
      })
    } finally {
      finishVisibleSend(sendingOperation)
    }
  }

  /** Optimistically truncate the cache, then ask Runtime to create an
   *  immutable replacement branch from `userMessageID`. The authoritative
   *  snapshot later confirms the visible branch. */
  async function resendFromUserMessage(
    userMessageID: string,
    text: string,
    preferLocal: boolean,
    localRunOptions?: LocalHarnessRunOptions,
    targetConversationID = activeIDRef.current,
    preservePriorTurns = false,
  ) {
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
      if (activeIDRef.current !== targetConversationID) {
        detachVisibleSend()
        navigationVersionRef.current += 1
        setPendingWorkspace(undefined)
        setPendingProject(undefined)
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
    if (!runRuntimeConnection) {
      throw new Error(t('app.notice.runtimeDisconnected'))
    }
    if (!runtimeConnection) {
      setRuntimeConnection(runRuntimeConnection)
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
    const commandId = createLocalID('cmd')
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
    await localData.saveWithPendingRuntimeCommand(conversation, pendingCommand)

    let keepConversation = true
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
      setLocalRuns((items) => upsertLocalRun(items, run))
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
    } catch {
      setPendingCommandDeliveryVersion((version) => version + 1)
      assistantMessage.status = assistantMessage.runId ? 'streaming' : 'pending'
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

  /** Stop whatever cancelable run is currently active for the active
   *  conversation. Local runtime runs emit `run.canceled` on their SSE
   *  channel. */
  const cancelActiveRun = useCallback(async () => {
    if (!activeConversation) {
      return
    }
    // Most-recent assistant message that's still cancelable — that's the
    // in-flight run from the user's PoV.
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
    try {
      if (!runtimeConnection) {
        return
      }
      const existing = (await localData.listPendingRuntimeCommands()).find(
        (command): command is PendingRunCancelCommand =>
          command.type === 'run.cancel' && command.input.runId === activeMessage.runId,
      )
      const command = existing ?? {
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
        setPendingCommandDeliveryVersion((version) => version + 1)
        throw error
      }
    } catch (error) {
      setNotice(error instanceof Error ? error.message : t('app.notice.sendFailed'))
    }
  }, [activeConversation, localData, runtimeConnection, settleDeliveredLocalRunCommand, t])

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

  async function appendInstructionToActiveRun() {
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
    try {
      const existing = (await localData.listPendingRuntimeCommands()).find(
        (command): command is PendingRunInjectCommand =>
          command.type === 'run.inject' &&
          command.input.runId === activeMessage.runId &&
          command.input.content === content,
      )
      const command = existing ?? {
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
      setPendingCommandDeliveryVersion((version) => version + 1)
      setDraft((current) => current || content)
      setNotice(error instanceof Error ? error.message : t('app.notice.steeringFailed'))
    }
  }

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

  async function handlePermissionDecisionOnce(
    messageID: string,
    requestID: string,
    decision: 'approve' | 'edit' | 'deny',
    scope: LocalPermissionScope,
    editedAction?: { name: string, args: Record<string, unknown> },
  ): Promise<boolean> {
    const conversationID = activeIDRef.current
    if (!conversationID || !runtimeConnection) {
      setNotice(t('app.notice.runtimeDisconnected'))
      return false
    }
    const persistedConversation = await localData.get(conversationID)
    const visibleConversation = conversations.find((item) => item.id === conversationID)
    const findPermissionMessage = (conversation: Conversation | undefined) =>
      conversation?.messages.find((item) => item.id === messageID)
      ?? conversation?.messages.find((item) =>
        item.agentEvents?.some((event) => event.permissionRequestId === requestID),
      )
    const sourceConversation = [persistedConversation, visibleConversation].find(
      (candidate) => Boolean(findPermissionMessage(candidate)?.runId),
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
    try {
      const existing = (await localData.listPendingRuntimeCommands()).find(
        (command): command is PendingPermissionResolveCommand =>
          command.type === 'permission.resolve' && command.input.permissionId === requestID,
      )
      const command = existing ?? {
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
        setPendingCommandDeliveryVersion((version) => version + 1)
        throw error
      }
      // Decision-acknowledgement toast so the user sees their click landed —
      // the bar disappears the moment the resume stream starts, otherwise
      // there's no feedback at all.
      toast.success(
        command.input.decision === 'approve' || command.input.decision === 'edit'
          ? t(command.input.scope === 'run' ? 'app.notice.permissionRunApproved' : 'app.notice.permissionApproved')
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
          (event) => !(
            event.type === 'ui.permission_decision_pending'
            && event.permissionRequestId === requestID
          ),
        )
      }
      setNotice(error instanceof Error ? error.message : t('app.notice.localPermissionFailed'))
      scheduleConversationRender(conversation, renderContext)
    } finally {
      conversation.updatedAt = new Date().toISOString()
      await localData.save(conversation)
      await refreshConversationsAfterStream(conversation.id, renderContext)
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

  async function handleToolReconciliationOnce(
    messageID: string,
    requestID: string,
    decision: LocalToolReconciliationDecision,
  ) {
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
    try {
      const existing = (await localData.listPendingRuntimeCommands()).find(
        (command): command is PendingToolReconcileCommand =>
          command.type === 'tool.reconcile' && command.input.operationId === requestID,
      )
      const command = existing ?? {
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
        setPendingCommandDeliveryVersion((version) => version + 1)
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
      setNotice(error instanceof Error ? error.message : t('app.notice.localPermissionFailed'))
      scheduleConversationRender(conversation, renderContext)
    } finally {
      conversation.updatedAt = new Date().toISOString()
      await localData.save(conversation)
      await refreshConversationsAfterStream(conversation.id, renderContext)
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

  async function handleQuestionAnswerOnce(messageID: string, requestID: string, answers: Record<string, string[]>) {
    const conversationID = activeIDRef.current
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
    const conversation = selectedConversation === visibleConversation && visibleConversation
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
    try {
      const existing = (await localData.listPendingRuntimeCommands()).find(
        (command): command is PendingQuestionAnswerCommand =>
          command.type === 'question.answer' && command.input.questionId === requestID,
      )
      const command = existing ?? {
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
        setPendingCommandDeliveryVersion((version) => version + 1)
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
      setNotice(error instanceof Error ? error.message : t('app.notice.localPermissionFailed'))
      scheduleConversationRender(conversation, renderContext)
    } finally {
      conversation.updatedAt = new Date().toISOString()
      await localData.save(conversation)
      await refreshConversationsAfterStream(conversation.id, renderContext)
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

  async function handlePlanApprovalDecisionOnce(
    messageID: string,
    requestID: string,
    decision: LocalPlanApprovalDecision,
    instructions?: string,
  ) {
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
    try {
      const existing = (await localData.listPendingRuntimeCommands()).find(
        (command): command is PendingPlanResolveCommand =>
          command.type === 'plan.resolve' && command.input.approvalId === requestID,
      )
      const command = existing ?? {
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
        setPendingCommandDeliveryVersion((version) => version + 1)
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
      setNotice(error instanceof Error ? error.message : t('app.notice.localPermissionFailed'))
      scheduleConversationRender(conversation, renderContext)
    } finally {
      conversation.updatedAt = new Date().toISOString()
      await localData.save(conversation)
      await refreshConversationsAfterStream(conversation.id, renderContext)
    }
  }

  async function openLocalArtifact(artifactID: string) {
    if (!runtimeConnection) {
      setNotice(t('app.notice.runtimeDisconnected'))
      return
    }
    setNotice('')
    try {
      setArtifactPreview(await getLocalArtifact(artifactID, runtimeConnection))
    } catch (error) {
      setNotice(error instanceof Error ? error.message : t('app.notice.artifactReadFailed'))
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
      setLocalRuns(freshRuns)
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

  /** Composer's project-picker handler — opens the OS directory picker
   *  and binds the chosen workspace as this chat's project. Two paths:
   *
   *  - **No active conversation yet** (user clicked "新对话" but hasn't
   *    sent the first message): stash the project + workspace as
   *    pending. The next `sendMessage` will pick them up when it
   *    creates the conversation, so the user sees the locked chip in
   *    the composer immediately without us writing an empty chat to
   *    IndexedDB.
   *
   *  - **Active conversation exists**: bind workspace + project to it
   *    in-place. The user can explicitly remove that binding before
   *    choosing another directory for a later Run.
   *
   *  Returns silently if the user cancels the OS picker. Surfaces a
   *  toast on runtime-side errors (e.g. not yet paired). */
  async function selectProjectForActiveConversation(recoveryTarget?: RecoveryTarget) {
    const config = runtimeConnection ?? getRuntimeConnection()
    if (!hasRuntimeAuthorization(config)) {
      setNotice(t('app.notice.runtimeNotPairedAuthorize'))
      return
    }
    if (!runtimeConnection) {
      setRuntimeConnection(config)
    }
    const targetConversationID = recoveryTarget?.conversationID ?? activeIDRef.current
    const picked = await chooseWorkspaceDirectory()
    if (!picked) return
    try {
      const ws = await authorizeLocalWorkspace(picked, config)
      setAuthorizedWorkspaces((items) => upsertWorkspace(items, ws))
      const name = pathBasename(ws.path) || ws.label || ws.path
      const workspace: ConversationWorkspace = {
        path: ws.path,
        label: ws.label,
        authorized: true,
        authorizationId: ws.id,
      }
      const project: ConversationProject = { name }
      if (targetConversationID) {
        await updateConversationMetadata(targetConversationID, (item) => {
          item.project = project
          item.workspace = workspace
        })
      } else {
        setPendingWorkspace(workspace)
        setPendingProject(project)
      }
      if (recoveryTarget) {
        setNotice(t('app.notice.workspaceBound', { label: name }))
        await retryRecoveryTarget(recoveryTarget)
        return
      }
      setNotice(t('project.notice.bound', { name }))
    } catch (err) {
      setNotice(err instanceof Error ? err.message : t('app.notice.workspaceAuthorizeFailed'))
    }
  }

  async function removeProjectFromActiveConversation() {
    if (isSending || hasActiveRun) return
    const conversationID = activeIDRef.current
    if (!conversationID) {
      setPendingWorkspace(undefined)
      setPendingProject(undefined)
      return
    }
    await updateConversationMetadata(conversationID, (conversation) => {
      delete conversation.workspace
      delete conversation.project
    })
  }

  async function selectAttachments() {
    if (isSending || hasActiveRun) return
    const paths = await window.shejaneClient?.selectAttachmentFiles?.()
    addAttachmentPaths(paths ?? [])
  }

  function addAttachmentPaths(paths: string[]) {
    if (!paths.length) return
    setPendingAttachments((current) => mergeAttachments(
      current,
      paths.map((path) => ({ path, name: pathBasename(path) || path })),
    ))
  }

  function dropAttachments(files: File[]) {
    if (isSending || hasActiveRun) return
    const getPathForFile = window.shejaneClient?.getPathForFile
    if (!getPathForFile) return
    addAttachmentPaths(files.flatMap((file) => {
      try {
        const path = getPathForFile(file)
        return path ? [path] : []
      } catch {
        return []
      }
    }))
  }

  function removeAttachment(path: string) {
    if (isSending || hasActiveRun) return
    setPendingAttachments((items) => items.filter((item) => item.path !== path))
  }

  async function authorizeWorkspace(path: string): Promise<LocalWorkspaceAuthorization> {
    if (!hasRuntimeAuthorization(runtimeConnection)) {
      throw new Error(t('app.notice.runtimeNotPairedAuthorize'))
    }
    const nextPath = path.trim()
    if (!nextPath) {
      throw new Error(t('app.notice.emptyWorkspacePath'))
    }
    const workspace = await authorizeLocalWorkspace(nextPath, runtimeConnection)
    setAuthorizedWorkspaces((items) => upsertWorkspace(items, workspace))
    await saveActiveConversationWorkspace({
      path: workspace.path,
      label: workspace.label,
      authorized: true,
      authorizationId: workspace.id,
    })
    setNotice(t('app.notice.workspaceBound', { label: workspace.label }))
    return workspace
  }

  async function diagnoseWorkspace(path: string): Promise<LocalWorkspaceDiagnosis> {
    if (!hasRuntimeAuthorization(runtimeConnection)) {
      throw new Error(t('app.notice.runtimeNotPairedDiagnose'))
    }
    const nextPath = path.trim()
    if (!nextPath) {
      throw new Error(t('app.notice.emptyWorkspacePath'))
    }
    return diagnoseLocalWorkspace(nextPath, runtimeConnection)
  }

  async function saveActiveConversationWorkspace(workspace: ConversationWorkspace | undefined) {
    if (!activeID) {
      setPendingWorkspace(workspace)
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
    setConversations((items) => upsertConversation(items, cloneConversation(conversation)))
  }

  async function updateConversationMetadata(
    conversationID: string,
    update: (conversation: Conversation) => void,
    options: { touch?: boolean } = {},
  ): Promise<Conversation | undefined> {
    const conversation = await localData.get(conversationID)
    if (!conversation) {
      setNotice(t('app.notice.conversationMissing'))
      return undefined
    }
    update(conversation)
    if (options.touch ?? true) {
      conversation.updatedAt = new Date().toISOString()
    }
    const runtimeOwnsThread = runtimeThreadIDsRef.current.has(conversationID)
    if (runtimeOwnsThread && hasRuntimeAuthorization(runtimeConnection)) {
      try {
        await updateLocalThread(
          conversationID,
          {
            title: conversation.title,
            archived: conversation.archived,
            metadata: {
              pinned: conversation.pinned ?? false,
              model: conversation.model,
              project: conversation.project,
              workspace: conversation.workspace,
            },
          },
          runtimeConnection,
        )
      } catch (error) {
        setNotice(error instanceof Error ? error.message : t('app.notice.localRunFailed'))
        return undefined
      }
    }
    await localData.save(conversation)
    await refreshConversations(activeIDRef.current, { preserveEmptyActive: !activeIDRef.current })
    return conversation
  }

  async function togglePinConversation(conversationID: string) {
    const conversation = await updateConversationMetadata(
      conversationID,
      (item) => {
        item.pinned = !item.pinned
      },
      { touch: false },
    )
    if (conversation) {
      setNotice(t(conversation.pinned ? 'app.notice.conversationPinned' : 'app.notice.conversationUnpinned', { title: conversation.title }))
    }
  }

  async function renameConversation(conversationID: string, title: string) {
    const nextTitle = title.trim()
    if (!nextTitle) {
      return
    }
    const conversation = await updateConversationMetadata(conversationID, (item) => {
      item.title = nextTitle
    })
    if (conversation) {
      setNotice(t('app.notice.conversationRenamed', { title: conversation.title }))
    }
  }

  async function deleteConversationData(conversationID: string) {
    const conversation = await localData.get(conversationID)
    if (!conversation) {
      setNotice(t('app.notice.conversationMissing'))
      return
    }
    const deletedActive = activeIDRef.current === conversationID
    const runtimeOwnsThread = runtimeThreadIDsRef.current.has(conversationID)
    if (runtimeOwnsThread && hasRuntimeAuthorization(runtimeConnection)) {
      try {
        await deleteLocalThread(conversationID, runtimeConnection)
        const nextRuntimeThreadIDs = new Set(runtimeThreadIDsRef.current)
        nextRuntimeThreadIDs.delete(conversationID)
        storeRuntimeThreadIDs(nextRuntimeThreadIDs)
        runtimeThreadIDsRef.current = nextRuntimeThreadIDs
      } catch (error) {
        setNotice(error instanceof Error ? error.message : t('app.notice.localRunFailed'))
        return
      }
    }
    pendingConversationRendersRef.current.delete(conversationID)
    await localData.delete(conversationID)
    if (deletedActive) {
      setPendingWorkspace(undefined)
      setPendingProject(undefined)
    }
    await refreshConversations(deletedActive ? undefined : activeIDRef.current, {
      preserveEmptyActive: !deletedActive && !activeIDRef.current,
    })
    setNotice(t('app.notice.conversationDeleted', { title: conversation.title }))
  }

  async function exportConversationData(conversationID: string) {
    const conversation = await localData.get(conversationID)
    if (!conversation) {
      setNotice(t('app.notice.conversationMissing'))
      return
    }
    const payload = {
      version: 1,
      exportedAt: new Date().toISOString(),
      conversations: [conversation],
    } as const
    const url = URL.createObjectURL(new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' }))
    const link = document.createElement('a')
    link.href = url
    link.download = `shejane-conversation-${safeFilename(conversation.title)}-${new Date().toISOString().slice(0, 10)}.json`
    link.click()
    URL.revokeObjectURL(url)
    setNotice(t('app.notice.conversationExported', { title: conversation.title }))
  }

  async function importLocalData(file: File | undefined) {
    if (!file) {
      return
    }
    const modelServices = await localData.importAll(await file.text())
    if (runtimeConnection && modelServices.length > 0) {
      const existing = new Set(
        (await listModelServices(runtimeConnection)).map((service) => service.id),
      )
      for (const service of modelServices) {
        if (
          service.preset_id !== 'custom'
          && service.preset_id !== 'shejane-official'
          && service.region !== 'official'
          && !existing.has(service.id)
        ) {
          await importModelService({ ...service, region: service.region }, runtimeConnection)
        }
      }
      setModelCatalogVersion((current) => current + 1)
    }
    await refreshConversations()
    setNotice(t('app.notice.localDataImported'))
  }

  async function exportLocalData() {
    const modelServices: ExportedModelService[] = runtimeConnection
      ? (await listModelServices(runtimeConnection)).map((service) => ({
          id: service.id,
          preset_id: service.preset_id,
          name: service.name,
          region: service.region,
          adapter_id: service.adapter_id,
          base_url: service.base_url,
          models: service.models,
        }))
      : []
    const conversationExport = await localData.exportAll(modelServices)
    const payload = {
      ...conversationExport,
      settings: {
        agentSettings,
        chatMode: mode,
        locale,
      },
    }
    const url = URL.createObjectURL(new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' }))
    const link = document.createElement('a')
    link.href = url
    link.download = `shejane-local-data-${new Date().toISOString().slice(0, 10)}.json`
    link.click()
    URL.revokeObjectURL(url)
    setNotice(t('app.notice.localDataExported'))
  }

  async function refreshCurrentModel() {
    const selected = parseRuntimeModelSpec(mode)
    const connectionID = selected?.split(':', 3)[1]
    if (!runtimeConnection || !connectionID) return
    try {
      await refreshModelService(connectionID, runtimeConnection)
      setModelCatalogVersion((current) => current + 1)
    } catch {
      // Cached models remain visible; settings exposes the actionable error.
    }
  }

  // The renderer is always hosted by Electron; Runtime is its only execution backend.
  const shellClassName = isDesktop ? 'app-window-shell electron-window-shell' : 'app-window-shell'
  const appShellStyle = { '--sidebar-width': `${sidebarWidth}px` } as CSSProperties
  const shortcutModifier = keyboardShortcutModifier()
  const shortcutRows = [
    { label: t('shortcuts.newChat'), keys: [`${shortcutModifier}N`] },
    { label: t('shortcuts.searchChats'), keys: [`${shortcutModifier}K`] },
    { label: t('shortcuts.stopRun'), keys: ['Esc'] },
    { label: t('shortcuts.help'), keys: ['?'] },
  ]

  function beginSidebarResize(event: ReactPointerEvent<HTMLDivElement>) {
    if (event.pointerType === 'mouse' && event.button !== 0) {
      return
    }
    if (!Number.isFinite(event.clientX)) {
      return
    }
    event.preventDefault()
    sidebarResizeStateRef.current = {
      startX: event.clientX,
      startWidth: sidebarWidth,
    }
    setIsResizingSidebar(true)
  }

  function handleSidebarResizeKeyDown(event: ReactKeyboardEvent<HTMLDivElement>) {
    if (event.key === 'ArrowLeft') {
      event.preventDefault()
      setSidebarWidth((current) => clampSidebarWidth(current - sidebarKeyboardStep))
      return
    }
    if (event.key === 'ArrowRight') {
      event.preventDefault()
      setSidebarWidth((current) => clampSidebarWidth(current + sidebarKeyboardStep))
      return
    }
    if (event.key === 'Home') {
      event.preventDefault()
      setSidebarWidth(minSidebarWidth)
      return
    }
    if (event.key === 'End') {
      event.preventDefault()
      setSidebarWidth(maxSidebarWidth)
    }
  }

  function collapseSidebar() {
    if (sidebarMotionTimerRef.current) {
      window.clearTimeout(sidebarMotionTimerRef.current)
    }
    setSidebarMotion('closing')
    setSidebarCollapsed(true)
    sidebarMotionTimerRef.current = window.setTimeout(() => setSidebarMotion('idle'), sidebarMotionMs)
  }

  function expandSidebar() {
    if (sidebarMotionTimerRef.current) {
      window.clearTimeout(sidebarMotionTimerRef.current)
    }
    setSidebarMotion('opening')
    setSidebarCollapsed(false)
    sidebarMotionTimerRef.current = window.setTimeout(() => setSidebarMotion('idle'), sidebarMotionMs)
  }

  return {
    activeConversation,
    activeDocument,
    activeID,
    activeWorkspace,
    agentSettings,
    appShellStyle,
    appendInstructionToActiveRun,
    artifactPreview,
    beginSidebarResize,
    cancelActiveRun,
    changeAgentSettings,
    changeImageMode,
    changeMode,
    collapseSidebar,
    conversations,
    conversationSidebarRef,
    deleteConversationData,
    docPreviewRefreshKey,
    draft,
    dropAttachments,
    expandSidebar,
    exportConversationData,
    exportLocalData,
    exportLocalRunDiagnostics,
    handleAgentFailureAction,
    handleDeleteMessage,
    handleEditResendMessage,
    handlePermissionDecision,
    handlePlanApprovalDecision,
    handleQuestionAnswer,
    handleRegenerateMessage,
    handleSidebarResizeKeyDown,
    handleToolReconciliation,
    hasActiveRun,
    importLocalData,
    isDesktop,
    isResizingSidebar,
    isSending,
    imageMode,
    imageModels,
    keyboardHelpOpen,
    listInstalledSkillsForView,
    listMcpServersForView,
    listPluginsForView,
    mainView,
    mode,
    modelRequiredOpen,
    modelServiceAddRequested,
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
    pluginCatalogVersion,
    pluginsTab,
    removeAttachment,
    removeProjectFromActiveConversation,
    refreshCurrentModel,
    renameConversation,
    runtime,
    runtimeConnection,
    runtimeSettingsConfig,
    selectAttachments,
    selectConversation,
    selectProjectForActiveConversation,
    sendMessage,
    setActiveDocument,
    setAgentSettings,
    setArtifactPreview,
    setDraft,
    setKeyboardHelpOpen,
    setMainView,
    setModelRequiredOpen,
    setModelServiceAddRequested,
    setModelCatalogVersion,
    setPendingDeleteMessageID,
    setPendingDiagnosticsRunID,
    setPermissionMode,
    setPluginsTab,
    shellClassName,
    shortcutRows,
    showLocalFileContextMenu,
    sidebarCollapsed,
    sidebarMotion,
    sidebarWidth,
    startNewConversation,
    submitPluginCommand,
    t,
    togglePinConversation,
  }
}

type AppContentViewModel = ReturnType<typeof useAppContentViewModel>

function AppContent() {
  return <AppContentView view={useAppContentViewModel()} />
}

function AppContentView({ view }: { view: AppContentViewModel }) {
  const {
    activeID,
    agentSettings,
    appShellStyle,
    beginSidebarResize,
    changeAgentSettings,
    collapseSidebar,
    conversations,
    conversationSidebarRef,
    deleteConversationData,
    expandSidebar,
    exportConversationData,
    exportLocalData,
    handleSidebarResizeKeyDown,
    importLocalData,
    isDesktop,
    isResizingSidebar,
    keyboardHelpOpen,
    listInstalledSkillsForView,
    listMcpServersForView,
    listPluginsForView,
    mainView,
    modelServiceAddRequested,
    pluginCatalogVersion,
    pluginsTab,
    renameConversation,
    runtime,
    runtimeConnection,
    runtimeSettingsConfig,
    selectConversation,
    setAgentSettings,
    setKeyboardHelpOpen,
    setMainView,
    setModelServiceAddRequested,
    setModelCatalogVersion,
    setPluginsTab,
    shellClassName,
    shortcutRows,
    sidebarCollapsed,
    sidebarMotion,
    sidebarWidth,
    startNewConversation,
    submitPluginCommand,
    t,
    togglePinConversation,
  } = view

  const runtimeAssetControls = useMemo(() => runtimeConnection ? {
    getStatus: (pluginID: FixedRuntimeAssetPluginID) => (
      getLocalFixedRuntimeAssetStatus(pluginID, runtimeConnection)
    ),
    download: (pluginID: FixedRuntimeAssetPluginID) => (
      prepareLocalFixedRuntimeAsset(pluginID, runtimeConnection)
    ),
    remove: (pluginID: FixedRuntimeAssetPluginID) => (
      removeLocalFixedRuntimeAsset(pluginID, runtimeConnection)
    ),
    getStorage: () => getLocalRuntimeAssetStorage(runtimeConnection),
    cleanup: (scope: 'history' | 'all') => (
      cleanupLocalRuntimeAssetStorage(scope, runtimeConnection)
    ),
  } : null, [runtimeConnection])

  return (
    <TooltipProvider>
      <main className={shellClassName}>
        <div className="window-drag-layer" aria-hidden="true" />
        <div
          className="app-shell"
          style={appShellStyle}
          data-collapsed={sidebarCollapsed ? 'true' : undefined}
          data-sidebar-motion={sidebarMotion === 'idle' ? undefined : sidebarMotion}
        >
          <ConversationSidebar
            ref={conversationSidebarRef}
            conversations={conversations}
            activeID={activeID}
            onNewConversation={startNewConversation}
            onSelectConversation={selectConversation}
            onExportConversation={(conversationID) => void exportConversationData(conversationID)}
            onImportLocalData={(file) => void importLocalData(file)}
            onTogglePinConversation={(conversationID) => void togglePinConversation(conversationID)}
            onRenameConversation={(conversationID, title) => void renameConversation(conversationID, title)}
            onDeleteConversation={(conversationID) => void deleteConversationData(conversationID)}
            onCollapseSidebar={collapseSidebar}
            isDesktop={isDesktop}
            onOpenPlugins={() => setMainView('plugins')}
            onOpenSettings={() => setMainView('settings')}
            activeView={mainView}
            resizeHandle={(
              <div
                className="sidebar-resize-handle"
                role="separator"
                aria-label={t('app.resizeSidebar')}
                aria-orientation="vertical"
                aria-valuemin={minSidebarWidth}
                aria-valuemax={maxSidebarWidth}
                aria-valuenow={sidebarWidth}
                data-resizing={isResizingSidebar ? 'true' : undefined}
                tabIndex={0}
                onKeyDown={handleSidebarResizeKeyDown}
                onPointerDown={beginSidebarResize}
              />
            )}
          />

          {sidebarCollapsed ? (
            <div className="topbar-expand-hotspot">
              <button
                type="button"
                className="topbar-expand-button"
                title={t('app.expandSidebar')}
                aria-label={t('app.expandSidebar')}
                onClick={expandSidebar}
              >
                <IconLayoutSidebarLeftExpand size={16} aria-hidden="true" />
              </button>
            </div>
          ) : null}

          <div className="view-transition">
          <Suspense fallback={null}>
          {mainView === 'plugins' ? (
            <PluginsHub activeTab={pluginsTab} onTabChange={setPluginsTab}>
            <Suspense fallback={null}>
            {pluginsTab === 'skills' ? (
            <SkillsView
              embedded
              listInstalled={listInstalledSkillsForView}
              onCreateSkill={async (input) => {
                if (!runtimeConnection) return
                await createLocalSkill(input, runtimeConnection)
              }}
              onLoadSkill={(name) => {
                if (!runtimeConnection) return Promise.reject(new Error('Runtime unavailable'))
                return getLocalSkillFile(name, runtimeConnection)
              }}
              onUpdateSkill={async (name, input) => {
                if (!runtimeConnection) return
                await updateLocalSkill(name, input, runtimeConnection)
              }}
              onDeleteSkill={async (name) => {
                if (!runtimeConnection) return
                await deleteLocalSkill(name, runtimeConnection)
              }}
              onOpenFolder={(path) => {
                const bridge = window.shejaneClient
                if (bridge?.openFileWithDefaultApp) {
                  void bridge.openFileWithDefaultApp(path)
                }
              }}
            />
          ) : pluginsTab === 'plugins' ? (
            <PluginsView
              embedded
              refreshVersion={pluginCatalogVersion}
              listPlugins={listPluginsForView}
              selectPackage={() => window.shejaneClient?.selectPluginPackage?.() ?? Promise.resolve(undefined)}
              installPlugin={(sourcePath, allowUnsigned) => {
                const commandId = createLocalID('cmd')
                return submitPluginCommand({
                  type: 'plugin.install',
                  commandId,
                  createdAt: new Date().toISOString(),
                  input: { sourcePath, allowUnsigned },
                })
              }}
              setEnabled={(plugin, enabled) => {
                const commandId = createLocalID('cmd')
                return submitPluginCommand({
                  type: enabled ? 'plugin.enable' : 'plugin.disable',
                  commandId,
                  createdAt: new Date().toISOString(),
                  input: { pluginId: plugin.id, expectedDigest: plugin.digest },
                })
              }}
              getReadiness={(plugin) => {
                if (!runtimeConnection) return Promise.reject(new Error('Runtime unavailable'))
                return getLocalPluginReadiness(plugin.id, runtimeConnection)
              }}
              advanceSetup={(plugin, readiness, actionId) => {
                if (!runtimeConnection) return Promise.reject(new Error('Runtime unavailable'))
                const commandId = createLocalID('cmd')
                return advanceLocalPluginSetupCommand(
                  commandId,
                  plugin.id as 'org.shejane.computer-use',
                  readiness.revision,
                  actionId,
                  runtimeConnection,
                )
              }}
              removePlugin={(plugin) => {
                const commandId = createLocalID('cmd')
                return submitPluginCommand({
                  type: 'plugin.remove',
                  commandId,
                  createdAt: new Date().toISOString(),
                  input: { pluginId: plugin.id, expectedDigest: plugin.digest },
                })
              }}
            />
          ) : (
            <MCPView
              embedded
              listCatalog={listMcpServersForView}
              disabledServers={agentSettings.mcpDisabled}
              onDisabledChange={(next) => {
                const updated: Required<AgentSettings> = { ...agentSettings, mcpDisabled: next }
                setAgentSettings(updated)
                writeAgentSettings(updated)
              }}
              onCreateServer={async (input) => {
                if (!runtimeConnection) return
                await createMcpServer(input, runtimeConnection)
              }}
              onUpdateServer={async (name, input) => {
                if (!runtimeConnection) return
                await updateMcpServer(name, input, runtimeConnection)
              }}
              onDeleteServer={async (name) => {
                if (!runtimeConnection) return
                await deleteMcpServer(name, runtimeConnection)
              }}
              onOpenFolder={(path) => {
                const bridge = window.shejaneClient
                if (bridge?.openFileWithDefaultApp) {
                  void bridge.openFileWithDefaultApp(path)
                }
              }}
            />
          )}
            </Suspense>
            </PluginsHub>
          ) : mainView === 'settings' ? (
            <SettingsView
              isDesktop={isDesktop}
              agentSettings={agentSettings}
              advancedSettingsReady={runtimeSettingsConfig === runtimeConnection && Boolean(runtime?.online)}
              runtimeConnection={runtimeConnection}
              getRuntimeAssetStatus={runtimeAssetControls?.getStatus}
              onDownloadRuntimeAsset={runtimeAssetControls?.download}
              onRemoveRuntimeAsset={runtimeAssetControls?.remove}
              getRuntimeAssetStorage={runtimeAssetControls?.getStorage}
              onCleanupRuntimeAssets={runtimeAssetControls?.cleanup}
              openModelServiceAdd={modelServiceAddRequested}
              onModelServiceAddOpened={() => setModelServiceAddRequested(false)}
              onModelServicesChange={() => setModelCatalogVersion((version) => version + 1)}
              onAgentSettingsChange={(next) => {
                changeAgentSettings(next)
              }}
              onImportLocalData={(file) => void importLocalData(file)}
              onExportLocalData={() => void exportLocalData()}
              onClearMemory={
                runtimeConnection
                  ? async () => {
                      try {
                        const result = await clearLocalMemory(runtimeConnection)
                        toast.success(t('app.notice.memoryCleared', { count: result.deleted_count }), {
                          id: appNoticeToastID,
                        })
                        return result.deleted_count
                      } catch (error) {
                        const message = error instanceof Error ? error.message : String(error)
                        toast.error(t('app.notice.memoryClearFailed', { message }), { id: appNoticeToastID })
                        throw error
                      }
                    }
                  : undefined
              }
            />
          ) : (
            <AppChatWorkspace view={view} />
          )}
          </Suspense>
          </div>
          <Dialog open={keyboardHelpOpen} onOpenChange={setKeyboardHelpOpen}>
            <DialogContent className="keyboard-shortcuts-dialog sm:max-w-[420px]">
              <DialogHeader>
                <DialogTitle>{t('shortcuts.title')}</DialogTitle>
                <DialogDescription>{t('shortcuts.description')}</DialogDescription>
              </DialogHeader>
              <div className="keyboard-shortcuts-list">
                {shortcutRows.map((row) => (
                  <div className="keyboard-shortcut-row" key={row.label}>
                    <span>{row.label}</span>
                    <span className="keyboard-shortcut-keys">
                      {row.keys.map((key) => (
                        <kbd key={key}>{key}</kbd>
                      ))}
                    </span>
                  </div>
                ))}
              </div>
            </DialogContent>
          </Dialog>
        </div>
      </main>
    </TooltipProvider>
  )
}

function AppChatWorkspace({ view }: { view: AppContentViewModel }) {
  const {
    activeConversation,
    activeDocument,
    activeWorkspace,
    appendInstructionToActiveRun,
    artifactPreview,
    cancelActiveRun,
    changeMode,
    changeImageMode,
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
    isDesktop,
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
    runtime,
    runtimeConnection,
    selectAttachments,
    selectProjectForActiveConversation,
    sendMessage,
    setActiveDocument,
    setArtifactPreview,
    setDraft,
    setMainView,
    setModelRequiredOpen,
    setModelServiceAddRequested,
    setPendingDeleteMessageID,
    setPendingDiagnosticsRunID,
    setPermissionMode,
    showLocalFileContextMenu,
    t,
  } = view

  function openModelServiceSettings() {
    setModelServiceAddRequested(true)
    setMainView('settings')
  }

  return (
    <section className="workspace">
      <header className="topbar">
        <div className="chat-toolbar-title">
          <span>{activeConversation?.title ?? t('app.newChat')}</span>
        </div>
        {isDesktop ? (
          <div className="topbar-status">
            <span
              className={`topbar-runtime-dot${runtime?.online ? ' is-online' : ' is-offline'}`}
              title={runtimeStatusLabel(runtime, runtimeConnection, t)}
              aria-label={runtimeStatusLabel(runtime, runtimeConnection, t)}
            />
          </div>
        ) : null}
      </header>
      {isDesktop && !runtime?.online ? (
        <div className="status-banner status-banner-warning" role="status">
          <span className="status-banner-text">{t('topbar.bannerRuntimeOffline')}</span>
        </div>
      ) : null}

      <ChatThread
        conversation={activeConversation}
        workspaceRoot={activeWorkspace?.path}
        onOpenArtifact={(artifactID) => void openLocalArtifact(artifactID)}
        onLoadArtifactContent={runtimeConnection
          ? (artifactID) => getLocalArtifactContent(artifactID, runtimeConnection)
          : undefined}
        onOpenDiagnostics={setPendingDiagnosticsRunID}
        onPreviewLocalFile={openLocalDocument}
        onLocalFileContextMenu={(ref) => void showLocalFileContextMenu(ref)}
        onPickSuggestion={setDraft}
        onRegenerateMessage={handleRegenerateMessage}
        onEditResendMessage={handleEditResendMessage}
        onDeleteMessage={setPendingDeleteMessageID}
        onFailureAction={handleAgentFailureAction}
      />

      {artifactPreview && runtimeConnection ? (
        <Suspense fallback={null}>
          <ArtifactPanel
            artifact={artifactPreview}
            onClose={() => setArtifactPreview(null)}
            onLoadContent={(artifactID) => getLocalArtifactContent(artifactID, runtimeConnection)}
          />
        </Suspense>
      ) : null}
      {activeDocument ? (
        <Suspense fallback={null}>
          <DocPreviewPanel
            doc={activeDocument}
            refreshKey={docPreviewRefreshKey}
            onClose={() => setActiveDocument(null)}
          />
        </Suspense>
      ) : null}
      <AlertDialog open={modelRequiredOpen} onOpenChange={setModelRequiredOpen}>
        <AlertDialogContent className="conversation-delete-dialog">
          <AlertDialogHeader className="conversation-delete-header">
            <AlertDialogMedia className="conversation-delete-media">
              <IconSparkles aria-hidden="true" />
            </AlertDialogMedia>
            <AlertDialogTitle>{t('composer.modelRequired.title')}</AlertDialogTitle>
            <AlertDialogDescription>{t('composer.modelRequired.description')}</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter className="conversation-delete-footer">
            <AlertDialogCancel variant="outline" autoFocus>
              <span className="conversation-delete-button-label">{t('composer.modelRequired.later')}</span>
            </AlertDialogCancel>
            <AlertDialogAction
              onClick={openModelServiceSettings}
            >
              <span className="conversation-delete-button-label">{t('composer.modelRequired.openSettings')}</span>
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <AlertDialog
        open={Boolean(pendingDiagnosticsRunID)}
        onOpenChange={(open) => !open && setPendingDiagnosticsRunID(undefined)}
      >
        <AlertDialogContent className="conversation-delete-dialog">
          <AlertDialogHeader className="conversation-delete-header">
            <AlertDialogMedia className="conversation-delete-media">
              <IconDownload aria-hidden="true" />
            </AlertDialogMedia>
            <AlertDialogTitle>{t('diagnostics.downloadConfirmTitle')}</AlertDialogTitle>
            <AlertDialogDescription>{t('diagnostics.downloadConfirmBody')}</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter className="conversation-delete-footer">
            <AlertDialogCancel variant="outline" autoFocus>
              <span className="conversation-delete-button-label">{t('sidebar.dialog.cancel')}</span>
            </AlertDialogCancel>
            <AlertDialogAction
              onClick={() => {
                if (pendingDiagnosticsRunID) {
                  void exportLocalRunDiagnostics(pendingDiagnosticsRunID)
                  setPendingDiagnosticsRunID(undefined)
                }
              }}
            >
              <span className="conversation-delete-button-label">{t('diagnostics.downloadConfirm')}</span>
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <AlertDialog
        open={Boolean(pendingDeleteMessageID)}
        onOpenChange={(open) => !open && setPendingDeleteMessageID(undefined)}
      >
        <AlertDialogContent className="conversation-delete-dialog">
          <AlertDialogHeader className="conversation-delete-header">
            <AlertDialogMedia className="conversation-delete-media">
              <IconTrash aria-hidden="true" />
            </AlertDialogMedia>
            <AlertDialogTitle>{t('message.deleteConfirmTitle')}</AlertDialogTitle>
            <AlertDialogDescription>{t('message.deleteConfirmBody')}</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter className="conversation-delete-footer">
            <AlertDialogCancel variant="outline" autoFocus onClick={() => setPendingDeleteMessageID(undefined)}>
              <span className="conversation-delete-button-label">{t('sidebar.dialog.cancel')}</span>
            </AlertDialogCancel>
            <AlertDialogAction
              variant="destructive"
              onClick={() => {
                if (pendingDeleteMessageID) {
                  void handleDeleteMessage(pendingDeleteMessageID)
                  setPendingDeleteMessageID(undefined)
                }
              }}
            >
              <span className="conversation-delete-button-label">{t('message.delete')}</span>
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <div className="composer-dock">
        <PendingApprovalBar
          approval={pendingApproval}
          onDecision={handlePermissionDecision}
          onReconcile={(messageID, requestID, decision) => void handleToolReconciliation(messageID, requestID, decision)}
        />

        <PendingPlanApprovalBar
          key={pendingPlanApproval?.requestID ?? 'no-plan-approval'}
          plan={pendingPlanApproval}
          onDecision={(messageID, requestID, decision, instructions) => void handlePlanApprovalDecision(messageID, requestID, decision, instructions)}
        />

        <PendingQuestionBar
          key={pendingQuestion?.requestID ?? 'no-question'}
          question={pendingQuestion}
          onAnswer={(messageID, requestID, answers) => void handleQuestionAnswer(messageID, requestID, answers)}
          onSkip={(messageID, requestID) => {
            if (!pendingQuestion) return
            const skipAnswers: Record<string, string[]> = {}
            for (const item of pendingQuestion.questions) {
              skipAnswers[item.question] = []
            }
            void handleQuestionAnswer(messageID, requestID, skipAnswers)
          }}
          onCancel={() => void cancelActiveRun()}
        />

        <Composer
          draft={draft}
          onDraftChange={setDraft}
          isSending={isSending}
          hasActiveRun={hasActiveRun}
          onSend={() => void sendMessage()}
          onAppendInstruction={hasActiveRun ? () => void appendInstructionToActiveRun() : undefined}
          onStop={() => void cancelActiveRun()}
          listSkills={async () => {
            if (!runtimeConnection) return []
            const catalog = await listInstalledSkills(runtimeConnection)
            return catalog.skills
          }}
          listMcpServers={
            runtimeConnection
              ? async () => {
                  const catalog = await listMcpServers(runtimeConnection)
                  return catalog.servers
                }
              : undefined
          }
          listPlugins={
            runtimeConnection
              ? async () => {
                  const plugins = await listLocalPlugins(runtimeConnection)
                  return Promise.all(
                    plugins.flatMap((plugin) =>
                      plugin.enabled && !plugin.retired && plugin.compatibility === 'compatible'
                        ? [getLocalPlugin(plugin.id, runtimeConnection)]
                        : []),
                  )
                }
              : undefined
          }
          mode={mode}
          models={models}
          onModeChange={changeMode}
          imageMode={imageMode}
          imageModels={imageModels}
          onImageModeChange={(next) => void changeImageMode(next)}
          permissionMode={permissionMode}
          onPermissionModeChange={setPermissionMode}
          onModelRequired={() => setModelRequiredOpen(true)}
          onConfigureModels={openModelServiceSettings}
          onRefreshCurrentModel={() => void refreshCurrentModel()}
          projectName={activeConversation?.project?.name ?? pendingProject?.name}
          onSelectProject={() => void selectProjectForActiveConversation()}
          onRemoveProject={() => void removeProjectFromActiveConversation()}
          attachments={pendingAttachments}
          onSelectAttachments={() => void selectAttachments()}
          onDropAttachments={dropAttachments}
          onRemoveAttachment={removeAttachment}
          isDesktop={isDesktop}
          slashCommandsEnabled={isDesktop}
        />
        <p className="composer-disclaimer">{t('composer.disclaimer')}</p>
      </div>
    </section>
  )
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

function runtimeStatusLabel(
  runtime: RuntimeProbe | null,
  config: RuntimeConnection | null,
  t: Translator,
): string {
  if (!runtime?.online) {
    return t('app.localStatus.runtimeOffline')
  }
  if (!hasRuntimeAuthorization(config)) {
    return t('app.localStatus.unpaired')
  }
  return t('app.localStatus.connected')
}

/** Tracks the assembled `arguments` for each `tool_call_id` within a
 *  single SSE stream session. Populated when the runtime emits a
 *  `tool.requested` event (which carries the full assembled args);
 *  read back when the matching `tool.completed` / `tool.failed`
 *  arrives so the renderer can show the same rich detail for the
 *  completed row. Lives on the call site as a plain Map alongside
 *  `seenEventIDs` — same per-session-scope discipline. */
export type ToolArgsByCallId = Map<string, Record<string, unknown>>

function appendLocalRunEvent(
  message: ChatMessage,
  event: AgentRunEvent,
  seenEventIDs: Set<string>,
  toolArgsByCallId: ToolArgsByCallId,
  t: Translator,
  onOfficeFileOpened?: (ref: LocalFileRef) => void,
) {
  applyRunPresentationEvent(message, event)
  if (event.event_type === 'llm.delta') {
    return
  }
  if (!message.presentation) {
    message.content = projectTransientAssistantText(message.content, event)
  }
  // Per-call usage streams as llm.usage; accumulate it onto the message so
  // the usage chip updates live. Dedupe on event.id (re-stream replay).
  // run.completed later overwrites with the authoritative turn total.
  if (event.event_type === 'llm.usage') {
    if (event.id && seenEventIDs.has(event.id)) {
      return
    }
    if (event.id) {
      seenEventIDs.add(event.id)
    }
    const payload = event.payload ?? {}
    const input = Number(payload.input_tokens) || 0
    const output = Number(payload.output_tokens) || 0
    if (input > 0 || output > 0) {
      message.tokens = (message.tokens ?? 0) + input + output
    }
    return
  }
  const alreadySeen = Boolean(event.id && seenEventIDs.has(event.id))
  if (event.id) {
    seenEventIDs.add(event.id)
  }
  if (event.event_type === 'run.completed') {
    const payload = event.payload ?? {}
    const input = Number(payload.input_tokens) || 0
    const output = Number(payload.output_tokens) || 0
    // Authoritative per-turn totals (sum of the turn's llm.usage events).
    if (input > 0 || output > 0) {
      message.tokens = input + output
    }
  }
  // Optional concrete model label for the completed turn.
  if (event.event_type === 'model.selected') {
    const payload = event.payload ?? {}
    message.runMode = {
      resolved: String(payload.label ?? payload.resolved_model_id ?? ''),
      reason: String(payload.reason ?? ''),
    }
  }
  // Tool args propagation. Runtime emits `tool.requested` events
  // carrying the fully assembled tool args. Cache them by
  // tool_call_id so the matching `tool.completed` / `tool.failed`
  // (which only carry the result, not the original args) can borrow
  // them and the renderer keeps showing the same rich detail through
  // the lifecycle. See event_translator._tool_requested_events_from_update.
  if (event.event_type === 'tool.requested') {
    const payload = event.payload ?? {}
    const id = String(payload.tool_call_id ?? '')
    const args = payload.arguments
    if (id && args && typeof args === 'object' && !Array.isArray(args)) {
      toolArgsByCallId.set(id, args as Record<string, unknown>)
    }
  } else if (event.event_type === 'tool.completed' || event.event_type === 'tool.failed') {
    const payload = event.payload ?? {}
    const id = String(payload.tool_call_id ?? '')
    const cached = id ? toolArgsByCallId.get(id) : undefined
    if (cached && !payload.arguments) {
      event = { ...event, payload: { ...payload, arguments: cached } }
    }
    // Side effect: detect a successful office WRITE and refresh the
    // right-side document preview panel to the freshly-edited copy.
    // Result shape from any office.* write tool:
    //   {ok, original_path, edited_path, kind, summary}
    // Reads (office.read / office.outline) intentionally do NOT
    // auto-open the preview — that was noisy. Preview opens only on:
    //   1. user clicking a filename in agent text
    //   2. an edit completing (this branch)
    if (event.event_type === 'tool.completed' && !alreadySeen) {
      const detected = detectOfficeFileEdited(event.payload)
      if (detected) {
        onOfficeFileOpened?.(detected)
      }
    }
  }
  if (!alreadySeen) {
    const subagents = applySubagentLifecycleEvent(message.subagents, event)
    if (subagents.length) {
      message.subagents = subagents
    }
  }
  const item = timelineItem(event, t)
  if (item) {
    if (!alreadySeen) {
      message.agentEvents = [...(message.agentEvents ?? []), item]
    }
  }
}

/** Tool names that emit the copy-on-first-write Phase 2 result shape:
 *  `{ok, original_path, edited_path, kind, summary}`. The renderer
 *  follows `edited_path` so the preview shows the freshly-edited copy
 *  rather than the untouched original. Keep this list in sync with
 *  `shejane_runtime/tools/office.py:OFFICE_WRITE_TOOLS`. */
const OFFICE_WRITE_TOOL_NAMES = new Set<string>([
  // Phase 2 — docx/xlsx
  'office.find_replace',
  'office.insert_paragraph',
  'office.update_paragraph',
  'office.delete_paragraph',
  'office.apply_style',
  'office.set_cells',
  'office.set_formula',
  'office.set_cell_format',
  'office.merge_cells',
  'office.add_row',
  // Phase 3 — pptx
  'office.create_pptx',
  'office.add_slide',
  'office.update_slide',
  'office.delete_slide',
  'office.reorder_slides',
  'office.set_slide_title',
  'office.set_slide_bullets',
  'office.set_slide_notes',
  'office.add_image_to_slide',
])

/** Inspect a `tool.completed` payload and return a LocalFileRef
 *  when the underlying tool was a successful office.* WRITE — the
 *  ref points at the `.edited.<ext>` copy that now holds the changes.
 *
 *  Returns null for unknown tools, non-success results, and missing
 *  edited_path (so a malformed result silently degrades to "don't
 *  switch the preview"). */
function detectOfficeFileEdited(payload: AgentRunEvent['payload']): LocalFileRef | null {
  if (!payload) return null
  const toolName = String((payload as Record<string, unknown>).tool ?? (payload as Record<string, unknown>).name ?? '')
  if (!OFFICE_WRITE_TOOL_NAMES.has(toolName)) return null
  const result = (payload as Record<string, unknown>).result
  const resultObj =
    result && typeof result === 'object' && !Array.isArray(result) ? (result as Record<string, unknown>) : null
  if (!resultObj) return null
  if (String(resultObj.ok ?? '') !== 'true') return null
  const editedPath = String(resultObj.edited_path ?? '')
  if (!editedPath) return null
  const kindRaw = String(resultObj.kind ?? '')
  const lower = editedPath.toLowerCase()
  const kind: LocalFileRef['kind'] =
    kindRaw === 'word' || kindRaw === 'excel' || kindRaw === 'powerpoint'
      ? kindRaw
      : lower.endsWith('.xlsx')
        ? 'excel'
        : lower.endsWith('.pptx')
          ? 'powerpoint'
          : 'word'
  const name = editedPath.split(/[\\/]/).pop() || editedPath
  return { path: editedPath, kind, name }
}

function appendLocalDelta(message: ChatMessage, delta: string, event: AgentRunEvent, seenEventIDs: Set<string>) {
  if (message.presentation) {
    return
  }
  if (event.id && seenEventIDs.has(event.id)) {
    return
  }
  if (event.id) {
    seenEventIDs.add(event.id)
  }
  message.content = projectTransientAssistantText(message.content, {
    ...event,
    payload: { ...(event.payload ?? {}), content: delta },
  })
}

function recordLocalEventCursor(message: ChatMessage, event: AgentRunEvent) {
  if (Number.isSafeInteger(event.seq) && Number(event.seq) >= 0) {
    message.lastEventSeq = Math.max(message.lastEventSeq ?? 0, Number(event.seq))
  }
}

async function projectRuntimeThreadCache(
  snapshot: LocalThreadSnapshot,
  existing: Conversation | undefined,
  config: RuntimeConnection,
  t: Translator,
): Promise<Conversation> {
  const conversation = projectRuntimeThread(snapshot, existing, t)
  if (!snapshot.events_truncated) return conversation

  for (const message of conversation.messages) {
    if (
      message.role !== 'assistant'
      || !message.runId
      || (message.status !== 'waiting_permission' && message.status !== 'waiting_input')
    ) {
      continue
    }
    const events = await listLocalRunEvents(message.runId, message.lastEventSeq ?? 0, config)
    const seenEventIDs = new Set(
      (message.agentEvents ?? []).flatMap((event) => event.eventId ? [event.eventId] : []),
    )
    const toolArgsByCallId: ToolArgsByCallId = new Map()
    for (const event of events) {
      recordLocalEventCursor(message, event)
      appendLocalRunEvent(message, event, seenEventIDs, toolArgsByCallId, t)
    }
    finalizeLocalRunStatus(message)
  }
  return conversation
}

async function streamLocalMessage(
  runID: string,
  config: RuntimeConnection,
  conversation: Conversation,
  message: ChatMessage,
  t: Translator,
  onOfficeFileOpened: (ref: LocalFileRef) => void,
  onUpdate: () => void,
) {
  let seenEventIDs = new Set(
    (message.agentEvents ?? []).flatMap((event) => event.eventId ? [event.eventId] : []),
  )
  const toolArgsByCallId: ToolArgsByCallId = new Map()
  const subscribe = () => streamLocalRun(runID, config, {
    afterSeq: message.lastEventSeq,
    onEvent: (event) => {
      recordLocalEventCursor(message, event)
      appendLocalRunEvent(message, event, seenEventIDs, toolArgsByCallId, t, onOfficeFileOpened)
      onUpdate()
    },
    onDelta: (delta, event) => {
      recordLocalEventCursor(message, event)
      appendLocalDelta(message, delta, event, seenEventIDs)
      onUpdate()
    },
  })

  let reconnects = 0
  let cursorResets = 0
  while (true) {
    try {
      const result = await subscribe()
      if (result.completed) return result
    } catch (error) {
      if (error instanceof LocalStreamCursorResetRequiredError) {
        if (cursorResets >= 1) throw error
        cursorResets += 1
        const rebuilt = projectRuntimeThread(
          await getLocalThreadSnapshot(conversation.id, config),
          undefined,
          t,
        )
        const projectedMessage = rebuilt.messages.find((item) => item.runId === runID)
        if (!projectedMessage) throw error
        for (const key of Object.keys(message)) Reflect.deleteProperty(message, key)
        Object.assign(message, projectedMessage)
        message.lastEventSeq = Math.max(message.lastEventSeq ?? 0, error.resumeAfter)
        rebuilt.messages = rebuilt.messages.map((item) => item.id === projectedMessage.id ? message : item)
        for (const key of Object.keys(conversation)) Reflect.deleteProperty(conversation, key)
        Object.assign(conversation, rebuilt)
        seenEventIDs = new Set(
          (message.agentEvents ?? []).flatMap((event) => event.eventId ? [event.eventId] : []),
        )
        toolArgsByCallId.clear()
        onUpdate()
        continue
      }
      if (reconnects >= 5) throw error
    }
    if (reconnects >= 5) {
      throw new Error('Runtime event stream ended before its completion marker.')
    }
    const delay = 100 * (2 ** reconnects)
    reconnects += 1
    await new Promise((resolve) => window.setTimeout(resolve, delay))
  }
}

/** Fire a system notification when an assistant turn finishes. The
 *  Electron main process internally drops the call when the window is
 *  focused, so this is safe to call on every completion. We trim the
 *  body so the OS doesn't have to deal with a multi-screen reply. */
function notifyAgentCompleted(message: ChatMessage, t: Translator): void {
  const bridge = window.shejaneClient
  if (!bridge?.notify) {
    return
  }
  const raw = (message.content || '').trim().replace(/\s+/g, ' ')
  const body = raw.length > 140 ? `${raw.slice(0, 140)}…` : raw
  void bridge.notify({
    title: t('notify.agentCompleted.title'),
    body: body || t('notify.agentCompleted.empty'),
  })
}

/** Fire a system notification when an assistant turn FAILS. Mirrors
 *  notifyAgentCompleted (main suppresses it while focused). The body
 *  prefers the run.failed event label, falling back to the bubble content
 *  (set to the error message on a network/HTTP drop). */
function notifyAgentFailed(message: ChatMessage, t: Translator): void {
  const bridge = window.shejaneClient
  if (!bridge?.notify) {
    return
  }
  const raw = (latestRunFailedLabel(message) || message.content || '').trim().replace(/\s+/g, ' ')
  const body = raw.length > 140 ? `${raw.slice(0, 140)}…` : raw
  void bridge.notify({
    title: t('notify.agentFailed.title'),
    body: body || t('notify.agentFailed.empty'),
  })
}

function notifyScheduledRun(schedule: LocalScheduledRun, t: Translator): void {
  const bridge = window.shejaneClient
  if (!bridge?.notify) {
    return
  }
  const fallback = schedule.goal || t('notify.scheduledRun.empty')
  const raw = (schedule.status === 'failed' ? schedule.error_message : schedule.result_text)
    || fallback
  const normalized = raw.trim().replace(/\s+/g, ' ')
  const body = normalized.length > 140 ? `${normalized.slice(0, 140)}…` : normalized
  void bridge.notify({
    title: schedule.status === 'failed'
      ? t('notify.scheduledRunFailed.title')
      : t('notify.scheduledRunCompleted.title'),
    body: body || t('notify.scheduledRun.empty'),
  })
}

function latestRunFailedLabel(message: ChatMessage): string {
  return [...(message.agentEvents ?? [])].reverse().find(
    (event) => event.type === 'run.failed' || event.type === 'run.cleanup_required',
  )?.label ?? ''
}

function finalizeLocalRunStatus(message: ChatMessage) {
  const events = message.agentEvents ?? []
  if (events.some((event) => event.type === 'run.failed' || event.type === 'run.cleanup_required')) {
    message.status = 'error'
    if (!message.content.trim()) {
      message.content = latestRunFailedLabel(message)
    }
    return
  }
  if (events.some((event) => event.type === 'run.completed')) {
    message.status = 'done'
    return
  }
  if (hasPendingPermission(events)) {
    message.status = 'waiting_permission'
    return
  }
  if (hasPendingPlanApproval(events)) {
    message.status = 'waiting_input'
    return
  }
  message.status = hasPendingQuestion(events) ? 'waiting_input' : 'done'
}

function hasPendingPermission(events: AgentTimelineItem[]): boolean {
  const pending = new Set<string>()
  for (const event of events) {
    if (event.type === 'permission.required' && event.permissionRequestId) {
      pending.add(event.permissionRequestId)
    }
    if (event.type === 'tool.reconciliation_required' && event.permissionRequestId) {
      pending.add(event.permissionRequestId)
    }
    if (event.type === 'tool.reconciliation_resolved' && event.permissionRequestId) {
      pending.delete(event.permissionRequestId)
    }
    if (event.type === 'permission.resolved' && event.permissionRequestId) {
      pending.delete(event.permissionRequestId)
    }
  }
  return pending.size > 0
}

function hasPendingQuestion(events: AgentTimelineItem[]): boolean {
  const pending = new Set<string>()
  for (const event of events) {
    if (event.type === 'question.asked' && event.questionRequestId) {
      pending.add(event.questionRequestId)
    }
    if (event.type === 'question.answered' && event.questionRequestId) {
      pending.delete(event.questionRequestId)
    }
  }
  return pending.size > 0
}

function hasPendingPlanApproval(events: AgentTimelineItem[]): boolean {
  const pending = new Set<string>()
  for (const event of events) {
    if (event.type === 'plan.approval_required' && event.planApprovalRequestId) {
      pending.add(event.planApprovalRequestId)
    }
    if (event.type === 'plan.approval_resolved' && event.planApprovalRequestId) {
      pending.delete(event.planApprovalRequestId)
    }
  }
  return pending.size > 0
}

function downloadLocalRunDiagnostics(diagnostics: LocalRunDiagnostics) {
  const url = URL.createObjectURL(new Blob([JSON.stringify(diagnostics, null, 2)], { type: 'application/json' }))
  const link = document.createElement('a')
  link.href = url
  link.download = `shejane-local-run-${diagnostics.run.id}-diagnostics.json`
  link.click()
  URL.revokeObjectURL(url)
}

function appendUnique(items: string[], item: string): string[] {
  return items.includes(item) ? items : [...items, item]
}

function mergeAttachments(
  current: LocalAttachmentRef[],
  additions: LocalAttachmentRef[],
): LocalAttachmentRef[] {
  const byPath = new Map(current.map((item) => [item.path, item]))
  for (const item of additions) byPath.set(item.path, item)
  return [...byPath.values()].slice(0, 10)
}

function upsertWorkspace(items: LocalWorkspaceAuthorization[], workspace: LocalWorkspaceAuthorization): LocalWorkspaceAuthorization[] {
  return [workspace, ...items.filter((item) => item.id !== workspace.id && item.path !== workspace.path)]
}

function upsertLocalRun(items: LocalHarnessRun[], run: LocalHarnessRun): LocalHarnessRun[] {
  return [run, ...items.filter((item) => item.id !== run.id)]
}

function upsertConversation(items: Conversation[], conversation: Conversation): Conversation[] {
  return sortConversationsForSidebar([conversation, ...items.filter((item) => item.id !== conversation.id)])
}

function cloneConversation(conversation: Conversation): Conversation {
  return {
    ...conversation,
    project: conversation.project ? { ...conversation.project } : undefined,
    workspace: conversation.workspace ? { ...conversation.workspace } : undefined,
    messages: conversation.messages.map((message) => ({
      ...message,
      attachments: message.attachments?.map((attachment) => ({ ...attachment })),
      agentEvents: message.agentEvents ? [...message.agentEvents] : undefined,
    })),
  }
}

function sortConversationsForSidebar(items: Conversation[]): Conversation[] {
  return [...items].sort((a, b) => {
    if (Boolean(a.pinned) !== Boolean(b.pinned)) {
      return a.pinned ? -1 : 1
    }
    return b.updatedAt.localeCompare(a.updatedAt)
  })
}

function findWorkspaceByPath(items: LocalWorkspaceAuthorization[], path: string): LocalWorkspaceAuthorization | undefined {
  const normalized = path.trim()
  return normalized ? items.find((item) => pathInsideWorkspace(item.path, normalized)) : undefined
}

function pathInsideWorkspace(root: string, target: string): boolean {
  const normalizedRoot = trimPath(root)
  const normalizedTarget = trimPath(target)
  if (!normalizedRoot || !normalizedTarget) {
    return false
  }
  return normalizedTarget === normalizedRoot || normalizedTarget.startsWith(`${normalizedRoot}/`) || normalizedTarget.startsWith(`${normalizedRoot}\\`)
}

function trimPath(path: string): string {
  return path.trim().replace(/[\\/]+$/u, '')
}

function safeFilename(value: string): string {
  return value.trim().replace(/[^\p{L}\p{N}_-]+/gu, '-').replace(/^-+|-+$/gu, '').slice(0, 48) || 'conversation'
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

async function mapWithConcurrency<T, R>(
  values: T[],
  concurrency: number,
  map: (value: T) => Promise<R>,
): Promise<R[]> {
  const results = new Array<R>(values.length)
  let next = 0
  async function worker() {
    while (next < values.length) {
      const index = next
      next += 1
      results[index] = await map(values[index])
    }
  }
  await Promise.all(
    Array.from({ length: Math.min(Math.max(1, concurrency), values.length) }, () => worker()),
  )
  return results
}

function loadRuntimeThreadIDs(): Set<string> {
  try {
    const value = JSON.parse(localStorage.getItem(runtimeThreadIDsStorageKey) ?? '[]')
    return new Set(
      Array.isArray(value) ? value.filter((id): id is string => typeof id === 'string') : [],
    )
  } catch {
    return new Set()
  }
}

function storeRuntimeThreadIDs(ids: Set<string>) {
  localStorage.setItem(runtimeThreadIDsStorageKey, JSON.stringify([...ids]))
}

/** Cross-platform basename: strips trailing separators then returns the
 *  segment after the last "/" or "\\". Used as the default name for a
 *  project conversation when the user picks a directory.
 */
function pathBasename(path: string): string {
  const trimmed = path.replace(/[/\\]+$/, '')
  const idx = Math.max(trimmed.lastIndexOf('/'), trimmed.lastIndexOf('\\'))
  return idx >= 0 ? trimmed.slice(idx + 1) : trimmed
}
