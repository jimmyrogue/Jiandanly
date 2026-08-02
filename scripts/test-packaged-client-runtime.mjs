#!/usr/bin/env node

import { execFile } from 'node:child_process'
import { constants } from 'node:fs'
import { access, mkdir, mkdtemp, readFile, readdir, rm, writeFile } from 'node:fs/promises'
import { createRequire } from 'node:module'
import { tmpdir } from 'node:os'
import { basename, dirname, join, resolve } from 'node:path'
import { promisify } from 'node:util'
import { spawn } from 'node:child_process'

const execFileAsync = promisify(execFile)
const packagedPath = resolve(process.argv[2] || '')
const isMacOSApp = process.platform === 'darwin' && packagedPath.endsWith('.app')
const isWindowsExecutable = process.platform === 'win32' && packagedPath.endsWith('.exe')
if (!isMacOSApp && !isWindowsExecutable) {
  throw new Error(
    'usage: node scripts/test-packaged-client-runtime.mjs /path/to/App.app-or-App.exe',
  )
}

const wait = (milliseconds) => new Promise((done) => setTimeout(done, milliseconds))
const PACKAGED_RUNTIME_START_TIMEOUT_MS = 180_000
const PROCESS_EXIT_TIMEOUT_MS = 10_000
const ALLOWED_ELECTRON_LOCALES = new Set([
  'en',
  'en-GB',
  'en-US',
  'zh-CN',
  'zh-TW',
  'zh_CN',
  'zh_TW',
])
const FORBIDDEN_PACKAGED_MODULES = [
  '@lexical/react',
  '@shejane/runtime-sdk',
  '@tabler/icons-react',
  '@tailwindcss/vite',
  '@vitejs/plugin-react',
  'docx-preview',
  'highlight.js',
  'lexical',
  'radix-ui',
  'react',
  'react-dom',
  'react-markdown',
  'shadcn',
  'tailwindcss',
]
const FORBIDDEN_FROZEN_RUNTIME_PATHS = [
  '_internal/builtin-assets',
  '_internal/_pytest',
  '_internal/mypy',
  '_internal/onnxruntime/quantization',
  '_internal/onnxruntime/tools',
  '_internal/onnxruntime/transformers',
  '_internal/pytest',
  '_internal/ruff',
  '_internal/sympy',
]

async function waitUntil(check, { timeoutMs, failure }) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    const value = await check()
    if (value) {
      return value
    }
    await wait(50)
  }
  throw new Error(failure)
}

function processExists(pid) {
  try {
    process.kill(pid, 0)
    return true
  } catch (error) {
    if (error?.code === 'ESRCH') {
      return false
    }
    throw error
  }
}

async function assertPathMissing(path, message) {
  try {
    await access(path)
  } catch (error) {
    if (error?.code === 'ENOENT') return
    throw error
  }
  throw new Error(message)
}

async function verifyPackagedContents() {
  const appAsar = join(resourcesPath, 'app.asar')
  const require = createRequire(import.meta.url)
  const electronBuilderPackage = require.resolve('electron-builder/package.json', {
    paths: [resolve('client')],
  })
  const { listPackage } = createRequire(electronBuilderPackage)('@electron/asar')
  const packagedFiles = listPackage(appAsar)
  for (const moduleName of FORBIDDEN_PACKAGED_MODULES) {
    const prefix = `/node_modules/${moduleName}/`
    if (packagedFiles.some((path) => path.startsWith(prefix))) {
      throw new Error(`packaged Client contains bundled Renderer dependency: ${moduleName}`)
    }
  }
  for (const moduleName of ['@anthropic-ai/sandbox-runtime', 'electron-updater']) {
    const prefix = `/node_modules/${moduleName}/`
    if (!packagedFiles.some((path) => path.startsWith(prefix))) {
      throw new Error(`packaged Client is missing required production dependency: ${moduleName}`)
    }
  }
  const sourceMap = packagedFiles.find((path) => path.endsWith('.map'))
  if (sourceMap) {
    throw new Error(`packaged Client contains a production source map: ${sourceMap}`)
  }
  const zodSource = packagedFiles.find((path) => path.startsWith('/node_modules/zod/src/'))
  if (zodSource) {
    throw new Error(`packaged Client contains Zod source or test files: ${zodSource}`)
  }
  const sandboxType = packagedFiles.find(
    (path) => path.startsWith('/node_modules/@anthropic-ai/sandbox-runtime/') && path.endsWith('.d.ts'),
  )
  if (sandboxType) {
    throw new Error(`packaged Client contains Sandbox Runtime type declarations: ${sandboxType}`)
  }
  if (isMacOSApp) {
    const seccomp = packagedFiles.find((path) =>
      path.startsWith('/node_modules/@anthropic-ai/sandbox-runtime/vendor/seccomp/'),
    )
    if (seccomp) {
      throw new Error(`macOS Client contains Linux-only seccomp files: ${seccomp}`)
    }
  }

  const localeRoot = isMacOSApp
    ? join(
        packagedPath,
        'Contents',
        'Frameworks',
        'Electron Framework.framework',
        'Versions',
        'A',
        'Resources',
      )
    : join(dirname(resourcesPath), 'locales')
  const localeSuffix = isMacOSApp ? '.lproj' : '.pak'
  const locales = (await readdir(localeRoot))
    .filter((name) => name.endsWith(localeSuffix))
    .map((name) => name.slice(0, -localeSuffix.length))
  const unexpectedLocales = locales.filter((name) => !ALLOWED_ELECTRON_LOCALES.has(name))
  if (locales.length === 0 || unexpectedLocales.length > 0) {
    throw new Error(`packaged Client contains unexpected Electron locales: ${unexpectedLocales}`)
  }

  const runtimeRoot = join(resourcesPath, 'runtime')
  for (const relative of FORBIDDEN_FROZEN_RUNTIME_PATHS) {
    await assertPathMissing(
      join(runtimeRoot, ...relative.split('/')),
      `packaged Runtime contains excluded build-only path: ${relative}`,
    )
  }
}

const temporaryRoot = await mkdtemp(join(tmpdir(), 'shejane-packaged-client-smoke-'))
const smokeFile = join(temporaryRoot, 'runtime.json')
const quitFile = join(temporaryRoot, 'quit')
const home = join(temporaryRoot, 'home')
const userData = join(temporaryRoot, 'user-data')
const nativeCrashDirectory = join(temporaryRoot, 'native-crashes')
const resourcesPath = isMacOSApp
  ? join(packagedPath, 'Contents', 'Resources')
  : join(dirname(packagedPath), 'resources')
const macOSDirectory = isMacOSApp ? join(packagedPath, 'Contents', 'MacOS') : null
const vmAssets = join(resourcesPath, 'sandbox', 'vm-assets')
const runtimeExecutable = join(
  resourcesPath,
  'runtime',
  process.platform === 'win32' ? 'shejane-runtime.exe' : 'shejane-runtime',
)
let appProcess
let runtimePid = 0
let stdout = ''
let stderr = ''
let primaryError = null

try {
  await access(resourcesPath, constants.R_OK)
  await access(runtimeExecutable, constants.X_OK)
  await verifyPackagedContents()
  await assertPathMissing(vmAssets, 'packaged Client unexpectedly contains Managed Worker VM assets')
  await mkdir(home, { recursive: true })
  await mkdir(userData, { recursive: true })
  let executable = packagedPath
  if (macOSDirectory) {
    const executableNames = (await readdir(macOSDirectory, { withFileTypes: true }))
      .filter((entry) => entry.isFile())
      .map((entry) => entry.name)
    if (executableNames.length !== 1) {
      throw new Error(`packaged app has an ambiguous main executable: ${executableNames.join(', ')}`)
    }
    executable = join(macOSDirectory, executableNames[0])
  }
  appProcess = spawn(executable, [`--user-data-dir=${userData}`], {
    env: {
      ...process.env,
      HOME: home,
      USERPROFILE: home,
      TMPDIR: temporaryRoot,
      TEMP: temporaryRoot,
      TMP: temporaryRoot,
      SHEJANE_CLIENT_SMOKE_FILE: smokeFile,
      SHEJANE_CLIENT_SMOKE_QUIT_FILE: quitFile,
    },
    stdio: ['ignore', 'pipe', 'pipe'],
  })
  appProcess.stdout.on('data', (chunk) => {
    stdout = `${stdout}${chunk}`.slice(-32_768)
  })
  appProcess.stderr.on('data', (chunk) => {
    stderr = `${stderr}${chunk}`.slice(-32_768)
  })

  const handoff = await waitUntil(
    async () => {
      try {
        return JSON.parse(await readFile(smokeFile, 'utf8'))
      } catch (error) {
        if (error?.code === 'ENOENT' || error instanceof SyntaxError) {
          if (appProcess.exitCode !== null) {
            throw new Error(`packaged app exited before Runtime became ready (${appProcess.exitCode})`)
          }
          return null
        }
        throw error
      }
    },
    {
      timeoutMs: PACKAGED_RUNTIME_START_TIMEOUT_MS,
      failure: 'packaged app did not publish its Runtime handoff',
    },
  )
  if (
    handoff.schema !== 1 ||
    typeof handoff.baseURL !== 'string' ||
    !handoff.baseURL.startsWith('http://127.0.0.1:') ||
    typeof handoff.token !== 'string' ||
    handoff.token.length < 32 ||
    resolve(handoff.resourcesPath) !== resolve(resourcesPath) ||
    !Number.isSafeInteger(handoff.runtimePid) ||
    handoff.runtimePid <= 0 ||
    typeof handoff.crashDirectory !== 'string' ||
    handoff.crashDirectory.length === 0
  ) {
    throw new Error('packaged app published an invalid Runtime handoff')
  }
  runtimePid = handoff.runtimePid
  await access(handoff.crashDirectory, constants.R_OK | constants.W_OK)
  const bundledRuntimeCrashFiles = (await readdir(handoff.crashDirectory))
    .filter((filename) => filename.startsWith('runtime-native-') && filename.endsWith('.log'))
  if (bundledRuntimeCrashFiles.length === 0) {
    throw new Error('packaged Client did not inject the native crash directory into Runtime')
  }
  const headers = { Authorization: `Bearer ${handoff.token}` }
  const health = await fetch(`${handoff.baseURL}/v1/health`, { headers })
  if (!health.ok || (await health.json()).status !== 'ok') {
    throw new Error(`packaged Runtime health failed with HTTP ${health.status}`)
  }
  const plugins = await fetch(`${handoff.baseURL}/v1/plugins`, { headers })
  if (!plugins.ok || !Array.isArray((await plugins.json()).plugins)) {
    throw new Error(`packaged Runtime plugin catalog failed with HTTP ${plugins.status}`)
  }
  const presets = await fetch(`${handoff.baseURL}/v1/model-services/presets`, { headers })
  const presetCatalog = presets.ok ? await presets.json() : null
  const official = presetCatalog?.services?.[0]
  if (
    official?.id !== 'shejane-official'
    || official.connection_method !== 'browser_authorization'
    || official.regions?.length !== 0
  ) {
    throw new Error(`packaged Runtime official-service preset failed with HTTP ${presets.status}`)
  }
  const authorization = await fetch(
    `${handoff.baseURL}/v1/model-services/shejane/authorization`,
    { method: 'POST', headers },
  )
  const authorizationBody = await authorization.json()
  if (authorization.status !== 201) {
    throw new Error(`packaged Runtime authorization start failed with HTTP ${authorization.status}`)
  }
  const authorizationURL = new URL(authorizationBody.authorization_url)
  const callbackURL = new URL(authorizationURL.searchParams.get('redirect_uri') || '')
  if (
    authorizationURL.origin !== 'https://app.shejane.com'
    || callbackURL.hostname !== '127.0.0.1'
    || callbackURL.pathname !== '/shejane/auth/callback'
  ) {
    throw new Error('packaged Runtime published an invalid official authorization URL')
  }

  if (process.platform === 'darwin') {
    const { stdout: command } = await execFileAsync('/bin/ps', [
      '-p',
      String(runtimePid),
      '-o',
      'command=',
    ])
    if (command.includes('--managed-worker-vm-assets')) {
      throw new Error('normal Client startup injected unexpected Managed Worker VM assets')
    }
  }

  await writeFile(quitFile, '')
  await waitUntil(
    async () => appProcess.exitCode !== null,
    { timeoutMs: 30_000, failure: 'packaged app did not exit through its normal quit lifecycle' },
  )
  if (appProcess.exitCode !== 0) {
    throw new Error(`packaged app exited with code ${appProcess.exitCode}`)
  }
  await waitUntil(
    async () => !processExists(runtimePid),
    { timeoutMs: 10_000, failure: 'packaged app left its bundled Runtime running after quit' },
  )

  const nativeCrashProcess = spawn(runtimeExecutable, ['--crash-report-self-test'], {
    env: {
      ...process.env,
      HOME: home,
      USERPROFILE: home,
      SHEJANE_RUNTIME_CRASH_DIRECTORY: nativeCrashDirectory,
      SHEJANE_CRASH_CANARY_SECRET: 'must-not-enter-native-crash-report',
    },
    stdio: 'ignore',
  })
  const nativeCrashResult = await new Promise((resolveCrash, rejectCrash) => {
    nativeCrashProcess.once('error', rejectCrash)
    nativeCrashProcess.once('exit', (code, signal) => resolveCrash({ code, signal }))
  })
  if (nativeCrashResult.code === 0) {
    throw new Error('packaged Runtime native crash self-test exited successfully')
  }
  const nativeCrashFiles = (await readdir(nativeCrashDirectory))
    .filter((filename) => filename.startsWith('runtime-native-') && filename.endsWith('.log'))
  if (nativeCrashFiles.length !== 1) {
    throw new Error('packaged Runtime native crash self-test did not create exactly one dump')
  }
  const nativeCrash = await readFile(join(nativeCrashDirectory, nativeCrashFiles[0]), 'utf8')
  if (
    !nativeCrash.includes('Fatal Python error:') ||
    nativeCrash.includes('must-not-enter-native-crash-report')
  ) {
    throw new Error('packaged Runtime native crash dump failed content boundaries')
  }
  if (isMacOSApp) {
    await execFileAsync('/usr/bin/codesign', ['--verify', '--deep', '--strict', packagedPath])
  }
  process.stdout.write(`packaged Client Runtime smoke passed: ${basename(packagedPath)}\n`)
} catch (error) {
  primaryError = error
  if (stdout) {
    process.stderr.write(`packaged app stdout:\n${stdout}\n`)
  }
  if (stderr) {
    process.stderr.write(`packaged app stderr:\n${stderr}\n`)
  }
  throw error
} finally {
  const cleanupErrors = []
  try {
    if (appProcess?.exitCode === null) {
      appProcess.kill('SIGKILL')
      await waitUntil(
        async () => appProcess.exitCode !== null,
        {
          timeoutMs: PROCESS_EXIT_TIMEOUT_MS,
          failure: 'packaged app did not exit after smoke cleanup kill',
        },
      )
    }
  } catch (cleanupError) {
    cleanupErrors.push(cleanupError)
  }
  try {
    if (runtimePid > 0 && processExists(runtimePid)) {
      process.kill(runtimePid, 'SIGKILL')
      await waitUntil(
        async () => !processExists(runtimePid),
        {
          timeoutMs: PROCESS_EXIT_TIMEOUT_MS,
          failure: 'packaged Runtime did not exit after smoke cleanup kill',
        },
      )
    }
  } catch (cleanupError) {
    cleanupErrors.push(cleanupError)
  }
  try {
    await rm(temporaryRoot, {
      recursive: true,
      force: true,
      maxRetries: 20,
      retryDelay: 100,
    })
  } catch (cleanupError) {
    cleanupErrors.push(cleanupError)
  }
  if (cleanupErrors.length > 0) {
    const cleanupFailure = new AggregateError(cleanupErrors, 'packaged smoke cleanup failed')
    if (primaryError === null) {
      throw cleanupFailure
    }
    process.stderr.write(`packaged smoke cleanup warning: ${cleanupFailure}\n`)
  }
}
