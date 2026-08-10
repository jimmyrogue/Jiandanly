/** Plugin lifecycle commands and Runtime Asset management. */

import { decodeLocalResponse, localHeaders, normalizeBaseURL } from './http.js'
import type { Fetcher, RuntimeClientConfig } from './http.js'
import type { RuntimeModelSpec } from './model_services.js'
import type {
  PluginInstallCommand,
  PluginInstallCommandReceipt,
  PluginModelBindCommand,
  PluginModelBindCommandReceipt,
  RuntimeAssetInstallCommand,
  RuntimeAssetInstallCommandReceipt,
  FixedRuntimeAssetStatus,
  FixedRuntimeAssetPluginID,
  RuntimeAssetStorage,
  RuntimeAssetCleanupResult,
  RuntimeAssetCleanupScope,
  PluginStateCommandReceipt,
  PluginVersionSwitchCommandReceipt,
  PluginRemoveCommandReceipt,
  PluginSetupAdvanceCommandReceipt,
  PluginSetupActionID,
  PluginSummary,
  PluginDetail,
  PluginReadinessSnapshot,
} from './types.js'

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

export async function getLocalRuntimeAssetStorage(
  config: RuntimeClientConfig,
  fetcher: Fetcher = fetch,
): Promise<RuntimeAssetStorage> {
  const response = await fetcher(
    `${normalizeBaseURL(config.baseURL)}/v1/plugins/runtime-assets/storage`,
    { method: 'GET', headers: localHeaders(config, false) },
  )
  return decodeLocalResponse<RuntimeAssetStorage>(response)
}

export async function cleanupLocalRuntimeAssetStorage(
  scope: RuntimeAssetCleanupScope,
  config: RuntimeClientConfig,
  fetcher: Fetcher = fetch,
): Promise<RuntimeAssetCleanupResult> {
  const response = await fetcher(
    `${normalizeBaseURL(config.baseURL)}/v1/plugins/runtime-assets/storage?scope=${scope}`,
    { method: 'DELETE', headers: localHeaders(config, false) },
  )
  return decodeLocalResponse<RuntimeAssetCleanupResult>(response)
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
