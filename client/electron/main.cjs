const {
  app,
  BrowserWindow,
  dialog,
  ipcMain,
  Menu,
  nativeImage,
  Notification,
  safeStorage,
  session,
  shell,
  Tray,
} = require('electron')
const fs = require('node:fs')
const path = require('node:path')
const childProcess = require('node:child_process')
const crypto = require('node:crypto')
const net = require('node:net')
const { installLocalRuntimeAuthorization } = require('./runtime-auth.cjs')
const { materializeFileCopy } = require('./file-open.cjs')
const {
  installUpdateAfterRuntimeStop,
  isPortConflictError,
  startRuntimeWithPortRetry,
  stopRuntimeProcess,
  waitForRuntimeReady,
  waitForRuntimeProcessClose,
} = require('./runtime-process.cjs')
const { appNameForLocale, desktopText, normalizeDesktopLocale } = require('./client-i18n.cjs')
const {
  createRuntimeConnectionUpdateGate,
  normalizeExternalRuntimeURL,
  normalizeRuntimeToken,
  readRuntimeConnection,
  writeRuntimeConnection,
} = require('./runtime-connection-store.cjs')
const {
  configureApplicationMenuForPlatform,
  fileContextMenuTemplate,
  suppressWindowMenuForPlatform,
  trayIconConfigForPlatform,
  trayMenuTemplateForPlatform,
  windowMenuOptionsForPlatform,
} = require('./menu.cjs')
const {
  installDesktopSmokeQuitWatcher,
  writeDesktopSmokeConfig,
} = require('./smoke-support.cjs')

const isDev = process.env.ELECTRON_DEV === 'true'
const dockLangFile =
  process.env.SHEJANE_DOCK_LANG_FILE || path.resolve(__dirname, '..', '..', '.tmp', 'dev', 'dock-lang')
function readDockLocale() {
  try {
    const value = fs.readFileSync(dockLangFile, 'utf8').trim()
    return normalizeDesktopLocale(value)
  } catch {
    return systemDesktopLocale()
  }
}
let currentLocale = readDockLocale()
function currentAppName() {
  return appNameForLocale(currentLocale)
}
function systemDesktopLocale() {
  try {
    if (typeof app.getLocale === 'function') {
      return normalizeDesktopLocale(app.getLocale())
    }
  } catch {
    // Fall through to the platform Intl locale when Electron has not initialized locale data yet.
  }
  return normalizeDesktopLocale(Intl.DateTimeFormat().resolvedOptions().locale)
}
const appIconPath = path.join(__dirname, 'assets/app-icon.png')
function isAllowedExternalURL(rawURL) {
  if (typeof rawURL !== 'string' || rawURL.length === 0) {
    return false
  }
  try {
    const parsed = new URL(rawURL)
    return ['http:', 'https:', 'shejane:'].includes(parsed.protocol)
  } catch {
    return false
  }
}
// Tray/menu-bar icons are platform-specific:
// - macOS wants a black + transparent template mask so the system can tint it.
// - Windows/Linux use a small full-color app icon so dark taskbars still read.
// Electron auto-loads matching `@2x` PNGs when they sit beside the base file.
function createTrayIcon(platform = process.platform) {
  const { filename, template } = trayIconConfigForPlatform(platform)
  const icon = nativeImage.createFromPath(path.join(__dirname, 'assets', filename))
  if (template) {
    icon.setTemplateImage(true)
  }
  return icon
}

/** Module-scope references so tray/menu/notification handlers can find
 *  the main window without searching every time. */
let mainWindow = null
let tray = null
// Bundled local-agent runtime (packaged builds spawn it; dev uses scripts/dev.sh).
let runtimeProcess = null
let runtimeURL = null
let runtimeToken = null
let runtimeReady = false
let runtimeStopPromise = null
let desktopInitializationComplete = false
let runtimeSessionReady = false
let runtimeTarget = { mode: 'bundled', source: 'default' }
let runtimeConnectionError = null
let clientAutoUpdater = null
let promptedUpdateVersion = null
let clientUpdateState = {
  currentVersion: app.getVersion(),
  status: app.isPackaged ? 'idle' : 'unavailable',
}
const runtimeConnectionUpdateGate = createRuntimeConnectionUpdateGate()
const appWindowButtonPosition = { x: 29, y: 27 }
const authWindowButtonPosition = { x: 29, y: 20 }

function createWindow() {
  if (!desktopInitializationComplete) {
    return
  }
  const windowOptions = {
    width: 1220,
    height: 820,
    minWidth: 960,
    minHeight: 680,
    title: currentAppName(),
    ...windowMenuOptionsForPlatform(process.platform),
    ...(process.platform === 'darwin'
      ? {
          titleBarStyle: 'hidden',
          // Calibrated to the v4 design deliverable (SheJane 原型 v4.html):
          // the floating sidebar card sits at 12px margin + ~1px border +
          // 10px side padding, and the prototype's TrafficLights row adds
          // 6px left / 14px top inside that — lights group lands at
          // (12+1+10+6, 12+1+14) = (29, 27) from the window corner, clear
          // of the card's 14px corner radius.
          trafficLightPosition: appWindowButtonPosition,
        }
      : {}),
    icon: appIconPath,
    backgroundColor: '#FAF9F6',
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
      preload: path.join(__dirname, 'preload.cjs'),
      additionalArguments: runtimeArgs(),
    },
  }

  const window = new BrowserWindow(windowOptions)
  mainWindow = window
  suppressWindowMenuForPlatform(window, process.platform)

  window.webContents.setWindowOpenHandler(({ url }) => {
    if (isAllowedExternalURL(url)) {
      shell.openExternal(url)
    }
    return { action: 'deny' }
  })

  // Close-to-tray: clicking the red traffic light (or Cmd+W) hides
  // the window instead of quitting, so the app keeps running in the
  // tray. Real quit only happens when the user explicitly chooses Quit
  // from the tray menu (which sets app.isQuitting).
  window.on('close', (event) => {
    if (!app.isQuitting) {
      event.preventDefault()
      window.hide()
    }
  })

  window.on('closed', () => {
    if (mainWindow === window) {
      mainWindow = null
    }
  })

  if (isDev) {
    window.loadURL(process.env.ELECTRON_DEV_URL || 'http://localhost:5173')
  } else {
    window.loadFile(path.join(__dirname, '../dist/index.html'))
  }
}

function showOrCreateMainWindow() {
  if (!desktopInitializationComplete) {
    return
  }
  if (mainWindow) {
    if (mainWindow.isMinimized()) {
      mainWindow.restore()
    }
    mainWindow.show()
    mainWindow.focus()
    return
  }
  createWindow()
}

function requestNewChat() {
  showOrCreateMainWindow()
  if (mainWindow) {
    mainWindow.webContents.send('shejane:new-chat')
  }
}

function configureApplicationMenu() {
  configureApplicationMenuForPlatform(Menu, process.platform, currentLocale, {
    onCheckForUpdates: () => void checkClientUpdate(),
    onNewChat: requestNewChat,
  })
}

function refreshTrayMenu() {
  if (!tray) {
    return
  }
  tray.setToolTip(currentAppName())
  tray.setContextMenu(
    Menu.buildFromTemplate(
      trayMenuTemplateForPlatform(process.platform, currentLocale, {
        onShow: showOrCreateMainWindow,
        onNewChat: requestNewChat,
      }),
    ),
  )
}

function createTray() {
  const trayIcon = createTrayIcon(process.platform)
  tray = new Tray(trayIcon)
  refreshTrayMenu()
  // Single click on the tray icon: bring the window forward. Right-click
  // shows the context menu on its own (platform-default).
  tray.on('click', () => showOrCreateMainWindow())
}

function applyDesktopLocale(locale) {
  currentLocale = normalizeDesktopLocale(locale)
  app.setName(currentAppName())
  configureApplicationMenu()
  refreshTrayMenu()
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.setTitle(currentAppName())
  }
  return currentLocale
}

app.setName(currentAppName())
const hasSingleInstanceLock = app.requestSingleInstanceLock()
if (!hasSingleInstanceLock) {
  app.quit()
} else {
  app.on('second-instance', () => {
    showOrCreateMainWindow()
  })
}

// ─── Bundled local-agent runtime ────────────────────────────────────────────
// Packaged builds spawn the frozen runtime (shipped via electron-builder
// extraResources) on a fresh loopback port with a one-time pairing token, then
// keep its token in Main, and expose only an authenticated client-session
// marker to the renderer. Dev still relies on scripts/dev.sh to spawn.

function pickFreePort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer()
    srv.on('error', reject)
    srv.listen(0, '127.0.0.1', () => {
      const { port } = srv.address()
      srv.close(() => resolve(port))
    })
  })
}

function runtimeBinaryPath() {
  const exe = process.platform === 'win32' ? 'shejane-runtime.exe' : 'shejane-runtime'
  return path.join(process.resourcesPath, 'runtime', exe)
}

function managedWorkerSandboxCommand() {
  return JSON.stringify([
    process.execPath,
    path.join(process.resourcesPath, 'sandbox', 'srt-launcher.mjs'),
  ])
}

// Allowlist env forward (mirrors scripts/dev.sh's `env -i`): the runtime never
// inherits unrelated application or shell secrets.
function runtimeEnv(extra) {
  const allow =
    process.platform === 'win32'
      ? ['PATH', 'PATHEXT', 'SystemRoot', 'SystemDrive', 'TEMP', 'TMP', 'USERPROFILE', 'HOMEDRIVE', 'HOMEPATH', 'COMSPEC', 'WINDIR', 'LANG', 'NUMBER_OF_PROCESSORS']
      : ['PATH', 'HOME', 'USER', 'TMPDIR', 'SHELL', 'LANG', 'LC_ALL', 'LC_CTYPE']
  const env = {}
  for (const key of allow) {
    if (process.env[key] !== undefined) {
      env[key] = process.env[key]
    }
  }
  return { ...env, ...extra }
}

function runtimeConnectionFile() {
  return path.join(app.getPath('userData'), 'runtime-connection.json')
}

function hasRuntimeEnvironmentOverride() {
  return Boolean(process.env.SHEJANE_RUNTIME_URL || process.env.SHEJANE_RUNTIME_TOKEN)
}

function loadRuntimeTarget() {
  if (hasRuntimeEnvironmentOverride()) {
    return {
      mode: 'external-local',
      source: 'environment',
      baseURL: normalizeExternalRuntimeURL(
        process.env.SHEJANE_RUNTIME_URL || 'http://127.0.0.1:17371',
      ),
      token: normalizeRuntimeToken(process.env.SHEJANE_RUNTIME_TOKEN),
    }
  }
  const saved = readRuntimeConnection(runtimeConnectionFile(), safeStorage)
  return saved.mode === 'external-local'
    ? { ...saved, source: 'saved' }
    : { mode: 'bundled', source: 'default' }
}

function publicRuntimeConnection() {
  return {
    mode: runtimeTarget.mode,
    source: runtimeTarget.source,
    state: runtimeSessionReady ? 'ready' : 'offline',
    ...(runtimeTarget.mode === 'external-local'
      ? {
          baseURL: runtimeTarget.baseURL,
          tokenConfigured: Boolean(runtimeTarget.token),
        }
      : {}),
    ...(runtimeConnectionError ? { error: runtimeConnectionError } : {}),
  }
}

function localRuntimeConnection() {
  return {
    baseURL:
      runtimeURL ||
      (runtimeTarget.mode === 'external-local' ? runtimeTarget.baseURL : null) ||
      'http://127.0.0.1:17371',
    token: runtimeToken || (runtimeTarget.mode === 'external-local' ? runtimeTarget.token : '') || '',
  }
}

// Renderer receives only the address and an opaque client-session marker.
// Electron's request session adds the private bearer token in Main.
function runtimeArgs() {
  const connection = localRuntimeConnection()
  return [
    `--shejane-runtime-url=${connection.baseURL}`,
    ...(connection.token ? ['--shejane-runtime-session=client'] : []),
    `--shejane-runtime-ready=${runtimeSessionReady ? 'true' : 'false'}`,
  ]
}

async function spawnBundledRuntime() {
  const port = await pickFreePort()
  runtimeToken = crypto.randomBytes(32).toString('hex')
  runtimeURL = `http://127.0.0.1:${port}`

  const child = childProcess.spawn(runtimeBinaryPath(), [
    '--host', '127.0.0.1',
    '--port', String(port),
    '--token', runtimeToken,
    '--data-dir', path.join(app.getPath('userData'), 'runtime'),
    ...(process.platform === 'darwin' && process.arch === 'arm64'
      ? [
          '--managed-worker-vm-assets',
          path.join(process.resourcesPath, 'sandbox', 'vm-assets', 'manifest.json'),
        ]
      : []),
  ], {
    env: runtimeEnv({
      PYTHONUNBUFFERED: '1',
      SHEJANE_MANAGED_WORKER_SANDBOX_COMMAND: managedWorkerSandboxCommand(),
      SHEJANE_RUNTIME_NODE_PATH: process.execPath,
    }),
    stdio: ['ignore', 'pipe', 'pipe'],
    windowsHide: true,
  })
  runtimeProcess = child
  child.runtimeClosed = false
  child.once('close', () => {
    child.runtimeClosed = true
  })
  let startupErrorOutput = ''
  child.stdout.on('data', (chunk) => process.stdout.write(`[runtime] ${chunk}`))
  child.stderr.on('data', (chunk) => {
    startupErrorOutput = `${startupErrorOutput}${chunk}`.slice(-2048)
    if (isPortConflictError(startupErrorOutput)) {
      child.runtimePortConflict = true
    }
    process.stderr.write(`[runtime] ${chunk}`)
  })
  child.on('error', (err) => {
    console.error('[runtime] failed:', err && err.message)
  })
  child.on('exit', (code, signal) => {
    if (runtimeProcess === child) {
      runtimeProcess = null
    }
    const wasReady = runtimeReady
    runtimeReady = false
    runtimeSessionReady = false
    if (wasReady && !app.isQuitting) {
      dialog.showErrorBox(currentAppName(), desktopText(currentLocale, 'runtime.exited', { code, signal }))
    }
  })
  return child
}

async function stopBundledRuntime(child = runtimeProcess) {
  runtimeReady = false
  runtimeSessionReady = false
  if (!child) {
    runtimeURL = null
    runtimeToken = null
    return
  }
  if (child.exitCode === null) {
    await stopRuntimeProcess(child, {
      forceKill: async (pid) => {
        if (process.platform === 'win32') {
          try {
            await new Promise((resolve, reject) => {
              const killer = childProcess.spawn('taskkill', ['/pid', String(pid), '/T', '/F'], { windowsHide: true })
              killer.once('error', reject)
              killer.once('exit', (code) => {
                if (code === 0) {
                  resolve()
                  return
                }
                reject(new Error(`taskkill exited with code ${code}`))
              })
            })
            return
          } catch (error) {
            console.error('[runtime] taskkill failed, falling back to process.kill:', error)
          }
        }
        process.kill(pid, 'SIGKILL')
      },
    })
  }
  await waitForRuntimeProcessClose(child)
  if (runtimeProcess === child) {
    runtimeProcess = null
  }
  runtimeURL = null
  runtimeToken = null
}

async function waitForRuntimeConnection(connection, ownedProcess, timeoutMs = 30000) {
  if (!ownedProcess) {
    return waitForRuntimeReady({
      baseURL: connection.baseURL,
      token: connection.token,
      timeoutMs,
    })
  }
  if (ownedProcess.exitCode !== null || runtimeProcess !== ownedProcess) {
    return false
  }

  const readinessController = new AbortController()
  const readiness = waitForRuntimeReady({
    baseURL: connection.baseURL,
    token: connection.token,
    timeoutMs,
    signal: readinessController.signal,
  })
  let onStopped
  const stopped = new Promise((resolve) => {
    onStopped = () => resolve(false)
    ownedProcess.once('error', onStopped)
    ownedProcess.once('close', onStopped)
  })
  try {
    const ready = await Promise.race([readiness, stopped])
    return ready && ownedProcess.exitCode === null && runtimeProcess === ownedProcess
  } finally {
    readinessController.abort()
    ownedProcess.off('error', onStopped)
    ownedProcess.off('close', onStopped)
  }
}

async function startBundledRuntime() {
  return startRuntimeWithPortRetry({
    start: spawnBundledRuntime,
    ready: (child, timeoutMs) => waitForRuntimeConnection(localRuntimeConnection(), child, timeoutMs),
    retryable: (child) => child.runtimePortConflict === true,
    stop: stopBundledRuntime,
  })
}

function registerRuntimeConnectionHandlers() {
  ipcMain.handle('shejane:runtime-connection-get', () => publicRuntimeConnection())
  ipcMain.handle('shejane:runtime-connection-set', async (_event, input) => {
    if (runtimeTarget.source === 'environment') {
      throw new Error('Runtime connection is managed by environment variables')
    }
    const update = runtimeConnectionUpdateGate.begin()
    if (input?.mode === 'bundled') {
      update.assertCurrent()
      writeRuntimeConnection(runtimeConnectionFile(), safeStorage, { mode: 'bundled' })
      runtimeTarget = { mode: 'bundled', source: 'default' }
      runtimeConnectionError = null
      return publicRuntimeConnection()
    }
    if (input?.mode !== 'external-local') {
      throw new Error('Runtime connection mode is invalid')
    }

    const baseURL = normalizeExternalRuntimeURL(input.baseURL)
    const suppliedToken = typeof input.token === 'string' && input.token.trim()
      ? normalizeRuntimeToken(input.token)
      : ''
    const token = suppliedToken || (
      runtimeTarget.mode === 'external-local' &&
      runtimeTarget.source === 'saved' &&
      runtimeTarget.baseURL === baseURL
        ? runtimeTarget.token
        : ''
    )
    if (!token) {
      throw new Error('Runtime token is required')
    }
    const ready = await waitForRuntimeReady({
      baseURL,
      token: normalizeRuntimeToken(token),
      timeoutMs: 5000,
      signal: update.signal,
    })
    update.assertCurrent()
    if (!ready) {
      throw new Error('Runtime did not pass the authenticated protocol handshake')
    }

    const nextTarget = { mode: 'external-local', source: 'saved', baseURL, token }
    writeRuntimeConnection(runtimeConnectionFile(), safeStorage, nextTarget)
    runtimeTarget = nextTarget
    runtimeConnectionError = null
    return publicRuntimeConnection()
  })
  ipcMain.handle('shejane:restart-app', () => {
    app.relaunch()
    app.quit()
  })
}

function setMainWindowButtonPosition(position) {
  if (process.platform !== 'darwin' || !mainWindow || typeof mainWindow.setWindowButtonPosition !== 'function') {
    return false
  }
  const next =
    position === 'auth'
      ? authWindowButtonPosition
      : position === 'app'
        ? appWindowButtonPosition
        : appWindowButtonPosition
  mainWindow.setWindowButtonPosition(next)
  return true
}

function publishClientUpdateState(patch) {
  clientUpdateState = { ...clientUpdateState, ...patch }
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send('shejane:update-state-changed', clientUpdateState)
  }
  return clientUpdateState
}

async function checkClientUpdate() {
  if (!clientAutoUpdater) {
    return clientUpdateState
  }
  publishClientUpdateState({ status: 'checking', availableVersion: undefined, progress: undefined })
  try {
    await clientAutoUpdater.checkForUpdates()
  } catch (error) {
    console.warn('[updater] check failed:', error && error.message)
    publishClientUpdateState({ status: 'error', progress: undefined })
  }
  return clientUpdateState
}

async function installClientUpdate() {
  if (!clientAutoUpdater || clientUpdateState.status !== 'ready') {
    return false
  }
  const updater = clientAutoUpdater
  app.isQuitting = true
  try {
    // electron-updater emits before-quit only after starting the installer.
    // Finish our asynchronous Runtime shutdown first so that handler cannot
    // cancel the updater-owned quit while the installer waits for this app.
    await installUpdateAfterRuntimeStop({
      stopRuntime: stopBundledRuntime,
      quitAndInstall: (isSilent, isForceRunAfter) => updater.quitAndInstall(isSilent, isForceRunAfter),
    })
    return true
  } catch (error) {
    app.isQuitting = false
    publishClientUpdateState({ status: 'error', progress: undefined })
    console.error('[updater] install failed:', error)
    dialog.showErrorBox(
      currentAppName(),
      desktopText(currentLocale, 'update.installFailed', {
        message: error instanceof Error ? error.message : String(error),
      }),
    )
    return false
  }
}

async function promptForClientUpdate(version) {
  if (promptedUpdateVersion === version) return
  promptedUpdateVersion = version
  const options = {
    type: 'info',
    title: currentAppName(),
    message: desktopText(currentLocale, 'update.readyMessage', { version }),
    detail: desktopText(currentLocale, 'update.readyDetail'),
    buttons: [
      desktopText(currentLocale, 'update.restartNow'),
      desktopText(currentLocale, 'update.later'),
    ],
    defaultId: 0,
    cancelId: 1,
  }
  const result = mainWindow
    ? await dialog.showMessageBox(mainWindow, options)
    : await dialog.showMessageBox(options)
  if (result.response === 0) {
    await installClientUpdate()
  }
}

function startClientUpdater() {
  if (!app.isPackaged) return
  try {
    const { autoUpdater } = require('electron-updater')
    clientAutoUpdater = autoUpdater
    autoUpdater.on('checking-for-update', () => {
      publishClientUpdateState({ status: 'checking', progress: undefined })
    })
    autoUpdater.on('update-available', (info) => {
      publishClientUpdateState({
        status: 'downloading',
        availableVersion: info.version,
        progress: 0,
      })
    })
    autoUpdater.on('download-progress', (progress) => {
      publishClientUpdateState({ status: 'downloading', progress: progress.percent })
    })
    autoUpdater.on('update-not-available', () => {
      publishClientUpdateState({ status: 'current', availableVersion: undefined, progress: undefined })
    })
    autoUpdater.on('update-downloaded', (info) => {
      publishClientUpdateState({
        status: 'ready',
        availableVersion: info.version,
        progress: 100,
      })
      void promptForClientUpdate(info.version).catch((error) => {
        console.warn('[updater] prompt failed:', error && error.message)
      })
    })
    autoUpdater.on('error', (error) => {
      console.warn('[updater] failed:', error && error.message)
      publishClientUpdateState({ status: 'error', progress: undefined })
    })
    void checkClientUpdate()
  } catch (error) {
    console.warn('[updater] unavailable:', error && error.message)
    publishClientUpdateState({ status: 'error' })
  }
}

ipcMain.handle('shejane:update-state', () => clientUpdateState)
ipcMain.handle('shejane:update-check', () => checkClientUpdate())
ipcMain.handle('shejane:update-install', () => installClientUpdate())

app.whenReady().then(async () => {
  app.setName(currentAppName())
  await fs.promises.rm(path.join(app.getPath('temp'), 'shejane-open-files'), {
    recursive: true,
    force: true,
  }).catch(() => undefined)
  configureApplicationMenu()
  if (process.platform === 'darwin') {
    app.dock.setIcon(appIconPath)
  }
  registerRuntimeConnectionHandlers()
  try {
    runtimeTarget = loadRuntimeTarget()
  } catch (error) {
    if (hasRuntimeEnvironmentOverride()) {
      runtimeTarget = {
        mode: 'external-local',
        source: 'environment',
        baseURL: 'http://127.0.0.1:17371',
        token: '',
      }
    }
    runtimeConnectionError = error instanceof Error ? error.message : String(error)
  }

  try {
    if (runtimeConnectionError) {
      throw new Error(runtimeConnectionError)
    }
    // Packaged builds start the bundled Runtime only when the user has not
    // selected an external local Runtime. Both ownership modes use the same
    // authenticated protocol handshake.
    const ownsRuntime = app.isPackaged && runtimeTarget.mode === 'bundled'
    const ownedProcess = ownsRuntime ? await startBundledRuntime() : null
    const runtimeConnection = localRuntimeConnection()
    if (runtimeConnection.token) {
      installLocalRuntimeAuthorization(session.defaultSession.webRequest, runtimeConnection)
    }
    const runtimeAvailable = ownsRuntime
      ? Boolean(ownedProcess)
      : Boolean(runtimeConnection.token) && await waitForRuntimeConnection(runtimeConnection, null)
    if (
      !runtimeConnection.token ||
      !runtimeAvailable
    ) {
      throw new Error(desktopText(currentLocale, 'runtime.startTimeout'))
    }
    runtimeReady = Boolean(ownedProcess)
    runtimeSessionReady = true
    runtimeConnectionError = null
    if (runtimeProcess) {
      const smokeConfigWritten = writeDesktopSmokeConfig({
        baseURL: runtimeConnection.baseURL,
        token: runtimeConnection.token,
        resourcesPath: process.resourcesPath,
        runtimePid: runtimeProcess.pid || 0,
      })
      if (smokeConfigWritten) {
        installDesktopSmokeQuitWatcher({ quit: () => app.quit() })
      }
    }
  } catch (error) {
    let shutdownError = null
    try {
      await stopBundledRuntime()
    } catch (caughtShutdownError) {
      shutdownError = caughtShutdownError
      console.error('[runtime] shutdown after failed startup failed:', caughtShutdownError)
    }
    const startupMessage = error instanceof Error ? error.message : String(error)
    runtimeConnectionError = shutdownError
      ? `${startupMessage}\n${shutdownError instanceof Error ? shutdownError.message : String(shutdownError)}`
      : startupMessage
    runtimeSessionReady = false
  } finally {
    desktopInitializationComplete = true
    createWindow()
    createTray()
  }
  if (runtimeConnectionError) {
    dialog.showErrorBox(
      currentAppName(),
      desktopText(currentLocale, 'runtime.startFailed', { message: runtimeConnectionError }),
    )
  }
  startClientUpdater()
})

// Marker that "real quit" was requested (vs. window-close). The close
// handler on the main window checks this flag to decide whether to hide
// to tray or actually let the close go through.
app.on('before-quit', (event) => {
  app.isQuitting = true
  if (runtimeStopPromise) {
    event.preventDefault()
    return
  }
  if (!runtimeProcess) {
    return
  }
  event.preventDefault()
  runtimeStopPromise = stopBundledRuntime().then(
    () => {
      runtimeStopPromise = null
      app.quit()
    },
    (error) => {
      runtimeStopPromise = null
      app.isQuitting = false
      console.error('[runtime] shutdown failed:', error)
      dialog.showErrorBox(
        currentAppName(),
        desktopText(currentLocale, 'runtime.startFailed', {
          message: error instanceof Error ? error.message : String(error),
        }),
      )
    },
  )
})

ipcMain.handle('shejane:set-locale', async (_event, locale) => {
  const normalized = applyDesktopLocale(locale)
  try {
    fs.mkdirSync(path.dirname(dockLangFile), { recursive: true })
    fs.writeFileSync(dockLangFile, normalized, 'utf8')
  } catch {
    // Best-effort: if disk write fails, dock label simply won't update next launch.
  }
  return normalized
})

ipcMain.handle('shejane:set-window-button-position', async (_event, position) => setMainWindowButtonPosition(position))

/** Surface a native OS notification. Returns `false` when the main
 *  window is currently focused — in that case the user can already see
 *  whatever just happened, so adding a notification on top is noise.
 *  Clicking the notification brings the main window forward. */
ipcMain.handle('shejane:notify', async (_event, payload) => {
  if (!Notification.isSupported()) {
    return false
  }
  const title = typeof payload?.title === 'string' ? payload.title : currentAppName()
  const body = typeof payload?.body === 'string' ? payload.body : ''
  if (mainWindow && mainWindow.isFocused() && mainWindow.isVisible()) {
    return false
  }
  // `icon` is only honored on Windows + Linux; macOS pulls the icon
  // from the bundle's .icns. The branded .icns gets dropped into the
  // wrapper bundle by electron/run-dev.sh during dev.
  const notification = new Notification({ title, body, silent: false, icon: appIconPath })
  notification.on('click', () => {
    showOrCreateMainWindow()
  })
  notification.show()
  return true
})

ipcMain.handle('shejane:select-workspace-directory', async () => {
  const window = BrowserWindow.getFocusedWindow() || BrowserWindow.getAllWindows()[0]
  const options = {
    title: desktopText(currentLocale, 'dialogs.selectWorkspaceTitle'),
    properties: ['openDirectory'],
  }
  const result = window ? await dialog.showOpenDialog(window, options) : await dialog.showOpenDialog(options)
  if (result.canceled || result.filePaths.length === 0) {
    return undefined
  }
  return result.filePaths[0]
})

ipcMain.handle('shejane:select-attachment-files', async () => {
  const window = BrowserWindow.getFocusedWindow() || BrowserWindow.getAllWindows()[0]
  const options = {
    title: desktopText(currentLocale, 'dialogs.selectAttachmentsTitle'),
    properties: ['openFile', 'multiSelections'],
  }
  const result = window ? await dialog.showOpenDialog(window, options) : await dialog.showOpenDialog(options)
  return result.canceled ? [] : result.filePaths.slice(0, 10)
})

ipcMain.handle('shejane:select-plugin-package', async () => {
  const window = BrowserWindow.getFocusedWindow() || BrowserWindow.getAllWindows()[0]
  const options = {
    title: desktopText(currentLocale, 'dialogs.selectPluginPackageTitle'),
    properties: ['openFile'],
    filters: [{ name: 'SheJane Plugin', extensions: ['shejane-plugin'] }],
  }
  const result = window ? await dialog.showOpenDialog(window, options) : await dialog.showOpenDialog(options)
  return result.canceled ? undefined : result.filePaths[0]
})

ipcMain.handle('shejane:open-external', async (_event, rawURL) => {
  if (!isAllowedExternalURL(rawURL)) {
    return 'unsupported url protocol'
  }
  try {
    await shell.openExternal(rawURL)
    return 'ok'
  } catch (err) {
    return err instanceof Error ? err.message : String(err)
  }
})

ipcMain.handle('shejane:open-file-with-default-app', async (_event, filePath) => {
  // Used by the right-side PptxPreview "Open in PowerPoint" button.
  // Returns "" on success (Electron's contract) or an error message
  // string on failure.
  if (typeof filePath !== 'string' || filePath.length === 0) {
    return 'path required'
  }
  try {
    return await shell.openPath(filePath)
  } catch (err) {
    return err instanceof Error ? err.message : String(err)
  }
})

ipcMain.handle('shejane:open-file-snapshot', async (_event, input) => {
  if (!input || typeof input.name !== 'string' || !input.bytes) {
    return 'file snapshot required'
  }
  try {
    const filePath = await materializeFileCopy(
      path.join(app.getPath('temp'), 'shejane-open-files'),
      input.name,
      input.bytes,
    )
    if (input.action === 'reveal') {
      shell.showItemInFolder(filePath)
      return ''
    }
    return await shell.openPath(filePath)
  } catch (err) {
    return err instanceof Error ? err.message : String(err)
  }
})

ipcMain.handle('shejane:reveal-file-in-folder', async (_event, filePath) => {
  if (typeof filePath !== 'string' || filePath.length === 0) {
    return false
  }
  shell.showItemInFolder(filePath)
  return true
})

ipcMain.handle('shejane:show-file-context-menu', (event, input) => {
  const window = BrowserWindow.fromWebContents(event.sender)
  if (!window) return undefined
  return new Promise((resolve) => {
    let settled = false
    const finish = (action) => {
      if (settled) return
      settled = true
      resolve(action)
    }
    const template = fileContextMenuTemplate(
      process.platform,
      currentLocale,
      Boolean(input?.canPreview),
      {
        onPreview: () => finish('preview'),
        onOpen: () => finish('open'),
        onSave: () => finish('save'),
        onReveal: () => finish('reveal'),
      },
    )
    Menu.buildFromTemplate(template).popup({
      window,
      callback: () => finish(undefined),
    })
  })
})

app.on('window-all-closed', () => {
  // Intentionally do NOT app.quit() here — the tray keeps the app
  // alive across all OSes after the user "closes" the window. Real
  // quit is via the tray menu's Quit item (role: 'quit'), which
  // bypasses the close-to-tray guard via the before-quit flag.
})

app.on('activate', () => {
  showOrCreateMainWindow()
})
