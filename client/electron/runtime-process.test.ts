import { EventEmitter } from 'node:events'
import { createRequire } from 'node:module'
import { describe, expect, it, vi } from 'vitest'

const require = createRequire(import.meta.url)
const {
  BUNDLED_RUNTIME_START_TIMEOUT_MS,
  classifyRuntimeStartupFailure,
  fixedRuntimeAssetBaseURL,
  installUpdateAfterRuntimeStop,
  isPortConflictError,
  startRuntimeWithPortRetry,
  stopRuntimeProcess,
  waitForRuntimeReady,
  waitForRuntimeProcessClose,
} = require('./runtime-process.cjs') as {
  BUNDLED_RUNTIME_START_TIMEOUT_MS: number
  classifyRuntimeStartupFailure: (child: {
    exitCode: number | null
    signalCode?: string | null
    runtimeSpawnError?: string
    runtimeStopRequested?: boolean
  }) => { kind: 'spawn_error' | 'process_exit' | 'timeout', detail?: string, code?: number, signal?: string }
  fixedRuntimeAssetBaseURL: (version: string) => string
  installUpdateAfterRuntimeStop: (options: {
    stopRuntime: () => Promise<void>
    quitAndInstall: (isSilent: boolean, isForceRunAfter: boolean) => void
  }) => Promise<void>
  isPortConflictError: (output: string) => boolean
  startRuntimeWithPortRetry: <T extends { exitCode: number | null }>(options: {
    maxAttempts?: number
    timeoutMs?: number
    start: () => Promise<T>
    ready: (child: T, timeoutMs: number) => Promise<boolean>
    retryable: (child: T) => boolean
    stop: (child: T) => Promise<void>
  }) => Promise<T | null>
  waitForRuntimeReady: (options: {
    baseURL: string
    token: string
    fetchImpl: typeof fetch
    timeoutMs?: number
    pollMs?: number
    requestTimeoutMs?: number
    delay?: (ms: number) => Promise<void>
    signal?: AbortSignal
  }) => Promise<boolean>
  stopRuntimeProcess: (
    child: EventEmitter & { pid: number, exitCode: number | null, kill: (signal: string) => boolean },
    options: {
      graceMs?: number
      forceExitMs?: number
      delay?: (ms: number) => Promise<void>
      forceKill: (pid: number) => void | Promise<void>
    },
  ) => Promise<void>
  waitForRuntimeProcessClose: (
    child: EventEmitter & { runtimeClosed?: boolean },
    timeoutMs?: number,
  ) => Promise<boolean>
}

function runtimeProcess(kill: (signal: string) => boolean) {
  return Object.assign(new EventEmitter(), { pid: 42, exitCode: null, kill })
}

describe('Electron local Runtime process lifecycle', () => {
  it('keeps a bounded cold bundled Runtime readiness deadline', async () => {
    vi.useFakeTimers()
    vi.setSystemTime(0)
    const child = { exitCode: null }
    const ready = vi.fn().mockResolvedValue(true)

    await expect(startRuntimeWithPortRetry({
      start: async () => child,
      ready,
      retryable: () => false,
      stop: async () => undefined,
    })).resolves.toBe(child)
    expect(BUNDLED_RUNTIME_START_TIMEOUT_MS).toBe(120_000)
    expect(ready).toHaveBeenCalledWith(child, BUNDLED_RUNTIME_START_TIMEOUT_MS)
    vi.useRealTimers()
  })

  it('binds on-demand Runtime Assets to the current Client release', () => {
    expect(fixedRuntimeAssetBaseURL('0.1.27')).toBe(
      'https://github.com/jimmyrogue/SheJane/releases/download/client-v0.1.27',
    )
  })

  it('stops the bundled Runtime before handing quit control to the updater', async () => {
    let finishRuntimeStop!: () => void
    const stopRuntime = vi.fn(() => new Promise<void>((resolve) => {
      finishRuntimeStop = resolve
    }))
    const quitAndInstall = vi.fn()

    const install = installUpdateAfterRuntimeStop({ stopRuntime, quitAndInstall })
    await Promise.resolve()

    expect(stopRuntime).toHaveBeenCalledOnce()
    expect(quitAndInstall).not.toHaveBeenCalled()

    finishRuntimeStop()
    await install

    expect(quitAndInstall).toHaveBeenCalledWith(false, true)
  })

  it('recognizes only address-in-use startup errors as port conflicts', () => {
    expect(isPortConflictError("ERROR: [Errno 48] address already in use")).toBe(true)
    expect(isPortConflictError('OSError: [WinError 10048] only one usage is permitted')).toBe(true)
    expect(isPortConflictError('Runtime database migration failed')).toBe(false)
  })

  it('distinguishes an immediate Runtime exit from a real readiness timeout', () => {
    expect(classifyRuntimeStartupFailure({ exitCode: 3, signalCode: null })).toEqual({
      kind: 'process_exit',
      code: 3,
    })
    expect(classifyRuntimeStartupFailure({
      exitCode: null,
      runtimeSpawnError: 'spawn EACCES',
    })).toEqual({ kind: 'spawn_error', detail: 'spawn EACCES' })
    expect(classifyRuntimeStartupFailure({
      exitCode: null,
      signalCode: 'SIGTERM',
      runtimeStopRequested: true,
    })).toEqual({ kind: 'timeout' })
  })

  it('retries a new port only when the previous Runtime already stopped', async () => {
    const stopped = { exitCode: 98, portConflict: false }
    const ready = { exitCode: null }
    const start = vi.fn()
      .mockResolvedValueOnce(stopped)
      .mockResolvedValueOnce(ready)
    const checkReady = vi.fn()
      .mockResolvedValueOnce(false)
      .mockResolvedValueOnce(true)
    const stop = vi.fn().mockImplementation(async (child) => {
      child.portConflict = true
    })

    await expect(startRuntimeWithPortRetry({
      start,
      ready: checkReady,
      retryable: (child) => child.portConflict === true,
      stop,
    })).resolves.toBe(ready)
    expect(start).toHaveBeenCalledTimes(2)
    expect(stop).toHaveBeenCalledOnce()
    expect(stop).toHaveBeenCalledWith(stopped)
  })

  it('waits for stdio close before classifying a stopped Runtime', async () => {
    const child = Object.assign(new EventEmitter(), { runtimeClosed: false })
    const closed = waitForRuntimeProcessClose(child, 100)
    child.runtimeClosed = true
    child.emit('close')

    await expect(closed).resolves.toBe(true)
  })

  it('does not retry a Runtime that is still alive after readiness times out', async () => {
    const alive = { exitCode: null }
    const start = vi.fn().mockResolvedValue(alive)
    const stop = vi.fn().mockResolvedValue(undefined)

    await expect(startRuntimeWithPortRetry({
      start,
      ready: async () => false,
      retryable: () => false,
      stop,
    })).resolves.toBeNull()
    expect(start).toHaveBeenCalledOnce()
    expect(stop).toHaveBeenCalledWith(alive)
  })

  it('does not retry a stopped Runtime unless the failure is a port conflict', async () => {
    const configFailure = { exitCode: 1 }
    const start = vi.fn().mockResolvedValue(configFailure)

    await expect(startRuntimeWithPortRetry({
      start,
      ready: async () => false,
      retryable: () => false,
      stop: async () => undefined,
    })).resolves.toBeNull()
    expect(start).toHaveBeenCalledOnce()
  })

  it('shares one startup deadline across port retries', async () => {
    vi.useFakeTimers()
    vi.setSystemTime(0)
    const conflict = { exitCode: 98 }
    const start = vi.fn().mockResolvedValue(conflict)

    await expect(startRuntimeWithPortRetry({
      timeoutMs: 30,
      start,
      ready: async () => {
        vi.setSystemTime(30)
        return false
      },
      retryable: () => true,
      stop: async () => undefined,
    })).resolves.toBeNull()
    expect(start).toHaveBeenCalledOnce()
    vi.useRealTimers()
  })

  it('waits for the authenticated Runtime protocol and required capabilities', async () => {
    const fetchImpl = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ status: 'ok' }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        protocol_version: 1,
        capabilities: ['agent.run', 'agent.stream'],
      }), { status: 200 }))

    await expect(waitForRuntimeReady({
      baseURL: 'http://127.0.0.1:17371',
      token: 'pairing-token',
      fetchImpl,
      timeoutMs: 100,
      pollMs: 1,
      delay: async () => undefined,
    })).resolves.toBe(true)
    expect(fetchImpl).toHaveBeenLastCalledWith(
      'http://127.0.0.1:17371/v1/runtime',
      expect.objectContaining({
        headers: { Authorization: 'Bearer pairing-token' },
        signal: expect.any(AbortSignal),
      }),
    )
  })

  it('lets the Runtime exit gracefully before the deadline', async () => {
    const forceKill = vi.fn()
    const child = runtimeProcess((signal) => {
      expect(signal).toBe('SIGTERM')
      child.emit('exit', 0, signal)
      return true
    })

    await stopRuntimeProcess(child, {
      forceKill,
      delay: async () => undefined,
    })

    expect(forceKill).not.toHaveBeenCalled()
  })

  it('aborts a hung readiness request at the overall deadline', async () => {
    vi.useFakeTimers()
    const fetchImpl = vi.fn((_url: string, init?: RequestInit) => new Promise<Response>((_resolve, reject) => {
      init?.signal?.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')))
    }))

    const ready = waitForRuntimeReady({
      baseURL: 'http://127.0.0.1:17371',
      token: 'pairing-token',
      fetchImpl: fetchImpl as typeof fetch,
      timeoutMs: 20,
      requestTimeoutMs: 20,
      pollMs: 1,
    })
    await vi.advanceTimersByTimeAsync(20)

    await expect(ready).resolves.toBe(false)
    expect(fetchImpl).toHaveBeenCalledTimes(1)
    vi.useRealTimers()
  })

  it('force-kills a Runtime that misses the graceful deadline', async () => {
    const forceKill = vi.fn()
    const child = runtimeProcess(() => true)
    forceKill.mockImplementation(() => {
      child.emit('exit', null, 'SIGKILL')
    })

    await stopRuntimeProcess(child, {
      forceKill,
      graceMs: 1,
      delay: async () => undefined,
    })

    expect(forceKill).toHaveBeenCalledWith(42)
  })

  it('falls back to a confirmed force kill when SIGTERM cannot be sent', async () => {
    const forceKill = vi.fn()
    const child = runtimeProcess(() => {
      throw new Error('SIGTERM unavailable')
    })
    forceKill.mockImplementation(() => {
      child.emit('exit', null, 'SIGKILL')
    })

    await stopRuntimeProcess(child, {
      forceKill,
      delay: async () => undefined,
    })

    expect(forceKill).toHaveBeenCalledWith(42)
  })

  it('reports a failed force kill instead of silently confirming shutdown', async () => {
    const child = runtimeProcess(() => true)

    await expect(stopRuntimeProcess(child, {
      forceKill: async () => {
        throw new Error('taskkill failed')
      },
      graceMs: 1,
      delay: async () => undefined,
    })).rejects.toThrow('taskkill failed')
  })

  it('requires an exit event after the force-kill command completes', async () => {
    const child = runtimeProcess(() => true)

    await expect(stopRuntimeProcess(child, {
      forceKill: async () => undefined,
      graceMs: 1,
      forceExitMs: 1,
      delay: async () => undefined,
    })).rejects.toThrow('did not exit after force kill')
  })
})
