const REQUIRED_RUNTIME_CAPABILITIES = ['agent.run', 'agent.stream']
const BUNDLED_RUNTIME_START_TIMEOUT_MS = 120000
const OFFICIAL_CLIENT_RELEASES_URL = 'https://github.com/jimmyrogue/SheJane/releases/download'

function fixedRuntimeAssetBaseURL(version) {
  return `${OFFICIAL_CLIENT_RELEASES_URL}/client-v${encodeURIComponent(version)}`
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

function isPortConflictError(output) {
  const normalized = String(output).toLowerCase()
  return normalized.includes('address already in use') || normalized.includes('winerror 10048')
}

function waitForRuntimeProcessClose(child, timeoutMs = 2000) {
  if (child.runtimeClosed) {
    return Promise.resolve(true)
  }
  return new Promise((resolve) => {
    const onClose = () => {
      clearTimeout(timer)
      resolve(true)
    }
    const timer = setTimeout(() => {
      child.off('close', onClose)
      resolve(false)
    }, timeoutMs)
    child.once('close', onClose)
  })
}

async function startRuntimeWithPortRetry({
  maxAttempts = 3,
  timeoutMs = BUNDLED_RUNTIME_START_TIMEOUT_MS,
  start,
  ready,
  retryable,
  stop,
}) {
  const deadline = Date.now() + timeoutMs
  for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
    if (Date.now() >= deadline) {
      return null
    }
    const child = await start()
    const remainingMs = Math.max(0, deadline - Date.now())
    if (remainingMs > 0 && await ready(child, remainingMs)) {
      return child
    }

    const stoppedBeforeCleanup = child.exitCode !== null
    await stop(child)
    const shouldRetry = stoppedBeforeCleanup && retryable(child)
    if (!shouldRetry) {
      return null
    }
  }
  return null
}

async function waitForRuntimeReady({
  baseURL,
  token,
  fetchImpl = globalThis.fetch,
  timeoutMs = 30000,
  pollMs = 300,
  requestTimeoutMs = 2000,
  delay = sleep,
  signal,
}) {
  const endpoint = `${baseURL.replace(/\/$/, '')}/v1/runtime`
  const deadline = Date.now() + timeoutMs

  while (Date.now() < deadline) {
    if (signal?.aborted) {
      return false
    }
    const controller = new AbortController()
    const abortRequest = () => controller.abort()
    signal?.addEventListener('abort', abortRequest, { once: true })
    const remainingMs = Math.max(1, deadline - Date.now())
    const requestTimer = setTimeout(
      () => controller.abort(),
      Math.min(requestTimeoutMs, remainingMs),
    )
    try {
      const response = await fetchImpl(endpoint, {
        headers: { Authorization: `Bearer ${token}` },
        signal: controller.signal,
      })
      if (response.ok) {
        const runtime = await response.json()
        const capabilities = Array.isArray(runtime?.capabilities) ? runtime.capabilities : []
        if (
          runtime?.protocol_version === 1 &&
          REQUIRED_RUNTIME_CAPABILITIES.every((capability) => capabilities.includes(capability))
        ) {
          return true
        }
      }
    } catch {
      // The Runtime may still be binding its loopback listener.
    } finally {
      clearTimeout(requestTimer)
      signal?.removeEventListener('abort', abortRequest)
    }
    if (signal?.aborted) {
      return false
    }
    const pollDelayMs = Math.min(pollMs, Math.max(0, deadline - Date.now()))
    if (pollDelayMs > 0) {
      await delay(pollDelayMs)
    }
  }

  return false
}

async function stopRuntimeProcess(child, {
  graceMs = 5000,
  forceExitMs = 2000,
  delay = sleep,
  forceKill,
}) {
  if (!child || child.exitCode !== null) {
    return
  }

  let exited = false
  const exitPromise = new Promise((resolve) => {
    child.once('exit', () => {
      exited = true
      resolve(true)
    })
  })

  let termSent = false
  try {
    child.kill('SIGTERM')
    termSent = true
  } catch {
    // Some platforms can reject the graceful signal even while the process is
    // still alive. Continue into the confirmed force-kill path.
  }

  const graceful = termSent
    ? await Promise.race([
        exitPromise,
        delay(graceMs).then(() => false),
      ])
    : exited || child.exitCode !== null
  if (!graceful && !exited) {
    await forceKill(child.pid)
    const forcedExit = await Promise.race([
      exitPromise,
      delay(forceExitMs).then(() => false),
    ])
    if (!forcedExit && !exited) {
      throw new Error(`Runtime process ${child.pid} did not exit after force kill`)
    }
  }
}

async function installUpdateAfterRuntimeStop({ stopRuntime, quitAndInstall }) {
  await stopRuntime()
  quitAndInstall(false, true)
}

module.exports = {
  BUNDLED_RUNTIME_START_TIMEOUT_MS,
  fixedRuntimeAssetBaseURL,
  installUpdateAfterRuntimeStop,
  isPortConflictError,
  startRuntimeWithPortRetry,
  stopRuntimeProcess,
  waitForRuntimeReady,
  waitForRuntimeProcessClose,
}
