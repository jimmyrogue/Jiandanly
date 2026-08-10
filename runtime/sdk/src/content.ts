/** Runtime-owned workspace files, Run inputs, and Artifacts. */

import {
  decodeLocalResponse,
  localHeaders,
  localResponseError,
  normalizeBaseURL,
  RuntimeHTTPError,
} from './http.js'
import type { Fetcher, RuntimeClientConfig } from './http.js'
import type {
  LocalArtifact,
  LocalWorkspaceAuthorization,
  LocalWorkspaceDiagnosis,
  PptxOutlineResponse,
} from './types.js'

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

