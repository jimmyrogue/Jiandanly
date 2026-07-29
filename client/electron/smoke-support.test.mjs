import { mkdtemp, readFile, writeFile } from 'node:fs/promises'
import { existsSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { createRequire } from 'node:module'
import { describe, expect, it } from 'vitest'

const require = createRequire(import.meta.url)
const { installDesktopSmokeQuitWatcher, writeDesktopSmokeConfig } = require('./smoke-support.cjs')

describe('desktop smoke support', () => {
  it('does not write anything unless an explicit smoke file is provided', () => {
    expect(
      writeDesktopSmokeConfig({
        baseURL: 'http://127.0.0.1:17371',
        token: 'secret-token',
        resourcesPath: '/tmp/resources',
        runtimePid: 123,
      }),
    ).toBe(false)
  })

  it('writes the runtime smoke handoff file for packaged app tests', async () => {
    const dir = await mkdtemp(join(tmpdir(), 'shejane-smoke-support-'))
    const filePath = join(dir, 'handoff.json')

    expect(
      writeDesktopSmokeConfig({
        filePath,
        baseURL: 'http://127.0.0.1:34567',
        token: 'secret-token',
        resourcesPath: '/tmp/resources',
        runtimePid: 456,
        crashDirectory: '/tmp/crashes',
      }),
    ).toBe(true)

    expect(existsSync(filePath)).toBe(true)
    const payload = JSON.parse(await readFile(filePath, 'utf8'))
    expect(payload).toMatchObject({
      schema: 1,
      baseURL: 'http://127.0.0.1:34567',
      token: 'secret-token',
      resourcesPath: '/tmp/resources',
      runtimePid: 456,
      crashDirectory: '/tmp/crashes',
    })
    expect(payload.writtenAt).toMatch(/^\d{4}-\d{2}-\d{2}T/)
  })

  it('quits through the normal app lifecycle after an explicit smoke signal', async () => {
    const dir = await mkdtemp(join(tmpdir(), 'shejane-smoke-support-'))
    const filePath = join(dir, 'quit')
    let quitCalls = 0
    const stop = installDesktopSmokeQuitWatcher({
      filePath,
      intervalMs: 5,
      quit: () => {
        quitCalls += 1
      },
    })

    expect(typeof stop).toBe('function')
    await writeFile(filePath, '')
    await new Promise((resolve) => setTimeout(resolve, 30))
    stop()

    expect(quitCalls).toBe(1)
  })
})
