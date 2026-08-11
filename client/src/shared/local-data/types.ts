import type {
  ImportModelServiceRequest,
  RuntimeModelSpec,
  RunPresentationState,
  SubagentInvocationStatus,
  SubagentReceiptStatus,
} from '@shejane/runtime-sdk'
import type { FilePreviewKind } from '../files/filePreview'

export type MessageRole = 'system' | 'user' | 'assistant'
/** Concrete Runtime model selection (`local:<connection>:<model>`). */
export type ChatMode = RuntimeModelSpec | ''
export type MessageStatus = 'pending' | 'streaming' | 'waiting_permission' | 'waiting_input' | 'done' | 'error'
export type ModelPhase = 'waiting_provider' | 'reasoning' | 'answering' | 'tool_calling' | 'completed'

export interface AgentQuestionChoice {
  label: string
  description?: string
}

export interface AgentQuestionItem {
  question: string
  header: string
  multiSelect?: boolean
  options: AgentQuestionChoice[]
}

export interface AgentToolDetail {
  /** `host` means render with the globe icon; `text` is the default
   *  inline string display; `count` is a numeric/short summary. */
  kind: 'host' | 'text' | 'count'
  /** The user-visible short text (host name, basename, query …).
   *  Already truncated; the renderer may further ellipsis on overflow. */
  text: string
  /** Optional full string (full path, full URL) for the title=
   *  tooltip when the user hovers the truncated text. */
  tooltip?: string
  /** Web tools (web.fetch, Browser QA plugin actions, open.url) ask
   *  the renderer to draw the default IconWorld glyph alongside the
   *  host. We don't fetch real favicons — privacy + no external deps. */
  showWebIcon?: boolean
}

export interface AgentPlanTodo {
  content: string
  status: 'pending' | 'in_progress' | 'completed'
}

export type AgentSubagentStatus = SubagentInvocationStatus

export type AgentSubagentReceiptStatus = SubagentReceiptStatus

export interface AgentSubagentUsage {
  modelCalls: number
  inputTokens: number
  outputTokens: number
  unmeteredCalls: number
  outcomeUnknownCalls: number
}

/** Disposable Client projection of one Runtime-owned `task` invocation.
 *  `operationId` is an execution identity, not an addressable Agent or child Run. */
export interface AgentSubagentProjection {
  operationId: string
  parentRunId: string
  parentOperationId?: string
  toolCallId: string
  subagentType: string
  description: string
  status: AgentSubagentStatus
  receiptStatus: AgentSubagentReceiptStatus
  attemptCount: number
  usage: AgentSubagentUsage
  errorType?: string
  createdAt: string
  startedAt?: string
  completedAt?: string
  updatedAt: string
}

export interface AgentTimelineItem {
  type: string
  label: string
  eventId?: string
  handoffLedgerState?: 'not_required' | 'missing' | 'fresh' | 'stale'
  handoffLedgerMessage?: string
  failureCategory?: string
  failureRetryable?: boolean
  failureActionKind?: 'retry' | 'user_action' | 'repair' | 'operator_action' | 'inspect'
  failureRecoveryAction?: 'retry' | 'repair' | 'workspace' | 'diagnostics'
  failureSuggestedAction?: string
  errorCode?: string
  retryAttempt?: number
  retrySourceRunId?: string
  retrySourceMessageId?: string
  repairAttempt?: number
  repairWorkflowStatus?: 'started' | 'completed' | 'failed' | 'rejected' | 'canceled'
  repairSourceRunId?: string
  repairSourceMessageId?: string
  tool?: string
  /** Runtime-assigned ID for the underlying LLM tool_call. Same value on
   *  the `tool.requested`, `tool.completed`, and `tool.failed` items of
   *  one logical call, so the renderer can correlate phases when many
   *  calls are in flight (notably `task` subagent dispatches running in
   *  parallel). Populated by `timelineItem()` from `payload.tool_call_id`. */
  toolCallId?: string
  subagentOperationId?: string
  subagentStatus?: AgentSubagentStatus
  subagentReceiptStatus?: AgentSubagentReceiptStatus
  subagentType?: string
  subagentDescription?: string
  subagentUsage?: AgentSubagentUsage
  /** Back-compat single-string identifier (host, basename, etc.).
   *  New code should set `toolDetail` instead; the renderer prefers
   *  toolDetail when both are present. */
  target?: string
  /** Rich per-tool primary-argument badge — populated by
   *  `toolDetail()` in chatStore.ts when the agent calls a tool whose
   *  args are surfaced via the `tool.requested` event. */
  toolDetail?: AgentToolDetail
  tokens?: number
  permissionRequestId?: string
  permissionTool?: string
  permissionToolName?: string
  permissionArguments?: Record<string, unknown>
  permissionCanGrantForRun?: boolean
  permissionDecision?: 'approve' | 'edit' | 'deny'
  permissionScope?: 'once' | 'run'
  permissionSource?: 'rule' | 'llm' | 'fallback' | 'run_grant'
  permissionReason?: string
  reconciliationDecision?: 'confirmed_completed' | 'retry_not_executed' | 'abort'
  planApprovalRequestId?: string
  planApprovalDecision?: 'approve' | 'modify' | 'reject'
  planTodos?: AgentPlanTodo[]
  questionRequestId?: string
  questions?: AgentQuestionItem[]
  questionAnswers?: Record<string, string[]>
  artifactId?: string
  artifactTitle?: string
  artifactTool?: string
  artifactMediaType?: string
  sourceTitle?: string
  sourceUrl?: string
  verificationStatus?: 'passed' | 'failed'
  /** Base64-encoded image/png payloads captured from a `code.execute`
   *  tool.completed event's `data.results[].data["image/png"]` (and
   *  variants). Populated by chatStore.ts so the message renderer can
   *  show matplotlib figures inline without re-parsing the wire
   *  envelope. Empty/absent when the tool produced no images. */
  codeExecImages?: string[]
}

export interface ChatMessage {
  id: string
  /** Stable id for retrying the Runtime command that created this message. */
  commandId?: string
  role: MessageRole
  content: string
  createdAt: string
  status: MessageStatus
  attachments?: LocalAttachmentRef[]
  pluginReferences?: Array<{
    pluginId: string
    name: string
    digest: string
  }>
  pluginCommand?: {
    pluginId: string
    pluginName: string
    commandId: string
    title: string
    digest: string
  }
  requestId?: string
  runId?: string
  /** Highest Runtime event sequence already reflected in this cache entry. */
  lastEventSeq?: number
  tokens?: number
  /** Runtime-owned phase of the latest model call; rebuilt from the Run snapshot. */
  modelPhase?: ModelPhase
  modelPhaseStartedAt?: string
  /** Runtime-owned execution presentation plus disposable live drafts. */
  presentation?: RunPresentationState
  agentEvents?: AgentTimelineItem[]
  /** Current subagent state rebuilt from the authoritative Run snapshot and
   *  advanced by durable lifecycle events while the stream is connected. */
  subagents?: AgentSubagentProjection[]
  /** UI model badge for the completed turn. */
  runMode?: {
    /** The concrete Runtime model id. */
    resolved: string
    reason: string
  }
}

export interface LocalAttachmentRef {
  path: string
  name: string
  /** Runtime-owned immutable input location for attachments already admitted to a Run. */
  runId?: string
  inputId?: string
  mediaType?: string
  bytes?: number
}

export interface ConversationWorkspace {
  path: string
  label: string
  authorized: boolean
  authorizationId?: string
}

/**
 * Currently-previewed office document.
 *
 * Set whenever something in the UI wants to surface a .docx / .xlsx in
 * the right-side DocPreviewPanel — two known triggers:
 *  1. Successful `office.read` tool call (App.tsx mines the tool result
 *     and opens with a local-file loader).
 *  2. Click on a recognized filename in agent text (MessageBubble
 *     resolves it against `conversation.workspace.path` and opens).
 * The renderer (DocxPreview / XlsxPreview) consumes `loadBytes()` and
 * doesn't care where the bytes come from. `sourceKey` lets the panel
 * dedupe — opening the same path twice doesn't trigger a
 * spurious reload (we just bump the refresh key).
 */
/** Sparse, all-optional PDF metadata shown in the local preview. */
export interface PdfDocumentMetadata {
  title?: string
  author?: string
  creator?: string
  producer?: string
  subject?: string
  keywords?: string
  pages?: number
  encrypted?: boolean
  pdf_version?: string
  page_size?: string
}

export interface OpenDocument {
  /** Stable identifier for this document — `local:<absolute-path>`. */
  sourceKey: string
  /** "word", "excel", "powerpoint", or "pdf" — drives which preview
   *  component the panel mounts (DocxPreview / XlsxPreview /
   *  PptxPreview / PdfPreview). */
  kind: FilePreviewKind
  /** Display label — typically the basename. */
  name: string
  /** Optional full path or description shown as tooltip on the header. */
  tooltip?: string
  /** Optional PDF metadata shown in the preview header. */
  metadata?: PdfDocumentMetadata
  /** Resolves with the workspace file's raw bytes.
   *
   *  For .pptx the preview uses the outline endpoint instead of these
   *  bytes (no mature pure-browser pptx renderer exists), but the
   *  loader is still required by the panel shell — pass a no-op
   *  ArrayBuffer resolver, or wire to fetchWorkspaceFile if you want
   *  the bytes available for a later "download" affordance. */
  loadBytes: () => Promise<ArrayBuffer>
  /** Absolute filesystem path on the user's machine. Required when
   *  `kind === 'powerpoint'` so the preview can call the outline
   *  endpoint + the "open natively" button can hand the path to the
   *  Electron shell. */
  localPath?: string
  runId?: string
  inputId?: string
}

/** Reference to an office file living inside an authorized workspace.
 *  Emitted by detectors / clickable-filename handlers; App.tsx wraps
 *  it into a full OpenDocument by binding `fetchWorkspaceFile`. */
export interface LocalFileRef {
  /** Absolute path on the user's machine. Must be inside an
   *  authorized workspace (the runtime enforces this on fetch). */
  path: string
  kind?: FilePreviewKind
  /** Display name — typically the basename. */
  name: string
  /** Prefer the immutable Runtime snapshot when this ref came from a sent attachment. */
  runId?: string
  inputId?: string
}

/**
 * A conversation belongs to a "project" when it was created by clicking
 * the Projects sidebar button — which prompts for a directory and binds
 * that directory's workspace into the conversation upfront.
 *
 * `project.name` is the directory's basename by default. The sidebar
 * uses `conversation.project` to decide whether the row goes in the
 * "Chats" group or the "Projects" group; the agent itself doesn't read
 * this field (it only reads `conversation.workspace`).
 */
export interface ConversationProject {
  name: string
}

export interface Conversation {
  id: string
  title: string
  archived: boolean
  pinned?: boolean
  createdAt: string
  updatedAt: string
  model?: ChatMode
  project?: ConversationProject
  workspace?: ConversationWorkspace
  messages: ChatMessage[]
}

export interface ConversationExport {
  version: 1
  exportedAt: string
  conversations: Conversation[]
  modelServices?: ExportedModelService[]
}

export type ExportedModelService = Omit<ImportModelServiceRequest, 'region'> & {
  region: ImportModelServiceRequest['region'] | 'official'
}
