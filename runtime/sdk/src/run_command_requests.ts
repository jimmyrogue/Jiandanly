/** Direct Runtime Run and human-decision command requests. */

import { decodeLocalResponse, localHeaders, normalizeBaseURL } from './http.js'
import type { Fetcher, RuntimeClientConfig } from './http.js'
import type { RuntimeModelSpec } from './model_services.js'
import type {
  AnswerQuestionCommandReceipt,
  CancelRunCommandReceipt,
  ForkRunRequest,
  InjectRunInstructionResponse,
  LocalEditedToolAction,
  LocalPermissionDecision,
  LocalPermissionScope,
  LocalPlanApprovalDecision,
  LocalRun,
  LocalToolReconciliationDecision,
  PermissionMode,
  PlanResolveCommandReceipt,
  ReasoningMode,
  ResolvePermissionCommandReceipt,
  ToolReconcileCommandReceipt,
} from './types.js'

export const LOCAL_RUNTIME_PROTOCOL_VERSION = 1

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
  reasoningMode?: ReasoningMode
  permissionMode?: PermissionMode
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

export function serializeAgentSettings(
  settings?: AgentSettings,
): Record<string, unknown> | undefined {
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
    ...(input.reasoningMode ? { reasoning_mode: input.reasoningMode } : {}),
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
  commandID: string,
  runID: string,
  content: string,
  config: RuntimeClientConfig,
  fetcher: Fetcher = fetch,
): Promise<InjectRunInstructionResponse> {
  const response = await fetcher(`${normalizeBaseURL(config.baseURL)}/v1/runs/${encodeURIComponent(runID)}/inject`, {
    method: 'POST',
    headers: localHeaders(config, true),
    body: JSON.stringify({ command_id: commandID, content }),
  })
  return decodeLocalResponse<InjectRunInstructionResponse>(response)
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
