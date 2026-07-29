const fs = require('node:fs')
const path = require('node:path')

const components = new Set(['runtime_launcher', 'updater'])
const categories = new Set(['launch_error', 'unexpected_exit', 'update_error', 'install_error'])
const releasePattern = /^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$/

function recordLocalCrash({ directory, component, category, release, timestamp = new Date().toISOString() }) {
  if (
    typeof directory !== 'string' ||
    !path.isAbsolute(directory) ||
    !components.has(component) ||
    !categories.has(category) ||
    typeof release !== 'string' ||
    release.length > 64 ||
    !releasePattern.test(release) ||
    typeof timestamp !== 'string' ||
    timestamp.length > 32 ||
    !Number.isFinite(Date.parse(timestamp)) ||
    new Date(Date.parse(timestamp)).toISOString() !== timestamp
  ) {
    return false
  }

  try {
    fs.mkdirSync(directory, { recursive: true, mode: 0o700 })
    fs.appendFileSync(
      path.join(directory, 'shejane-local-crash-events.jsonl'),
      `${JSON.stringify({ schema: 1, component, category, release, timestamp })}\n`,
      { encoding: 'utf8', mode: 0o600 },
    )
    return true
  } catch {
    return false
  }
}

function recordRuntimeFailure({ child, directory, release, wasReady, isQuitting }) {
  if (!child || child.localCrashRecorded || child.runtimePortConflict || isQuitting) {
    return false
  }
  child.localCrashRecorded = true
  return recordLocalCrash({
    directory,
    component: 'runtime_launcher',
    category: wasReady ? 'unexpected_exit' : 'launch_error',
    release,
  })
}

module.exports = { recordLocalCrash, recordRuntimeFailure }
