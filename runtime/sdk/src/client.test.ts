import { describe, expect, it, vi } from 'vitest'

import {
  createLocalRun,
  deliverPendingRuntimeCommands,
  fetchRunInput,
  getLocalArtifactContent,
  getCentralDiagnostics,
  importModelService,
  parseAgentSSEBuffer,
  parseRuntimeModelSpec,
  RuntimeHTTPError,
  SheJaneRuntimeClient,
  getSheJaneAuthorization,
  listModelServicePresets,
  listModelCapabilityBindings,
  reconnectModelService,
  startSheJaneAuthorization,
  streamLocalRun,
  updateRuntimeSettings,
  updateCentralDiagnostics,
  verifyModelServiceModel,
} from './index'

describe('fetchRunInput', () => {
  it('downloads immutable Runtime-owned input bytes with encoded identifiers', async () => {
    const fetcher = vi.fn().mockResolvedValue(
      new Response(new TextEncoder().encode('snapshot body'), {
        status: 200,
        headers: { 'Content-Type': 'text/plain', 'Content-Length': '13' },
      }),
    )

    const bytes = await fetchRunInput(
      'run/id',
      'attachment 1',
      { baseURL: 'http://127.0.0.1:17371/', token: 'runtime-token' },
      fetcher,
    )

    expect(new TextDecoder().decode(bytes)).toBe('snapshot body')
    expect(fetcher).toHaveBeenCalledWith(
      'http://127.0.0.1:17371/v1/runs/run%2Fid/inputs/attachment%201',
      {
        method: 'GET',
        headers: { Authorization: 'Bearer runtime-token' },
      },
    )
  })

  it('rejects an oversized response before buffering it', async () => {
    const response = new Response('small placeholder', {
      status: 200,
      headers: { 'Content-Length': String(21 * 1024 * 1024) },
    })
    const arrayBuffer = vi.spyOn(response, 'arrayBuffer')
    const fetcher = vi.fn().mockResolvedValue(response)

    await expect(fetchRunInput(
      'run_1',
      'source',
      { baseURL: 'http://127.0.0.1:17371', token: 'runtime-token' },
      fetcher,
      20 * 1024 * 1024,
    )).rejects.toThrow(/too large/i)
    expect(arrayBuffer).not.toHaveBeenCalled()
  })
})

describe('getLocalArtifactContent', () => {
  it('downloads the authenticated artifact body without JSON decoding', async () => {
    const fetcher = vi.fn().mockResolvedValue(
      new Response(new Uint8Array([0, 1, 2, 255]), {
        status: 200,
        headers: { 'Content-Type': 'application/octet-stream' },
      }),
    )

    const body = await getLocalArtifactContent(
      'artifact/blob id',
      { baseURL: 'http://127.0.0.1:17371/', token: 'runtime-token' },
      fetcher,
    )

    expect(new Uint8Array(await body.arrayBuffer())).toEqual(new Uint8Array([0, 1, 2, 255]))
    expect(fetcher).toHaveBeenCalledWith(
      'http://127.0.0.1:17371/v1/artifacts/artifact%2Fblob%20id/content',
      {
        method: 'GET',
        headers: { Authorization: 'Bearer runtime-token' },
      },
    )
  })
})

describe('createLocalRun plugin selection', () => {
  it('serializes explicit references and one plugin command', async () => {
    const fetcher = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({
        id: 'run_plugin',
        inputs: [{
          client_index: 0,
          input_id: 'source',
          virtual_path: '/attachments/brief.txt',
          original_name: 'brief.txt',
          media_type: 'text/plain',
          bytes: 5,
          sha256: 'a'.repeat(64),
        }],
      }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    )

    const run = await createLocalRun(
      {
        commandId: 'cmd_plugin_run',
        clientMessageId: 'msg_plugin_run',
        goal: 'use plugin',
        mode: 'local:test:model',
        requiredTools: ['image.generate'],
        pluginRefs: [
          {
            pluginId: 'dev.shejane.fixture.archive',
            expectedDigest: `sha256:${'a'.repeat(64)}`,
          },
        ],
        pluginCommand: {
          pluginId: 'dev.shejane.fixture.archive',
          commandId: 'archive.extract',
        },
      },
      { baseURL: 'http://127.0.0.1:17371', token: 'runtime-token' },
      fetcher,
    )

    const request = fetcher.mock.calls[0][1] as RequestInit
    const body = JSON.parse(String(request.body))
    expect(body.goal).toBe('use plugin')
    expect(body.user_input).toBeUndefined()
    expect(body.required_capabilities).toContain('plugins')
    expect(body.required_tools).toEqual(['image.generate'])
    expect(body.plugin_refs).toEqual([
      {
        plugin_id: 'dev.shejane.fixture.archive',
        required: true,
        expected_digest: `sha256:${'a'.repeat(64)}`,
      },
    ])
    expect(run.inputs?.[0]).toMatchObject({ input_id: 'source', original_name: 'brief.txt' })
    expect(body.plugin_command).toEqual({
      plugin_id: 'dev.shejane.fixture.archive',
      command_id: 'archive.extract',
    })
  })
})

describe('plugin command outbox delivery', () => {
  it('replays plugin commands in order through the shared Runtime command endpoint', async () => {
    const pluginId = 'dev.shejane.fixture.archive'
    const digest = `sha256:${'a'.repeat(64)}`
    const fetcher = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            type: 'plugin.install',
            command_id: 'cmd-install',
            plugin_id: pluginId,
            version: '0.1.0',
            digest,
            installed: true,
            enabled: false,
          }),
          { status: 200 },
        ),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            type: 'plugin.enable',
            command_id: 'cmd-enable',
            plugin_id: pluginId,
            digest,
            enabled: true,
          }),
          { status: 200 },
        ),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            type: 'plugin.model.bind',
            command_id: 'cmd-bind',
            plugin_id: pluginId,
            digest,
            model_binding_revision: 1,
            model_binding: {
              id: 'vision-default',
              requested_model: 'local:vision:vision-a',
              connection_id: 'vision',
              connection_version: 1,
              model_id: 'vision-a',
            },
          }),
          { status: 200 },
        ),
      )
    const settle = vi.fn().mockResolvedValue(undefined)
    const report = await deliverPendingRuntimeCommands(
      [
        {
          type: 'plugin.install',
          commandId: 'cmd-install',
          createdAt: '2026-07-16T00:00:00Z',
          input: { sourcePath: '/tmp/archive.shejane-plugin', allowUnsigned: true },
        },
        {
          type: 'plugin.enable',
          commandId: 'cmd-enable',
          createdAt: '2026-07-16T00:00:01Z',
          input: { pluginId, expectedDigest: digest },
        },
        {
          type: 'plugin.model.bind',
          commandId: 'cmd-bind',
          createdAt: '2026-07-16T00:00:02Z',
          input: {
            pluginId,
            bindingId: 'vision-default',
            model: 'local:vision:vision-a',
            expectedDigest: digest,
          },
        },
      ],
      { baseURL: 'http://127.0.0.1:17371', token: 'runtime-token' },
      settle,
      fetcher,
    )

    expect(report).toEqual({ delivered: 3, failures: [] })
    expect(settle).toHaveBeenCalledTimes(3)
    expect(JSON.parse(String(fetcher.mock.calls[0][1]?.body))).toMatchObject({
      type: 'plugin.install',
      command_id: 'cmd-install',
    })
    expect(JSON.parse(String(fetcher.mock.calls[1][1]?.body))).toMatchObject({
      type: 'plugin.enable',
      command_id: 'cmd-enable',
      plugin_id: pluginId,
    })
    expect(JSON.parse(String(fetcher.mock.calls[2][1]?.body))).toEqual({
      type: 'plugin.model.bind',
      command_id: 'cmd-bind',
      plugin_id: pluginId,
      binding_id: 'vision-default',
      model: 'local:vision:vision-a',
      expected_digest: digest,
    })
  })

})

describe('parseRuntimeModelSpec', () => {
  it('accepts only concrete Runtime model identifiers', () => {
    expect(parseRuntimeModelSpec(' local:openai:gpt-4.1 ')).toBe('local:openai:gpt-4.1')
    expect(parseRuntimeModelSpec('auto')).toBeUndefined()
    expect(parseRuntimeModelSpec('local::gpt-4.1')).toBeUndefined()
    expect(parseRuntimeModelSpec('local:open ai:gpt-4.1')).toBeUndefined()
    expect(parseRuntimeModelSpec('local:openai:gpt 4.1')).toBeUndefined()
    expect(parseRuntimeModelSpec(`local:conn_${'a'.repeat(32)}:${'m'.repeat(200)}`)).toBeDefined()
  })
})

describe('SheJaneRuntimeClient', () => {
  it('normalizes the Runtime URL and applies caller-provided authentication', async () => {
    const fetcher = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ protocol_version: 1, capabilities: ['agent.run'] }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    )
    const client = new SheJaneRuntimeClient({
      baseURL: 'http://127.0.0.1:17371/',
      token: 'runtime-token',
      fetcher,
    })

    await client.getRuntimeInfo()

    expect(fetcher).toHaveBeenCalledWith(
      'http://127.0.0.1:17371/v1/runtime',
      expect.objectContaining({ headers: { Authorization: 'Bearer runtime-token' } }),
    )
  })

  it('lists Runtime-owned model service presets without transport details', async () => {
    const fetcher = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({
        services: [{
          id: 'deepseek',
          name: 'DeepSeek',
          description: '推理和通用任务',
          api_key_url: 'https://platform.deepseek.com/api_keys',
          billing_url: 'https://platform.deepseek.com/usage',
          regions: [{ id: 'cn', name: '中国站', default: true }],
        }],
      }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    )

    const services = await listModelServicePresets(
      { baseURL: 'http://127.0.0.1:17371', token: 'runtime-token' },
      fetcher,
    )

    expect(services[0]).toMatchObject({ id: 'deepseek', regions: [{ id: 'cn' }] })
    expect(fetcher).toHaveBeenCalledWith(
      'http://127.0.0.1:17371/v1/model-services/presets',
      expect.objectContaining({
        headers: expect.objectContaining({ Authorization: 'Bearer runtime-token' }),
      }),
    )
  })

  it('replaces a model service API key without exposing it in the URL', async () => {
    const fetcher = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ id: 'conn_1' }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    )

    await reconnectModelService(
      'conn/1',
      { api_key: 'new-secret' },
      { baseURL: 'http://127.0.0.1:17371', token: 'runtime-token' },
      fetcher,
    )

    expect(fetcher).toHaveBeenCalledWith(
      'http://127.0.0.1:17371/v1/model-services/conn%2F1/credential',
      expect.objectContaining({
        method: 'PUT',
        body: JSON.stringify({ api_key: 'new-secret' }),
      }),
    )
  })

  it('starts and polls native authorization without sending Cloud configuration', async () => {
    const fetcher = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({
        authorization_id: `auth_${'a'.repeat(32)}`,
        authorization_url: 'https://cloud.example.test/shejane/authorize?...',
        expires_at: '2026-07-29T00:00:00Z',
      }), { status: 201, headers: { 'Content-Type': 'application/json' } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        authorization_id: `auth_${'a'.repeat(32)}`,
        status: 'pending',
        connection: null,
        error_code: null,
      }), { status: 200, headers: { 'Content-Type': 'application/json' } }))
    const config = { baseURL: 'http://127.0.0.1:17371', token: 'runtime-token' }

    await startSheJaneAuthorization(config, fetcher)
    await getSheJaneAuthorization(`auth_${'a'.repeat(32)}`, config, fetcher)

    expect(fetcher).toHaveBeenNthCalledWith(
      1,
      'http://127.0.0.1:17371/v1/model-services/shejane/authorization',
      {
        method: 'POST',
        headers: { Authorization: 'Bearer runtime-token' },
      },
    )
    expect(fetcher).toHaveBeenNthCalledWith(
      2,
      `http://127.0.0.1:17371/v1/model-services/shejane/authorization/auth_${'a'.repeat(32)}`,
      {
        headers: { Authorization: 'Bearer runtime-token' },
      },
    )
  })

  it('reads and updates diagnostics consent without exposing a diagnostics token', async () => {
    const status = {
      enabled: false,
      connection_id: null,
      success_sample_rate: 0,
      credential_configured: false,
    }
    const enabled = {
      ...status,
      enabled: true,
      connection_id: `conn_${'a'.repeat(32)}`,
      credential_configured: true,
    }
    const fetcher = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify(status), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify(enabled), { status: 200 }))
    const config = { baseURL: 'http://127.0.0.1:17371', token: 'runtime-token' }

    await getCentralDiagnostics(config, fetcher)
    const updated = await updateCentralDiagnostics({
      enabled: true,
      connection_id: enabled.connection_id,
      success_sample_rate: 0,
    }, config, fetcher)

    expect(updated).toEqual(enabled)
    expect(fetcher).toHaveBeenNthCalledWith(
      1,
      'http://127.0.0.1:17371/v1/shejane/diagnostics',
      { headers: { Authorization: 'Bearer runtime-token' } },
    )
    expect(fetcher).toHaveBeenNthCalledWith(
      2,
      'http://127.0.0.1:17371/v1/shejane/diagnostics',
      expect.objectContaining({
        method: 'PUT',
        body: JSON.stringify({
          enabled: true,
          connection_id: enabled.connection_id,
          success_sample_rate: 0,
        }),
      }),
    )
    expect(JSON.stringify(updated)).not.toContain('st-')
  })

  it('imports model-service metadata without an API key', async () => {
    const input = {
      id: `conn_${'a'.repeat(32)}`,
      preset_id: 'deepseek',
      name: 'DeepSeek',
      region: 'cn' as const,
      adapter_id: 'openai_chat' as const,
      base_url: 'https://api.deepseek.com/v1',
      models: [],
    }
    const fetcher = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(input), {
        status: 201,
        headers: { 'Content-Type': 'application/json' },
      }),
    )

    await importModelService(
      input,
      { baseURL: 'http://127.0.0.1:17371', token: 'runtime-token' },
      fetcher,
    )

    expect(fetcher).toHaveBeenCalledWith(
      'http://127.0.0.1:17371/v1/model-services/import',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify(input),
      }),
    )
  })

  it('verifies the selected model capability and protocol', async () => {
    const fetcher = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ model_id: 'gateway-model' }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    )

    await verifyModelServiceModel(
      'conn/1',
      'gateway/model',
      { capability: 'image_generation', protocol: 'openai_images_generations' },
      { baseURL: 'http://127.0.0.1:17371', token: 'runtime-token' },
      fetcher,
    )

    expect(fetcher).toHaveBeenCalledWith(
      'http://127.0.0.1:17371/v1/model-services/conn%2F1/models/gateway%2Fmodel/verify',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({
          capability: 'image_generation',
          protocol: 'openai_images_generations',
        }),
      }),
    )
  })

  it('lists model capability bindings', async () => {
    const fetcher = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ bindings: [{ capability: 'image_generation' }] }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    )

    const bindings = await listModelCapabilityBindings(
      { baseURL: 'http://127.0.0.1:17371', token: 'runtime-token' },
      fetcher,
    )

    expect(bindings).toHaveLength(1)
    expect(fetcher).toHaveBeenCalledWith(
      'http://127.0.0.1:17371/v1/model-capability-bindings',
      { method: 'GET', headers: { Authorization: 'Bearer runtime-token' } },
    )
  })

  it('keeps the legacy model verification call signature', async () => {
    const fetcher = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ model_id: 'gateway-model' }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    )

    await verifyModelServiceModel(
      'conn-1',
      'gateway-model',
      { baseURL: 'http://127.0.0.1:17371', token: 'runtime-token' },
      fetcher,
    )

    expect(fetcher).toHaveBeenCalledWith(
      'http://127.0.0.1:17371/v1/model-services/conn-1/models/gateway-model/verify',
      {
        method: 'POST',
        headers: { Authorization: 'Bearer runtime-token' },
      },
    )
  })

  it('lists plugins and installs one through the Runtime command endpoint', async () => {
    const plugin = {
      id: 'dev.shejane.fixture.archive',
      name: 'Archive fixture',
      version: '0.1.0',
      digest: `sha256:${'a'.repeat(64)}`,
      publisher: { id: 'dev.shejane', name: 'SheJane' },
      execution_kind: 'wasi',
      signature_status: 'unsigned',
      compatibility: 'compatible',
      enabled: false,
      retired: false,
    }
    const receipt = {
      type: 'plugin.install',
      command_id: 'cmd-1',
      plugin_id: plugin.id,
      version: plugin.version,
      digest: plugin.digest,
      installed: true,
      enabled: false,
    }
    const fetcher = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ plugins: [plugin] }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify(receipt), { status: 200 }))
    const client = new SheJaneRuntimeClient({
      baseURL: 'http://127.0.0.1:17371',
      token: 'runtime-token',
      fetcher,
    })

    await expect(client.listPlugins()).resolves.toEqual([plugin])
    await expect(client.installPlugin('cmd-1', '/tmp/archive.shejane-plugin', {
      allowUnsigned: true,
    })).resolves.toEqual(receipt)
    expect(fetcher).toHaveBeenNthCalledWith(2, 'http://127.0.0.1:17371/v1/commands', {
      method: 'POST',
      headers: {
        Authorization: 'Bearer runtime-token',
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        type: 'plugin.install',
        command_id: 'cmd-1',
        source_path: '/tmp/archive.shejane-plugin',
        allow_unsigned: true,
      }),
    })
  })

  it('installs an exact shared runtime asset through the command endpoint', async () => {
    const digest = `sha256:${'b'.repeat(64)}`
    const receipt = {
      type: 'plugin.runtime_asset.install',
      command_id: 'cmd-asset-1',
      asset_id: 'org.libreoffice.runtime',
      version: '25.8.7',
      platform: 'darwin/arm64',
      digest,
      installed: true,
    }
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify(receipt), { status: 200 }))
    const client = new SheJaneRuntimeClient({
      baseURL: 'http://127.0.0.1:17371',
      token: 'runtime-token',
      fetcher,
    })

    await expect(client.installRuntimeAsset(
      'cmd-asset-1',
      '/tmp/libreoffice.shejane-runtime-asset',
      digest,
    )).resolves.toEqual(receipt)
    expect(JSON.parse(String(fetcher.mock.calls[0][1]?.body))).toEqual({
      type: 'plugin.runtime_asset.install',
      command_id: 'cmd-asset-1',
      source_path: '/tmp/libreoffice.shejane-runtime-asset',
      expected_digest: digest,
    })
  })

  it('reads and prepares a fixed Runtime Asset without exposing its URL or digest', async () => {
    const missing = { plugin_id: 'org.shejane.browser-qa', downloaded: false }
    const downloaded = { ...missing, downloaded: true }
    const fetcher = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify(missing), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify(downloaded), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify(missing), { status: 200 }))
    const client = new SheJaneRuntimeClient({
      baseURL: 'http://127.0.0.1:17371',
      token: 'runtime-token',
      fetcher,
    })

    await expect(client.getFixedRuntimeAssetStatus(
      'org.shejane.browser-qa',
    )).resolves.toEqual(missing)
    await expect(client.prepareFixedRuntimeAsset(
      'org.shejane.browser-qa',
    )).resolves.toEqual(downloaded)
    await expect(client.removeFixedRuntimeAsset(
      'org.shejane.browser-qa',
    )).resolves.toEqual(missing)
    expect(fetcher).toHaveBeenNthCalledWith(1,
      'http://127.0.0.1:17371/v1/plugins/org.shejane.browser-qa/runtime-asset',
      {
        method: 'GET',
        headers: { Authorization: 'Bearer runtime-token' },
      })
    expect(fetcher).toHaveBeenNthCalledWith(2,
      'http://127.0.0.1:17371/v1/plugins/org.shejane.browser-qa/runtime-asset',
      {
        method: 'PUT',
        headers: { Authorization: 'Bearer runtime-token' },
      })
    expect(fetcher).toHaveBeenNthCalledWith(3,
      'http://127.0.0.1:17371/v1/plugins/org.shejane.browser-qa/runtime-asset',
      {
        method: 'DELETE',
        headers: { Authorization: 'Bearer runtime-token' },
      })
  })

  it('inspects and cleans Runtime Asset storage by explicit scope', async () => {
    const storage = {
      total_bytes: 1_500_000_000,
      history_bytes: 900_000_000,
      asset_count: 4,
      history_asset_count: 2,
    }
    const cleaned = { ...storage, history_bytes: 0, history_asset_count: 0, freed_bytes: 900_000_000 }
    const fetcher = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify(storage), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify(cleaned), { status: 200 }))
    const client = new SheJaneRuntimeClient({
      baseURL: 'http://127.0.0.1:17371',
      token: 'runtime-token',
      fetcher,
    })

    await expect(client.getRuntimeAssetStorage()).resolves.toEqual(storage)
    await expect(client.cleanupRuntimeAssetStorage('history')).resolves.toEqual(cleaned)
    expect(fetcher).toHaveBeenNthCalledWith(1,
      'http://127.0.0.1:17371/v1/plugins/runtime-assets/storage',
      { method: 'GET', headers: { Authorization: 'Bearer runtime-token' } })
    expect(fetcher).toHaveBeenNthCalledWith(2,
      'http://127.0.0.1:17371/v1/plugins/runtime-assets/storage?scope=history',
      { method: 'DELETE', headers: { Authorization: 'Bearer runtime-token' } })
  })

  it('reads and advances the fixed Computer Use setup flow', async () => {
    const pluginId = 'org.shejane.computer-use' as const
    const readiness = {
      state: 'action_required' as const,
      revision: 4,
      step: 'screen_recording' as const,
      action_id: 'request_screen_recording' as const,
      can_recheck: false,
    }
    const receipt = {
      type: 'plugin.setup.advance' as const,
      command_id: 'cmd-setup',
      plugin_id: pluginId,
      readiness: { ...readiness, state: 'awaiting_user' as const, revision: 5 },
    }
    const fetcher = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify(readiness), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify(receipt), { status: 200 }))
    const client = new SheJaneRuntimeClient({
      baseURL: 'http://127.0.0.1:17371',
      token: 'runtime-token',
      fetcher,
    })

    await expect(client.getPluginReadiness(pluginId)).resolves.toEqual(readiness)
    await expect(client.advancePluginSetup(
      'cmd-setup',
      pluginId,
      readiness.revision,
      readiness.action_id,
    )).resolves.toEqual(receipt)
    expect(fetcher).toHaveBeenNthCalledWith(
      1,
      `http://127.0.0.1:17371/v1/plugins/${pluginId}/readiness`,
      { method: 'GET', headers: { Authorization: 'Bearer runtime-token' } },
    )
    expect(JSON.parse(String(fetcher.mock.calls[1][1]?.body))).toEqual({
      type: 'plugin.setup.advance',
      command_id: 'cmd-setup',
      plugin_id: pluginId,
      expected_revision: 4,
      action_id: 'request_screen_recording',
    })
  })

  it('parses durable events and the terminal sentinel across one buffer', () => {
    const parsed = parseAgentSSEBuffer(
      'data: {"event_type":"run.completed","run_id":"run-1","seq":4}\n\ndata: [DONE]\n\n',
    )

    expect(parsed.rest).toBe('')
    expect(parsed.events).toEqual([
      {
        type: 'agent',
        event: { event_type: 'run.completed', run_id: 'run-1', seq: 4 },
      },
      { type: 'done' },
    ])
  })

  it('rejects JSON events that do not satisfy the Runtime envelope', () => {
    expect(() => parseAgentSSEBuffer('data: {"payload":{"content":"lost"}}\n\n'))
      .toThrow(/event_type/)
  })
})

describe('streamLocalRun', () => {
  it('preserves Runtime status and error code when the SSE handshake fails', async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      detail: { code: 'run_not_found', message: 'run does not exist' },
    }), {
      status: 404,
      headers: { 'Content-Type': 'application/json' },
    }))

    await expect(streamLocalRun(
      'run-missing',
      { baseURL: 'http://127.0.0.1:17371', token: 'runtime-token' },
      { onEvent: () => undefined, onDelta: () => undefined },
      fetcher,
    )).rejects.toMatchObject({
      name: RuntimeHTTPError.name,
      status: 404,
      code: 'run_not_found',
      message: 'run does not exist',
    })
  })
})

describe('Runtime validation errors', () => {
  it('preserves sanitized FastAPI field errors without exposing rejected input', async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      detail: [{ loc: ['body', 'memory'], msg: 'Input should be on or off', type: 'literal_error' }],
    }), {
      status: 422,
      headers: { 'Content-Type': 'application/json' },
    }))

    await expect(updateRuntimeSettings(
      { memory: 'off' },
      { baseURL: 'http://127.0.0.1:17371', token: 'runtime-token' },
      fetcher,
    )).rejects.toMatchObject({
      name: RuntimeHTTPError.name,
      status: 422,
      message: 'body.memory: Input should be on or off',
    })
  })
})
