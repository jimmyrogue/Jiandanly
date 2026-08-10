/** Shared authenticated HTTP transport for the Runtime SDK. */

export interface RuntimeClientConfig {
  baseURL: string
  token?: string
}

export interface RuntimeProbe {
  online: boolean
  status?: string
  mode?: string
  worker?: string
}

export class RuntimeHTTPError extends Error {
  override name = 'RuntimeHTTPError'

  constructor(
    message: string,
    readonly status: number,
    readonly code?: string,
  ) {
    super(message)
  }
}

export type Fetcher = typeof fetch

export async function probeRuntime(
  baseURL: string,
  fetcher: Fetcher = fetch,
): Promise<RuntimeProbe> {
  const controller = new AbortController()
  const timeout = globalThis.setTimeout(() => controller.abort(), 1200)
  try {
    const response = await fetcher(`${normalizeBaseURL(baseURL)}/v1/health`, {
      signal: controller.signal,
    })
    if (!response.ok) {
      return { online: false }
    }
    const body = (await response.json()) as { status?: string; mode?: string; worker?: string }
    return {
      online: body.status === 'ok',
      status: body.status,
      mode: body.mode,
      worker: body.worker,
    }
  } catch {
    return { online: false }
  } finally {
    globalThis.clearTimeout(timeout)
  }
}

export function localHeaders(
  config: RuntimeClientConfig,
  withContentType: boolean,
): HeadersInit {
  const headers: HeadersInit = withContentType ? { 'Content-Type': 'application/json' } : {}
  if (config.token) {
    headers.Authorization = `Bearer ${config.token}`
  }
  return headers
}

export function normalizeBaseURL(baseURL: string): string {
  return baseURL.replace(/\/$/, '')
}

export async function decodeLocalResponse<T>(response: Response): Promise<T> {
  if (!response.ok) {
    const error = await localResponseError(response)
    throw new RuntimeHTTPError(error.message, response.status, error.code)
  }
  return (await response.json()) as T
}

export async function localResponseError(response: Response): Promise<{
  message: string
  code?: string
  resumeAfter?: number
}> {
  try {
    const body = (await response.json()) as {
      detail?:
        | string
        | {
            code?: string
            message?: string
            first_available_seq?: number | null
          }
        | Array<{ loc?: Array<string | number>; msg?: string; type?: string }>
      error?: string
      message?: string
    }
    const detail =
      body.detail && typeof body.detail === 'object' && !Array.isArray(body.detail)
        ? body.detail
        : undefined
    const validation = Array.isArray(body.detail) ? body.detail[0] : undefined
    const validationMessage = validation?.msg
      ? `${validation.loc?.join('.') || 'request'}: ${validation.msg}`
      : undefined
    return {
      message:
        body.message ||
        body.error ||
        detail?.message ||
        validationMessage ||
        (typeof body.detail === 'string' ? body.detail : '') ||
        `Runtime HTTP ${response.status}`,
      ...(detail?.code ? { code: detail.code } : {}),
      ...(typeof detail?.first_available_seq === 'number'
        ? { resumeAfter: Math.max(0, detail.first_available_seq - 1) }
        : {}),
    }
  } catch {
    return { message: `Runtime HTTP ${response.status}` }
  }
}
