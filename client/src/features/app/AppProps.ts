import type { RuntimeConnection, RuntimeProbe } from '@/runtime/client'
import type { Translator } from '@/shared/i18n/i18n'
import type {
  ChatMessage,
  ChatMode,
  Conversation,
  ConversationProject,
  ConversationWorkspace,
  LocalAttachmentRef,
  LocalFileRef,
  OpenDocument,
} from '@/shared/local-data/types'
import type { ModelOption } from '@/features/chat/components/ModeSelector'
import type { AgentFailureAction } from '@/features/chat/recovery'
import type { LocalArtifact, LocalPermissionScope, LocalPlanApprovalDecision, LocalToolReconciliationDecision } from '@/runtime/client'

/**
 * State and actions shared by both view surfaces (shell chrome
 * and chat workspace). Extracting these from the viewmodel lets
 * each component import only the slice it inspects.
 */

export interface CommonProps {
  isDesktop: boolean
  t: Translator
  runtime: RuntimeProbe | null
  runtimeConnection: RuntimeConnection | null
  setMainView: (view: 'chat' | 'plugins' | 'settings') => void
  setModelServiceAddRequested: (requested: boolean) => void
}

export interface ShellProps {
  conversations: Conversation[]
  activeID: string | undefined
  conversationSidebarRef: React.RefObject<{ openSearch: () => void }>
  startNewConversation: () => void
  selectConversation: (id: string) => void
  renameConversation: (id: string, title: string) => void
  deleteConversationData: (id: string) => void
  togglePinConversation: (id: string) => void
  exportConversationData: (id: string) => void
  exportLocalData: () => void
  importLocalData: (file: File | undefined) => void
  sidebarWidth: number
  sidebarCollapsed: boolean
  sidebarMotion: 'idle' | 'closing' | 'opening'
  isResizingSidebar: boolean
  beginSidebarResize: (event: React.PointerEvent<HTMLDivElement>) => void
  handleSidebarResizeKeyDown: (event: React.KeyboardEvent<HTMLDivElement>) => void
  collapseSidebar: () => void
  expandSidebar: () => void
  shellClassName: string
  appShellStyle: React.CSSProperties
  mainView: 'chat' | 'plugins' | 'settings'
  pluginsTab: 'skills' | 'plugins' | 'mcp'
  setPluginsTab: (tab: 'skills' | 'plugins' | 'mcp') => void
  keyboardHelpOpen: boolean
  setKeyboardHelpOpen: (open: boolean) => void
  shortcutRows: { label: string, keys: string[] }[]
}

export interface PluginsSettingsProps {
  listInstalledSkillsForView: () => Promise<{ skills: unknown[], roots: string[] }>
  listMcpServersForView: () => Promise<{ servers: unknown[], sources_scanned: string[] }>
  listPluginsForView: () => Promise<unknown[]>
  pluginCatalogVersion: number
  submitPluginCommand: (command: unknown) => Promise<unknown>
  agentSettings: { mcpDisabled: string[], advanced: Record<string, unknown> }
  changeAgentSettings: (next: { mcpDisabled: string[], advanced: Record<string, unknown> }) => void
  runtimeSettingsConfig: RuntimeConnection | null
  setModelCatalogVersion: () => void
}

export interface ChatWorkspaceProps {
  activeConversation: Conversation | undefined
  activeWorkspace: ConversationWorkspace | undefined
  hasActiveRun: boolean
  draft: string
  setDraft: (draft: string) => void
  mode: ChatMode
  changeMode: (mode: ChatMode) => void
  models: ModelOption[]
  imageMode: ChatMode | undefined
  imageModels: ModelOption[]
  changeImageMode: (mode: ChatMode) => Promise<void>
  permissionMode: string
  setPermissionMode: (mode: string) => void
  pendingApproval: unknown | null
  pendingPlanApproval: unknown | null
  pendingQuestion: unknown | null
  pendingProject: ConversationProject | undefined
  pendingAttachments: LocalAttachmentRef[]
  selectAttachments: () => void
  dropAttachments: (files: File[]) => void
  removeAttachment: (path: string) => void
  selectProjectForActiveConversation: (recoveryTarget?: unknown) => void
  removeProjectFromActiveConversation: () => void
  refreshCurrentModel: () => void
  isSending: boolean
  sendMessage: () => Promise<void>
  appendInstructionToActiveRun: () => Promise<void>
  cancelActiveRun: () => Promise<void>
  handleAgentFailureAction: (action: AgentFailureAction, messageID: string) => void
  handleDeleteMessage: (messageID: string) => void
  handleEditResendMessage: (userMessageID: string, text: string) => void
  handleRegenerateMessage: (assistantMessageID: string) => void
  handlePermissionDecision: (
    messageID: string,
    requestID: string,
    decision: 'approve' | 'edit' | 'deny',
    scope: LocalPermissionScope,
    editedAction?: { name: string, args: Record<string, unknown> },
  ) => void
  handlePlanApprovalDecision: (
    messageID: string,
    requestID: string,
    decision: LocalPlanApprovalDecision,
    instructions?: string,
  ) => void
  handleQuestionAnswer: (
    messageID: string,
    requestID: string,
    answers: Record<string, string[]>,
  ) => Promise<void>
  handleToolReconciliation: (
    messageID: string,
    requestID: string,
    decision: LocalToolReconciliationDecision,
  ) => Promise<void>
  activeDocument: OpenDocument | null
  setActiveDocument: (doc: OpenDocument | null) => void
  docPreviewRefreshKey: number
  artifactPreview: LocalArtifact | null
  setArtifactPreview: (artifact: LocalArtifact | null) => void
  openLocalArtifact: (artifactID: string) => void
  openLocalDocument: (ref: LocalFileRef) => void
  showLocalFileContextMenu: (ref: LocalFileRef) => void
  exportLocalRunDiagnostics: (runID: string) => void
  modelRequiredOpen: boolean
  setModelRequiredOpen: (open: boolean) => void
  pendingDeleteMessageID: string | undefined
  setPendingDeleteMessageID: (id: string | undefined) => void
  pendingDiagnosticsRunID: string | undefined
  setPendingDiagnosticsRunID: (id: string | undefined) => void
}
