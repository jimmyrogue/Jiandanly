/** Runtime Run reads, diagnostics, event replay, and SSE streaming. */

import { streamAgentSSE, type AgentRunEvent } from './sse.js'
import {
  decodeLocalResponse,
  localHeaders,
  localResponseError,
  normalizeBaseURL,
  RuntimeHTTPError,
} from './http.js'
import type { Fetcher, RuntimeClientConfig } from './http.js'
import type {
  ClearMemoryResponse,
  ListAgentMessagesResponse,
  ListChildRunsResponse,
  ListRunEventsResponse,
  LocalAgentMessage,
  LocalChildRun,
  LocalCollaborationSnapshot,
  LocalRun,
  LocalRunDiagnostics,
  LocalThreadEvent,
} from './types.js'

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

export async function listLocalRuns(config: RuntimeClientConfig, fetcher: Fetcher = fetch): Promise<LocalRun[]> {
  const response = await fetcher(`${normalizeBaseURL(config.baseURL)}/v1/runs`, {
    method: 'GET',
    headers: localHeaders(config, false),
  })
  const body = await decodeLocalResponse<{ runs?: LocalRun[] }>(response)
  return body.runs ?? []
}

export async function getLocalRun(
  runID: string,
  config: RuntimeClientConfig,
  fetcher: Fetcher = fetch,
): Promise<LocalRun> {
  const response = await fetcher(
    `${normalizeBaseURL(config.baseURL)}/v1/runs/${encodeURIComponent(runID)}`,
    { method: 'GET', headers: localHeaders(config, false) },
  )
  return decodeLocalResponse<LocalRun>(response)
}

export async function listLocalChildRuns(
  runID: string,
  config: RuntimeClientConfig,
  fetcher: Fetcher = fetch,
): Promise<LocalChildRun[]> {
  const response = await fetcher(
    `${normalizeBaseURL(config.baseURL)}/v1/runs/${encodeURIComponent(runID)}/children`,
    { method: 'GET', headers: localHeaders(config, false) },
  )
  const body = await decodeLocalResponse<ListChildRunsResponse>(response)
  return body.children
}

export async function getLocalCollaborationSnapshot(
  runID: string,
  config: RuntimeClientConfig,
  fetcher: Fetcher = fetch,
): Promise<LocalCollaborationSnapshot> {
  const response = await fetcher(
    `${normalizeBaseURL(config.baseURL)}/v1/runs/${encodeURIComponent(runID)}/collaboration`,
    { method: 'GET', headers: localHeaders(config, false) },
  )
  return decodeLocalResponse<LocalCollaborationSnapshot>(response)
}

export async function listLocalAgentMessages(
  runID: string,
  box: 'inbox' | 'outbox',
  config: RuntimeClientConfig,
  fetcher: Fetcher = fetch,
): Promise<LocalAgentMessage[]> {
  const response = await fetcher(
    `${normalizeBaseURL(config.baseURL)}/v1/runs/${encodeURIComponent(runID)}/mailbox?box=${box}`,
    { method: 'GET', headers: localHeaders(config, false) },
  )
  const body = await decodeLocalResponse<ListAgentMessagesResponse>(response)
  return body.messages
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

export async function listLocalRunEvents(
  runID: string,
  afterSeq: number,
  config: RuntimeClientConfig,
  fetcher: Fetcher = fetch,
): Promise<LocalThreadEvent[]> {
  const events: LocalThreadEvent[] = []
  let after = Number.isSafeInteger(afterSeq) ? Math.max(0, afterSeq) : 0
  for (let pageNumber = 0; pageNumber < 10_000; pageNumber += 1) {
    const params = new URLSearchParams({ after: String(after), limit: '1000' })
    const response = await fetcher(
      `${normalizeBaseURL(config.baseURL)}/v1/runs/${encodeURIComponent(runID)}/events?${params.toString()}`,
      { method: 'GET', headers: localHeaders(config, false) },
    )
    const page = await decodeLocalResponse<ListRunEventsResponse>(response)
    events.push(...page.events)
    if (!page.has_more) return events
    if (!Number.isSafeInteger(page.next_after) || page.next_after <= after) {
      throw new Error('Runtime returned an invalid run event cursor')
    }
    after = page.next_after
  }
  throw new Error('Runtime run event pagination limit exceeded')
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

