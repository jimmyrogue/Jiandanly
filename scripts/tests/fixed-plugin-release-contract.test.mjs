import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import {
  assertExpectedFixedPlugins,
  assertFixedPluginsEnabled,
  enableFixedPluginsForUpgrade,
  expectedFixedPlugins,
  UPGRADE_PERSISTENCE_PLUGIN_IDS,
} from '../fixed-plugin-release-contract.mjs'

const active = (id, version) => ({
  id,
  version,
  compatibility: 'compatible',
  enabled: false,
  retired: false,
})

test('release platforms declare the fixed packages built by the workflow', () => {
  assert.deepEqual(expectedFixedPlugins('darwin', 'arm64'), [
    { id: 'org.shejane.computer-use', version: '0.2.3' },
    { id: 'org.shejane.browser-qa', version: '0.1.3' },
    { id: 'org.shejane.ocr', version: '0.1.5' },
  ])
  assert.deepEqual(expectedFixedPlugins('win32', 'x64'), [
    { id: 'org.shejane.browser-qa', version: '0.1.3' },
    { id: 'org.shejane.ocr', version: '0.1.5' },
  ])
  assert.throws(
    () => expectedFixedPlugins('darwin', 'x64'),
    /unsupported fixed-plugin release platform/,
  )
})

test('packaged catalog requires every expected fixed plugin to be active', () => {
  const expected = expectedFixedPlugins('win32', 'x64')
  const catalog = expected.map(({ id, version }) => active(id, version))

  assert.doesNotThrow(() => assertExpectedFixedPlugins(catalog, expected, 'candidate'))
  assert.throws(
    () => assertExpectedFixedPlugins(catalog.slice(1), expected, 'candidate'),
    /candidate is missing fixed plugin org\.shejane\.browser-qa@0\.1\.3/,
  )
  assert.throws(
    () => assertExpectedFixedPlugins([
      active('org.shejane.browser-qa', '0.1.2'),
      active('org.shejane.ocr', '0.1.5'),
    ], expected, 'candidate'),
    /candidate is missing fixed plugin org\.shejane\.browser-qa@0\.1\.3/,
  )
  assert.throws(
    () => assertExpectedFixedPlugins([
      active('org.shejane.browser-qa', '0.1.3'),
      { ...active('org.shejane.ocr', '0.1.5'), retired: true },
    ], expected, 'candidate'),
    /candidate fixed plugin org\.shejane\.ocr@0\.1\.5 is not active/,
  )
  assert.throws(
    () => assertExpectedFixedPlugins([
      { ...active('org.shejane.browser-qa', '0.1.3'), compatibility: 'incompatible' },
      active('org.shejane.ocr', '0.1.5'),
    ], expected, 'candidate'),
    /candidate fixed plugin org\.shejane\.browser-qa@0\.1\.3 is not active/,
  )
})

test('upgrade persistence requires Browser QA and OCR to remain enabled', () => {
  assert.deepEqual(UPGRADE_PERSISTENCE_PLUGIN_IDS, [
    'org.shejane.browser-qa',
    'org.shejane.ocr',
  ])
  const plugins = [
    { ...active('org.shejane.browser-qa', '0.1.3'), enabled: true },
    { ...active('org.shejane.ocr', '0.1.5'), enabled: true },
  ]

  assert.doesNotThrow(() => assertFixedPluginsEnabled(
    plugins,
    ['org.shejane.browser-qa', 'org.shejane.ocr'],
    'candidate',
  ))
  assert.throws(
    () => assertFixedPluginsEnabled(
      [{ ...plugins[0], enabled: false }, plugins[1]],
      ['org.shejane.browser-qa', 'org.shejane.ocr'],
      'candidate',
    ),
    /candidate fixed plugin org\.shejane\.browser-qa is not enabled/,
  )
})

test('upgrade smoke enables only persistence plugins through commands', async () => {
  const plugins = [
    { ...active('org.shejane.computer-use', '0.2.3'), digest: `sha256:${'a'.repeat(64)}` },
    { ...active('org.shejane.browser-qa', '0.1.3'), digest: `sha256:${'b'.repeat(64)}` },
    { ...active('org.shejane.ocr', '0.1.5'), digest: `sha256:${'c'.repeat(64)}` },
  ]
  const requests = []

  await enableFixedPluginsForUpgrade(
    plugins,
    UPGRADE_PERSISTENCE_PLUGIN_IDS,
    'previous',
    async (path, label, init) => requests.push({ path, label, init }),
  )

  assert.deepEqual(requests.map(({ path, label, init }) => ({
    path,
    label,
    method: init.method,
    contentType: init.headers['Content-Type'],
    body: JSON.parse(init.body),
  })), [
    {
      path: '/v1/commands',
      label: 'enable org.shejane.browser-qa',
      method: 'POST',
      contentType: 'application/json',
      body: {
        type: 'plugin.enable',
        command_id: 'cmd_upgrade_smoke_enable_org.shejane.browser-qa',
        plugin_id: 'org.shejane.browser-qa',
        expected_digest: `sha256:${'b'.repeat(64)}`,
      },
    },
    {
      path: '/v1/commands',
      label: 'enable org.shejane.ocr',
      method: 'POST',
      contentType: 'application/json',
      body: {
        type: 'plugin.enable',
        command_id: 'cmd_upgrade_smoke_enable_org.shejane.ocr',
        plugin_id: 'org.shejane.ocr',
        expected_digest: `sha256:${'c'.repeat(64)}`,
      },
    },
  ])
})

test('release upgrade smoke isolates credentials and pins downloaded installers', () => {
  const workflow = readFileSync(
    new URL('../../.github/workflows/release-client.yml', import.meta.url),
    'utf8',
  )
  const upgradeSmoke = readFileSync(
    new URL('../test-packaged-runtime-upgrade.mjs', import.meta.url),
    'utf8',
  )
  const assetLock = JSON.parse(readFileSync(
    new URL('../client-release-asset-lock.json', import.meta.url),
    'utf8',
  ))

  assert.doesNotMatch(upgradeSmoke, /\.\.\.process\.env/)
  assert.match(upgradeSmoke, /const CHILD_ENVIRONMENT_KEYS =/)
  assert.match(upgradeSmoke, /env: childEnvironment\(/)
  assert.match(workflow, /permissions:\n  contents: read/)
  assert.match(workflow, /release:\n(?:.|\n)*?permissions:\n      contents: write/)
  assert.match(workflow, /client-release-asset-lock\.json/)
  assert.deepEqual(assetLock['client-v0.1.38'], {
    'SheJane-0.1.38-arm64.zip': 'sha256:dbfba9e283eb22b446808f488d6ebbc6b844f1b3f68736a9dbae8b9cd84a0eac',
    'SheJane-0.1.38-x64.exe': 'sha256:ea2db236f0f0f3547935619ba1de2a6b178751389ef8c1250f02eb8c4ac465f6',
  })
})

test('Client release entry points reject prerelease suffixes from the stable lane', () => {
  const workflow = readFileSync(
    new URL('../../.github/workflows/release-client.yml', import.meta.url),
    'utf8',
  )
  const makefile = readFileSync(new URL('../../Makefile', import.meta.url), 'utf8')
  const upgradeSmoke = readFileSync(
    new URL('../test-packaged-runtime-upgrade.mjs', import.meta.url),
    'utf8',
  )

  assert.match(workflow, /\^client-v\[0-9\]\+\\\.\[0-9\]\+\\\.\[0-9\]\+\$/)
  assert.match(makefile, /\^\[0-9\]\+\\\.\[0-9\]\+\\\.\[0-9\]\+\$\$/)
  assert.match(upgradeSmoke, /\^client-v\\d\+\\\.\\d\+\\\.\\d\+\$/)
})
