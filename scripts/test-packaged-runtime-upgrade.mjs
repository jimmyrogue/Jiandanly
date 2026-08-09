#!/usr/bin/env node

import { spawn } from 'node:child_process'
import { constants } from 'node:fs'
import { access, mkdir, mkdtemp, rm } from 'node:fs/promises'
import { createServer } from 'node:net'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { randomBytes } from 'node:crypto'

import {
  assertExpectedFixedPlugins,
  assertFixedPluginsEnabled,
  enableFixedPluginsForUpgrade,
  expectedFixedPlugins,
  UPGRADE_PERSISTENCE_PLUGIN_IDS,
} from './fixed-plugin-release-contract.mjs'

const [previousInput, currentInput, previousTag, currentTag] = process.argv.slice(2)
if (!previousInput || !currentInput || !previousTag || !currentTag) {
  throw new Error(
    'usage: node scripts/test-packaged-runtime-upgrade.mjs '
    + '/path/to/previous-runtime /path/to/current-runtime previous-tag current-tag',
  )
}
for (const tag of [previousTag, currentTag]) {
  if (!/^client-v\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$/.test(tag)) {
    throw new Error(`invalid Client release tag: ${tag}`)
  }
}

const previousRuntime = resolve(previousInput)
const currentRuntime = resolve(currentInput)
const temporaryRoot = await mkdtemp(join(tmpdir(), 'shejane-packaged-runtime-upgrade-'))
const dataDir = join(temporaryRoot, 'runtime')
const home = join(temporaryRoot, 'home')
await mkdir(dataDir)
await mkdir(home)
const token = randomBytes(32).toString('hex')
const wait = (milliseconds) => new Promise((done) => setTimeout(done, milliseconds))

async function freePort() {
  return new Promise((resolvePort, rejectPort) => {
    const server = createServer()
    server.once('error', rejectPort)
    server.listen(0, '127.0.0.1', () => {
      const address = server.address()
      const port = typeof address === 'object' && address ? address.port : 0
      server.close((error) => error ? rejectPort(error) : resolvePort(port))
    })
  })
}

async function stopProcess(child) {
  if (child.exitCode !== null) return
  const closed = new Promise((resolveClose) => child.once('close', resolveClose))
  child.kill('SIGTERM')
  if (await Promise.race([closed.then(() => true), wait(10_000).then(() => false)])) return
  child.kill('SIGKILL')
  await closed
}

async function startAndVerify(runtime, tag, pluginIdsToEnable = []) {
  const port = await freePort()
  let stdout = ''
  let stderr = ''
  const child = spawn(runtime, [
    '--host', '127.0.0.1',
    '--port', String(port),
    '--token', token,
    '--data-dir', dataDir,
    '--fixed-runtime-asset-base-url',
    `https://github.com/jimmyrogue/SheJane/releases/download/${tag}`,
  ], {
    env: {
      ...process.env,
      HOME: home,
      USERPROFILE: home,
      PYTHONUNBUFFERED: '1',
    },
    stdio: ['ignore', 'pipe', 'pipe'],
  })
  child.stdout.on('data', (chunk) => {
    stdout = `${stdout}${chunk}`.slice(-32_768)
  })
  child.stderr.on('data', (chunk) => {
    stderr = `${stderr}${chunk}`.slice(-32_768)
  })

  async function fetchJson(path, label, init = {}) {
    let lastError
    for (let attempt = 1; attempt <= 5 && child.exitCode === null; attempt += 1) {
      try {
        const response = await fetch(`http://127.0.0.1:${port}${path}`, {
          ...init,
          headers: {
            Authorization: `Bearer ${token}`,
            ...init.headers,
          },
          signal: AbortSignal.timeout(5_000),
        })
        if (!response.ok) {
          throw new Error(`${tag} ${label} returned HTTP ${response.status}`)
        }
        return await response.json()
      } catch (error) {
        lastError = error
        if (attempt < 5 && child.exitCode === null) await wait(250)
      }
    }
    throw new Error(
      `${tag} ${label} failed after Runtime became healthy: ${lastError}`
      + ` (exit=${child.exitCode}, signal=${child.signalCode})`
      + `\nstdout:\n${stdout}\nstderr:\n${stderr}`,
    )
  }

  try {
    const deadline = Date.now() + 120_000
    while (Date.now() < deadline && child.exitCode === null) {
      let healthy = false
      try {
        const response = await fetch(`http://127.0.0.1:${port}/v1/health`, {
          headers: { Authorization: `Bearer ${token}` },
          signal: AbortSignal.timeout(1_000),
        })
        healthy = response.ok && (await response.json()).status === 'ok'
      } catch {
        // The frozen Runtime may still be opening or migrating its data.
      }
      if (healthy) {
        const summaries = (await fetchJson('/v1/plugins', 'plugin catalog')).plugins
        let details = []
        for (const { id } of summaries) {
          details.push(await fetchJson(
            `/v1/plugins/${encodeURIComponent(id)}`,
            `plugin ${id}`,
          ))
        }
        await enableFixedPluginsForUpgrade(details, pluginIdsToEnable, tag, fetchJson)
        if (pluginIdsToEnable.length > 0) {
          details = []
          for (const { id } of summaries) {
            details.push(await fetchJson(
              `/v1/plugins/${encodeURIComponent(id)}`,
              `enabled plugin ${id}`,
            ))
          }
          assertFixedPluginsEnabled(details, pluginIdsToEnable, tag)
        }
        return details
      }
      await wait(100)
    }
    throw new Error(
      `${tag} frozen Runtime did not become healthy (exit=${child.exitCode}, signal=${child.signalCode})`
      + `\nstdout:\n${stdout}\nstderr:\n${stderr}`,
    )
  } finally {
    await stopProcess(child)
  }
}

try {
  await access(previousRuntime, constants.R_OK)
  await access(currentRuntime, constants.R_OK)
  const previousPlugins = await startAndVerify(
    previousRuntime,
    previousTag,
    UPGRADE_PERSISTENCE_PLUGIN_IDS,
  )
  const currentPlugins = await startAndVerify(currentRuntime, currentTag)
  assertExpectedFixedPlugins(
    currentPlugins,
    expectedFixedPlugins(process.platform, process.arch),
    currentTag,
  )
  assertFixedPluginsEnabled(
    currentPlugins,
    UPGRADE_PERSISTENCE_PLUGIN_IDS,
    currentTag,
  )
  const currentByID = new Map(currentPlugins.map((plugin) => [plugin.id, plugin]))
  for (const previous of previousPlugins) {
    const current = currentByID.get(previous.id)
    if (!current) {
      throw new Error(`${currentTag} dropped fixed plugin ${previous.id}`)
    }
    if (!current.versions.some(
      (version) => version.version === previous.version && version.digest === previous.digest,
    )) {
      throw new Error(`${currentTag} did not preserve ${previous.id}@${previous.version}`)
    }
  }
  process.stdout.write(`packaged Runtime upgrade smoke passed: ${previousTag} -> ${currentTag}\n`)
} finally {
  await rm(temporaryRoot, { recursive: true, force: true, maxRetries: 20, retryDelay: 100 })
}
