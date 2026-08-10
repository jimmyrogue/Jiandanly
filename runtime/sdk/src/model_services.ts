/** Runtime discovery, settings, authorization, and model-service APIs. */

import type { components } from './generated.js'
import {
  decodeLocalResponse,
  localHeaders,
  normalizeBaseURL,
} from './http.js'
import type { Fetcher, RuntimeClientConfig } from './http.js'

type Schemas = components['schemas']

export type RuntimeModelSpec = `local:${string}:${string}`

export function parseRuntimeModelSpec(value: string): RuntimeModelSpec | undefined {
  const trimmed = value.trim()
  return trimmed.length <= 256 && /^local:[^:\s]+:\S+$/.test(trimmed)
    ? trimmed as RuntimeModelSpec
    : undefined
}

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
