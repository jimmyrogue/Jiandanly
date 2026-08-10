/** Runtime-owned Thread pagination, snapshots, and metadata mutations. */

import { decodeLocalResponse, localHeaders, normalizeBaseURL } from './http.js'
import type { Fetcher, RuntimeClientConfig } from './http.js'
import type {
  LocalRun,
  LocalThread,
  LocalThreadChange,
  LocalThreadSnapshot,
  RunPresentationSnapshot,
} from './types.js'

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
    const presentations = new Map<string, RunPresentationSnapshot>()
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
      for (const [runID, presentation] of Object.entries(page.presentations ?? {})) {
        // Pages are fetched newest first. Keep the first projection so an
        // older page containing only the Run's user item cannot overwrite a
        // projection that already included its later final answer.
        if (!presentations.has(runID)) presentations.set(runID, presentation)
      }
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
          presentations: Object.fromEntries(presentations),
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

