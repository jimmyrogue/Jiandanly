const { execFile } = require('node:child_process')
const { mkdtemp, rm } = require('node:fs/promises')
const { tmpdir } = require('node:os')
const { basename, dirname, extname, join } = require('node:path')
const { promisify } = require('node:util')

const execFileAsync = promisify(execFile)
const DEFAULT_TIMEOUT_MS = 5 * 60 * 60 * 1_000
const POLL_INTERVAL_MS = 60_000
const TRANSIENT_RETRY_MS = 30_000

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

function isTransientNetworkError(error) {
  const message = [error?.code, error?.stderr, error?.stdout, error?.message, error]
    .filter(Boolean)
    .join('\n')
  return /NSURLErrorDomain Code=-(1001|1005|1009)|offline|No network route|network connection was lost|ECONNRESET|ETIMEDOUT|EAI_AGAIN|ENETUNREACH|HTTP(?:Error)?\(statusCode: 5\d\d/i.test(message)
}

async function notarizeSubmission(artifactPath, options) {
  const {
    authorizationArgs,
    runNotarytool,
    sleep: wait = sleep,
    timeoutMs = DEFAULT_TIMEOUT_MS,
    now = Date.now,
    logger = console,
  } = options
  const submission = await runNotarytool([
    'submit',
    artifactPath,
    ...authorizationArgs,
    '--output-format',
    'json',
  ])
  if (!submission?.id) throw new Error('Apple notarization submission did not return an id')

  const submissionId = submission.id
  const deadline = now() + timeoutMs
  logger.info(`Apple notarization submitted: ${submissionId}`)

  while (now() < deadline) {
    let result
    try {
      result = await runNotarytool([
        'info',
        submissionId,
        ...authorizationArgs,
        '--output-format',
        'json',
      ])
    } catch (error) {
      if (!isTransientNetworkError(error)) throw error
      logger.warn(`Apple notarization status query lost network; retrying submission ${submissionId}`)
      await wait(TRANSIENT_RETRY_MS)
      continue
    }

    if (result.status === 'Accepted') {
      logger.info(`Apple notarization accepted: ${submissionId}`)
      return submissionId
    }
    if (result.status === 'Invalid') {
      let diagnostics = ''
      try {
        diagnostics = JSON.stringify(await runNotarytool([
          'log',
          submissionId,
          ...authorizationArgs,
        ]))
      } catch (error) {
        diagnostics = String(error?.stderr || error?.stdout || error?.message || error)
      }
      throw new Error(`Apple notarization invalid: ${submissionId}\n${diagnostics}`)
    }
    if (result.status !== 'In Progress') {
      throw new Error(`Unexpected Apple notarization status ${JSON.stringify(result.status)} for ${submissionId}`)
    }

    logger.info(`Apple notarization still in progress: ${submissionId}`)
    await wait(POLL_INTERVAL_MS)
  }

  throw new Error(`Apple notarization timed out after ${timeoutMs}ms: ${submissionId}`)
}

function authorizationArgsFromEnvironment(env) {
  const credentials = [env.APPLE_API_KEY, env.APPLE_API_KEY_ID, env.APPLE_API_ISSUER]
  if (credentials.every((value) => !value)) return null
  if (credentials.some((value) => !value)) {
    throw new Error('APPLE_API_KEY, APPLE_API_KEY_ID and APPLE_API_ISSUER must be set together')
  }
  return [
    '--key', env.APPLE_API_KEY,
    '--key-id', env.APPLE_API_KEY_ID,
    '--issuer', env.APPLE_API_ISSUER,
  ]
}

async function runNotarytool(args) {
  try {
    const { stdout } = await execFileAsync('xcrun', ['notarytool', ...args], {
      maxBuffer: 10 * 1024 * 1024,
      timeout: args[0] === 'submit' ? 30 * 60_000 : 2 * 60_000,
    })
    return JSON.parse(stdout.trim())
  } catch (error) {
    if (error instanceof SyntaxError) {
      throw new Error(`notarytool returned invalid JSON: ${error.message}`)
    }
    throw error
  }
}

async function stapleApp(appPath) {
  let lastError
  for (let attempt = 1; attempt <= 5; attempt += 1) {
    try {
      await execFileAsync('xcrun', ['stapler', 'staple', '-v', appPath])
      return
    } catch (error) {
      lastError = error
      if (attempt < 5) await sleep(15_000)
    }
  }
  throw lastError
}

async function macosNotarize(appPath) {
  const authorizationArgs = authorizationArgsFromEnvironment(process.env)
  if (!authorizationArgs) {
    console.info('Skipped macOS notarization because Apple API credentials are unavailable')
    return
  }
  const isAppBundle = extname(appPath) === '.app'
  const temporaryDirectory = isAppBundle
    ? await mkdtemp(join(tmpdir(), 'shejane-notarize-'))
    : null
  const submissionPath = temporaryDirectory
    ? join(temporaryDirectory, `${basename(appPath)}.zip`)
    : appPath
  try {
    if (temporaryDirectory) {
      await execFileAsync('ditto', [
        '-c',
        '-k',
        '--sequesterRsrc',
        '--keepParent',
        basename(appPath),
        submissionPath,
      ], { cwd: dirname(appPath) })
    }
    await notarizeSubmission(submissionPath, {
      authorizationArgs,
      runNotarytool,
      timeoutMs: Number(process.env.SHEJANE_NOTARIZATION_TIMEOUT_MS) || DEFAULT_TIMEOUT_MS,
    })
    await stapleApp(appPath)
    console.info(`Apple notarization ticket stapled: ${appPath}`)
  } finally {
    if (temporaryDirectory) {
      await rm(temporaryDirectory, { recursive: true, force: true })
    }
  }
}

macosNotarize.authorizationArgsFromEnvironment = authorizationArgsFromEnvironment
macosNotarize.isTransientNetworkError = isTransientNetworkError
macosNotarize.notarizeSubmission = notarizeSubmission

module.exports = macosNotarize
