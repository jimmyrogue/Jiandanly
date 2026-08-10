/** Public Runtime protocol aliases generated from OpenAPI. */

import type { components } from './generated.js'

type Schemas = components['schemas']

export type LocalRun = Schemas['LocalRun']
export type LocalChildRun = Schemas['LocalChildRun']
export type LocalAgentMessage = Schemas['LocalAgentMessage']
export type LocalCollaborationSnapshot = Schemas['LocalCollaborationSnapshot']
export type PermissionMode = Schemas['CreateRunRequest']['permission_mode']
export type LocalThread = Schemas['LocalThread']
export type LocalThreadItem = Schemas['LocalThreadItem']
export type LocalThreadChange = Schemas['LocalThreadChange']
export type LocalThreadEvent = Schemas['LocalThreadEvent']
export type LocalThreadSnapshot = Schemas['LocalThreadSnapshot']
export type RunPresentationSnapshot = Schemas['RunPresentationSnapshot']
export type RunPresentationItem = NonNullable<RunPresentationSnapshot['items']>[number]
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
export type RuntimeAssetStorage = Schemas['RuntimeAssetStorage']
export type RuntimeAssetCleanupResult = Schemas['RuntimeAssetCleanupResult']
export type RuntimeAssetCleanupScope = 'history' | 'all'
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
export type ListChildRunsResponse = Schemas['ListChildRunsResponse']
export type ListAgentMessagesResponse = Schemas['ListAgentMessagesResponse']
export type ListRunEventsResponse = Schemas['ListRunEventsResponse']

