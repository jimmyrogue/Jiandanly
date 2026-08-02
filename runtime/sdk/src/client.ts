import { streamAgentSSE, type AgentRunEvent } from './sse.js'
import type { components } from './generated.js'

// -- Auto-generated types ----------------------------------------------------
//
// The runtime owns these shapes via pydantic models in
// `runtime/src/shejane_runtime/api_schemas.py`. `make schemas`
// regenerates `openapi.json` + `generated.ts`. Don't hand-edit the
// re-exports — change the pydantic model, regenerate, commit both.
//
// `Schemas` is the union of every component schema FastAPI emitted.
// We re-export individual names as aliases so call-sites stay
// readable (`LocalRun` vs `components['schemas']['LocalRun']`).
type Schemas = components['schemas']

/** Concrete BYOK model identifier accepted by Runtime run commands. */
export type RuntimeModelSpec = `local:${string}:${string}`

/** Validate untrusted storage or catalog data before it becomes a model selection. */
export function parseRuntimeModelSpec(value: string): RuntimeModelSpec | undefined {
  const trimmed = value.trim()
  return trimmed.length <= 256 && /^local:[^:\s]+:\S+$/.test(trimmed)
    ? trimmed as RuntimeModelSpec
    : undefined
}

export type LocalRun = Schemas['LocalRun']
export type PermissionMode = Schemas['CreateRunRequest']['permission_mode']
export type LocalThread = Schemas['LocalThread']
export type LocalThreadItem = Schemas['LocalThreadItem']
export type LocalThreadChange = Schemas['LocalThreadChange']
export type LocalThreadSnapshot = Schemas['LocalThreadSnapshot']
export type RuntimeInfo = Schemas['RuntimeInfo']
export type RuntimeSettings = Schemas['RuntimeSettingsResponse']
export type UpdateRuntimeSettingsRequest = Schemas['UpdateRuntimeSettingsRequest']
export type ModelServicePreset = Schemas['ModelServicePreset']
export type ModelServiceConnection = Schemas['ModelServiceConnection']
export type SheJaneAuthorizationStart = Schemas['SheJaneAuthorizationStartResponse']
export type SheJaneAuthorizationStatus = Schemas['SheJaneAuthorizationStatusResponse']
export type CentralDiagnosticsStatus = Schemas['CentralDiagnosticsStatusResponse']
export type UpdateCentralDiagnosticsRequest = Schemas['UpdateCentralDiagnosticsRequest']
export type ModelServiceModel = Schemas['ModelServiceModel']
export type ConnectModelServiceRequest = Schemas['ConnectModelServiceRequest']
export type ReconnectModelServiceRequest = Schemas['ReconnectModelServiceRequest']
export type ImportModelServiceRequest = Schemas['ImportModelServiceRequest']
export type AddModelServiceModelRequest = Schemas['AddModelServiceModelRequest']
export type VerifyModelServiceModelRequest = Schemas['VerifyModelServiceModelRequest']
export type ModelCapabilityBinding = Schemas['ModelCapabilityBinding']
export type SetModelCapabilityBindingRequest = Schemas['SetModelCapabilityBindingRequest']
export type LocalRuntimeModel = Schemas['LocalRuntimeModel']
export type LocalScheduledRun = Schemas['LocalScheduledRun']
export type LocalArtifact = Schemas['LocalArtifact']
export type LocalWorkspaceAuthorization = Schemas['LocalWorkspaceAuthorization']
export type LocalWorkspaceDiagnosis = Schemas['LocalWorkspaceDiagnosis']
export type LocalRunDiagnostics = Schemas['LocalRunDiagnostics']
export type CancelRunCommandReceipt = Schemas['CancelRunCommandReceipt']
export type AnswerQuestionCommandReceipt = Schemas['AnswerQuestionCommandReceipt']
export type ResolvePermissionCommandReceipt = Schemas['ResolvePermissionCommandReceipt']
export type PlanResolveCommandReceipt = Schemas['PlanResolveCommandReceipt']
export type ToolReconcileCommandReceipt = Schemas['ToolReconcileCommandReceipt']
export type PluginInstallCommand = Schemas['PluginInstallCommand']
export type PluginInstallCommandReceipt = Schemas['PluginInstallCommandReceipt']
export type PluginModelBindCommand = Schemas['PluginModelBindCommand']
export type PluginModelBindCommandReceipt = Schemas['PluginModelBindCommandReceipt']
export type PluginModelBindingSummary = Schemas['PluginModelBindingSummary']
export type RuntimeAssetInstallCommand = Schemas['RuntimeAssetInstallCommand']
export type RuntimeAssetInstallCommandReceipt = Schemas['RuntimeAssetInstallCommandReceipt']
export type FixedRuntimeAssetStatus = Schemas['FixedRuntimeAssetStatus']
export type FixedRuntimeAssetPluginID = FixedRuntimeAssetStatus['plugin_id']
export type PluginStateCommandReceipt = Schemas['PluginStateCommandReceipt']
export type PluginVersionSwitchCommandReceipt = Schemas['PluginVersionSwitchCommandReceipt']
export type PluginRemoveCommandReceipt = Schemas['PluginRemoveCommandReceipt']
export type PluginReadinessSnapshot = Schemas['PluginReadinessSnapshot']
export type PluginSetupAdvanceCommandReceipt = Schemas['PluginSetupAdvanceCommandReceipt']
export type PluginSetupActionID =
  | 'install_helper'
  | 'request_screen_recording'
  | 'open_screen_recording_settings'
  | 'request_accessibility'
  | 'open_accessibility_settings'
  | 'recheck'
export type PluginSummary = Schemas['PluginSummary']
export type PluginDetail = Schemas['PluginDetail']
export type PluginReference = Schemas['PluginReference']
export type PluginCommandReference = Schemas['PluginCommandReference']
export type ForkRunRequest = Schemas['ForkRunRequest']
export type InjectRunInstructionResponse = Schemas['InjectRunInstructionResponse']
export type ClearMemoryResponse = Schemas['ClearMemoryResponse']
export type McpServerInfo = Schemas['McpServerInfo']
export type McpServerCatalog = Schemas['McpServerCatalog']
export type McpServerWriteRequest = Schemas['McpServerWriteRequest']
export type McpServerWriteResponse = Schemas['McpServerWriteResponse']
export type McpServerDeleteResponse = Schemas['McpServerDeleteResponse']
export type SkillFile = Schemas['SkillFile']
export type SkillWriteRequest = Schemas['SkillWriteRequest']
export type SkillWriteResponse = Schemas['SkillWriteResponse']
export type SkillDeleteResponse = Schemas['SkillDeleteResponse']
export type LocalPermissionScope = 'once' | 'run'
export type LocalPermissionDecision = 'approve' | 'edit' | 'deny'
export type LocalToolReconciliationDecision = 'confirmed_completed' | 'retry_not_executed' | 'abort'
export interface LocalEditedToolAction {
  name: string
  args: Record<string, unknown>
}
export type PptxSlideOutline = Schemas['PptxSlideOutline']
export type PptxOutlineResponse = Schemas['PptxOutlineResponse']
export type LocalPlanApprovalDecision = 'approve' | 'modify' | 'reject'

// -- Hand-written types (not in OpenAPI) -------------------------------------
//
// Things below this line aren't derivable from openapi.json:
//   • RuntimeClientConfig — caller-provided connection parameters.
//   • RuntimeProbe — the client probe returns a DERIVED `online`
//     bool, not the raw HealthResponse.
//   • LocalStreamHandlers — SSE callback shape. Event payloads live
//     in `AgentRunEvent` which is hand-written because discriminated
//     unions over `event_type` don't roundtrip cleanly through openapi.

export interface RuntimeClientConfig {
  baseURL: string
  token?: string
}

export interface RuntimeProbe {
  online: boolean
  status?: string
  mode?: string
  worker?: string
}

export interface LocalStreamHandlers {
  afterSeq?: number
  onDelta: (content: string, event: AgentRunEvent) => void
  onEvent: (event: AgentRunEvent) => void
}

export class LocalStreamCursorResetRequiredError extends Error {
  override name = 'LocalStreamCursorResetRequiredError'

  constructor(
    message: string,
    readonly resumeAfter: number,
  ) {
    super(message)
  }
}

export class RuntimeHTTPError extends Error {
  override name = 'RuntimeHTTPError'

  constructor(
    message: string,
    readonly status: number,
    readonly code?: string,
  ) {
    super(message)
  }
}

type Fetcher = typeof fetch
export const LOCAL_RUNTIME_PROTOCOL_VERSION = 1

export async function probeRuntime(baseURL: string, fetcher: Fetcher = fetch): Promise<RuntimeProbe> {
  const controller = new AbortController()
  // `globalThis.setTimeout` so this works in both browser (Electron
  // renderer) and Node (contract tests against a live runtime, which
  // run under vitest's node env).
  const timeout = globalThis.setTimeout(() => controller.abort(), 1200)
  try {
    const response = await fetcher(`${baseURL.replace(/\/$/, '')}/v1/health`, {
      signal: controller.signal,
    })
    if (!response.ok) {
      return { online: false }
    }
    const body = (await response.json()) as { status?: string; mode?: string; worker?: string }
    return {
      online: body.status === 'ok',
      status: body.status,
      mode: body.mode,
      worker: body.worker,
    }
  } catch {
    return { online: false }
  } finally {
    globalThis.clearTimeout(timeout)
  }
}

export async function getLocalRuntimeInfo(
  config: RuntimeClientConfig,
  fetcher: Fetcher = fetch,
): Promise<RuntimeInfo> {
  const response = await fetcher(`${normalizeBaseURL(config.baseURL)}/v1/runtime`, {
    headers: localHeaders(config, false),
  })
  return decodeLocalResponse<RuntimeInfo>(response)
}

export async function getRuntimeSettings(
  config: RuntimeClientConfig,
  fetcher: Fetcher = fetch,
): Promise<RuntimeSettings> {
  const response = await fetcher(`${normalizeBaseURL(config.baseURL)}/v1/settings`, {
    headers: localHeaders(config, false),
  })
  return decodeLocalResponse<RuntimeSettings>(response)
}

export async function updateRuntimeSettings(
  input: UpdateRuntimeSettingsRequest,
  config: RuntimeClientConfig,
  fetcher: Fetcher = fetch,
): Promise<RuntimeSettings> {
  const response = await fetcher(`${normalizeBaseURL(config.baseURL)}/v1/settings`, {
    method: 'PUT',
    headers: localHeaders(config, true),
    body: JSON.stringify(input),
  })
  return decodeLocalResponse<RuntimeSettings>(response)
}

export async function listModelServicePresets(
  config: RuntimeClientConfig,
  fetcher: Fetcher = fetch,
): Promise<ModelServicePreset[]> {
  const response = await fetcher(`${normalizeBaseURL(config.baseURL)}/v1/model-services/presets`, {
    headers: localHeaders(config, false),
  })
  const body = await decodeLocalResponse<{ services?: ModelServicePreset[] }>(response)
  return body.services ?? []
}

export async function listModelServices(
  config: RuntimeClientConfig,
  fetcher: Fetcher = fetch,
): Promise<ModelServiceConnection[]> {
  const response = await fetcher(`${normalizeBaseURL(config.baseURL)}/v1/model-services`, {
    headers: localHeaders(config, false),
  })
  const body = await decodeLocalResponse<{ services?: ModelServiceConnection[] }>(response)
  return body.services ?? []
}

export async function startSheJaneAuthorization(
  config: RuntimeClientConfig,
  fetcher: Fetcher = fetch,
): Promise<SheJaneAuthorizationStart> {
  const response = await fetcher(
    `${normalizeBaseURL(config.baseURL)}/v1/model-services/shejane/authorization`,
    { method: 'POST', headers: localHeaders(config, false) },
  )
  return decodeLocalResponse<SheJaneAuthorizationStart>(response)
}

export async function getSheJaneAuthorization(
  authorizationID: string,
  config: RuntimeClientConfig,
  fetcher: Fetcher = fetch,
): Promise<SheJaneAuthorizationStatus> {
  const response = await fetcher(
    `${normalizeBaseURL(config.baseURL)}/v1/model-services/shejane/authorization/${encodeURIComponent(authorizationID)}`,
    { headers: localHeaders(config, false) },
  )
  return decodeLocalResponse<SheJaneAuthorizationStatus>(response)
}

export async function getCentralDiagnostics(
  config: RuntimeClientConfig,
  fetcher: Fetcher = fetch,
): Promise<CentralDiagnosticsStatus> {
  const response = await fetcher(
    `${normalizeBaseURL(config.baseURL)}/v1/shejane/diagnostics`,
    { headers: localHeaders(config, false) },
  )
  return decodeLocalResponse<CentralDiagnosticsStatus>(response)
}

export async function updateCentralDiagnostics(
  input: UpdateCentralDiagnosticsRequest,
  config: RuntimeClientConfig,
  fetcher: Fetcher = fetch,
): Promise<CentralDiagnosticsStatus> {
  const response = await fetcher(
    `${normalizeBaseURL(config.baseURL)}/v1/shejane/diagnostics`,
    {
      method: 'PUT',
      headers: localHeaders(config, true),
      body: JSON.stringify(input),
    },
  )
  return decodeLocalResponse<CentralDiagnosticsStatus>(response)
}

export async function listModelCapabilityBindings(
  config: RuntimeClientConfig,
  fetcher: Fetcher = fetch,
): Promise<ModelCapabilityBinding[]> {
  const response = await fetcher(`${normalizeBaseURL(config.baseURL)}/v1/model-capability-bindings`, {
    method: 'GET',
    headers: localHeaders(config, false),
  })
  const body = await decodeLocalResponse<{ bindings?: ModelCapabilityBinding[] }>(response)
  return body.bindings ?? []
}

export async function setModelCapabilityBinding(
  capability: ModelCapabilityBinding['capability'],
  input: SetModelCapabilityBindingRequest,
  config: RuntimeClientConfig,
  fetcher: Fetcher = fetch,
): Promise<ModelCapabilityBinding> {
  const response = await fetcher(
    `${normalizeBaseURL(config.baseURL)}/v1/model-capability-bindings/${encodeURIComponent(capability)}`,
    {
      method: 'PUT',
      headers: localHeaders(config, true),
      body: JSON.stringify(input),
    },
  )
  return decodeLocalResponse<ModelCapabilityBinding>(response)
}

export async function deleteModelCapabilityBinding(
  capability: ModelCapabilityBinding['capability'],
  config: RuntimeClientConfig,
  fetcher: Fetcher = fetch,
): Promise<void> {
  const response = await fetcher(
    `${normalizeBaseURL(config.baseURL)}/v1/model-capability-bindings/${encodeURIComponent(capability)}`,
    { method: 'DELETE', headers: localHeaders(config, false) },
  )
  if (!response.ok) await decodeLocalResponse(response)
}

export async function connectModelService(
  input: ConnectModelServiceRequest,
  config: RuntimeClientConfig,
  fetcher: Fetcher = fetch,
): Promise<ModelServiceConnection> {
  const response = await fetcher(`${normalizeBaseURL(config.baseURL)}/v1/model-services`, {
    method: 'POST',
    headers: localHeaders(config, true),
    body: JSON.stringify(input),
  })
  return decodeLocalResponse<ModelServiceConnection>(response)
}

export async function refreshModelService(
  connectionID: string,
  config: RuntimeClientConfig,
  fetcher: Fetcher = fetch,
): Promise<ModelServiceConnection> {
  const response = await fetcher(`${normalizeBaseURL(config.baseURL)}/v1/model-services/${encodeURIComponent(connectionID)}/refresh`, {
    method: 'POST',
    headers: localHeaders(config, false),
  })
  return decodeLocalResponse<ModelServiceConnection>(response)
}

export async function reconnectModelService(
  connectionID: string,
  input: ReconnectModelServiceRequest,
  config: RuntimeClientConfig,
  fetcher: Fetcher = fetch,
): Promise<ModelServiceConnection> {
  const response = await fetcher(
    `${normalizeBaseURL(config.baseURL)}/v1/model-services/${encodeURIComponent(connectionID)}/credential`,
    {
      method: 'PUT',
      headers: localHeaders(config, true),
      body: JSON.stringify(input),
    },
  )
  return decodeLocalResponse<ModelServiceConnection>(response)
}

export async function importModelService(
  input: ImportModelServiceRequest,
  config: RuntimeClientConfig,
  fetcher: Fetcher = fetch,
): Promise<ModelServiceConnection> {
  const response = await fetcher(`${normalizeBaseURL(config.baseURL)}/v1/model-services/import`, {
    method: 'POST',
    headers: localHeaders(config, true),
    body: JSON.stringify(input),
  })
  return decodeLocalResponse<ModelServiceConnection>(response)
}

export async function addModelServiceModel(
  connectionID: string,
  input: AddModelServiceModelRequest,
  config: RuntimeClientConfig,
  fetcher: Fetcher = fetch,
): Promise<ModelServiceModel> {
  const response = await fetcher(`${normalizeBaseURL(config.baseURL)}/v1/model-services/${encodeURIComponent(connectionID)}/models`, {
    method: 'POST',
    headers: localHeaders(config, true),
    body: JSON.stringify(input),
  })
  return decodeLocalResponse<ModelServiceModel>(response)
}

export function verifyModelServiceModel(
  connectionID: string,
  modelID: string,
  config: RuntimeClientConfig,
  fetcher?: Fetcher,
): Promise<ModelServiceModel>
export function verifyModelServiceModel(
  connectionID: string,
  modelID: string,
  input: VerifyModelServiceModelRequest,
  config: RuntimeClientConfig,
  fetcher?: Fetcher,
): Promise<ModelServiceModel>
export async function verifyModelServiceModel(
  connectionID: string,
  modelID: string,
  inputOrConfig: VerifyModelServiceModelRequest | RuntimeClientConfig,
  configOrFetcher?: RuntimeClientConfig | Fetcher,
  maybeFetcher: Fetcher = fetch,
): Promise<ModelServiceModel> {
  const hasInput = 'capability' in inputOrConfig && 'protocol' in inputOrConfig
  const input = hasInput ? inputOrConfig : undefined
  const config = (hasInput ? configOrFetcher : inputOrConfig) as RuntimeClientConfig
  const fetcher = hasInput
    ? maybeFetcher
    : typeof configOrFetcher === 'function'
      ? configOrFetcher
      : fetch
  const response = await fetcher(
    `${normalizeBaseURL(config.baseURL)}/v1/model-services/${encodeURIComponent(connectionID)}/models/${encodeURIComponent(modelID)}/verify`,
    {
      method: 'POST',
      headers: localHeaders(config, Boolean(input)),
      ...(input ? { body: JSON.stringify(input) } : {}),
    },
  )
  return decodeLocalResponse<ModelServiceModel>(response)
}

export async function deleteModelService(
  connectionID: string,
  config: RuntimeClientConfig,
  fetcher: Fetcher = fetch,
): Promise<void> {
  const response = await fetcher(`${normalizeBaseURL(config.baseURL)}/v1/model-services/${encodeURIComponent(connectionID)}`, {
    method: 'DELETE',
    headers: localHeaders(config, false),
  })
  if (!response.ok) await decodeLocalResponse(response)
}

export async function listLocalRuntimeModels(
  config: RuntimeClientConfig,
  fetcher: Fetcher = fetch,
): Promise<LocalRuntimeModel[]> {
  const response = await fetcher(`${normalizeBaseURL(config.baseURL)}/v1/models`, {
    headers: localHeaders(config, false),
  })
  const body = await decodeLocalResponse<{ models?: LocalRuntimeModel[] }>(response)
  return body.models ?? []
}

/**
 * User-configurable per-run agent settings. Sent with every run-create request
 * and applied by runtime (overriding its env defaults). Open-ended shape so
 * more knobs can be surfaced later; only `memory` is exposed today.
 */
/**
 * Advanced per-run knobs surfaced in the settings dialog's "Advanced" section.
 * Every field is optional: an unset field is omitted from the wire payload so
 * the runtime keeps its own env/default value. Keys mirror what
 * `runs._apply_advanced_overrides` reads on the runtime (snake_case on the wire;
 * the serializer below translates).
 */
export interface AdvancedAgentSettings {
  /** Hard cap on LLM calls per run (runaway guard). Runtime default 20. */
  maxModelCalls?: number
  /** Retries for a failing tool before giving up. Runtime default 2. */
  maxToolRetries?: number
  /** Results the research / deep-search path requests per query. Runtime default 3. */
  researchSearchLimit?: number
  /** deepagents subagents (the `task` tool). Runtime default on. */
  subagents?: boolean
  /** Run the browser tool headless. Runtime default on. */
  browserHeadless?: boolean
  /** Prompt-injection input guard. Runtime default observe. */
  inputGuard?: 'off' | 'observe' | 'block'
  /** Plan-first middleware. Runtime default off. */
  planFirst?: 'off' | 'auto' | 'always'
}

export interface AgentSettings {
  memory?: 'off' | 'on'
  skills?: 'off' | 'on'
  mcp?: 'off' | 'on'
  /** Per-server opt-out list. When `mcp === 'on'`, every discovered
   *  server is loaded EXCEPT names in this list. When `mcp === 'off'`
   *  this is moot (no servers load at all). Wire format is snake_case
   *  `mcp_disabled` to match the runtime's run_settings reader; the
   *  serializer below handles that. */
  mcpDisabled?: string[]
  /** Advanced knobs (settings dialog → "Advanced"). Omitted fields keep
   *  the runtime's env/default value. */
  advanced?: AdvancedAgentSettings
}

export type LocalRunMetadata = Record<string, unknown>

export interface CreateLocalRunInput {
  commandId: string
  clientMessageId: string
  threadId?: string
  assistantMessageId?: string
  userInput?: string
  threadTitle?: string
  threadMetadata?: Record<string, unknown>
  userItemMetadata?: Record<string, unknown>
  replaceFromClientId?: string
  goal: string
  workspacePath?: string
  attachmentPaths?: string[]
  requiredTools?: Array<'image.generate' | 'image.edit'>
  history?: Array<{ role: 'user' | 'assistant'; content: string }>
  parentRunId?: string
  pluginRefs?: Array<{
    pluginId: string
    required?: boolean
    expectedDigest?: string
  }>
  pluginCommand?: {
    pluginId: string
    commandId: string
    expectedDigest?: string
  }
  settings?: AgentSettings
  metadata?: LocalRunMetadata
  mode: RuntimeModelSpec
  permissionMode?: PermissionMode
}

interface PendingRuntimeCommandBase {
  commandId: string
  createdAt: string
  canceledAt?: string
  settledAt?: string
}

export interface PendingRunStartCommand extends PendingRuntimeCommandBase {
  type: 'run.start'
  input: CreateLocalRunInput
}

export interface ForkLocalRunInput {
  sourceRunId: string
  protocolVersion: number
  requiredCapabilities: string[]
  clientMessageId: string
  assistantMessageId: string
  threadId: string
  checkpointId: string
  goal?: string
  userInput: string
  threadTitle?: string
  threadMetadata?: Record<string, unknown>
  userItemMetadata?: Record<string, unknown>
  metadata?: LocalRunMetadata
}

export interface PendingRunForkCommand extends PendingRuntimeCommandBase {
  type: 'run.fork'
  input: ForkLocalRunInput
}

export interface PendingRunCancelCommand extends PendingRuntimeCommandBase {
  type: 'run.cancel'
  input: { runId: string; threadId: string }
}

export interface PendingQuestionAnswerCommand extends PendingRuntimeCommandBase {
  type: 'question.answer'
  input: {
    questionId: string
    answers: Record<string, string[]>
    runId: string
    threadId: string
  }
}

export interface PendingPermissionResolveCommand extends PendingRuntimeCommandBase {
  type: 'permission.resolve'
  input: {
    permissionId: string
    decision: LocalPermissionDecision
    scope: LocalPermissionScope
    editedAction?: LocalEditedToolAction
    runId: string
    threadId: string
  }
}

export interface PendingPlanResolveCommand extends PendingRuntimeCommandBase {
  type: 'plan.resolve'
  input: {
    approvalId: string
    decision: LocalPlanApprovalDecision
    instructions?: string
    runId: string
    threadId: string
  }
}

export interface PendingToolReconcileCommand extends PendingRuntimeCommandBase {
  type: 'tool.reconcile'
  input: {
    operationId: string
    decision: LocalToolReconciliationDecision
    runId: string
    threadId: string
  }
}

export interface PendingPluginInstallCommand extends PendingRuntimeCommandBase {
  type: 'plugin.install'
  input: { sourcePath: string; expectedDigest?: string; allowUnsigned: boolean }
}

export interface PendingRuntimeAssetInstallCommand extends PendingRuntimeCommandBase {
  type: 'plugin.runtime_asset.install'
  input: { sourcePath: string; expectedDigest?: string }
}

export interface PendingPluginModelBindCommand extends PendingRuntimeCommandBase {
  type: 'plugin.model.bind'
  input: {
    pluginId: string
    bindingId: string
    model: RuntimeModelSpec
    expectedDigest?: string
  }
}

export interface PendingPluginStateCommand extends PendingRuntimeCommandBase {
  type: 'plugin.enable' | 'plugin.disable'
  input: { pluginId: string; expectedDigest?: string }
}

export interface PendingPluginUpdateCommand extends PendingRuntimeCommandBase {
  type: 'plugin.update'
  input: {
    pluginId: string
    sourcePath: string
    expectedDigest?: string
    allowUnsigned: boolean
  }
}

export interface PendingPluginRollbackCommand extends PendingRuntimeCommandBase {
  type: 'plugin.rollback'
  input: { pluginId: string; targetDigest: string; expectedDigest?: string }
}

export interface PendingPluginRemoveCommand extends PendingRuntimeCommandBase {
  type: 'plugin.remove'
  input: { pluginId: string; expectedDigest?: string }
}

export type PendingRuntimeCommand =
  | PendingRunStartCommand
  | PendingRunForkCommand
  | PendingRunCancelCommand
  | PendingQuestionAnswerCommand
  | PendingPermissionResolveCommand
  | PendingPlanResolveCommand
  | PendingToolReconcileCommand
  | PendingPluginInstallCommand
  | PendingRuntimeAssetInstallCommand
  | PendingPluginModelBindCommand
  | PendingPluginStateCommand
  | PendingPluginUpdateCommand
  | PendingPluginRollbackCommand
  | PendingPluginRemoveCommand
export type RuntimeCommandResult =
  | LocalRun
  | CancelRunCommandReceipt
  | AnswerQuestionCommandReceipt
  | ResolvePermissionCommandReceipt
  | PlanResolveCommandReceipt
  | ToolReconcileCommandReceipt
  | PluginInstallCommandReceipt
  | PluginModelBindCommandReceipt
  | RuntimeAssetInstallCommandReceipt
  | PluginStateCommandReceipt
  | PluginVersionSwitchCommandReceipt
  | PluginRemoveCommandReceipt

export interface PendingRuntimeCommandFailure {
  command: PendingRuntimeCommand
  error: unknown
  retryable: boolean
}

export interface PendingRuntimeCommandDeliveryReport {
  delivered: number
  failures: PendingRuntimeCommandFailure[]
}

function serializeAgentSettings(settings?: AgentSettings): Record<string, unknown> | undefined {
  const src = settings
  if (!src || Object.keys(src).length === 0) return undefined
  const out: Record<string, unknown> = {}
  if (src.memory !== undefined) out.memory = src.memory
  if (src.skills !== undefined) out.skills = src.skills
  if (src.mcp !== undefined) out.mcp = src.mcp
  if (src.mcpDisabled !== undefined && src.mcpDisabled.length > 0) {
    out.mcp_disabled = src.mcpDisabled
  }
  // Advanced knobs -> flat snake_case keys the runtime's run_settings
  // reader understands. Only defined fields ship, so an untouched knob
  // leaves the runtime's own default in force.
  const adv = src.advanced
  if (adv) {
    if (adv.maxModelCalls !== undefined) out.max_model_calls = adv.maxModelCalls
    if (adv.maxToolRetries !== undefined) out.max_tool_retries = adv.maxToolRetries
    if (adv.researchSearchLimit !== undefined) out.research_search_limit = adv.researchSearchLimit
    if (adv.subagents !== undefined) out.subagents = adv.subagents
    if (adv.browserHeadless !== undefined) out.browser_headless = adv.browserHeadless
    if (adv.inputGuard !== undefined) out.input_guard = adv.inputGuard
    if (adv.planFirst !== undefined) out.plan_first = adv.planFirst
  }
  return Object.keys(out).length === 0 ? undefined : out
}

export async function createLocalRun(
  input: CreateLocalRunInput,
  config: RuntimeClientConfig,
  fetcher: Fetcher = fetch,
): Promise<LocalRun> {
  // Translate camelCase → snake_case for the few keys the runtime
  // reads as snake_case (mcp_disabled). Everything else (memory /
  // skills / mcp) is already named the same on both sides.
  const settings = serializeAgentSettings(input.settings)
  const requiredCapabilities = new Set(['agent.run', 'agent.stream', 'hitl'])
  if (input.workspacePath) requiredCapabilities.add('workspace.files')
  if (input.attachmentPaths?.length) requiredCapabilities.add('attachments')
  if (input.settings?.memory !== 'off') requiredCapabilities.add('memory')
  if (input.settings?.skills !== 'off') requiredCapabilities.add('skills')
  if (input.settings?.mcp !== 'off') requiredCapabilities.add('mcp')
  if (input.pluginRefs?.length || input.pluginCommand) requiredCapabilities.add('plugins')
  if (input.settings?.advanced?.subagents !== false) requiredCapabilities.add('subagents')
  const body = JSON.stringify({
    command_id: input.commandId,
    client_message_id: input.clientMessageId,
    thread_id: input.threadId,
    assistant_message_id: input.assistantMessageId,
    protocol_version: LOCAL_RUNTIME_PROTOCOL_VERSION,
    required_capabilities: [...requiredCapabilities].sort(),
    required_tools: input.requiredTools?.length ? input.requiredTools : undefined,
    goal: input.goal,
    user_input: input.userInput,
    thread_title: input.threadTitle,
    thread_metadata: input.threadMetadata,
    user_item_metadata: input.userItemMetadata,
    replace_from_client_id: input.replaceFromClientId,
    workspace_path: input.workspacePath || undefined,
    attachment_paths: input.attachmentPaths?.length ? input.attachmentPaths : undefined,
    history: input.history ?? [],
    parent_run_id: input.parentRunId || undefined,
    plugin_refs: input.pluginRefs?.map((reference) => ({
      plugin_id: reference.pluginId,
      required: reference.required ?? true,
      expected_digest: reference.expectedDigest,
    })),
    plugin_command: input.pluginCommand
      ? {
          plugin_id: input.pluginCommand.pluginId,
          command_id: input.pluginCommand.commandId,
          expected_digest: input.pluginCommand.expectedDigest,
        }
      : undefined,
    settings,
    metadata: input.metadata && Object.keys(input.metadata).length > 0 ? input.metadata : undefined,
    model: input.mode,
    permission_mode: input.permissionMode,
  })
  const request = () =>
    fetcher(`${normalizeBaseURL(config.baseURL)}/v1/runs`, {
      method: 'POST',
      headers: localHeaders(config, true),
      body,
    })
  let response: Response
  try {
    response = await request()
  } catch (error) {
    if (!input.commandId || !input.clientMessageId) throw error
    // One immediate retry hides brief transport resets; the durable outbox
    // handles longer outages without creating another command.
    response = await request()
  }
  return decodeLocalResponse<LocalRun>(response)
}

export async function deliverPendingRuntimeCommands(
  commands: PendingRuntimeCommand[],
  config: RuntimeClientConfig,
  settle: (command: PendingRuntimeCommand, result: RuntimeCommandResult) => Promise<void>,
  fetcher: Fetcher = fetch,
): Promise<PendingRuntimeCommandDeliveryReport> {
  const byThread = new Map<string, PendingRuntimeCommand[]>()
  for (const command of [...commands].sort((a, b) => a.createdAt.localeCompare(b.createdAt))) {
    const key = 'threadId' in command.input
      ? command.input.threadId ?? command.commandId
      : 'pluginId' in command.input
        ? `plugin:${command.input.pluginId}`
        : command.commandId
    const threadCommands = byThread.get(key)
    if (threadCommands) threadCommands.push(command)
    else byThread.set(key, [command])
  }
  const delivered = await Promise.all(
    [...byThread.values()].map(async (threadCommands) => {
      let delivered = 0
      const failures: PendingRuntimeCommandFailure[] = []
      for (const command of threadCommands) {
        try {
          const result = await deliverRuntimeCommand(command, config, fetcher)
          await settle(command, result)
          delivered += 1
        } catch (error) {
          failures.push({ command, error, retryable: isRetryableCommandDeliveryError(error) })
          break
        }
      }
      return { delivered, failures }
    }),
  )
  return {
    delivered: delivered.reduce((total, result) => total + result.delivered, 0),
    failures: delivered.flatMap((result) => result.failures),
  }
}

function isRetryableCommandDeliveryError(error: unknown): boolean {
  if (!(error instanceof RuntimeHTTPError)) return true
  return error.status === 408 || error.status === 425 || error.status === 429 || error.status >= 500
}

async function deliverRuntimeCommand(
  command: PendingRuntimeCommand,
  config: RuntimeClientConfig,
  fetcher: Fetcher,
): Promise<RuntimeCommandResult> {
  switch (command.type) {
    case 'run.start':
      return createLocalRun(command.input, config, fetcher)
    case 'run.fork':
      return forkLocalRun(command.commandId, command.input, config, fetcher)
    case 'run.cancel':
      return cancelLocalRunCommand(command.commandId, command.input.runId, config, fetcher)
    case 'question.answer':
      return answerLocalQuestionCommand(
        command.commandId,
        command.input.questionId,
        command.input.answers,
        config,
        fetcher,
      )
    case 'permission.resolve':
      return resolveLocalPermissionCommand(
        command.commandId,
        command.input.permissionId,
        command.input.decision,
        { scope: command.input.scope, editedAction: command.input.editedAction },
        config,
        fetcher,
      )
    case 'plan.resolve':
      return resolveLocalPlanCommand(
        command.commandId,
        command.input.approvalId,
        command.input.decision,
        command.input.instructions,
        config,
        fetcher,
      )
    case 'tool.reconcile':
      return reconcileLocalToolCommand(
        command.commandId,
        command.input.operationId,
        command.input.decision,
        config,
        fetcher,
      )
    case 'plugin.install':
      return installLocalPluginCommand(
        command.commandId,
        command.input.sourcePath,
        {
          expectedDigest: command.input.expectedDigest,
          allowUnsigned: command.input.allowUnsigned,
        },
        config,
        fetcher,
      )
    case 'plugin.runtime_asset.install':
      return installLocalRuntimeAssetCommand(
        command.commandId,
        command.input.sourcePath,
        command.input.expectedDigest,
        config,
        fetcher,
      )
    case 'plugin.model.bind':
      return bindLocalPluginModelCommand(
        command.commandId,
        command.input.pluginId,
        command.input.bindingId,
        command.input.model,
        command.input.expectedDigest,
        config,
        fetcher,
      )
    case 'plugin.enable':
    case 'plugin.disable':
      return setLocalPluginEnabledCommand(
        command.commandId,
        command.input.pluginId,
        command.type === 'plugin.enable',
        command.input.expectedDigest,
        config,
        fetcher,
      )
    case 'plugin.update':
      return updateLocalPluginCommand(
        command.commandId,
        command.input.pluginId,
        command.input.sourcePath,
        {
          expectedDigest: command.input.expectedDigest,
          allowUnsigned: command.input.allowUnsigned,
        },
        config,
        fetcher,
      )
    case 'plugin.rollback':
      return rollbackLocalPluginCommand(
        command.commandId,
        command.input.pluginId,
        command.input.targetDigest,
        command.input.expectedDigest,
        config,
        fetcher,
      )
    case 'plugin.remove':
      return removeLocalPluginCommand(
        command.commandId,
        command.input.pluginId,
        command.input.expectedDigest,
        config,
        fetcher,
      )
  }
}

export async function forkLocalRun(
  commandID: string,
  input: ForkLocalRunInput,
  config: RuntimeClientConfig,
  fetcher: Fetcher = fetch,
): Promise<LocalRun> {
  const body: ForkRunRequest = {
    command_id: commandID,
    client_message_id: input.clientMessageId,
    assistant_message_id: input.assistantMessageId,
    thread_id: input.threadId,
    protocol_version: input.protocolVersion,
    required_capabilities: input.requiredCapabilities,
    checkpoint_id: input.checkpointId,
    goal: input.goal || undefined,
    user_input: input.userInput,
    thread_title: input.threadTitle,
    thread_metadata: input.threadMetadata,
    user_item_metadata: input.userItemMetadata,
    metadata: input.metadata && Object.keys(input.metadata).length > 0 ? input.metadata : undefined,
  }
  const response = await fetcher(`${normalizeBaseURL(config.baseURL)}/v1/runs/${encodeURIComponent(input.sourceRunId)}/fork`, {
    method: 'POST',
    headers: localHeaders(config, true),
    body: JSON.stringify(body),
  })
  return decodeLocalResponse<LocalRun>(response)
}

export interface InstalledSkill {
  name: string
  description: string
  /** Absolute path to the skill's SKILL.md on the user's disk. */
  path: string
  /** Friendly label for the root this skill was discovered in:
   *  "shejane" for `~/.shejane/skills/`, "claude" for `~/.claude/skills/`,
   *  or the last segment for a custom `SHEJANE_RUNTIME_SKILLS_PATH`. */
  source?: string
  /** Absolute path of the root directory itself, e.g.
   *  "/Users/x/.shejane/skills". Used to open the folder in Finder. */
  root_path?: string
}

/** One known skill root the runtime scans — surfaced even when empty so
 *  the UI can render a section header + "drop a SKILL.md here" hint. */
export interface SkillRoot {
  source: string
  path: string
}

export interface SkillCatalog {
  skills: InstalledSkill[]
  roots: SkillRoot[]
}

export async function listInstalledSkills(
  config: RuntimeClientConfig,
  fetcher: Fetcher = fetch,
): Promise<SkillCatalog> {
  const response = await fetcher(`${normalizeBaseURL(config.baseURL)}/v1/skills`, {
    method: 'GET',
    headers: localHeaders(config, false),
  })
  const body = await decodeLocalResponse<Partial<SkillCatalog>>(response)
  return {
    skills: body.skills ?? [],
    roots: body.roots ?? [],
  }
}

/** List MCP Servers explicitly configured for this Runtime.
 *  Secret values are never returned; only `env_keys` is exposed. */
export async function listMcpServers(
  config: RuntimeClientConfig,
  fetcher: Fetcher = fetch,
): Promise<McpServerCatalog> {
  const response = await fetcher(`${normalizeBaseURL(config.baseURL)}/v1/mcp-servers`, {
    method: 'GET',
    headers: localHeaders(config, false),
  })
  const body = await decodeLocalResponse<Partial<McpServerCatalog>>(response)
  return {
    servers: body.servers ?? [],
    sources_scanned: body.sources_scanned ?? [],
  }
}

export async function createMcpServer(
  input: McpServerWriteRequest,
  config: RuntimeClientConfig,
  fetcher: Fetcher = fetch,
): Promise<McpServerWriteResponse> {
  const response = await fetcher(`${normalizeBaseURL(config.baseURL)}/v1/mcp-servers`, {
    method: 'POST',
    headers: localHeaders(config, true),
    body: JSON.stringify(input),
  })
  return decodeLocalResponse<McpServerWriteResponse>(response)
}

export async function updateMcpServer(
  name: string,
  input: McpServerWriteRequest,
  config: RuntimeClientConfig,
  fetcher: Fetcher = fetch,
): Promise<McpServerWriteResponse> {
  const response = await fetcher(`${normalizeBaseURL(config.baseURL)}/v1/mcp-servers/${encodeURIComponent(name)}`, {
    method: 'PUT',
    headers: localHeaders(config, true),
    body: JSON.stringify(input),
  })
  return decodeLocalResponse<McpServerWriteResponse>(response)
}

export async function deleteMcpServer(
  name: string,
  config: RuntimeClientConfig,
  fetcher: Fetcher = fetch,
): Promise<McpServerDeleteResponse> {
  const response = await fetcher(`${normalizeBaseURL(config.baseURL)}/v1/mcp-servers/${encodeURIComponent(name)}`, {
    method: 'DELETE',
    headers: localHeaders(config, false),
  })
  return decodeLocalResponse<McpServerDeleteResponse>(response)
}

export async function createLocalSkill(
  input: SkillWriteRequest,
  config: RuntimeClientConfig,
  fetcher: Fetcher = fetch,
): Promise<SkillWriteResponse> {
  const response = await fetcher(`${normalizeBaseURL(config.baseURL)}/v1/skills`, {
    method: 'POST',
    headers: localHeaders(config, true),
    body: JSON.stringify(input),
  })
  return decodeLocalResponse<SkillWriteResponse>(response)
}

export async function getLocalSkillFile(
  name: string,
  config: RuntimeClientConfig,
  fetcher: Fetcher = fetch,
): Promise<SkillFile> {
  const response = await fetcher(`${normalizeBaseURL(config.baseURL)}/v1/skills/${encodeURIComponent(name)}`, {
    method: 'GET',
    headers: localHeaders(config, false),
  })
  return decodeLocalResponse<SkillFile>(response)
}

export async function updateLocalSkill(
  name: string,
  input: SkillWriteRequest,
  config: RuntimeClientConfig,
  fetcher: Fetcher = fetch,
): Promise<SkillWriteResponse> {
  const response = await fetcher(`${normalizeBaseURL(config.baseURL)}/v1/skills/${encodeURIComponent(name)}`, {
    method: 'PUT',
    headers: localHeaders(config, true),
    body: JSON.stringify(input),
  })
  return decodeLocalResponse<SkillWriteResponse>(response)
}

export async function deleteLocalSkill(
  name: string,
  config: RuntimeClientConfig,
  fetcher: Fetcher = fetch,
): Promise<SkillDeleteResponse> {
  const response = await fetcher(`${normalizeBaseURL(config.baseURL)}/v1/skills/${encodeURIComponent(name)}`, {
    method: 'DELETE',
    headers: localHeaders(config, false),
  })
  return decodeLocalResponse<SkillDeleteResponse>(response)
}

export async function listLocalRuns(config: RuntimeClientConfig, fetcher: Fetcher = fetch): Promise<LocalRun[]> {
  const response = await fetcher(`${normalizeBaseURL(config.baseURL)}/v1/runs`, {
    method: 'GET',
    headers: localHeaders(config, false),
  })
  const body = await decodeLocalResponse<{ runs?: LocalRun[] }>(response)
  return body.runs ?? []
}

export async function listLocalPlugins(
  config: RuntimeClientConfig,
  fetcher: Fetcher = fetch,
): Promise<PluginSummary[]> {
  const response = await fetcher(`${normalizeBaseURL(config.baseURL)}/v1/plugins`, {
    method: 'GET',
    headers: localHeaders(config, false),
  })
  const body = await decodeLocalResponse<{ plugins?: PluginSummary[] }>(response)
  return body.plugins ?? []
}

export async function getLocalPlugin(
  pluginID: string,
  config: RuntimeClientConfig,
  fetcher: Fetcher = fetch,
): Promise<PluginDetail> {
  const response = await fetcher(
    `${normalizeBaseURL(config.baseURL)}/v1/plugins/${encodeURIComponent(pluginID)}`,
    { method: 'GET', headers: localHeaders(config, false) },
  )
  return decodeLocalResponse<PluginDetail>(response)
}

export async function getLocalPluginReadiness(
  pluginID: string,
  config: RuntimeClientConfig,
  fetcher: Fetcher = fetch,
): Promise<PluginReadinessSnapshot> {
  const response = await fetcher(
    `${normalizeBaseURL(config.baseURL)}/v1/plugins/${encodeURIComponent(pluginID)}/readiness`,
    { method: 'GET', headers: localHeaders(config, false) },
  )
  return decodeLocalResponse<PluginReadinessSnapshot>(response)
}

export async function advanceLocalPluginSetupCommand(
  commandID: string,
  pluginID: 'org.shejane.computer-use',
  expectedRevision: number,
  actionID: PluginSetupActionID,
  config: RuntimeClientConfig,
  fetcher: Fetcher = fetch,
): Promise<PluginSetupAdvanceCommandReceipt> {
  const response = await fetcher(`${normalizeBaseURL(config.baseURL)}/v1/commands`, {
    method: 'POST',
    headers: localHeaders(config, true),
    body: JSON.stringify({
      type: 'plugin.setup.advance',
      command_id: commandID,
      plugin_id: pluginID,
      expected_revision: expectedRevision,
      action_id: actionID,
    }),
  })
  return decodeLocalResponse<PluginSetupAdvanceCommandReceipt>(response)
}

export async function listLocalThreads(
  config: RuntimeClientConfig,
  fetcher: Fetcher = fetch,
): Promise<{ threads: LocalThread[]; cursor: number }> {
  const threads: LocalThread[] = []
  let beforeCreatedAt: string | undefined
  let beforeID: string | undefined
  let baselineCursor = 0
  for (let pageNumber = 0; pageNumber < 10_000; pageNumber += 1) {
    const params = new URLSearchParams()
    if (beforeCreatedAt && beforeID) {
      params.set('before_created_at', beforeCreatedAt)
      params.set('before_id', beforeID)
    }
    const suffix = params.size ? `?${params.toString()}` : ''
    const response = await fetcher(`${normalizeBaseURL(config.baseURL)}/v1/threads${suffix}`, {
      method: 'GET',
      headers: localHeaders(config, false),
    })
    const page = await decodeLocalResponse<{
      threads: LocalThread[]
      cursor: number
      has_more?: boolean
      next_before_created_at?: string | null
      next_before_id?: string | null
    }>(response)
    if (pageNumber === 0) baselineCursor = page.cursor
    threads.push(...page.threads)
    if (!page.has_more) return { threads, cursor: baselineCursor }
    beforeCreatedAt = page.next_before_created_at ?? undefined
    beforeID = page.next_before_id ?? undefined
    if (!beforeCreatedAt || !beforeID) throw new Error('Runtime returned an invalid thread page cursor')
  }
  throw new Error('Runtime thread pagination limit exceeded')
}

export async function getLocalThreadSnapshot(
  threadID: string,
  config: RuntimeClientConfig,
  fetcher: Fetcher = fetch,
): Promise<LocalThreadSnapshot> {
  const baseURL = `${normalizeBaseURL(config.baseURL)}/v1/threads/${encodeURIComponent(threadID)}`
  for (let attempt = 0; attempt < 2; attempt += 1) {
    const items = new Map<string, LocalThreadSnapshot['items'][number]>()
    const runs = new Map<string, LocalRun>()
    const events = new Map<string, LocalThreadSnapshot['events'][number]>()
    const eventHighWatermarks = new Map<string, number>()
    let firstPage: LocalThreadSnapshot | undefined
    let beforePosition: number | undefined
    let eventsTruncated = false
    let retry = false
    for (let pageNumber = 0; pageNumber < 10_000; pageNumber += 1) {
      const params = new URLSearchParams()
      if (beforePosition !== undefined) {
        params.set('before_position', String(beforePosition))
        params.set('expected_version', String(firstPage?.thread.version))
      }
      const response = await fetcher(`${baseURL}${params.size ? `?${params.toString()}` : ''}`, {
        method: 'GET',
        headers: localHeaders(config, false),
      })
      if (response.status === 409 && firstPage) {
        retry = true
        break
      }
      const page = await decodeLocalResponse<LocalThreadSnapshot>(response)
      firstPage ??= page
      for (const item of page.items) items.set(item.id, item)
      for (const run of page.runs) runs.set(run.id, run)
      for (const event of page.events ?? []) events.set(event.id, event)
      for (const [runID, highWatermark] of Object.entries(page.event_high_watermarks ?? {})) {
        eventHighWatermarks.set(runID, Math.max(eventHighWatermarks.get(runID) ?? 0, highWatermark))
      }
      eventsTruncated ||= Boolean(page.events_truncated)
      if (!page.has_more_items) {
        return {
          ...firstPage,
          items: [...items.values()].sort((a, b) => a.position - b.position || a.id.localeCompare(b.id)),
          runs: [...runs.values()],
          events: [...events.values()],
          event_high_watermarks: Object.fromEntries(eventHighWatermarks),
          has_more_items: false,
          next_before_position: null,
          events_truncated: eventsTruncated,
        }
      }
      beforePosition = page.next_before_position ?? undefined
      if (beforePosition === undefined) throw new Error('Runtime returned an invalid item page cursor')
    }
    if (!retry) throw new Error('Runtime item pagination limit exceeded')
  }
  throw new Error('Runtime thread changed repeatedly while reading snapshot')
}

export async function listLocalThreadChanges(
  afterCursor: number,
  config: RuntimeClientConfig,
  fetcher: Fetcher = fetch,
): Promise<{ changes: LocalThreadChange[]; cursor: number; resetRequired: boolean }> {
  const changes: LocalThreadChange[] = []
  let cursor = Math.max(0, afterCursor)
  for (let pageNumber = 0; pageNumber < 10; pageNumber += 1) {
    const params = new URLSearchParams({ after: String(cursor), limit: '1000' })
    const response = await fetcher(
      `${normalizeBaseURL(config.baseURL)}/v1/threads/changes?${params.toString()}`,
      { method: 'GET', headers: localHeaders(config, false) },
    )
    const page = await decodeLocalResponse<{ changes: LocalThreadChange[]; cursor: number }>(response)
    changes.push(...page.changes)
    cursor = Math.max(cursor, page.cursor)
    if (page.changes.length < 1000) return { changes, cursor, resetRequired: false }
  }
  return { changes: [], cursor, resetRequired: true }
}

export async function updateLocalThread(
  threadID: string,
  input: { title?: string; metadata?: Record<string, unknown>; archived?: boolean },
  config: RuntimeClientConfig,
  fetcher: Fetcher = fetch,
): Promise<LocalThread> {
  const response = await fetcher(
    `${normalizeBaseURL(config.baseURL)}/v1/threads/${encodeURIComponent(threadID)}`,
    {
      method: 'PATCH',
      headers: localHeaders(config, true),
      body: JSON.stringify(input),
    },
  )
  return decodeLocalResponse<LocalThread>(response)
}

export async function deleteLocalThread(
  threadID: string,
  config: RuntimeClientConfig,
  fetcher: Fetcher = fetch,
): Promise<{ id: string; deleted: true; version: number }> {
  const response = await fetcher(
    `${normalizeBaseURL(config.baseURL)}/v1/threads/${encodeURIComponent(threadID)}`,
    { method: 'DELETE', headers: localHeaders(config, false) },
  )
  return decodeLocalResponse<{ id: string; deleted: true; version: number }>(response)
}

export async function listLocalSchedules(
  config: RuntimeClientConfig,
  options: { notifyPending?: boolean; status?: LocalScheduledRun['status'] } = {},
  fetcher: Fetcher = fetch,
): Promise<LocalScheduledRun[]> {
  const params = new URLSearchParams()
  if (options.notifyPending) {
    params.set('notify_pending', 'true')
  }
  if (options.status) {
    params.set('status', options.status)
  }
  const suffix = params.size > 0 ? `?${params.toString()}` : ''
  const response = await fetcher(`${normalizeBaseURL(config.baseURL)}/v1/schedules${suffix}`, {
    method: 'GET',
    headers: localHeaders(config, false),
  })
  const body = await decodeLocalResponse<{ schedules?: LocalScheduledRun[] }>(response)
  return body.schedules ?? []
}

export async function createLocalSchedule(
  input: {
    goal: string
    runAt: string
    workspacePath?: string
    mode: RuntimeModelSpec
    permissionMode?: PermissionMode
    history?: Array<{ role: string; content: string }>
    settings?: AgentSettings
    metadata?: LocalRunMetadata
  },
  config: RuntimeClientConfig,
  fetcher: Fetcher = fetch,
): Promise<LocalScheduledRun> {
  const response = await fetcher(`${normalizeBaseURL(config.baseURL)}/v1/schedules`, {
    method: 'POST',
    headers: localHeaders(config, true),
    body: JSON.stringify({
      goal: input.goal,
      run_at: input.runAt,
      workspace_path: input.workspacePath || undefined,
      model: input.mode,
      permission_mode: input.permissionMode,
      history: input.history ?? [],
      settings: serializeAgentSettings(input.settings),
      metadata: input.metadata && Object.keys(input.metadata).length > 0 ? input.metadata : undefined,
    }),
  })
  return decodeLocalResponse<LocalScheduledRun>(response)
}

export async function cancelLocalSchedule(
  scheduleID: string,
  config: RuntimeClientConfig,
  fetcher: Fetcher = fetch,
): Promise<LocalScheduledRun> {
  const response = await fetcher(`${normalizeBaseURL(config.baseURL)}/v1/schedules/${encodeURIComponent(scheduleID)}`, {
    method: 'DELETE',
    headers: localHeaders(config, false),
  })
  return decodeLocalResponse<LocalScheduledRun>(response)
}

export async function markLocalScheduleNotified(
  scheduleID: string,
  config: RuntimeClientConfig,
  fetcher: Fetcher = fetch,
): Promise<LocalScheduledRun> {
  const response = await fetcher(`${normalizeBaseURL(config.baseURL)}/v1/schedules/${encodeURIComponent(scheduleID)}/notified`, {
    method: 'POST',
    headers: localHeaders(config, false),
  })
  return decodeLocalResponse<LocalScheduledRun>(response)
}

export async function getLocalRunDiagnostics(
  runID: string,
  config: RuntimeClientConfig,
  fetcher: Fetcher = fetch,
): Promise<LocalRunDiagnostics> {
  const response = await fetcher(`${normalizeBaseURL(config.baseURL)}/v1/runs/${encodeURIComponent(runID)}/diagnostics`, {
    method: 'GET',
    headers: localHeaders(config, false),
  })
  return decodeLocalResponse<LocalRunDiagnostics>(response)
}

/** Wipe every persisted note in the authenticated principal's memory namespaces.
 *
 *  Backs the "清空记忆 / Clear memory" button in the agent settings
 *  dialog. The runtime walks only that principal's global/workspace namespaces,
 *  returning the count so the UI can show an accurate toast. Idempotent:
 *  calling on an empty store returns `deleted_count: 0`. */
export async function clearLocalMemory(
  config: RuntimeClientConfig,
  fetcher: Fetcher = fetch,
): Promise<ClearMemoryResponse> {
  const response = await fetcher(`${normalizeBaseURL(config.baseURL)}/v1/memory`, {
    method: 'DELETE',
    headers: localHeaders(config, false),
  })
  return decodeLocalResponse<ClearMemoryResponse>(response)
}

export async function cancelLocalRunCommand(
  commandID: string,
  runID: string,
  config: RuntimeClientConfig,
  fetcher: Fetcher = fetch,
): Promise<CancelRunCommandReceipt> {
  const response = await fetcher(`${normalizeBaseURL(config.baseURL)}/v1/commands`, {
    method: 'POST',
    headers: localHeaders(config, true),
    body: JSON.stringify({ type: 'run.cancel', command_id: commandID, run_id: runID }),
  })
  return decodeLocalResponse<CancelRunCommandReceipt>(response)
}

export async function installLocalPluginCommand(
  commandID: string,
  sourcePath: string,
  options: { expectedDigest?: string; allowUnsigned?: boolean },
  config: RuntimeClientConfig,
  fetcher: Fetcher = fetch,
): Promise<PluginInstallCommandReceipt> {
  const body: PluginInstallCommand = {
    type: 'plugin.install',
    command_id: commandID,
    source_path: sourcePath,
    allow_unsigned: options.allowUnsigned ?? false,
    ...(options.expectedDigest ? { expected_digest: options.expectedDigest } : {}),
  }
  const response = await fetcher(`${normalizeBaseURL(config.baseURL)}/v1/commands`, {
    method: 'POST',
    headers: localHeaders(config, true),
    body: JSON.stringify(body),
  })
  return decodeLocalResponse<PluginInstallCommandReceipt>(response)
}

export async function installLocalRuntimeAssetCommand(
  commandID: string,
  sourcePath: string,
  expectedDigest: string | undefined,
  config: RuntimeClientConfig,
  fetcher: Fetcher = fetch,
): Promise<RuntimeAssetInstallCommandReceipt> {
  const body: RuntimeAssetInstallCommand = {
    type: 'plugin.runtime_asset.install',
    command_id: commandID,
    source_path: sourcePath,
    ...(expectedDigest ? { expected_digest: expectedDigest } : {}),
  }
  const response = await fetcher(`${normalizeBaseURL(config.baseURL)}/v1/commands`, {
    method: 'POST',
    headers: localHeaders(config, true),
    body: JSON.stringify(body),
  })
  return decodeLocalResponse<RuntimeAssetInstallCommandReceipt>(response)
}

export async function getLocalFixedRuntimeAssetStatus(
  pluginID: FixedRuntimeAssetPluginID,
  config: RuntimeClientConfig,
  fetcher: Fetcher = fetch,
): Promise<FixedRuntimeAssetStatus> {
  const response = await fetcher(
    `${normalizeBaseURL(config.baseURL)}/v1/plugins/${encodeURIComponent(pluginID)}/runtime-asset`,
    { method: 'GET', headers: localHeaders(config, false) },
  )
  return decodeLocalResponse<FixedRuntimeAssetStatus>(response)
}

export async function prepareLocalFixedRuntimeAsset(
  pluginID: FixedRuntimeAssetPluginID,
  config: RuntimeClientConfig,
  fetcher: Fetcher = fetch,
): Promise<FixedRuntimeAssetStatus> {
  const response = await fetcher(
    `${normalizeBaseURL(config.baseURL)}/v1/plugins/${encodeURIComponent(pluginID)}/runtime-asset`,
    {
      method: 'PUT',
      headers: localHeaders(config, false),
    },
  )
  return decodeLocalResponse<FixedRuntimeAssetStatus>(response)
}

export async function removeLocalFixedRuntimeAsset(
  pluginID: FixedRuntimeAssetPluginID,
  config: RuntimeClientConfig,
  fetcher: Fetcher = fetch,
): Promise<FixedRuntimeAssetStatus> {
  const response = await fetcher(
    `${normalizeBaseURL(config.baseURL)}/v1/plugins/${encodeURIComponent(pluginID)}/runtime-asset`,
    {
      method: 'DELETE',
      headers: localHeaders(config, false),
    },
  )
  return decodeLocalResponse<FixedRuntimeAssetStatus>(response)
}

export async function bindLocalPluginModelCommand(
  commandID: string,
  pluginID: string,
  bindingID: string,
  model: RuntimeModelSpec,
  expectedDigest: string | undefined,
  config: RuntimeClientConfig,
  fetcher: Fetcher = fetch,
): Promise<PluginModelBindCommandReceipt> {
  const body: PluginModelBindCommand = {
    type: 'plugin.model.bind',
    command_id: commandID,
    plugin_id: pluginID,
    binding_id: bindingID,
    model,
    ...(expectedDigest ? { expected_digest: expectedDigest } : {}),
  }
  const response = await fetcher(`${normalizeBaseURL(config.baseURL)}/v1/commands`, {
    method: 'POST',
    headers: localHeaders(config, true),
    body: JSON.stringify(body),
  })
  return decodeLocalResponse<PluginModelBindCommandReceipt>(response)
}

export async function setLocalPluginEnabledCommand(
  commandID: string,
  pluginID: string,
  enabled: boolean,
  expectedDigest: string | undefined,
  config: RuntimeClientConfig,
  fetcher: Fetcher = fetch,
): Promise<PluginStateCommandReceipt> {
  const response = await fetcher(`${normalizeBaseURL(config.baseURL)}/v1/commands`, {
    method: 'POST',
    headers: localHeaders(config, true),
    body: JSON.stringify({
      type: enabled ? 'plugin.enable' : 'plugin.disable',
      command_id: commandID,
      plugin_id: pluginID,
      ...(expectedDigest ? { expected_digest: expectedDigest } : {}),
    }),
  })
  return decodeLocalResponse<PluginStateCommandReceipt>(response)
}

export async function updateLocalPluginCommand(
  commandID: string,
  pluginID: string,
  sourcePath: string,
  options: { expectedDigest?: string; allowUnsigned?: boolean },
  config: RuntimeClientConfig,
  fetcher: Fetcher = fetch,
): Promise<PluginVersionSwitchCommandReceipt> {
  const response = await fetcher(`${normalizeBaseURL(config.baseURL)}/v1/commands`, {
    method: 'POST',
    headers: localHeaders(config, true),
    body: JSON.stringify({
      type: 'plugin.update',
      command_id: commandID,
      plugin_id: pluginID,
      source_path: sourcePath,
      allow_unsigned: options.allowUnsigned ?? false,
      ...(options.expectedDigest ? { expected_digest: options.expectedDigest } : {}),
    }),
  })
  return decodeLocalResponse<PluginVersionSwitchCommandReceipt>(response)
}

export async function rollbackLocalPluginCommand(
  commandID: string,
  pluginID: string,
  targetDigest: string,
  expectedDigest: string | undefined,
  config: RuntimeClientConfig,
  fetcher: Fetcher = fetch,
): Promise<PluginVersionSwitchCommandReceipt> {
  const response = await fetcher(`${normalizeBaseURL(config.baseURL)}/v1/commands`, {
    method: 'POST',
    headers: localHeaders(config, true),
    body: JSON.stringify({
      type: 'plugin.rollback',
      command_id: commandID,
      plugin_id: pluginID,
      target_digest: targetDigest,
      ...(expectedDigest ? { expected_digest: expectedDigest } : {}),
    }),
  })
  return decodeLocalResponse<PluginVersionSwitchCommandReceipt>(response)
}

export async function removeLocalPluginCommand(
  commandID: string,
  pluginID: string,
  expectedDigest: string | undefined,
  config: RuntimeClientConfig,
  fetcher: Fetcher = fetch,
): Promise<PluginRemoveCommandReceipt> {
  const response = await fetcher(`${normalizeBaseURL(config.baseURL)}/v1/commands`, {
    method: 'POST',
    headers: localHeaders(config, true),
    body: JSON.stringify({
      type: 'plugin.remove',
      command_id: commandID,
      plugin_id: pluginID,
      ...(expectedDigest ? { expected_digest: expectedDigest } : {}),
    }),
  })
  return decodeLocalResponse<PluginRemoveCommandReceipt>(response)
}

export async function answerLocalQuestionCommand(
  commandID: string,
  questionID: string,
  answers: Record<string, string[]>,
  config: RuntimeClientConfig,
  fetcher: Fetcher = fetch,
): Promise<AnswerQuestionCommandReceipt> {
  const response = await fetcher(`${normalizeBaseURL(config.baseURL)}/v1/commands`, {
    method: 'POST',
    headers: localHeaders(config, true),
    body: JSON.stringify({
      type: 'question.answer',
      command_id: commandID,
      question_id: questionID,
      answers,
    }),
  })
  return decodeLocalResponse<AnswerQuestionCommandReceipt>(response)
}

export async function resolveLocalPermissionCommand(
  commandID: string,
  permissionID: string,
  decision: LocalPermissionDecision,
  options: { scope?: LocalPermissionScope, editedAction?: LocalEditedToolAction },
  config: RuntimeClientConfig,
  fetcher: Fetcher = fetch,
): Promise<ResolvePermissionCommandReceipt> {
  const scope = options.scope === 'run' ? 'run' : 'once'
  if (decision === 'edit' && !options.editedAction) {
    throw new Error('editedAction is required for an edit decision')
  }
  const body: Record<string, unknown> = {
    type: 'permission.resolve',
    command_id: commandID,
    permission_id: permissionID,
    decision,
    scope,
  }
  if (options.editedAction) body.edited_action = options.editedAction
  const response = await fetcher(`${normalizeBaseURL(config.baseURL)}/v1/commands`, {
    method: 'POST',
    headers: localHeaders(config, true),
    body: JSON.stringify(body),
  })
  return decodeLocalResponse<ResolvePermissionCommandReceipt>(response)
}

export async function injectLocalRunInstruction(
  runID: string,
  content: string,
  config: RuntimeClientConfig,
  fetcher: Fetcher = fetch,
): Promise<InjectRunInstructionResponse> {
  const response = await fetcher(`${normalizeBaseURL(config.baseURL)}/v1/runs/${encodeURIComponent(runID)}/inject`, {
    method: 'POST',
    headers: localHeaders(config, true),
    body: JSON.stringify({ content }),
  })
  return decodeLocalResponse<InjectRunInstructionResponse>(response)
}

/** Fetch the structured slide outline for a .pptx file.
 *
 *  The right-side DocPreviewPanel's PptxPreview component uses this
 *  to render an outline-style view (per-slide title + bullets +
 *  notes) — pptx has no mature pure-browser renderer, so we surface
 *  structure rather than a faithful visual.
 *
 *  Backed by GET /v1/pptx-outline?path=... which is gated by
 *  the same workspace authorization as workspace-files.
 */
export async function fetchPptxOutline(
  path: string,
  config: RuntimeClientConfig,
  fetcher: Fetcher = fetch,
): Promise<PptxOutlineResponse> {
  const url = `${normalizeBaseURL(config.baseURL)}/v1/pptx-outline?path=${encodeURIComponent(path)}`
  const response = await fetcher(url, {
    method: 'GET',
    headers: localHeaders(config, false),
  })
  if (!response.ok) {
    const text = await response.text().catch(() => response.statusText)
    throw new Error(`pptx outline fetch failed (${response.status}): ${text}`)
  }
  return response.json()
}

/** Parse a PowerPoint outline from an immutable Runtime-owned attachment. */
export async function fetchRunInputPptxOutline(
  runID: string,
  inputID: string,
  config: RuntimeClientConfig,
  fetcher: Fetcher = fetch,
): Promise<PptxOutlineResponse> {
  const url = `${normalizeBaseURL(config.baseURL)}/v1/runs/${encodeURIComponent(runID)}/inputs/${encodeURIComponent(inputID)}/pptx-outline`
  const response = await fetcher(url, {
    method: 'GET',
    headers: localHeaders(config, false),
  })
  if (!response.ok) {
    const text = await response.text().catch(() => response.statusText)
    throw new Error(`pptx outline fetch failed (${response.status}): ${text}`)
  }
  return response.json()
}

/** Stream a file's bytes from an authorized workspace.
 *
 *  Backs the right-side DocPreviewPanel: docx-preview and exceljs both
 *  consume ArrayBuffer, so we hand them the bytes the runtime serves
 *  from `/v1/workspace-files`. The runtime rejects paths that
 *  aren't inside a previously-authorized workspace, so no extra
 *  client-side gating is needed.
 */
export async function fetchWorkspaceFile(
  path: string,
  config: RuntimeClientConfig,
  fetcher: Fetcher = fetch,
): Promise<ArrayBuffer> {
  const url = `${normalizeBaseURL(config.baseURL)}/v1/workspace-files?path=${encodeURIComponent(path)}`
  const response = await fetcher(url, {
    method: 'GET',
    headers: localHeaders(config, false),
  })
  if (!response.ok) {
    const text = await response.text().catch(() => response.statusText)
    throw new Error(`workspace file fetch failed (${response.status}): ${text}`)
  }
  return response.arrayBuffer()
}

/** Download one immutable attachment snapshot owned by a Runtime Run. */
export async function fetchRunInput(
  runID: string,
  inputID: string,
  config: RuntimeClientConfig,
  fetcher: Fetcher = fetch,
  maxBytes = 200 * 1024 * 1024,
): Promise<ArrayBuffer> {
  const url = `${normalizeBaseURL(config.baseURL)}/v1/runs/${encodeURIComponent(runID)}/inputs/${encodeURIComponent(inputID)}`
  const response = await fetcher(url, {
    method: 'GET',
    headers: localHeaders(config, false),
  })
  if (!response.ok) {
    const text = await response.text().catch(() => response.statusText)
    throw new Error(`run input fetch failed (${response.status}): ${text}`)
  }
  const declaredBytes = Number(response.headers.get('content-length'))
  if (Number.isFinite(declaredBytes) && declaredBytes > maxBytes) {
    throw new Error(`run input is too large to preview (${declaredBytes} bytes)`)
  }
  const body = await response.arrayBuffer()
  if (body.byteLength > maxBytes) {
    throw new Error(`run input is too large to preview (${body.byteLength} bytes)`)
  }
  return body
}

export async function listAuthorizedWorkspaces(config: RuntimeClientConfig, fetcher: Fetcher = fetch): Promise<LocalWorkspaceAuthorization[]> {
  const response = await fetcher(`${normalizeBaseURL(config.baseURL)}/v1/workspaces`, {
    method: 'GET',
    headers: localHeaders(config, false),
  })
  const body = await decodeLocalResponse<{ workspaces?: LocalWorkspaceAuthorization[] }>(response)
  return body.workspaces ?? []
}

export async function authorizeLocalWorkspace(
  path: string,
  config: RuntimeClientConfig,
  fetcher: Fetcher = fetch,
): Promise<LocalWorkspaceAuthorization> {
  const response = await fetcher(`${normalizeBaseURL(config.baseURL)}/v1/workspaces`, {
    method: 'POST',
    headers: localHeaders(config, true),
    body: JSON.stringify({ path }),
  })
  return decodeLocalResponse<LocalWorkspaceAuthorization>(response)
}

export async function diagnoseLocalWorkspace(
  path: string,
  config: RuntimeClientConfig,
  fetcher: Fetcher = fetch,
): Promise<LocalWorkspaceDiagnosis> {
  const response = await fetcher(`${normalizeBaseURL(config.baseURL)}/v1/workspaces/diagnose`, {
    method: 'POST',
    headers: localHeaders(config, true),
    body: JSON.stringify({ path }),
  })
  return decodeLocalResponse<LocalWorkspaceDiagnosis>(response)
}

export async function revokeLocalWorkspace(
  workspaceID: string,
  config: RuntimeClientConfig,
  fetcher: Fetcher = fetch,
): Promise<LocalWorkspaceAuthorization> {
  const response = await fetcher(`${normalizeBaseURL(config.baseURL)}/v1/workspaces/${encodeURIComponent(workspaceID)}`, {
    method: 'DELETE',
    headers: localHeaders(config, false),
  })
  return decodeLocalResponse<LocalWorkspaceAuthorization>(response)
}

export async function streamLocalRun(
  runID: string,
  config: RuntimeClientConfig,
  handlers: LocalStreamHandlers,
  fetcher: Fetcher = fetch,
): Promise<{ completed: boolean }> {
  const afterSeq = Math.max(0, Math.floor(handlers.afterSeq ?? 0))
  const suffix = afterSeq > 0 ? `?after=${afterSeq}` : ''
  const response = await fetcher(`${normalizeBaseURL(config.baseURL)}/v1/runs/${encodeURIComponent(runID)}/stream${suffix}`, {
    method: 'GET',
    headers: localHeaders(config, false),
  })
  if (!response.ok || !response.body) {
    const error = await localResponseError(response)
    if (error.code === 'event_cursor_reset_required') {
      throw new LocalStreamCursorResetRequiredError(
        error.message,
        error.resumeAfter ?? 0,
      )
    }
    throw new RuntimeHTTPError(error.message, response.status, error.code)
  }
  const result = await streamAgentSSE(response, {
    onEvent: (event) => handlers.onEvent(event),
    onDelta: (content, event) => handlers.onDelta(content, event),
  })
  return { completed: result.completed }
}

export async function reconcileLocalToolCommand(
  commandID: string,
  operationID: string,
  decision: LocalToolReconciliationDecision,
  config: RuntimeClientConfig,
  fetcher: Fetcher = fetch,
): Promise<ToolReconcileCommandReceipt> {
  const response = await fetcher(`${normalizeBaseURL(config.baseURL)}/v1/commands`, {
    method: 'POST',
    headers: localHeaders(config, true),
    body: JSON.stringify({
      type: 'tool.reconcile',
      command_id: commandID,
      operation_id: operationID,
      decision,
    }),
  })
  return decodeLocalResponse<ToolReconcileCommandReceipt>(response)
}

export async function resolveLocalPlanCommand(
  commandID: string,
  approvalID: string,
  decision: LocalPlanApprovalDecision,
  instructions: string | undefined,
  config: RuntimeClientConfig,
  fetcher: Fetcher = fetch,
): Promise<PlanResolveCommandReceipt> {
  const body: Record<string, unknown> = {
    type: 'plan.resolve',
    command_id: commandID,
    approval_id: approvalID,
    decision,
  }
  const note = instructions?.trim()
  if (decision === 'modify' && !note) {
    throw new Error('instructions are required for a modified plan')
  }
  if (decision === 'modify') {
    body.instructions = note
  }
  const response = await fetcher(`${normalizeBaseURL(config.baseURL)}/v1/commands`, {
    method: 'POST',
    headers: localHeaders(config, true),
    body: JSON.stringify(body),
  })
  return decodeLocalResponse<PlanResolveCommandReceipt>(response)
}

export async function getLocalArtifact(artifactID: string, config: RuntimeClientConfig, fetcher: Fetcher = fetch): Promise<LocalArtifact> {
  const response = await fetcher(`${normalizeBaseURL(config.baseURL)}/v1/artifacts/${encodeURIComponent(artifactID)}`, {
    method: 'GET',
    headers: localHeaders(config, false),
  })
  return decodeLocalResponse<LocalArtifact>(response)
}

export async function getLocalArtifactContent(
  artifactID: string,
  config: RuntimeClientConfig,
  fetcher: Fetcher = fetch,
): Promise<Blob> {
  const response = await fetcher(
    `${normalizeBaseURL(config.baseURL)}/v1/artifacts/${encodeURIComponent(artifactID)}/content`,
    {
      method: 'GET',
      headers: localHeaders(config, false),
    },
  )
  if (!response.ok) {
    const error = await localResponseError(response)
    throw new RuntimeHTTPError(error.message, response.status, error.code)
  }
  return response.blob()
}

function localHeaders(config: RuntimeClientConfig, withContentType: boolean): HeadersInit {
  const headers: HeadersInit = withContentType ? { 'Content-Type': 'application/json' } : {}
  if (config.token) {
    headers.Authorization = `Bearer ${config.token}`
  }
  return headers
}

function normalizeBaseURL(baseURL: string): string {
  return baseURL.replace(/\/$/, '')
}

async function decodeLocalResponse<T>(response: Response): Promise<T> {
  if (!response.ok) {
    const error = await localResponseError(response)
    throw new RuntimeHTTPError(error.message, response.status, error.code)
  }
  return (await response.json()) as T
}

async function localResponseError(response: Response): Promise<{
  message: string
  code?: string
  resumeAfter?: number
}> {
  try {
    // FastAPI's `HTTPException(detail=...)` puts the message in `detail`;
    // some runtime routes use `{error}` or `{message}`. Accept all three —
    // otherwise the UI shows the generic `Runtime HTTP 4xx` for every
    // failure and the actual reason ("goal required", "permission not
    // found", etc.) gets lost.
    const body = (await response.json()) as {
      detail?: string | {
        code?: string
        message?: string
        first_available_seq?: number | null
      } | Array<{ loc?: Array<string | number>; msg?: string; type?: string }>
      error?: string
      message?: string
    }
    const detail = body.detail && typeof body.detail === 'object' && !Array.isArray(body.detail)
      ? body.detail
      : undefined
    const validation = Array.isArray(body.detail) ? body.detail[0] : undefined
    const validationMessage = validation?.msg
      ? `${validation.loc?.join('.') || 'request'}: ${validation.msg}`
      : undefined
    return {
      message: body.message || body.error || detail?.message || validationMessage || (typeof body.detail === 'string' ? body.detail : '') || `Runtime HTTP ${response.status}`,
      ...(detail?.code ? { code: detail.code } : {}),
      ...(typeof detail?.first_available_seq === 'number'
        ? { resumeAfter: Math.max(0, detail.first_available_seq - 1) }
        : {}),
    }
  } catch {
    return { message: `Runtime HTTP ${response.status}` }
  }
}

export interface SheJaneRuntimeClientOptions extends RuntimeClientConfig {
  fetcher?: Fetcher
}

export class SheJaneRuntimeClient {
  readonly config: RuntimeClientConfig
  readonly fetcher: Fetcher

  constructor(options: SheJaneRuntimeClientOptions) {
    const baseURL = normalizeBaseURL(options.baseURL.trim())
    if (!baseURL) throw new Error('baseURL is required')
    this.config = { baseURL, ...(options.token ? { token: options.token } : {}) }
    this.fetcher = options.fetcher ?? fetch
  }

  getRuntimeInfo(): Promise<RuntimeInfo> {
    return getLocalRuntimeInfo(this.config, this.fetcher)
  }

  getSettings(): Promise<RuntimeSettings> {
    return getRuntimeSettings(this.config, this.fetcher)
  }

  updateSettings(input: UpdateRuntimeSettingsRequest): Promise<RuntimeSettings> {
    return updateRuntimeSettings(input, this.config, this.fetcher)
  }

  listModelServicePresets(): Promise<ModelServicePreset[]> {
    return listModelServicePresets(this.config, this.fetcher)
  }

  listModelServices(): Promise<ModelServiceConnection[]> {
    return listModelServices(this.config, this.fetcher)
  }

  startSheJaneAuthorization(): Promise<SheJaneAuthorizationStart> {
    return startSheJaneAuthorization(this.config, this.fetcher)
  }

  getSheJaneAuthorization(authorizationID: string): Promise<SheJaneAuthorizationStatus> {
    return getSheJaneAuthorization(authorizationID, this.config, this.fetcher)
  }

  getCentralDiagnostics(): Promise<CentralDiagnosticsStatus> {
    return getCentralDiagnostics(this.config, this.fetcher)
  }

  updateCentralDiagnostics(
    input: UpdateCentralDiagnosticsRequest,
  ): Promise<CentralDiagnosticsStatus> {
    return updateCentralDiagnostics(input, this.config, this.fetcher)
  }

  listModelCapabilityBindings(): Promise<ModelCapabilityBinding[]> {
    return listModelCapabilityBindings(this.config, this.fetcher)
  }

  setModelCapabilityBinding(
    capability: ModelCapabilityBinding['capability'],
    input: SetModelCapabilityBindingRequest,
  ): Promise<ModelCapabilityBinding> {
    return setModelCapabilityBinding(capability, input, this.config, this.fetcher)
  }

  deleteModelCapabilityBinding(capability: ModelCapabilityBinding['capability']): Promise<void> {
    return deleteModelCapabilityBinding(capability, this.config, this.fetcher)
  }

  connectModelService(input: ConnectModelServiceRequest): Promise<ModelServiceConnection> {
    return connectModelService(input, this.config, this.fetcher)
  }

  refreshModelService(connectionID: string): Promise<ModelServiceConnection> {
    return refreshModelService(connectionID, this.config, this.fetcher)
  }

  reconnectModelService(
    connectionID: string,
    input: ReconnectModelServiceRequest,
  ): Promise<ModelServiceConnection> {
    return reconnectModelService(connectionID, input, this.config, this.fetcher)
  }

  importModelService(input: ImportModelServiceRequest): Promise<ModelServiceConnection> {
    return importModelService(input, this.config, this.fetcher)
  }

  addModelServiceModel(
    connectionID: string,
    input: AddModelServiceModelRequest,
  ): Promise<ModelServiceModel> {
    return addModelServiceModel(connectionID, input, this.config, this.fetcher)
  }

  verifyModelServiceModel(connectionID: string, modelID: string): Promise<ModelServiceModel>
  verifyModelServiceModel(
    connectionID: string,
    modelID: string,
    input: VerifyModelServiceModelRequest,
  ): Promise<ModelServiceModel>
  verifyModelServiceModel(
    connectionID: string,
    modelID: string,
    input?: VerifyModelServiceModelRequest,
  ): Promise<ModelServiceModel> {
    return input
      ? verifyModelServiceModel(connectionID, modelID, input, this.config, this.fetcher)
      : verifyModelServiceModel(connectionID, modelID, this.config, this.fetcher)
  }

  deleteModelService(connectionID: string): Promise<void> {
    return deleteModelService(connectionID, this.config, this.fetcher)
  }

  createRun(input: CreateLocalRunInput): Promise<LocalRun> {
    return createLocalRun(input, this.config, this.fetcher)
  }

  forkRun(commandID: string, input: ForkLocalRunInput): Promise<LocalRun> {
    return forkLocalRun(commandID, input, this.config, this.fetcher)
  }

  deliverCommands(
    commands: PendingRuntimeCommand[],
    settle: (command: PendingRuntimeCommand, result: RuntimeCommandResult) => Promise<void>,
  ): Promise<PendingRuntimeCommandDeliveryReport> {
    return deliverPendingRuntimeCommands(commands, this.config, settle, this.fetcher)
  }

  streamRun(runID: string, handlers: LocalStreamHandlers): Promise<{ completed: boolean }> {
    return streamLocalRun(runID, this.config, handlers, this.fetcher)
  }

  listRuns(): Promise<LocalRun[]> {
    return listLocalRuns(this.config, this.fetcher)
  }

  listPlugins(): Promise<PluginSummary[]> {
    return listLocalPlugins(this.config, this.fetcher)
  }

  getPlugin(pluginID: string): Promise<PluginDetail> {
    return getLocalPlugin(pluginID, this.config, this.fetcher)
  }

  getPluginReadiness(pluginID: string): Promise<PluginReadinessSnapshot> {
    return getLocalPluginReadiness(pluginID, this.config, this.fetcher)
  }

  listThreads(): Promise<{ threads: LocalThread[]; cursor: number }> {
    return listLocalThreads(this.config, this.fetcher)
  }

  getThreadSnapshot(threadID: string): Promise<LocalThreadSnapshot> {
    return getLocalThreadSnapshot(threadID, this.config, this.fetcher)
  }

  listThreadChanges(afterCursor: number): Promise<{
    changes: LocalThreadChange[]
    cursor: number
    resetRequired: boolean
  }> {
    return listLocalThreadChanges(afterCursor, this.config, this.fetcher)
  }

  cancelRun(commandID: string, runID: string): Promise<CancelRunCommandReceipt> {
    return cancelLocalRunCommand(commandID, runID, this.config, this.fetcher)
  }

  installPlugin(
    commandID: string,
    sourcePath: string,
    options: { expectedDigest?: string; allowUnsigned?: boolean } = {},
  ): Promise<PluginInstallCommandReceipt> {
    return installLocalPluginCommand(
      commandID,
      sourcePath,
      options,
      this.config,
      this.fetcher,
    )
  }

  installRuntimeAsset(
    commandID: string,
    sourcePath: string,
    expectedDigest?: string,
  ): Promise<RuntimeAssetInstallCommandReceipt> {
    return installLocalRuntimeAssetCommand(
      commandID,
      sourcePath,
      expectedDigest,
      this.config,
      this.fetcher,
    )
  }

  getFixedRuntimeAssetStatus(
    pluginID: FixedRuntimeAssetPluginID,
  ): Promise<FixedRuntimeAssetStatus> {
    return getLocalFixedRuntimeAssetStatus(pluginID, this.config, this.fetcher)
  }

  prepareFixedRuntimeAsset(
    pluginID: FixedRuntimeAssetPluginID,
  ): Promise<FixedRuntimeAssetStatus> {
    return prepareLocalFixedRuntimeAsset(pluginID, this.config, this.fetcher)
  }

  removeFixedRuntimeAsset(
    pluginID: FixedRuntimeAssetPluginID,
  ): Promise<FixedRuntimeAssetStatus> {
    return removeLocalFixedRuntimeAsset(pluginID, this.config, this.fetcher)
  }

  bindPluginModel(
    commandID: string,
    pluginID: string,
    bindingID: string,
    model: RuntimeModelSpec,
    expectedDigest?: string,
  ): Promise<PluginModelBindCommandReceipt> {
    return bindLocalPluginModelCommand(
      commandID,
      pluginID,
      bindingID,
      model,
      expectedDigest,
      this.config,
      this.fetcher,
    )
  }

  setPluginEnabled(
    commandID: string,
    pluginID: string,
    enabled: boolean,
    expectedDigest?: string,
  ): Promise<PluginStateCommandReceipt> {
    return setLocalPluginEnabledCommand(
      commandID,
      pluginID,
      enabled,
      expectedDigest,
      this.config,
      this.fetcher,
    )
  }

  updatePlugin(
    commandID: string,
    pluginID: string,
    sourcePath: string,
    options: { expectedDigest?: string; allowUnsigned?: boolean } = {},
  ): Promise<PluginVersionSwitchCommandReceipt> {
    return updateLocalPluginCommand(
      commandID,
      pluginID,
      sourcePath,
      options,
      this.config,
      this.fetcher,
    )
  }

  rollbackPlugin(
    commandID: string,
    pluginID: string,
    targetDigest: string,
    expectedDigest?: string,
  ): Promise<PluginVersionSwitchCommandReceipt> {
    return rollbackLocalPluginCommand(
      commandID,
      pluginID,
      targetDigest,
      expectedDigest,
      this.config,
      this.fetcher,
    )
  }

  removePlugin(
    commandID: string,
    pluginID: string,
    expectedDigest?: string,
  ): Promise<PluginRemoveCommandReceipt> {
    return removeLocalPluginCommand(
      commandID,
      pluginID,
      expectedDigest,
      this.config,
      this.fetcher,
    )
  }

  advancePluginSetup(
    commandID: string,
    pluginID: 'org.shejane.computer-use',
    expectedRevision: number,
    actionID: PluginSetupActionID,
  ): Promise<PluginSetupAdvanceCommandReceipt> {
    return advanceLocalPluginSetupCommand(
      commandID,
      pluginID,
      expectedRevision,
      actionID,
      this.config,
      this.fetcher,
    )
  }

  answerQuestion(
    commandID: string,
    questionID: string,
    answers: Record<string, string[]>,
  ): Promise<AnswerQuestionCommandReceipt> {
    return answerLocalQuestionCommand(commandID, questionID, answers, this.config, this.fetcher)
  }

  resolvePermission(
    commandID: string,
    permissionID: string,
    decision: LocalPermissionDecision,
    options: { scope?: LocalPermissionScope; editedAction?: LocalEditedToolAction },
  ): Promise<ResolvePermissionCommandReceipt> {
    return resolveLocalPermissionCommand(
      commandID,
      permissionID,
      decision,
      options,
      this.config,
      this.fetcher,
    )
  }

  resolvePlan(
    commandID: string,
    approvalID: string,
    decision: LocalPlanApprovalDecision,
    instructions?: string,
  ): Promise<PlanResolveCommandReceipt> {
    return resolveLocalPlanCommand(
      commandID,
      approvalID,
      decision,
      instructions,
      this.config,
      this.fetcher,
    )
  }

  reconcileTool(
    commandID: string,
    operationID: string,
    decision: LocalToolReconciliationDecision,
  ): Promise<ToolReconcileCommandReceipt> {
    return reconcileLocalToolCommand(
      commandID,
      operationID,
      decision,
      this.config,
      this.fetcher,
    )
  }
}
