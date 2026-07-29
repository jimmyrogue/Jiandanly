const fs = require('node:fs')
const path = require('node:path')

function installDesktopSmokeQuitWatcher({
  filePath = process.env.SHEJANE_CLIENT_SMOKE_QUIT_FILE || '',
  quit,
  intervalMs = 100,
} = {}) {
  if (!filePath || typeof quit !== 'function') {
    return false
  }
  let timer = setInterval(() => {
    if (!fs.existsSync(filePath)) {
      return
    }
    clearInterval(timer)
    timer = null
    quit()
  }, intervalMs)
  timer.unref?.()
  return () => {
    if (timer !== null) {
      clearInterval(timer)
      timer = null
    }
  }
}

function writeDesktopSmokeConfig({
  filePath = process.env.SHEJANE_CLIENT_SMOKE_FILE || '',
  baseURL,
  token,
  resourcesPath,
  runtimePid,
  crashDirectory,
} = {}) {
  if (!filePath) {
    return false
  }
  const payload = {
    schema: 1,
    baseURL: String(baseURL || ''),
    token: String(token || ''),
    resourcesPath: String(resourcesPath || ''),
    runtimePid: Number(runtimePid || 0),
    crashDirectory: String(crashDirectory || ''),
    writtenAt: new Date().toISOString(),
  }
  fs.mkdirSync(path.dirname(filePath), { recursive: true })
  fs.writeFileSync(filePath, JSON.stringify(payload, null, 2), { mode: 0o600 })
  return true
}

module.exports = {
  installDesktopSmokeQuitWatcher,
  writeDesktopSmokeConfig,
}
