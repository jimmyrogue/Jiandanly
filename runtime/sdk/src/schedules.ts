/** Scheduled Run CRUD and notification acknowledgement. */

import { decodeLocalResponse, localHeaders, normalizeBaseURL } from './http.js'
import type { Fetcher, RuntimeClientConfig } from './http.js'
import type { RuntimeModelSpec } from './model_services.js'
import { serializeAgentSettings } from './run_commands.js'
import type { AgentSettings, LocalRunMetadata } from './run_commands.js'
import type { LocalScheduledRun, PermissionMode } from './types.js'

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

