import { execFileSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

import {
  createLocalSkill,
  createMcpServer,
  createLocalRun,
  deleteLocalSkill,
  deleteMcpServer,
  getLocalArtifactContent,
  getLocalPlugin,
  getLocalRunDiagnostics,
  getLocalRuntimeInfo,
  getLocalSkillFile,
  getRuntimeSettings,
  installLocalPluginCommand,
  listInstalledSkills,
  listModelServicePresets,
  listModelServices,
  listLocalPlugins,
  listLocalRuntimeModels,
  listMcpServers,
  removeLocalPluginCommand,
  resolveLocalPermissionCommand,
  setLocalPluginEnabledCommand,
  streamLocalRun,
  updateLocalSkill,
  updateLocalPluginCommand,
  updateMcpServer,
  updateRuntimeSettings,
} from './client'

const BASE_URL = process.env.VITE_TEST_RUNTIME_URL
const TOKEN = process.env.VITE_TEST_RUNTIME_TOKEN ?? 'dev-local-token'

describe.skipIf(!BASE_URL)('flow:P1-P6 > contract: Runtime catalogs (live runtime)', () => {
  const config = { baseURL: BASE_URL!, token: TOKEN }

  it('flow:P3 > authenticates Runtime discovery and rejects a wrong token', async () => {
    const info = await getLocalRuntimeInfo(config)
    expect(info.protocol_version).toBe(1)
    expect(info.runtime_version).toMatch(/^\d+\.\d+\.\d+/)
    expect(info.capabilities).toEqual(expect.arrayContaining(['agent.run', 'agent.stream']))

    const rejected = await fetch(`${BASE_URL}/v1/runtime`, {
      headers: { Authorization: 'Bearer wrong-token' },
    })
    expect(rejected.status).toBe(401)
  })

  it('persists Runtime settings through the public interface', async () => {
    const original = await getRuntimeSettings(config)
    const nextCalls = original.max_model_calls === 7 ? 8 : 7
    try {
      const updated = await updateRuntimeSettings({
        max_model_calls: nextCalls,
        plan_first: 'auto',
      }, config)
      expect(updated.max_model_calls).toBe(nextCalls)
      expect(updated.plan_first).toBe('auto')
      expect(updated.version).toBeGreaterThan(original.version)

      await expect(getRuntimeSettings(config)).resolves.toMatchObject({
        max_model_calls: nextCalls,
        plan_first: 'auto',
      })
    } finally {
      await updateRuntimeSettings({
        max_model_calls: original.max_model_calls,
        plan_first: original.plan_first,
      }, config)
    }
  })

  it('publishes the built-in tool catalog', async () => {
    const response = await fetch(`${BASE_URL}/v1/tools`, {
      headers: { Authorization: `Bearer ${TOKEN}` },
    })
    expect(response.status).toBe(200)
    const body = await response.json() as { tools: Array<{ name: string }> }
    const names = body.tools.map((tool) => tool.name)
    expect(names).toEqual(expect.arrayContaining([
      'read_file',
      'write_file',
      'execute',
      'time.now',
      'task.verify',
    ]))
  })

  it('publishes official and China-first model service presets with cached connections', async () => {
    const presets = await listModelServicePresets(config)
    expect(presets.map((preset) => preset.id)).toEqual([
      'shejane-official',
      'deepseek',
      'kimi',
      'qwen',
      'glm',
      'minimax',
      'siliconflow',
      'openai',
      'anthropic',
      'google',
      'custom',
    ])
    expect(presets[0]).toMatchObject({
      connection_method: 'browser_authorization',
      regions: [],
    })
    expect(presets[1]?.regions[0]).toMatchObject({ id: 'cn', default: true })
    await expect(listModelServices(config)).resolves.toEqual(expect.any(Array))
    await expect(listLocalRuntimeModels(config)).resolves.toEqual(expect.any(Array))
  })

  it('manages a Skill inside the isolated Runtime Skill root', async () => {
    const name = 'e2e-skill'
    await deleteLocalSkill(name, config).catch(() => undefined)
    try {
      await createLocalSkill({
        name,
        description: 'E2E Skill',
        content: '# E2E Skill\n\nReturn a deterministic result.',
      }, config)
      await expect(getLocalSkillFile(name, config)).resolves.toMatchObject({
        name,
        content: expect.stringContaining('deterministic result'),
      })
      await updateLocalSkill(name, {
        name,
        description: 'Updated E2E Skill',
        content: '# Updated E2E Skill',
      }, config)
      const catalog = await listInstalledSkills(config)
      expect(catalog.skills).toEqual(expect.arrayContaining([
        expect.objectContaining({ name }),
      ]))
    } finally {
      await deleteLocalSkill(name, config).catch(() => undefined)
    }
    expect((await listInstalledSkills(config)).skills.some((item) => item.name === name)).toBe(false)
  })

  it('manages Runtime-owned MCP configuration without exposing secret values', async () => {
    const name = 'e2e-mcp'
    const args = ['run', 'python', 'tests/fixtures/e2e_mcp_server.py']
    await deleteMcpServer(name, config).catch(() => undefined)
    try {
      const created = await createMcpServer({
        name,
        transport: 'stdio',
        command: 'uv',
        args,
        env: { E2E_SECRET: 'not-a-real-secret' },
      }, config)
      expect(created.server).toMatchObject({ name, env_keys: ['E2E_SECRET'] })
      expect(JSON.stringify(created)).not.toContain('not-a-real-secret')

      const updated = await updateMcpServer(name, {
        name,
        transport: 'stdio',
        command: 'uv',
        args,
        env: {},
      }, config)
      expect(updated.server).toMatchObject({ args, env_keys: [] })
      expect((await listMcpServers(config)).servers).toEqual(expect.arrayContaining([
        expect.objectContaining({ name, args }),
      ]))
    } finally {
      await deleteMcpServer(name, config).catch(() => undefined)
    }
    expect((await listMcpServers(config)).servers.some((item) => item.name === name)).toBe(false)
  })

  it('flow:P3/P6/P10-P12 > plugin:wasi > contains capability, schema, guest, and fuel failures before a healthy execution', async () => {
    const pluginID = 'dev.shejane.fixture.archive'
    const deniedPluginID = 'dev.shejane.fixture.denied'
    const trappedPluginID = 'dev.shejane.fixture.fuel-trap'
    const invalidOutputPluginID = 'dev.shejane.fixture.invalid-output'
    const temp = mkdtempSync(join(tmpdir(), 'shejane-e2e-plugin-'))
    const packagePath = join(temp, 'archive.shejane-plugin')
    const deniedPackagePath = join(temp, 'denied.shejane-plugin')
    const trappedPackagePath = join(temp, 'fuel-trap.shejane-plugin')
    const invalidOutputPackagePath = join(temp, 'invalid-output.shejane-plugin')
    const inputPath = join(temp, 'input.zip')
    const invalidInputPath = join(temp, 'invalid.zip')
    const trappedInputPath = join(temp, 'fuel-trap.zip')
    const runtimeRoot = resolve(process.cwd(), '../runtime')
    const fixtureRoot = resolve(process.cwd(), '../runtime/plugins/fixtures/wasi-archive')
    const suffix = Date.now().toString(36)
    let digest: string | undefined
    let deniedDigest: string | undefined
    let trappedDigest: string | undefined
    let invalidOutputDigest: string | undefined

    createPluginFixtures(
      runtimeRoot,
      fixtureRoot,
      packagePath,
      deniedPackagePath,
      trappedPackagePath,
      invalidOutputPackagePath,
      inputPath,
      trappedInputPath,
    )
    writeFileSync(invalidInputPath, 'not a ZIP archive')
    await removeLocalPluginCommand(`cmd_e2e_plugin_preclean_${suffix}`, pluginID, undefined, config)
      .catch(() => undefined)
    try {
      const installed = await installLocalPluginCommand(
        `cmd_e2e_plugin_install_${suffix}`,
        packagePath,
        { allowUnsigned: true },
        config,
      )
      digest = installed.digest
      expect(installed).toMatchObject({ plugin_id: pluginID, installed: true })
      await setLocalPluginEnabledCommand(
        `cmd_e2e_plugin_enable_${suffix}`,
        pluginID,
        true,
        digest,
        config,
      )
      expect(await getLocalPlugin(pluginID, config)).toMatchObject({
        id: pluginID,
        enabled: true,
        actions: [expect.objectContaining({ id: 'archive.extract' })],
      })

      const deniedInstalled = await installLocalPluginCommand(
        `cmd_e2e_plugin_denied_install_${suffix}`,
        deniedPackagePath,
        { allowUnsigned: true },
        config,
      )
      deniedDigest = deniedInstalled.digest
      await setLocalPluginEnabledCommand(
        `cmd_e2e_plugin_denied_enable_${suffix}`,
        deniedPluginID,
        true,
        deniedDigest,
        config,
      )
      const deniedCanonicalName = `plugin.${deniedPluginID}.archive.extract`
      const deniedPayload = Buffer.from(JSON.stringify({
        name: pluginWireToolName(deniedCanonicalName),
        args: { input_id: 'source' },
      }), 'utf8').toString('base64url')
      const deniedRun = await createLocalRun({
        commandId: `cmd_e2e_plugin_denied_run_${suffix}`,
        clientMessageId: `msg_e2e_plugin_denied_run_${suffix}`,
        goal: `Reject an unavailable Plugin capability.\n[[e2e:tool:${deniedPayload}]]`,
        attachmentPaths: [inputPath],
        pluginRefs: [{ pluginId: deniedPluginID, expectedDigest: deniedDigest }],
        permissionMode: 'auto',
        settings: { memory: 'off', skills: 'off', mcp: 'off' },
        mode: 'local:test:model',
      }, config)
      const deniedEvents: Array<{ event_type: string; payload?: Record<string, unknown> }> = []
      await streamLocalRun(deniedRun.id, config, {
        onEvent: event => deniedEvents.push(event),
        onDelta: () => undefined,
      })
      expect(deniedEvents.map(event => event.event_type)).toContain('tool.failed')
      expect(deniedEvents.map(event => event.event_type)).toContain('run.completed')
      expect(JSON.stringify(deniedEvents)).toContain('capability_denied')
      expect(JSON.stringify(deniedEvents)).toContain('network.http')
      const deniedDiagnostics = await getLocalRunDiagnostics(deniedRun.id, config)
      expect(deniedDiagnostics.tool_receipts).toEqual(expect.arrayContaining([
        expect.objectContaining({
          tool_name: deniedCanonicalName,
          status: 'failed',
          attempt_count: 1,
        }),
      ]))
      expect(deniedDiagnostics.artifacts).toHaveLength(0)

      const trappedInstalled = await installLocalPluginCommand(
        `cmd_e2e_plugin_trapped_install_${suffix}`,
        trappedPackagePath,
        { allowUnsigned: true },
        config,
      )
      trappedDigest = trappedInstalled.digest
      await setLocalPluginEnabledCommand(
        `cmd_e2e_plugin_trapped_enable_${suffix}`,
        trappedPluginID,
        true,
        trappedDigest,
        config,
      )
      const trappedCanonicalName = `plugin.${trappedPluginID}.archive.extract`
      const trappedPayload = Buffer.from(JSON.stringify({
        name: pluginWireToolName(trappedCanonicalName),
        args: { input_id: 'source' },
      }), 'utf8').toString('base64url')
      const trappedRun = await createLocalRun({
        commandId: `cmd_e2e_plugin_trapped_run_${suffix}`,
        clientMessageId: `msg_e2e_plugin_trapped_run_${suffix}`,
        goal: `Contain a deterministic WASI fuel trap.\n[[e2e:tool:${trappedPayload}]]`,
        attachmentPaths: [trappedInputPath],
        pluginRefs: [{ pluginId: trappedPluginID, expectedDigest: trappedDigest }],
        permissionMode: 'auto',
        settings: { memory: 'off', skills: 'off', mcp: 'off' },
        mode: 'local:test:model',
      }, config)
      const trappedEvents: Array<{ event_type: string; payload?: Record<string, unknown> }> = []
      await streamLocalRun(trappedRun.id, config, {
        onEvent: event => trappedEvents.push(event),
        onDelta: () => undefined,
      })
      expect(
        trappedEvents.map(event => event.event_type),
        JSON.stringify(trappedEvents),
      ).toContain('tool.failed')
      expect(trappedEvents.map(event => event.event_type)).toContain('run.completed')
      expect(JSON.stringify(trappedEvents)).toContain('resource_exhausted')
      const trappedDiagnostics = await getLocalRunDiagnostics(trappedRun.id, config)
      expect(trappedDiagnostics.tool_receipts).toEqual(expect.arrayContaining([
        expect.objectContaining({
          tool_name: trappedCanonicalName,
          status: 'failed',
          risk: 'plugin_action',
          attempt_count: 1,
        }),
      ]))
      expect(trappedDiagnostics.artifacts).toHaveLength(0)

      const invalidOutputInstalled = await installLocalPluginCommand(
        `cmd_e2e_plugin_invalid_output_install_${suffix}`,
        invalidOutputPackagePath,
        { allowUnsigned: true },
        config,
      )
      invalidOutputDigest = invalidOutputInstalled.digest
      await setLocalPluginEnabledCommand(
        `cmd_e2e_plugin_invalid_output_enable_${suffix}`,
        invalidOutputPluginID,
        true,
        invalidOutputDigest,
        config,
      )
      const invalidOutputCanonicalName = `plugin.${invalidOutputPluginID}.archive.extract`
      const invalidOutputPayload = Buffer.from(JSON.stringify({
        name: pluginWireToolName(invalidOutputCanonicalName),
        args: { input_id: 'source' },
      }), 'utf8').toString('base64url')
      const invalidOutputRun = await createLocalRun({
        commandId: `cmd_e2e_plugin_invalid_output_run_${suffix}`,
        clientMessageId: `msg_e2e_plugin_invalid_output_run_${suffix}`,
        goal: `Reject a Plugin result that violates its published output schema.\n[[e2e:tool:${invalidOutputPayload}]]`,
        attachmentPaths: [inputPath],
        pluginRefs: [{ pluginId: invalidOutputPluginID, expectedDigest: invalidOutputDigest }],
        permissionMode: 'auto',
        settings: { memory: 'off', skills: 'off', mcp: 'off' },
        mode: 'local:test:model',
      }, config)
      const invalidOutputEvents: Array<{ event_type: string; payload?: Record<string, unknown> }> = []
      await streamLocalRun(invalidOutputRun.id, config, {
        onEvent: event => invalidOutputEvents.push(event),
        onDelta: () => undefined,
      })
      expect(invalidOutputEvents.map(event => event.event_type)).toContain('tool.failed')
      expect(invalidOutputEvents.map(event => event.event_type)).toContain('run.completed')
      expect(JSON.stringify(invalidOutputEvents)).toContain('protocol_violation')
      const invalidOutputDiagnostics = await getLocalRunDiagnostics(invalidOutputRun.id, config)
      expect(invalidOutputDiagnostics.tool_receipts).toEqual(expect.arrayContaining([
        expect.objectContaining({
          tool_name: invalidOutputCanonicalName,
          status: 'failed',
          risk: 'plugin_action',
          attempt_count: 1,
        }),
      ]))
      expect(invalidOutputDiagnostics.artifacts).toHaveLength(0)

      const canonicalToolName = `plugin.${pluginID}.archive.extract`
      const wireToolName = pluginWireToolName(canonicalToolName)
      const payload = Buffer.from(JSON.stringify({
        name: wireToolName,
        args: { input_id: 'source' },
      }), 'utf8').toString('base64url')
      const failedRun = await createLocalRun({
        commandId: `cmd_e2e_plugin_failed_run_${suffix}`,
        clientMessageId: `msg_e2e_plugin_failed_run_${suffix}`,
        goal: `Reject the invalid archive.\n[[e2e:tool:${payload}]]`,
        attachmentPaths: [invalidInputPath],
        pluginRefs: [{ pluginId: pluginID, expectedDigest: digest }],
        permissionMode: 'auto',
        settings: { memory: 'off', skills: 'off', mcp: 'off' },
        mode: 'local:test:model',
      }, config)
      const failedEventTypes: string[] = []
      await streamLocalRun(failedRun.id, config, {
        onEvent: event => failedEventTypes.push(event.event_type),
        onDelta: () => undefined,
      })
      expect(failedEventTypes).toContain('tool.failed')
      expect(failedEventTypes).toContain('run.completed')
      const failedDiagnostics = await getLocalRunDiagnostics(failedRun.id, config)
      expect(failedDiagnostics.tool_receipts).toEqual(expect.arrayContaining([
        expect.objectContaining({
          tool_name: canonicalToolName,
          status: 'failed',
          risk: 'plugin_action',
          attempt_count: 1,
        }),
      ]))
      expect(failedDiagnostics.artifacts).toHaveLength(0)

      const run = await createLocalRun({
        commandId: `cmd_e2e_plugin_run_${suffix}`,
        clientMessageId: `msg_e2e_plugin_run_${suffix}`,
        goal: `Extract the authorized archive.\n[[e2e:tool:${payload}]]`,
        attachmentPaths: [inputPath],
        pluginRefs: [{ pluginId: pluginID, expectedDigest: digest }],
        permissionMode: 'auto',
        settings: { memory: 'off', skills: 'off', mcp: 'off' },
        mode: 'local:test:model',
      }, config)
      const eventTypes: string[] = []
      await streamLocalRun(run.id, config, {
        onEvent: event => eventTypes.push(event.event_type),
        onDelta: () => undefined,
      })

      expect(eventTypes).toContain('tool.completed')
      expect(eventTypes).toContain('permission.auto_approved')
      expect(eventTypes).toContain('run.completed')
      expect(eventTypes).not.toContain('permission.required')
      const diagnostics = await getLocalRunDiagnostics(run.id, config)
      expect(diagnostics.tool_receipts).toEqual(expect.arrayContaining([
        expect.objectContaining({ status: 'completed', risk: 'plugin_action' }),
      ]))
      const artifact = diagnostics.artifacts.find(item => item.kind === 'plugin_output')
      expect(artifact).toMatchObject({ tool_name: canonicalToolName })
      const content = await getLocalArtifactContent(artifact!.id, config)
      expect(await content.text()).toBe('real graph plugin tool\n')
    } finally {
      await removeLocalPluginCommand(
        `cmd_e2e_plugin_remove_${suffix}`,
        pluginID,
        digest,
        config,
      ).catch(() => undefined)
      await removeLocalPluginCommand(
        `cmd_e2e_plugin_denied_remove_${suffix}`,
        deniedPluginID,
        deniedDigest,
        config,
      ).catch(() => undefined)
      await removeLocalPluginCommand(
        `cmd_e2e_plugin_trapped_remove_${suffix}`,
        trappedPluginID,
        trappedDigest,
        config,
      ).catch(() => undefined)
      await removeLocalPluginCommand(
        `cmd_e2e_plugin_invalid_output_remove_${suffix}`,
        invalidOutputPluginID,
        invalidOutputDigest,
        config,
      ).catch(() => undefined)
      expect((await listLocalPlugins(config)).some(item => item.id === pluginID && !item.retired)).toBe(false)
      rmSync(temp, { recursive: true, force: true })
    }
  })

  it('freezes the admitted Plugin version while a Tool waits for approval', async () => {
    const pluginID = 'dev.shejane.fixture.archive'
    const temp = mkdtempSync(join(tmpdir(), 'shejane-e2e-plugin-freeze-'))
    const packagePath = join(temp, 'archive-v1.shejane-plugin')
    const updatedPackagePath = join(temp, 'archive-v2.shejane-plugin')
    const inputPath = join(temp, 'input.zip')
    const runtimeRoot = resolve(process.cwd(), '../runtime')
    const fixtureRoot = resolve(process.cwd(), '../runtime/plugins/fixtures/wasi-archive')
    const suffix = Date.now().toString(36)
    let activeDigest: string | undefined

    createPluginFreezeFixtures(
      runtimeRoot,
      fixtureRoot,
      packagePath,
      updatedPackagePath,
      inputPath,
    )
    await removeLocalPluginCommand(`cmd_e2e_plugin_freeze_preclean_${suffix}`, pluginID, undefined, config)
      .catch(() => undefined)
    try {
      const installed = await installLocalPluginCommand(
        `cmd_e2e_plugin_freeze_install_${suffix}`,
        packagePath,
        { allowUnsigned: true },
        config,
      )
      activeDigest = installed.digest
      await setLocalPluginEnabledCommand(
        `cmd_e2e_plugin_freeze_enable_${suffix}`,
        pluginID,
        true,
        activeDigest,
        config,
      )
      const canonicalToolName = `plugin.${pluginID}.archive.extract`
      const payload = Buffer.from(JSON.stringify({
        name: pluginWireToolName(canonicalToolName),
        args: { input_id: 'source' },
      }), 'utf8').toString('base64url')
      const frozenRun = await createLocalRun({
        commandId: `cmd_e2e_plugin_freeze_run_${suffix}`,
        clientMessageId: `msg_e2e_plugin_freeze_run_${suffix}`,
        goal: `Use the admitted Plugin version.\n[[e2e:tool:${payload}]]`,
        attachmentPaths: [inputPath],
        pluginRefs: [{ pluginId: pluginID, expectedDigest: activeDigest }],
        permissionMode: 'ask',
        settings: { memory: 'off', skills: 'off', mcp: 'off' },
        mode: 'local:test:model',
      }, config)
      const waitingEvents: Array<{ event_type: string; payload?: Record<string, unknown> }> = []
      await streamLocalRun(frozenRun.id, config, {
        onEvent: event => waitingEvents.push(event),
        onDelta: () => undefined,
      })
      const permission = waitingEvents.find(event => event.event_type === 'permission.required')
      expect(permission).toBeDefined()

      const updated = await updateLocalPluginCommand(
        `cmd_e2e_plugin_freeze_update_${suffix}`,
        pluginID,
        updatedPackagePath,
        { allowUnsigned: true, expectedDigest: activeDigest },
        config,
      )
      activeDigest = updated.digest
      await resolveLocalPermissionCommand(
        `cmd_e2e_plugin_freeze_approve_${suffix}`,
        String(permission?.payload?.request_id ?? ''),
        'approve',
        { scope: 'once' },
        config,
      )
      const resumedEvents: Array<{ event_type: string; payload?: Record<string, unknown> }> = []
      await streamLocalRun(frozenRun.id, config, {
        onEvent: event => resumedEvents.push(event),
        onDelta: () => undefined,
      })
      expect(resumedEvents.map(event => event.event_type)).toContain('tool.completed')
      expect(resumedEvents.map(event => event.event_type)).toContain('run.completed')
      expect(JSON.stringify(resumedEvents)).not.toContain('capability_denied')

      const refreshedRun = await createLocalRun({
        commandId: `cmd_e2e_plugin_freeze_refreshed_run_${suffix}`,
        clientMessageId: `msg_e2e_plugin_freeze_refreshed_run_${suffix}`,
        goal: `Use the updated Plugin version.\n[[e2e:tool:${payload}]]`,
        attachmentPaths: [inputPath],
        pluginRefs: [{ pluginId: pluginID, expectedDigest: activeDigest }],
        permissionMode: 'auto',
        settings: { memory: 'off', skills: 'off', mcp: 'off' },
        mode: 'local:test:model',
      }, config)
      const refreshedEvents: Array<{ event_type: string; payload?: Record<string, unknown> }> = []
      await streamLocalRun(refreshedRun.id, config, {
        onEvent: event => refreshedEvents.push(event),
        onDelta: () => undefined,
      })
      expect(refreshedEvents.map(event => event.event_type)).toContain('tool.failed')
      expect(refreshedEvents.map(event => event.event_type)).toContain('run.completed')
      expect(JSON.stringify(refreshedEvents)).toContain('capability_denied')
      expect((await getLocalRunDiagnostics(refreshedRun.id, config)).artifacts).toHaveLength(0)
    } finally {
      await removeLocalPluginCommand(
        `cmd_e2e_plugin_freeze_remove_${suffix}`,
        pluginID,
        activeDigest,
        config,
      ).catch(() => undefined)
      rmSync(temp, { recursive: true, force: true })
    }
  })
})

function pluginWireToolName(canonicalName: string): string {
  const stem = canonicalName.replace(/[^a-zA-Z0-9_-]+/g, '_').replace(/^_+|_+$/g, '')
  return `${stem.slice(0, 55)}_${createHash('sha256').update(canonicalName).digest('hex').slice(0, 8)}`
}

function createPluginFixtures(
  runtimeRoot: string,
  fixtureRoot: string,
  packagePath: string,
  deniedPackagePath: string,
  trappedPackagePath: string,
  invalidOutputPackagePath: string,
  inputPath: string,
  trappedInputPath: string,
): void {
  const script = `
import json
import sys
import zipfile
from pathlib import Path

fixture = Path(sys.argv[1])
package = Path(sys.argv[2])
denied_package = Path(sys.argv[3])
trapped_package = Path(sys.argv[4])
invalid_output_package = Path(sys.argv[5])
input_path = Path(sys.argv[6])
trapped_input_path = Path(sys.argv[7])
with zipfile.ZipFile(package, "w", zipfile.ZIP_DEFLATED) as archive:
    for path in fixture.rglob("*"):
        if path.is_file():
            archive.write(path, path.relative_to(fixture).as_posix())
with zipfile.ZipFile(denied_package, "w", zipfile.ZIP_DEFLATED) as archive:
    for path in fixture.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(fixture).as_posix()
        if relative == ".shejane-plugin/plugin.json":
            manifest = json.loads(path.read_text(encoding="utf-8"))
            manifest["id"] = "dev.shejane.fixture.denied"
            manifest["contributions"]["actions"][0]["capabilities"].append("network.http")
            archive.writestr(relative, json.dumps(manifest, separators=(",", ":")))
        else:
            archive.write(path, relative)
with zipfile.ZipFile(trapped_package, "w", zipfile.ZIP_DEFLATED) as archive:
    for path in fixture.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(fixture).as_posix()
        if relative == ".shejane-plugin/plugin.json":
            manifest = json.loads(path.read_text(encoding="utf-8"))
            manifest["id"] = "dev.shejane.fixture.fuel-trap"
            manifest["contributions"]["actions"][0]["limits"]["timeout_ms"] = 100
            archive.writestr(relative, json.dumps(manifest, separators=(",", ":")))
        else:
            archive.write(path, relative)
with zipfile.ZipFile(invalid_output_package, "w", zipfile.ZIP_DEFLATED) as archive:
    for path in fixture.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(fixture).as_posix()
        if relative == ".shejane-plugin/plugin.json":
            manifest = json.loads(path.read_text(encoding="utf-8"))
            manifest["id"] = "dev.shejane.fixture.invalid-output"
            archive.writestr(relative, json.dumps(manifest, separators=(",", ":")))
        elif relative == "actions/archive.extract.output.json":
            schema = json.loads(path.read_text(encoding="utf-8"))
            schema["properties"]["file_count"]["type"] = "string"
            archive.writestr(relative, json.dumps(schema, separators=(",", ":")))
        else:
            archive.write(path, relative)
with zipfile.ZipFile(input_path, "w", zipfile.ZIP_DEFLATED) as archive:
    archive.writestr("inside.txt", "real graph plugin tool\\n")
with zipfile.ZipFile(trapped_input_path, "w", zipfile.ZIP_DEFLATED) as archive:
    archive.writestr("large.bin", b"0" * (7 * 1024 * 1024))
`
  execFileSync('uv', [
    'run',
    'python',
    '-c',
    script,
    fixtureRoot,
    packagePath,
    deniedPackagePath,
    trappedPackagePath,
    invalidOutputPackagePath,
    inputPath,
    trappedInputPath,
  ], {
    cwd: runtimeRoot,
  })
}

function createPluginFreezeFixtures(
  runtimeRoot: string,
  fixtureRoot: string,
  packagePath: string,
  updatedPackagePath: string,
  inputPath: string,
): void {
  const script = `
import json
import sys
import zipfile
from pathlib import Path

fixture = Path(sys.argv[1])
package = Path(sys.argv[2])
updated_package = Path(sys.argv[3])
input_path = Path(sys.argv[4])
with zipfile.ZipFile(package, "w", zipfile.ZIP_DEFLATED) as archive:
    for path in fixture.rglob("*"):
        if path.is_file():
            archive.write(path, path.relative_to(fixture).as_posix())
with zipfile.ZipFile(updated_package, "w", zipfile.ZIP_DEFLATED) as archive:
    for path in fixture.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(fixture).as_posix()
        if relative == ".shejane-plugin/plugin.json":
            manifest = json.loads(path.read_text(encoding="utf-8"))
            manifest["version"] = "0.2.0"
            manifest["contributions"]["actions"][0]["capabilities"].append("network.http")
            archive.writestr(relative, json.dumps(manifest, separators=(",", ":")))
        else:
            archive.write(path, relative)
with zipfile.ZipFile(input_path, "w", zipfile.ZIP_DEFLATED) as archive:
    archive.writestr("inside.txt", "frozen plugin version\\n")
`
  execFileSync('uv', [
    'run',
    'python',
    '-c',
    script,
    fixtureRoot,
    packagePath,
    updatedPackagePath,
    inputPath,
  ], {
    cwd: runtimeRoot,
  })
}
