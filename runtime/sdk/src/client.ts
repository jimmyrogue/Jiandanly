import type {
  LocalRun,
  LocalChildRun,
  LocalAgentMessage,
  LocalCollaborationSnapshot,
  LocalThread,
  LocalThreadChange,
  LocalThreadSnapshot,
  CancelRunCommandReceipt,
  AnswerQuestionCommandReceipt,
  ResolvePermissionCommandReceipt,
  PlanResolveCommandReceipt,
  ToolReconcileCommandReceipt,
  PluginInstallCommandReceipt,
  PluginModelBindCommandReceipt,
  RuntimeAssetInstallCommandReceipt,
  FixedRuntimeAssetStatus,
  FixedRuntimeAssetPluginID,
  RuntimeAssetStorage,
  RuntimeAssetCleanupResult,
  RuntimeAssetCleanupScope,
  PluginStateCommandReceipt,
  PluginVersionSwitchCommandReceipt,
  PluginRemoveCommandReceipt,
  PluginReadinessSnapshot,
  PluginSetupAdvanceCommandReceipt,
  PluginSetupActionID,
  PluginSummary,
  PluginDetail,
  LocalPermissionScope,
  LocalPermissionDecision,
  LocalToolReconciliationDecision,
  LocalEditedToolAction,
  LocalPlanApprovalDecision,
} from './types.js'
import { normalizeBaseURL } from './http.js'
import type { Fetcher, RuntimeClientConfig } from './http.js'
import {
  addModelServiceModel,
  connectModelService,
  deleteModelCapabilityBinding,
  deleteModelService,
  getCentralDiagnostics,
  getLocalRuntimeInfo,
  getRuntimeSettings,
  getSheJaneAuthorization,
  importModelService,
  listModelCapabilityBindings,
  listModelServicePresets,
  listModelServices,
  reconnectModelService,
  refreshModelService,
  setModelCapabilityBinding,
  startSheJaneAuthorization,
  updateCentralDiagnostics,
  updateRuntimeSettings,
  verifyModelServiceModel,
} from './model_services.js'
import {
  advanceLocalPluginSetupCommand,
  bindLocalPluginModelCommand,
  cleanupLocalRuntimeAssetStorage,
  getLocalFixedRuntimeAssetStatus,
  getLocalPlugin,
  getLocalPluginReadiness,
  getLocalRuntimeAssetStorage,
  installLocalPluginCommand,
  installLocalRuntimeAssetCommand,
  listLocalPlugins,
  prepareLocalFixedRuntimeAsset,
  removeLocalFixedRuntimeAsset,
  removeLocalPluginCommand,
  rollbackLocalPluginCommand,
  setLocalPluginEnabledCommand,
  updateLocalPluginCommand,
} from './plugin_commands.js'
import {
  answerLocalQuestionCommand,
  cancelLocalRunCommand,
  createLocalRun,
  deliverPendingRuntimeCommands,
  forkLocalRun,
  reconcileLocalToolCommand,
  resolveLocalPermissionCommand,
  resolveLocalPlanCommand,
} from './run_commands.js'
import {
  getLocalCollaborationSnapshot,
  getLocalRun,
  listLocalAgentMessages,
  listLocalChildRuns,
  listLocalRuns,
  streamLocalRun,
} from './runs.js'
import type { LocalStreamHandlers } from './runs.js'
import {
  getLocalThreadSnapshot,
  listLocalThreadChanges,
  listLocalThreads,
} from './threads.js'
import type {
  CreateLocalRunInput,
  ForkLocalRunInput,
  PendingRuntimeCommand,
  PendingRuntimeCommandDeliveryReport,
  RuntimeCommandResult,
} from './run_commands.js'
import type {
  AddModelServiceModelRequest,
  CentralDiagnosticsStatus,
  ConnectModelServiceRequest,
  ImportModelServiceRequest,
  ModelCapabilityBinding,
  ModelServiceConnection,
  ModelServiceModel,
  ModelServicePreset,
  ReconnectModelServiceRequest,
  RuntimeInfo,
  RuntimeModelSpec,
  RuntimeSettings,
  SetModelCapabilityBindingRequest,
  SheJaneAuthorizationStart,
  SheJaneAuthorizationStatus,
  UpdateCentralDiagnosticsRequest,
  UpdateRuntimeSettingsRequest,
  VerifyModelServiceModelRequest,
} from './model_services.js'

export { probeRuntime, RuntimeHTTPError } from './http.js'
export type { RuntimeClientConfig, RuntimeProbe } from './http.js'
export * from './model_services.js'
export * from './plugin_commands.js'
export * from './run_commands.js'
export * from './runs.js'
export * from './schedules.js'
export * from './skills_mcp.js'
export * from './content.js'
export * from './threads.js'
export * from './types.js'

// -- Auto-generated types ----------------------------------------------------
//
// The runtime owns these shapes via pydantic models in
// `runtime/src/shejane_runtime/api_schemas.py`. `make schemas`
// regenerates `openapi.json` + `generated.ts`. Don't hand-edit the
// re-exports — change the pydantic model, regenerate, commit both.
//
// -- Hand-written types (not in OpenAPI) -------------------------------------
//
// Things below this line aren't derivable from openapi.json:
//   • RuntimeClientConfig — caller-provided connection parameters.
//   • RuntimeProbe — the client probe returns a DERIVED `online`
//     bool, not the raw HealthResponse.
//   • LocalStreamHandlers — SSE callback shape. Event payloads live
//     in `AgentRunEvent` which is hand-written because discriminated
//     unions over `event_type` don't roundtrip cleanly through openapi.

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

  getRun(runID: string): Promise<LocalRun> {
    return getLocalRun(runID, this.config, this.fetcher)
  }

  listChildRuns(runID: string): Promise<LocalChildRun[]> {
    return listLocalChildRuns(runID, this.config, this.fetcher)
  }

  getCollaborationSnapshot(runID: string): Promise<LocalCollaborationSnapshot> {
    return getLocalCollaborationSnapshot(runID, this.config, this.fetcher)
  }

  listAgentMessages(runID: string, box: 'inbox' | 'outbox' = 'inbox'): Promise<LocalAgentMessage[]> {
    return listLocalAgentMessages(runID, box, this.config, this.fetcher)
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

  getRuntimeAssetStorage(): Promise<RuntimeAssetStorage> {
    return getLocalRuntimeAssetStorage(this.config, this.fetcher)
  }

  cleanupRuntimeAssetStorage(
    scope: RuntimeAssetCleanupScope,
  ): Promise<RuntimeAssetCleanupResult> {
    return cleanupLocalRuntimeAssetStorage(scope, this.config, this.fetcher)
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
