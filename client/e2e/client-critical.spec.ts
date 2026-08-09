import {
  _electron as electron,
  expect,
  test,
  type ElectronApplication,
  type Page,
  type TestInfo,
} from '@playwright/test'
import { execFileSync, spawn, type ChildProcess } from 'node:child_process'
import { once } from 'node:events'
import fs from 'node:fs'
import { createServer, request as httpRequest, type Server } from 'node:http'
import type { AddressInfo } from 'node:net'
import os from 'node:os'
import path from 'node:path'
import {
  cancelLocalRunCommand,
  getLocalRunDiagnostics,
  getRuntimeSettings,
  listLocalRuns,
  updateRuntimeSettings,
} from '../src/runtime/client'

const clientDir = path.resolve(import.meta.dirname, '..')
const realLLMModel = process.env.SHEJANE_E2E_REAL_LLM_MODEL

function realPrompt(fakePrompt: string, livePrompt: string): string {
  return realLLMModel ? livePrompt : fakePrompt
}

type ClientHarness = {
  app: ElectronApplication
  page: Page
  rendererErrors: string[]
  root: string
}

type RuntimeFaultProxy = {
  baseURL: string
  armNextStreamHalfClose: () => void
  faultCount: () => number
  healthCount: () => number
  requestCount: (method: string, pathname: string) => number
  close: () => Promise<void>
}

async function launchClient(existingRoot?: string, runtimeURL?: string): Promise<ClientHarness> {
  const root = existingRoot ?? fs.mkdtempSync(
    path.join(process.env.SHEJANE_E2E_TMP_DIR ?? os.tmpdir(), 'client-window-'),
  )
  fs.mkdirSync(path.join(root, 'home'), { recursive: true })
  fs.mkdirSync(path.join(root, 'tmp'), { recursive: true })
  const rendererErrors: string[] = []
  const app = await electron.launch({
    args: [
      path.join(clientDir, 'electron/main.cjs'),
      `--user-data-dir=${path.join(root, 'electron-user-data')}`,
    ],
    cwd: clientDir,
    env: {
      PATH: process.env.PATH ?? '',
      HOME: path.join(root, 'home'),
      USER: process.env.USER ?? 'shejane-e2e',
      TMPDIR: path.join(root, 'tmp'),
      LANG: process.env.LANG ?? 'en_US.UTF-8',
      ...(process.env.DISPLAY ? { DISPLAY: process.env.DISPLAY } : {}),
      ...(process.env.XAUTHORITY ? { XAUTHORITY: process.env.XAUTHORITY } : {}),
      ELECTRON_DEV: 'true',
      ELECTRON_DEV_URL: requiredEnv('SHEJANE_E2E_CLIENT_URL'),
      SHEJANE_RUNTIME_URL: runtimeURL ?? requiredEnv('SHEJANE_E2E_RUNTIME_URL'),
      SHEJANE_RUNTIME_TOKEN: requiredEnv('SHEJANE_E2E_RUNTIME_TOKEN'),
      SHEJANE_DOCK_LANG_FILE: path.join(root, 'dock-lang'),
    },
  })
  const page = await app.firstWindow()
  page.on('console', (message) => {
    if (message.type() === 'error') {
      const source = message.location().url
      rendererErrors.push(source ? `${message.text()} (${source})` : message.text())
    }
  })
  page.on('pageerror', (error) => rendererErrors.push(error.message))
  return { app, page, rendererErrors, root }
}

function requiredEnv(name: string): string {
  const value = process.env[name]
  if (!value) throw new Error(`${name} must be set by scripts/test-contract.sh`)
  return value
}

function createPptxFixture(filePath: string): void {
  const script = `
import sys
from pptx import Presentation

deck = Presentation()
slide = deck.slides.add_slide(deck.slide_layouts[1])
slide.shapes.title.text = "E2E Window Deck"
slide.placeholders[1].text = "Opened through the real Client preview"
deck.save(sys.argv[1])
`
  execFileSync('uv', ['run', 'python', '-c', script, filePath], {
    cwd: path.resolve(clientDir, '../runtime'),
  })
}

test.describe.serial('flow:P2-P12 > Electron critical path', () => {
  let harness!: ClientHarness
  let replacementRuntime: ChildProcess | undefined
  let faultProxy!: RuntimeFaultProxy
  let suiteStartedAt = ''

  test.beforeAll(async () => {
    suiteStartedAt = new Date().toISOString()
    faultProxy = await startRuntimeFaultProxy(requiredEnv('SHEJANE_E2E_RUNTIME_URL'))
    harness = await launchClient(undefined, faultProxy.baseURL)
  })

  test.afterEach(async ({}, testInfo) => {
    if (testInfo.status === testInfo.expectedStatus) return
    await attachRuntimeFailureEvidence(testInfo, suiteStartedAt)
  })

  test.afterAll(async () => {
    if (harness) {
      await harness.app.close()
      fs.rmSync(harness.root, { recursive: true, force: true })
    }
    if (replacementRuntime?.exitCode === null && replacementRuntime.signalCode === null) {
      const exited = once(replacementRuntime, 'exit')
      if (process.platform !== 'win32' && replacementRuntime.pid) {
        process.kill(-replacementRuntime.pid, 'SIGKILL')
      } else {
        replacementRuntime.kill('SIGKILL')
      }
      await exited
    }
    await faultProxy?.close()
  })

  test('launches, connects, sends a task, and renders the streamed reply', async () => {
    const { page } = harness
    await expect(page.getByLabel(/Runtime (?:已连接|connected)/)).toBeVisible()
    await expect(
      page.getByText(/今天想从哪件事开始|What would you like to work on today/),
    ).toBeVisible()

    const composer = page.getByRole('textbox', {
      name: /交给石间|Hand it to SheJane|Describe a task/,
    })
    const modelPicker = page.getByRole('button', { name: /选择模型|Pick model/ })
    await expect(modelPicker).toBeVisible()
    await modelPicker.click()
    await page.locator('.composer-mode-model-item').first().click()
    const prompt = realPrompt(
      'desktop window E2E',
      'Reply with exactly DESKTOP_REAL_LLM_OK. Do not call any tool.',
    )
    const expectedReply = realLLMModel
      ? 'DESKTOP_REAL_LLM_OK'
      : 'Fake runtime reply for the SSE contract test.'
    await composer.fill(prompt)
    await page.getByRole('button', { name: /发送|Send/ }).click()

    await expect(
      page.locator('.message.user .message-content').getByText(prompt, { exact: true }),
    ).toBeVisible()
    await expect(
      page.locator('.message.assistant .message-content').getByText(
        expectedReply,
        { exact: !realLLMModel },
      ),
    ).toBeVisible()
    await expect(page.getByRole('button', { name: /停止生成|Stop/ })).toHaveCount(0)
    expect(harness.rendererErrors).toEqual([])

    await harness.app.close()
    harness = await launchClient(harness.root, faultProxy.baseURL)
    await expect(
      harness.page.locator('.message.assistant .message-content').getByText(
        expectedReply,
        { exact: !realLLMModel },
      ),
    ).toBeVisible()
    expect(harness.rendererErrors).toEqual([])
  })

  test('reconnects an incomplete SSE stream from its durable cursor without reloading', async () => {
    const { page } = harness
    await page.getByRole('button', { name: /新对话|New chat/ }).click()
    faultProxy.armNextStreamHalfClose()
    const composer = page.getByRole('textbox', {
      name: /交给石间|Hand it to SheJane|Describe a task/,
    })
    const prompt = '[[e2e:disconnect]] recover this Client stream'
    await composer.fill(prompt)
    await page.getByRole('button', { name: /发送|Send/ }).click()

    await expect(
      page.locator('.message.assistant .message-content').getByText(
        'Fake runtime reply for the SSE contract test.',
        { exact: true },
      ),
    ).toBeVisible()
    expect(faultProxy.faultCount()).toBe(1)
    await expect(page.getByRole('button', { name: /停止生成|Stop/ })).toHaveCount(0)
    expect(harness.rendererErrors).toEqual([])
  })

  test('binds a workspace and resolves a Tool approval from the visible UI', async () => {
    const workspace = fs.mkdtempSync(
      path.join(process.env.SHEJANE_E2E_TMP_DIR ?? os.tmpdir(), 'client-workspace-'),
    )
    try {
      const { app, page } = harness
      await page.getByRole('button', { name: /新对话|New chat/ }).click()
      await app.evaluate(({ dialog }, selectedPath) => {
        dialog.showOpenDialog = async () => ({ canceled: false, filePaths: [selectedPath] })
      }, workspace)
      await expect(page.getByLabel(/Runtime (?:已连接|connected)/)).toBeVisible()
      await expect(page.getByRole('button', { name: /选择模型|Pick model/ })).toBeVisible()

      await page.getByRole('button', { name: /权限模式|Permission mode/ }).click()
      await page.getByRole('menuitem', { name: /请求批准|Ask for approval/ }).click()
      await expect(
        page.getByRole('button', { name: /权限模式：请求批准|Permission mode: Ask for approval/ }),
      ).toBeVisible()
      await page.getByRole('button', { name: /添加项目|Add project/ }).click()
      await expect(page.getByText(path.basename(workspace), { exact: true })).toBeVisible()

      const composer = page.getByRole('textbox', {
        name: /交给石间|Hand it to SheJane|Describe a task/,
      })
      const prompt = realPrompt(
        '[[e2e:write-file]] approve this visible Tool request',
        'Use write_file exactly once to create /approved.txt with the exact content "approved by E2E". Do not use any other tool. After the tool succeeds, reply with DESKTOP_REAL_WRITE_OK.',
      )
      await composer.fill(prompt)
      await page.getByRole('button', { name: /发送|Send/ }).click()
      await expect(
        page.locator('.message.user .message-content').getByText(prompt, { exact: true }),
      ).toBeVisible()

      const approval = page.locator('.approval-bar')
      await expect(approval).toBeVisible()
      await expect(approval.getByRole('button')).toHaveCount(3)
      await expect(approval.getByRole('button', { name: /允许一次|Allow once/ })).toBeVisible()
      await expect(approval.getByRole('button', { name: /不再询问|Don't ask again/ })).toBeVisible()
      await expect(approval.getByRole('button', { name: /拒绝|Deny/ })).toBeVisible()
      await expect(approval.getByRole('button', { name: /修改参数|Edit arguments/ })).toHaveCount(0)
      const permissionRequestsBefore = faultProxy.requestCount('POST', '/v1/commands')
      await approval.getByRole('button', { name: /不再询问|Don't ask again/ }).click()
      await expect.poll(
        () => faultProxy.requestCount('POST', '/v1/commands'),
        { timeout: 5_000 },
      ).toBeGreaterThan(permissionRequestsBefore)
      await expect(
        page.locator('.message.assistant .message-content').getByText(
          realLLMModel ? 'DESKTOP_REAL_WRITE_OK' : 'E2E approved file written.',
          { exact: !realLLMModel },
        ),
      ).toBeVisible()
      expect(fs.readFileSync(path.join(workspace, 'approved.txt'), 'utf8')).toBe('approved by E2E')
      expect(harness.rendererErrors).toEqual([])
    } finally {
      fs.rmSync(workspace, { recursive: true, force: true })
    }
  })

  test('answers a structured user.ask question and resumes the visible run', async () => {
    const { page } = harness
    await page.getByRole('button', { name: /新对话|New chat/ }).click()
    const composer = page.getByRole('textbox', {
      name: /交给石间|Hand it to SheJane|Describe a task/,
    })
    await composer.fill(realPrompt(
      '[[e2e:ask]] ask from the visible Client',
      'Use user.ask exactly once. Ask the exact question "Choose an E2E option" with exactly two options labeled "Option A" and "Option B". After I answer, reply with DESKTOP_REAL_ASK_OPTION_B_OK if I chose Option B.',
    ))
    await page.getByRole('button', { name: /发送|Send/ }).click()

    const question = page.getByRole('region', { name: /需要你的选择|Your input is needed/ })
    await expect(question.getByText('Choose an E2E option', { exact: true })).toBeVisible()
    await question.getByRole('radio', { name: /Option B/ }).click()
    await expect(question).toHaveCount(0)
    await expect(
      page.locator('.message.assistant .message-content').getByText(
        realLLMModel ? 'DESKTOP_REAL_ASK_OPTION_B_OK' : 'E2E selected: Option B',
        { exact: !realLLMModel },
      ),
    ).toBeVisible()
    expect(harness.rendererErrors).toEqual([])
  })

  test('dismisses a structured user.ask question when the run is canceled', async () => {
    const { page } = harness
    await page.getByRole('button', { name: /新对话|New chat/ }).click()
    const composer = page.getByRole('textbox', {
      name: /交给石间|Hand it to SheJane|Describe a task/,
    })
    await composer.fill(realPrompt(
      '[[e2e:ask]] cancel from the visible Client',
      'Use user.ask exactly once. Ask the exact question "Cancel this E2E question?" with exactly two options labeled "Keep" and "Cancel".',
    ))
    await page.getByRole('button', { name: /发送|Send/ }).click()

    const question = page.getByRole('region', { name: /需要你的选择|Your input is needed/ })
    await expect(question).toBeVisible()
    await question.getByRole('button', { name: /取消对话|Stop run/ }).click()

    await expect(question).toHaveCount(0)
    await expect(page.getByRole('button', { name: /停止生成|Stop/ })).toHaveCount(0)
    expect(harness.rendererErrors).toEqual([])
  })

  test('denies a visible Tool request without producing its file side effect', async () => {
    const workspace = fs.mkdtempSync(
      path.join(process.env.SHEJANE_E2E_TMP_DIR ?? os.tmpdir(), 'client-deny-workspace-'),
    )
    try {
      const { app, page } = harness
      await page.getByRole('button', { name: /新对话|New chat/ }).click()
      await app.evaluate(({ dialog }, selectedPath) => {
        dialog.showOpenDialog = async () => ({ canceled: false, filePaths: [selectedPath] })
      }, workspace)
      await page.getByRole('button', { name: /权限模式|Permission mode/ }).click()
      await page.getByRole('menuitem', { name: /请求批准|Ask for approval/ }).click()
      await page.getByRole('button', { name: /添加项目|Add project/ }).click()
      await expect(page.getByText(path.basename(workspace), { exact: true })).toBeVisible()

      const composer = page.getByRole('textbox', {
        name: /交给石间|Hand it to SheJane|Describe a task/,
      })
      await composer.fill('[[e2e:write-file]] deny this visible Tool request')
      await page.getByRole('button', { name: /发送|Send/ }).click()
      const approval = page.locator('.approval-bar')
      await expect(approval).toBeVisible()
      const permissionRequestsBefore = faultProxy.requestCount('POST', '/v1/commands')
      await approval.getByRole('button', { name: /拒绝|Deny/ }).click()

      await expect(approval).toHaveCount(0)
      await expect.poll(
        () => faultProxy.requestCount('POST', '/v1/commands'),
        { timeout: 5_000 },
      ).toBeGreaterThan(permissionRequestsBefore)
      await expect(page.getByRole('button', { name: /停止生成|Stop/ })).toHaveCount(0)
      expect(fs.existsSync(path.join(workspace, 'approved.txt'))).toBe(false)
      expect(harness.rendererErrors).toEqual([])
    } finally {
      fs.rmSync(workspace, { recursive: true, force: true })
    }
  })

  test('shows a transient failure CTA and retries the same visible task successfully', async () => {
    const { page } = harness
    await page.getByRole('button', { name: /新对话|New chat/ }).click()
    const composer = page.getByRole('textbox', {
      name: /交给石间|Hand it to SheJane|Describe a task/,
    })
    const prompt = '[[e2e:fail-once:client-retry]] exercise the visible recovery path'
    await composer.fill(prompt)
    await page.getByRole('button', { name: /发送|Send/ }).click()

    const recovery = page.getByRole('alert').filter({ hasText: /重试|Retry/ })
    await expect(recovery).toBeVisible()
    await recovery.getByRole('button', { name: /重试|Retry/ }).click()

    await expect(
      page.locator('.message.assistant .message-content').getByText(
        'Fake runtime reply for the SSE contract test.',
        { exact: true },
      ),
    ).toBeVisible()
    await expect(
      page.locator('.message.user .message-content').getByText(prompt, { exact: true }),
    ).toHaveCount(1)
    expect(harness.rendererErrors).toEqual([])
  })

  test('starts the repair workflow from a validation failure CTA', async () => {
    const { page } = harness
    await page.getByRole('button', { name: /新对话|New chat/ }).click()
    const composer = page.getByRole('textbox', {
      name: /交给石间|Hand it to SheJane|Describe a task/,
    })
    const prompt = '[[e2e:repair]] exercise the visible repair workflow'
    await composer.fill(prompt)
    await page.getByRole('button', { name: /发送|Send/ }).click()

    const repair = page.getByRole('button', { name: /尝试修复|Try to repair/ })
    await expect(repair).toBeVisible()
    await repair.click()

    await expect(
      page.locator('.message.assistant .message-content').getByText(
        'Fake runtime reply for the SSE contract test.',
        { exact: true },
      ),
    ).toBeVisible()
    await expect(
      page.locator('.message.user .message-content').getByText(prompt, { exact: true }),
    ).toHaveCount(1)
    expect(harness.rendererErrors).toEqual([])
  })

  test('opens an allowlisted external link through Electron Main and contains OS errors', async () => {
    const { app, page } = harness
    await app.evaluate(({ shell }) => {
      const state = globalThis as typeof globalThis & { __shejaneE2EOpenedURLs?: string[] }
      state.__shejaneE2EOpenedURLs = []
      shell.openExternal = async (url) => {
        state.__shejaneE2EOpenedURLs?.push(url)
      }
    })

    await page.getByRole('button', { name: /新对话|New chat/ }).click()
    const composer = page.getByRole('textbox', {
      name: /交给石间|Hand it to SheJane|Describe a task/,
    })
    await composer.fill('[[e2e:external-link]] exercise the external URL boundary')
    await page.getByRole('button', { name: /发送|Send/ }).click()

    await page.getByRole('link', { name: 'E2E external link' }).click()
    await expect.poll(() => app.windows().length).toBe(1)
    await app.evaluate(() => {
      const state = globalThis as typeof globalThis & { __shejaneE2EOpenedURLs?: string[] }
      state.__shejaneE2EOpenedURLs = []
    })
    await expect.poll(() =>
      page.evaluate(() =>
        window.shejaneClient?.openExternal?.('https://example.test/shejane-e2e'),
      ),
    ).toBe('ok')
    expect(
      await app.evaluate(() => {
        const state = globalThis as typeof globalThis & { __shejaneE2EOpenedURLs?: string[] }
        return state.__shejaneE2EOpenedURLs ?? []
      }),
    ).toEqual(['https://example.test/shejane-e2e'])

    await expect.poll(() =>
      page.evaluate(() => window.shejaneClient?.openExternal?.('file:///private/etc/passwd')),
    ).toBe('unsupported url protocol')
    expect(
      await app.evaluate(() => {
        const state = globalThis as typeof globalThis & { __shejaneE2EOpenedURLs?: string[] }
        return state.__shejaneE2EOpenedURLs ?? []
      }),
    ).toEqual(['https://example.test/shejane-e2e'])

    await app.evaluate(({ shell }) => {
      shell.openExternal = async () => {
        throw new Error('E2E OS handler unavailable')
      }
    })
    await expect.poll(() =>
      page.evaluate(() =>
        window.shejaneClient?.openExternal?.('https://example.test/shejane-e2e-failure'),
      ),
    ).toBe('E2E OS handler unavailable')
    expect(harness.rendererErrors).toEqual([])
  })

  test('copies a visible reply to the OS clipboard and contains permission denial', async () => {
    const { app, page } = harness
    await page.getByRole('button', { name: /新对话|New chat/ }).click()
    const composer = page.getByRole('textbox', {
      name: /交给石间|Hand it to SheJane|Describe a task/,
    })
    await composer.fill('exercise the Client clipboard boundary')
    await page.getByRole('button', { name: /发送|Send/ }).click()
    const reply = page.locator('.message.assistant').filter({
      hasText: 'Fake runtime reply for the SSE contract test.',
    })
    const replyContent = reply.locator('.message-content')
    await expect(replyContent).toHaveCSS('user-select', 'text')

    await reply.getByRole('button', { name: /^复制$|^Copy$/ }).click()
    await expect(reply.getByRole('button', { name: /已复制|Copied/ })).toBeVisible()
    expect(await app.evaluate(({ clipboard }) => clipboard.readText())).toBe(
      'Fake runtime reply for the SSE contract test.',
    )

    await expect(reply.getByRole('button', { name: /^复制$|^Copy$/ })).toBeVisible({ timeout: 3_000 })
    await page.evaluate(() => {
      navigator.clipboard.writeText = async () => {
        throw new DOMException('E2E clipboard permission denied', 'NotAllowedError')
      }
    })
    await reply.getByRole('button', { name: /^复制$|^Copy$/ }).click()
    await expect(reply.getByRole('button', { name: /复制失败|Copy failed/ })).toBeVisible()
    expect(harness.rendererErrors).toEqual([])
  })

  test('auto-scrolls to the latest message when a run completes, and the scroll-to-bottom button re-pins a scrolled-away view', async () => {
    test.skip(Boolean(realLLMModel), 'Uses the deterministic fake Runtime stream fixture.')
    const { page } = harness
    await page.getByRole('button', { name: /新对话|New chat/ }).click()
    await page.getByRole('button', { name: /选择模型|Pick model/ }).click()
    await page.locator('.composer-mode-model-item').first().click()
    const composer = page.getByRole('textbox', {
      name: /交给石间|Hand it to SheJane|Describe a task/,
    })
    const lastAssistantContent = page.locator('.message.assistant').last().locator('.message-content')
    const scrollAway = () => page.locator('.messages').evaluate((element) => {
      element.scrollTop = Math.floor((element.scrollHeight - element.clientHeight) / 2)
      element.dispatchEvent(new Event('scroll'))
    })
    const distanceFromBottom = () => page.locator('.messages').evaluate((element) => (
      element.scrollHeight - element.scrollTop - element.clientHeight
    ))
    const fab = page.getByRole('button', { name: /回到底部|Back to latest/ })
    const stop = page.getByRole('button', { name: /停止生成|Stop/ })

    // Run #1: build a conversation tall enough to scroll.
    await composer.fill('[[e2e:scroll]] exercise the scroll-to-bottom boundary')
    await page.getByRole('button', { name: /发送|Send/ }).click()
    await expect(stop).toBeVisible()
    await expect(stop).toHaveCount(0, { timeout: 30_000 })
    await expect(lastAssistantContent).toContainText('Scroll_159')

    // Scroll away after the run settled, then use the button to re-pin.
    await scrollAway()
    await expect(fab).toBeVisible()
    await fab.click()
    await expect.poll(distanceFromBottom).toBeLessThan(120)
    await expect(fab).toHaveCount(0)

    // Run #2: while scrolled away mid-run, completion must return the view
    // to the latest message on its own.
    await composer.fill('[[e2e:scroll]] exercise the scroll-to-bottom boundary again')
    await page.getByRole('button', { name: /发送|Send/ }).click()
    await expect(stop).toBeVisible()
    await scrollAway()
    await expect(fab).toBeVisible()

    await expect(stop).toHaveCount(0, { timeout: 30_000 })
    await expect(page.locator('.message.assistant').last().locator('.message-content'))
      .toContainText('Scroll_159')
    await expect.poll(distanceFromBottom).toBeLessThan(120)
    await expect(fab).toHaveCount(0)
    expect(harness.rendererErrors).toEqual([])
  })

  test('previews an authorized PowerPoint file and surfaces the OS open error', async () => {
    const workspace = fs.mkdtempSync(
      path.join(process.env.SHEJANE_E2E_TMP_DIR ?? os.tmpdir(), 'client-pptx-workspace-'),
    )
    const deckPath = path.join(workspace, 'e2e-window-deck.pptx')
    createPptxFixture(deckPath)
    try {
      const { app, page } = harness
      await app.evaluate(({ dialog, shell }, selectedPath) => {
        const state = globalThis as typeof globalThis & {
          __shejaneE2EOpenedPaths?: string[]
          __shejaneE2EOpenPathError?: string
        }
        state.__shejaneE2EOpenedPaths = []
        state.__shejaneE2EOpenPathError = ''
        dialog.showOpenDialog = async () => ({ canceled: false, filePaths: [selectedPath] })
        shell.openPath = async (filePath) => {
          state.__shejaneE2EOpenedPaths?.push(filePath)
          return state.__shejaneE2EOpenPathError ?? ''
        }
      }, workspace)

      await page.getByRole('button', { name: /新对话|New chat/ }).click()
      await page.getByRole('button', { name: /添加项目|Add project/ }).click()
      await expect(page.getByText(path.basename(workspace), { exact: true })).toBeVisible()
      const composer = page.getByRole('textbox', {
        name: /交给石间|Hand it to SheJane|Describe a task/,
      })
      await composer.fill('[[e2e:pptx-preview]] exercise the local file boundary')
      await page.getByRole('button', { name: /发送|Send/ }).click()

      await page
        .locator('.message.assistant .message-content')
        .getByRole('button', { name: 'e2e-window-deck.pptx' })
        .click()
      await expect(page.getByTestId('pptx-preview')).toBeVisible()
      await expect(page.getByText('E2E Window Deck', { exact: true })).toBeVisible()

      const openNatively = page.getByRole('button', {
        name: /在 PowerPoint 中打开|Open in PowerPoint/,
      })
      await openNatively.click()
      await expect.poll(() =>
        app.evaluate(() => {
          const state = globalThis as typeof globalThis & { __shejaneE2EOpenedPaths?: string[] }
          return state.__shejaneE2EOpenedPaths ?? []
        }),
      ).toEqual([fs.realpathSync(deckPath)])

      await app.evaluate(() => {
        const state = globalThis as typeof globalThis & { __shejaneE2EOpenPathError?: string }
        state.__shejaneE2EOpenPathError = 'E2E OS permission denied'
      })
      await openNatively.click()
      await expect(
        page.getByRole('alert').filter({
          hasText: /无法在 PowerPoint 中打开：E2E OS permission denied|Failed to open in PowerPoint: E2E OS permission denied/,
        }),
      ).toBeVisible()
      expect(harness.rendererErrors).toEqual([])
    } finally {
      fs.rmSync(workspace, { recursive: true, force: true })
    }
  })

  test('previews an immutable sent attachment and exposes its native file menu', async ({}, testInfo) => {
    const attachmentDir = fs.mkdtempSync(
      path.join(process.env.SHEJANE_E2E_TMP_DIR ?? os.tmpdir(), 'client-attachment-'),
    )
    const attachmentName = 'immutable-result-with-a-very-long-name-for-truncation.txt'
    const attachmentPath = path.join(attachmentDir, attachmentName)
    const marker = 'E2E immutable attachment preview'
    fs.writeFileSync(attachmentPath, marker)
    try {
      const { app, page } = harness
      await page.getByRole('button', { name: /新对话|New chat/ }).click()
      await app.evaluate(({ dialog }, selectedPath) => {
        dialog.showOpenDialog = async () => ({ canceled: false, filePaths: [selectedPath] })
      }, attachmentPath)
      await page.getByRole('button', { name: /添加附件|Attach/ }).click()
      await expect(page.getByText(attachmentName, { exact: true })).toBeVisible()

      const composer = page.getByRole('textbox', {
        name: /交给石间|Hand it to SheJane|Describe a task/,
      })
      await composer.fill('[[e2e:slow]] exercise the immutable attachment preview boundary')
      await page.getByRole('button', { name: /发送|Send/ }).click()
      const runtimeConnection = {
        baseURL: requiredEnv('SHEJANE_E2E_RUNTIME_URL'),
        token: requiredEnv('SHEJANE_E2E_RUNTIME_TOKEN'),
      }
      await expect.poll(async () => {
        const run = (await listLocalRuns(runtimeConnection)).find(
          item => item.goal.includes('immutable attachment preview boundary'),
        )
        return run?.status === 'running' && run.inputs[0]?.input_id === 'source'
      }).toBe(true)
      await expect(page.getByRole('button', { name: /停止生成|Stop/ })).toBeVisible()

      // The preview must now be backed by Runtime-owned bytes. Removing the
      // originally selected path proves the Client is not silently reopening it.
      fs.unlinkSync(attachmentPath)
      const card = page.locator('.message.user').getByRole('button', {
        name: attachmentName,
      })
      await expect(page.locator('.message.user > .message-attachments-detached').getByRole('button', {
        name: attachmentName,
      })).toBeVisible()
      await expect(page.locator('.message.user .message-bubble-inner').getByRole('button', {
        name: attachmentName,
      })).toHaveCount(0)
      await expect(card.locator('.tabler-icon-file-text')).toBeVisible()
      const fileName = card.locator('.message-attachment-name')
      await expect(fileName).toHaveCSS('text-overflow', 'ellipsis')
      expect(await fileName.evaluate(element => element.scrollWidth > element.clientWidth)).toBe(true)

      await app.evaluate(({ Menu }) => {
        const state = globalThis as typeof globalThis & {
          __shejaneE2EOriginalMenuBuilder?: typeof Menu.buildFromTemplate
          __shejaneE2EFileMenuLabels?: string[]
        }
        state.__shejaneE2EOriginalMenuBuilder = Menu.buildFromTemplate
        Menu.buildFromTemplate = ((template) => ({
          popup(options?: { callback?: () => void }) {
            state.__shejaneE2EFileMenuLabels = template
              .map(item => 'label' in item ? item.label : undefined)
              .filter((label): label is string => Boolean(label))
            options?.callback?.()
          },
        })) as typeof Menu.buildFromTemplate
      })
      await card.click({ button: 'right' })
      await expect.poll(() => app.evaluate(() => {
        const state = globalThis as typeof globalThis & { __shejaneE2EFileMenuLabels?: string[] }
        return state.__shejaneE2EFileMenuLabels ?? []
      })).toEqual(expect.arrayContaining([
        expect.stringMatching(/预览|Preview/),
        expect.stringMatching(/打开|Open/),
        expect.stringMatching(/保存副本|Save a Copy/),
        expect.stringMatching(/访达|文件夹|Finder|Folder/),
      ]))
      await card.click()
      await expect(page.getByText(marker, { exact: true })).toBeVisible()

      const screenshotPath = process.env.SHEJANE_VISUAL_SCREENSHOT ?? testInfo.outputPath('attachment-preview.png')
      await page.screenshot({ path: screenshotPath })
      expect(harness.rendererErrors).toEqual([])
      const currentRun = (await listLocalRuns(runtimeConnection)).find(
        item => item.goal.includes('immutable attachment preview boundary'),
      )
      if (currentRun?.status === 'running') {
        await cancelLocalRunCommand(
          `cmd_e2e_cancel_attachment_preview_${Date.now()}`,
          currentRun.id,
          runtimeConnection,
        )
      }
    } finally {
      await harness.app.evaluate(({ Menu }) => {
        const state = globalThis as typeof globalThis & {
          __shejaneE2EOriginalMenuBuilder?: typeof Menu.buildFromTemplate
          __shejaneE2EFileMenuLabels?: string[]
        }
        if (state.__shejaneE2EOriginalMenuBuilder) {
          Menu.buildFromTemplate = state.__shejaneE2EOriginalMenuBuilder
        }
        delete state.__shejaneE2EOriginalMenuBuilder
        delete state.__shejaneE2EFileMenuLabels
      }).catch(() => undefined)
      fs.rmSync(attachmentDir, { recursive: true, force: true })
    }
  })

  test('binds a workspace from the failure CTA and retries the blocked write', async () => {
    const workspace = fs.mkdtempSync(
      path.join(process.env.SHEJANE_E2E_TMP_DIR ?? os.tmpdir(), 'client-recovery-workspace-'),
    )
    try {
      const { app, page } = harness
      await page.getByRole('button', { name: /新对话|New chat/ }).click()
      await page.getByRole('button', { name: /权限模式|Permission mode/ }).click()
      await page.getByRole('menuitem', { name: /请求批准|Ask for approval/ }).click()

      const composer = page.getByRole('textbox', {
        name: /交给石间|Hand it to SheJane|Describe a task/,
      })
      const prompt = '[[e2e:question-write-file]] preserve my choice while recovering a workspace'
      await composer.fill(prompt)
      await page.getByRole('button', { name: /发送|Send/ }).click()
      const recoveryQuestion = page.getByRole('region', {
        name: /需要你的选择|Your input is needed/,
      })
      await expect(recoveryQuestion.getByText('Choose a recovery option', { exact: true }))
        .toBeVisible()
      await recoveryQuestion.getByRole('radio', { name: /Option B/ }).click()
      await page.locator('.approval-bar').getByRole('button', { name: /允许一次|Allow once/ }).click()

      const chooseWorkspace = page.getByRole('button', {
        name: /选择保存位置|Choose save location/,
      })
      await expect(chooseWorkspace).toBeVisible()
      expect(fs.existsSync(path.join(workspace, 'approved.txt'))).toBe(false)

      await app.evaluate(({ dialog }, selectedPath) => {
        dialog.showOpenDialog = async () => ({ canceled: false, filePaths: [selectedPath] })
      }, workspace)
      await chooseWorkspace.click()
      await expect(page.getByText(path.basename(workspace), { exact: true })).toBeVisible()

      const retryApproval = page.locator('.approval-bar')
      await expect(retryApproval).toBeVisible()
      await retryApproval.getByRole('button', { name: /允许一次|Allow once/ }).click()
      await expect(
        page.locator('.message.assistant .message-content').getByText(
          'E2E approved file written.',
          { exact: true },
        ),
      ).toBeVisible()
      expect(fs.readFileSync(path.join(workspace, 'approved.txt'), 'utf8')).toBe('approved by E2E')
      expect(harness.rendererErrors).toEqual([])
    } finally {
      fs.rmSync(workspace, { recursive: true, force: true })
    }
  })

  test('freezes visible agent settings for a waiting Run and applies changes only to the next Run', async () => {
    const workspace = fs.mkdtempSync(
      path.join(process.env.SHEJANE_E2E_TMP_DIR ?? os.tmpdir(), 'client-settings-workspace-'),
    )
    const runtimeConnection = {
      baseURL: requiredEnv('SHEJANE_E2E_RUNTIME_URL'),
      token: requiredEnv('SHEJANE_E2E_RUNTIME_TOKEN'),
    }
    const prompt = '[[e2e:settings-freeze]] keep the admitted subagent setting'
    try {
      const { app, page } = harness
      await updateRuntimeSettings({ subagents: true }, runtimeConnection)
      await page.getByRole('button', { name: /设置|Settings/ }).click()
      const subagentsSwitch = page.getByRole('switch', { name: /子代理|Subagents/ })
      await expect(subagentsSwitch).toBeVisible()
      await expect(subagentsSwitch).toBeChecked()

      await page.getByRole('button', { name: /新对话|New chat/ }).click()
      await app.evaluate(({ dialog }, selectedPath) => {
        dialog.showOpenDialog = async () => ({ canceled: false, filePaths: [selectedPath] })
      }, workspace)
      await page.getByRole('button', { name: /权限模式|Permission mode/ }).click()
      await page.getByRole('menuitem', { name: /请求批准|Ask for approval/ }).click()
      await page.getByRole('button', { name: /添加项目|Add project/ }).click()
      await expect(page.getByText(path.basename(workspace), { exact: true })).toBeVisible()

      const composer = page.getByRole('textbox', {
        name: /交给石间|Hand it to SheJane|Describe a task/,
      })
      await composer.fill(prompt)
      await page.getByRole('button', { name: /发送|Send/ }).click()
      await expect(page.locator('.approval-bar')).toBeVisible()

      const admittedRun = (await listLocalRuns(runtimeConnection)).find(run => run.goal === prompt)
      expect(admittedRun?.status).toBe('waiting_permission')
      const waitingDiagnostics = await getLocalRunDiagnostics(admittedRun!.id, runtimeConnection)
      expect(JSON.parse(waitingDiagnostics.run.settings_json)).toMatchObject({ subagents: true })

      await page.getByRole('button', { name: /设置|Settings/ }).click()
      await subagentsSwitch.click()
      await expect(subagentsSwitch).not.toBeChecked()
      await expect.poll(
        async () => (await getRuntimeSettings(runtimeConnection)).subagents,
        { timeout: 5_000 },
      ).toBe(false)

      await page.locator('.conversation-row.active .conversation').click()
      await page.locator('.approval-bar').getByRole('button', { name: /允许一次|Allow once/ }).click()
      await expect(
        page.locator('.message.assistant .message-content').getByText(
          /E2E frozen settings retained:.*E2E_SUBAGENT_RESULT/,
        ),
      ).toBeVisible({ timeout: 30_000 })
      expect(fs.readFileSync(path.join(workspace, 'settings-freeze.txt'), 'utf8'))
        .toBe('settings frozen at admission')

      const admittedDiagnostics = await getLocalRunDiagnostics(admittedRun!.id, runtimeConnection)
      expect(admittedDiagnostics.tool_receipts).toEqual(expect.arrayContaining([
        expect.objectContaining({ tool_name: 'task', status: 'completed' }),
      ]))
      expect(JSON.parse(admittedDiagnostics.run.settings_json)).toMatchObject({ subagents: true })
      expect((await getRuntimeSettings(runtimeConnection)).subagents).toBe(false)
      fs.unlinkSync(path.join(workspace, 'settings-freeze.txt'))

      await page.getByRole('button', { name: /新对话|New chat/ }).click()
      await page.getByRole('button', { name: /权限模式|Permission mode/ }).click()
      await page.getByRole('menuitem', { name: /请求批准|Ask for approval/ }).click()
      await page.getByRole('button', { name: /添加项目|Add project/ }).click()
      await expect(page.getByText(path.basename(workspace), { exact: true })).toBeVisible()
      await composer.fill(prompt)
      await page.getByRole('button', { name: /发送|Send/ }).click()
      await page.locator('.approval-bar').getByRole('button', { name: /允许一次|Allow once/ }).click()

      const nextRun = (await listLocalRuns(runtimeConnection))
        .filter(run => run.goal === prompt)
        .find(run => run.id !== admittedRun?.id)
      expect(nextRun).toBeDefined()
      await expect.poll(async () => (
        (await listLocalRuns(runtimeConnection)).find(run => run.id === nextRun?.id)?.status
      ), { timeout: 30_000 }).toMatch(/completed|failed/)
      const nextDiagnostics = await getLocalRunDiagnostics(nextRun!.id, runtimeConnection)
      expect(JSON.parse(nextDiagnostics.run.settings_json)).toMatchObject({ subagents: false })
      expect(nextDiagnostics.tool_receipts?.some(receipt => (
        receipt.tool_name === 'task' && receipt.status === 'completed'
      ))).toBe(false)
      expect(harness.rendererErrors).toEqual([])
    } finally {
      await updateRuntimeSettings({ subagents: true }, runtimeConnection).catch(() => undefined)
      fs.rmSync(workspace, { recursive: true, force: true })
    }
  })

  test('detects a Runtime crash and reconnects after restart without reloading', async () => {
    test.setTimeout(90_000)
    await harness.page.getByRole('button', { name: /新对话|New chat/ }).click()
    const interruptedComposer = harness.page.getByRole('textbox', {
      name: /交给石间|Hand it to SheJane|Describe a task/,
    })
    await interruptedComposer.fill('[[e2e:slow]] stream while the Runtime crashes')
    await harness.page.getByRole('button', { name: /发送|Send/ }).click()
    await expect(harness.page.getByText(/正在思考|Thinking/)).toBeVisible()
    const runtimeConnection = {
      baseURL: requiredEnv('SHEJANE_E2E_RUNTIME_URL'),
      token: requiredEnv('SHEJANE_E2E_RUNTIME_TOKEN'),
    }
    let interruptedRun: Awaited<ReturnType<typeof listLocalRuns>>[number] | undefined
    await expect.poll(async () => {
      interruptedRun = (await listLocalRuns(runtimeConnection)).find(
        (run) => run.goal === '[[e2e:slow]] stream while the Runtime crashes',
      )
      return interruptedRun?.id
    }, { timeout: 30_000 }).toBeTruthy()
    if (!interruptedRun) throw new Error('Interrupted E2E run was not created')

    const originalPID = Number(requiredEnv('SHEJANE_E2E_RUNTIME_PID'))
    expect(Number.isSafeInteger(originalPID) && originalPID > 1).toBe(true)
    const rendererErrorCountBeforeCrash = harness.rendererErrors.length
    const healthCountBeforeCrash = faultProxy.healthCount()
    process.kill(originalPID, 'SIGKILL')

    await expect.poll(async () => {
      try {
        return !(await fetch(`${requiredEnv('SHEJANE_E2E_RUNTIME_URL')}/v1/health`)).ok
      } catch {
        return true
      }
    }, { timeout: 10_000 }).toBe(true)
    await expect.poll(() => faultProxy.healthCount(), { timeout: 10_000 })
      .toBeGreaterThan(healthCountBeforeCrash)
    await expect(harness.page.getByLabel(/Runtime (?:离线|offline)/)).toBeVisible({
      timeout: 10_000,
    })

    replacementRuntime = startReplacementRuntime()
    await expect.poll(async () => {
      try {
        return (await fetch(`${requiredEnv('SHEJANE_E2E_RUNTIME_URL')}/v1/health`)).ok
      } catch {
        return false
      }
    }, { timeout: 15_000 }).toBe(true)

    await expect(harness.page.getByLabel(/Runtime (?:已连接|connected)/)).toBeVisible({
      timeout: 10_000,
    })
    await expect.poll(async () => (
      (await listLocalRuns(runtimeConnection)).find((run) => run.id === interruptedRun?.id)?.status
    ), { timeout: 45_000 }).toBe('cleanup_required')
    const cleanupMessage = harness.page.locator('.message.assistant').filter({
      hasText: /execution cleanup is unconfirmed|执行清理尚未确认/i,
    }).first()
    await expect(cleanupMessage).toBeVisible({ timeout: 10_000 })
    await cleanupMessage.getByRole('button', { name: /诊断|Diagnostics/ }).click()

    const diagnosticsDialog = harness.page.getByRole('alertdialog')
    await expect(diagnosticsDialog).toBeVisible()
    await expect(diagnosticsDialog.getByRole('heading', {
      name: /下载诊断信息|Download diagnostics/,
    })).toBeVisible()
    await expect(diagnosticsDialog.getByText(/发送给开发者|send this file to the developer/))
      .toBeVisible()

    const diagnosticsPath = path.join(harness.root, 'cleanup-diagnostics.json')
    const downloadPromise = harness.app.evaluate(async ({ session }, savePath) => (
      await new Promise<{ filename: string, state: string }>((resolve, reject) => {
        const timeout = setTimeout(() => {
          session.defaultSession.removeListener('will-download', onDownload)
          reject(new Error('Electron diagnostics download did not start'))
        }, 10_000)
        const onDownload = (_event: Electron.Event, item: Electron.DownloadItem) => {
          clearTimeout(timeout)
          item.setSavePath(savePath)
          const filename = item.getFilename()
          item.once('done', (_doneEvent, state) => resolve({ filename, state }))
        }
        session.defaultSession.once('will-download', onDownload)
      })
    ), diagnosticsPath)
    await diagnosticsDialog.getByRole('button', { name: /下载|Download/ }).click()
    const download = await downloadPromise
    expect(download).toEqual({
      state: 'completed',
      filename:
      `shejane-local-run-${interruptedRun!.id}-diagnostics.json`,
    })
    expect(JSON.parse(fs.readFileSync(diagnosticsPath, 'utf8'))).toMatchObject({
      schema_version: 2,
      run: { id: interruptedRun!.id, status: 'cleanup_required' },
    })
    await expect(diagnosticsDialog).toHaveCount(0)

    await harness.page.getByRole('button', { name: /新对话|New chat/ }).click()
    const composer = harness.page.getByRole('textbox', {
      name: /交给石间|Hand it to SheJane|Describe a task/,
    })
    await composer.fill('client Runtime restart E2E')
    await harness.page.getByRole('button', { name: /发送|Send/ }).click()
    await expect(
      harness.page.locator('.message.assistant .message-content').getByText(
        'Fake runtime reply for the SSE contract test.',
        { exact: true },
      ),
    ).toBeVisible()
    const crashWindowErrors = harness.rendererErrors.slice(rendererErrorCountBeforeCrash)
    expect(crashWindowErrors.length).toBeGreaterThan(0)
    expect(crashWindowErrors.every((error) => (
      error.includes('net::ERR_EMPTY_RESPONSE')
      || error.includes('net::ERR_CONNECTION_REFUSED')
    ))).toBe(true)
  })
})

async function attachRuntimeFailureEvidence(
  testInfo: TestInfo,
  suiteStartedAt: string,
): Promise<void> {
  const runtimeLog = process.env.SHEJANE_E2E_RUNTIME_LOG
  if (runtimeLog && fs.existsSync(runtimeLog)) {
    const attachmentPath = testInfo.outputPath('runtime.log')
    fs.copyFileSync(runtimeLog, attachmentPath)
    await testInfo.attach('runtime.log', {
      path: attachmentPath,
      contentType: 'text/plain',
    }).catch(() => undefined)
  }

  const config = {
    baseURL: requiredEnv('SHEJANE_E2E_RUNTIME_URL'),
    token: requiredEnv('SHEJANE_E2E_RUNTIME_TOKEN'),
  }
  try {
    const runs = (await listLocalRuns(config))
      .filter(run => run.created_at >= suiteStartedAt)
    const diagnostics = await Promise.all(runs.map(async (run) => {
      try {
        return await getLocalRunDiagnostics(run.id, config)
      } catch (error) {
        return {
          run,
          diagnostics_error: error instanceof Error ? error.message : String(error),
        }
      }
    }))
    const diagnosticsPath = testInfo.outputPath('runtime-diagnostics.json')
    fs.writeFileSync(
      diagnosticsPath,
      JSON.stringify({ suite_started_at: suiteStartedAt, diagnostics }, null, 2),
    )
    await testInfo.attach('runtime-diagnostics.json', {
      path: diagnosticsPath,
      contentType: 'application/json',
    })

    const eventLines = diagnostics.flatMap((diagnostic) => {
      if (!('events' in diagnostic) || !Array.isArray(diagnostic.events)) return []
      return diagnostic.events.map(event => JSON.stringify({
        run_id: diagnostic.run.id,
        command_id: diagnostic.run.command_id,
        seq: event.seq,
        event_id: event.id,
        event_type: event.event_type,
        created_at: event.created_at,
        payload: event.payload,
      }))
    })
    const eventsPath = testInfo.outputPath('runtime-sse-events.jsonl')
    fs.writeFileSync(eventsPath, eventLines.length > 0 ? `${eventLines.join('\n')}\n` : '')
    await testInfo.attach('runtime-sse-events.jsonl', {
      path: eventsPath,
      contentType: 'application/x-ndjson',
    })
  } catch (error) {
    const errorPath = testInfo.outputPath('runtime-diagnostics-error.txt')
    fs.writeFileSync(
      errorPath,
      error instanceof Error ? error.stack ?? error.message : String(error),
    )
    await testInfo.attach('runtime-diagnostics-error.txt', {
      path: errorPath,
      contentType: 'text/plain',
    }).catch(() => undefined)
  }
}

async function startRuntimeFaultProxy(upstreamBaseURL: string): Promise<RuntimeFaultProxy> {
  let armed = false
  let faults = 0
  let healthChecks = 0
  const requestCounts = new Map<string, number>()
  const server: Server = createServer((request, response) => {
    const upstreamURL = new URL(request.url ?? '/', upstreamBaseURL)
    const requestKey = `${request.method ?? 'GET'} ${upstreamURL.pathname}`
    requestCounts.set(requestKey, (requestCounts.get(requestKey) ?? 0) + 1)
    if (upstreamURL.pathname.endsWith('/v1/health')) healthChecks += 1
    const upstream = httpRequest(upstreamURL, {
      method: request.method,
      headers: request.headers,
    }, (upstreamResponse) => {
      const shouldFault = armed && upstreamURL.pathname.endsWith('/stream')
      if (!shouldFault) {
        response.writeHead(upstreamResponse.statusCode ?? 502, upstreamResponse.headers)
        upstreamResponse.pipe(response)
        return
      }
      armed = false
      faults += 1
      const headers = { ...upstreamResponse.headers }
      delete headers.connection
      delete headers['content-length']
      delete headers['transfer-encoding']
      response.writeHead(upstreamResponse.statusCode ?? 502, headers)
      let pending = Buffer.alloc(0)
      let closed = false
      upstreamResponse.on('data', (chunk: Buffer) => {
        if (closed) return
        pending = Buffer.concat([pending, chunk])
        const lf = pending.indexOf('\n\n')
        const crlf = pending.indexOf('\r\n\r\n')
        const boundary = lf >= 0 ? lf + 2 : crlf >= 0 ? crlf + 4 : -1
        if (boundary < 0) return
        closed = true
        upstream.destroy()
        response.end(pending.subarray(0, Math.min(pending.length, boundary + 5)))
      })
      upstreamResponse.on('end', () => {
        if (!closed) response.end(pending)
      })
    })
    upstream.on('error', (error) => {
      if (!response.destroyed && !response.writableEnded) response.destroy(error)
    })
    request.pipe(upstream)
  })
  await new Promise<void>((resolve, reject) => {
    server.once('error', reject)
    server.listen(0, '127.0.0.1', () => resolve())
  })
  const address = server.address() as AddressInfo
  return {
    baseURL: `http://127.0.0.1:${address.port}`,
    armNextStreamHalfClose: () => { armed = true },
    faultCount: () => faults,
    healthCount: () => healthChecks,
    requestCount: (method, pathname) => [...requestCounts.entries()]
      .filter(([key]) => key.startsWith(`${method} ${pathname}`))
      .reduce((total, [, count]) => total + count, 0),
    close: () => new Promise<void>((resolve, reject) => {
      server.closeAllConnections()
      server.close(error => error ? reject(error) : resolve())
    }),
  }
}

function startReplacementRuntime(): ChildProcess {
  const runtimeURL = new URL(requiredEnv('SHEJANE_E2E_RUNTIME_URL'))
  const logFD = fs.openSync(requiredEnv('SHEJANE_E2E_RUNTIME_LOG'), 'a')
  try {
    return spawn(path.resolve(clientDir, '../runtime/.venv/bin/python'), [
      '-m',
      'shejane_runtime',
      '--host',
      runtimeURL.hostname,
      '--port',
      runtimeURL.port,
      '--token',
      requiredEnv('SHEJANE_E2E_RUNTIME_TOKEN'),
      '--data-dir',
      requiredEnv('SHEJANE_E2E_RUNTIME_DATA_DIR'),
    ], {
      cwd: path.resolve(clientDir, '../runtime'),
      env: {
        ...process.env,
        PATH: `${requiredEnv('SHEJANE_E2E_RUNTIME_BIN_DIR')}:${process.env.PATH ?? ''}`,
        HOME: requiredEnv('SHEJANE_E2E_RUNTIME_HOME'),
        SHEJANE_FAKE_LLM: '1',
        LANGSMITH_TRACING: 'false',
        LANGCHAIN_TRACING_V2: 'false',
        PYTHONUNBUFFERED: '1',
      },
      detached: process.platform !== 'win32',
      stdio: ['ignore', logFD, logFD],
    })
  } finally {
    fs.closeSync(logFD)
  }
}
