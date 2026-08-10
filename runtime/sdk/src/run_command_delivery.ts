/** Ordered delivery for Client-owned pending Runtime commands. */

import { RuntimeHTTPError } from './http.js'
import type { Fetcher, RuntimeClientConfig } from './http.js'
import type { RuntimeModelSpec } from './model_services.js'
import {
  bindLocalPluginModelCommand,
  installLocalPluginCommand,
  installLocalRuntimeAssetCommand,
  removeLocalPluginCommand,
  rollbackLocalPluginCommand,
  setLocalPluginEnabledCommand,
  updateLocalPluginCommand,
} from './plugin_commands.js'
import {
  answerLocalQuestionCommand,
  cancelLocalRunCommand,
  createLocalRun,
  forkLocalRun,
  injectLocalRunInstruction,
  reconcileLocalToolCommand,
  resolveLocalPermissionCommand,
  resolveLocalPlanCommand,
} from './run_command_requests.js'
import type { CreateLocalRunInput, ForkLocalRunInput } from './run_command_requests.js'
import type {
  AnswerQuestionCommandReceipt,
  CancelRunCommandReceipt,
  InjectRunInstructionResponse,
  LocalEditedToolAction,
  LocalPermissionDecision,
  LocalPermissionScope,
  LocalPlanApprovalDecision,
  LocalRun,
  LocalToolReconciliationDecision,
  PlanResolveCommandReceipt,
  PluginInstallCommandReceipt,
  PluginModelBindCommandReceipt,
  PluginRemoveCommandReceipt,
  PluginStateCommandReceipt,
  PluginVersionSwitchCommandReceipt,
  ResolvePermissionCommandReceipt,
  RuntimeAssetInstallCommandReceipt,
  ToolReconcileCommandReceipt,
} from './types.js'

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


export interface PendingRunForkCommand extends PendingRuntimeCommandBase {
  type: 'run.fork'
  input: ForkLocalRunInput
}

export interface PendingRunCancelCommand extends PendingRuntimeCommandBase {
  type: 'run.cancel'
  input: { runId: string; threadId: string }
}

export interface PendingRunInjectCommand extends PendingRuntimeCommandBase {
  type: 'run.inject'
  input: { runId: string; threadId: string; content: string }
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
  | PendingRunInjectCommand
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
  | InjectRunInstructionResponse
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
    case 'run.inject':
      return injectLocalRunInstruction(
        command.commandId,
        command.input.runId,
        command.input.content,
        config,
        fetcher,
      )
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
