import { describe, expect, it, vi } from 'vitest'
import {
  answerLocalQuestionCommand,
  createLocalRun,
  cancelLocalRunCommand,
  deliverPendingRuntimeCommands,
  authorizeLocalWorkspace,
  diagnoseLocalWorkspace,
  getLocalRunDiagnostics,
  getLocalRuntimeInfo,
  connectModelService,
  listModelServices,
  getRuntimeConnection,
  getLocalArtifact,
  listAuthorizedWorkspaces,
  listInstalledSkills,
  createMcpServer,
  deleteMcpServer,
  createLocalSkill,
  getLocalSkillFile,
  updateLocalSkill,
  deleteLocalSkill,
  cancelLocalSchedule,
  createLocalSchedule,
  listLocalRuns,
  listLocalThreads,
  getLocalThreadSnapshot,
  listLocalThreadChanges,
  updateLocalThread,
  deleteLocalThread,
  listLocalSchedules,
  markLocalScheduleNotified,
  probeRuntime,
  revokeLocalWorkspace,
  resolveLocalPermissionCommand,
  forkLocalRun,
  LocalStreamCursorResetRequiredError,
  streamLocalRun,
  injectLocalRunInstruction,
  resolveLocalPlanCommand,
  reconcileLocalToolCommand,
} from './client'

const TEST_COMMAND = { commandId: 'cmd_client_test', clientMessageId: 'msg_client_test', mode: 'local:test:model' } as const

describe('Client Runtime adapter', () => {
  it('submits permission decisions through the immutable Runtime command endpoint', async () => {
    const fetcher = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          type: 'permission.resolve',
          command_id: 'resolve-permission-1',
          permission_id: 'permission-1',
          run_id: 'run-1',
          resolved: true,
          decision: 'edit',
          scope: 'once',
          resumed: true,
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    )

    await expect(
      resolveLocalPermissionCommand(
        'resolve-permission-1',
        'permission-1',
        'edit',
        { scope: 'once', editedAction: { name: 'execute', args: { command: 'make test' } } },
        { baseURL: 'http://127.0.0.1:17371', token: 'local-token' },
        fetcher,
      ),
    ).resolves.toMatchObject({ type: 'permission.resolve', resolved: true, resumed: true })
    expect(fetcher).toHaveBeenCalledWith(
      'http://127.0.0.1:17371/v1/commands',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({
          type: 'permission.resolve',
          command_id: 'resolve-permission-1',
          permission_id: 'permission-1',
          decision: 'edit',
          scope: 'once',
          edited_action: { name: 'execute', args: { command: 'make test' } },
        }),
      }),
    )
  })

  it('submits question answers through the immutable Runtime command endpoint', async () => {
    const fetcher = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          type: 'question.answer',
          command_id: 'answer-question-1',
          question_id: 'question-1',
          run_id: 'run-1',
          answered: true,
          resumed: true,
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    )

    await expect(
      answerLocalQuestionCommand(
        'answer-question-1',
        'question-1',
        { choice: ['mode X'] },
        { baseURL: 'http://127.0.0.1:17371', token: 'local-token' },
        fetcher,
      ),
    ).resolves.toMatchObject({ type: 'question.answer', answered: true, resumed: true })
    expect(fetcher).toHaveBeenCalledWith(
      'http://127.0.0.1:17371/v1/commands',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({
          type: 'question.answer',
          command_id: 'answer-question-1',
          question_id: 'question-1',
          answers: { choice: ['mode X'] },
        }),
      }),
    )
  })

  it('submits cancellation through the immutable Runtime command endpoint', async () => {
    const fetcher = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          type: 'run.cancel',
          command_id: 'cmd-cancel-1',
          run_id: 'run-1',
          canceled: true,
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    )

    await expect(
      cancelLocalRunCommand(
        'cmd-cancel-1',
        'run-1',
        { baseURL: 'http://127.0.0.1:17371', token: 'local-token' },
        fetcher,
      ),
    ).resolves.toMatchObject({ type: 'run.cancel', run_id: 'run-1', canceled: true })
    expect(fetcher).toHaveBeenCalledWith(
      'http://127.0.0.1:17371/v1/commands',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({
          type: 'run.cancel',
          command_id: 'cmd-cancel-1',
          run_id: 'run-1',
        }),
      }),
    )
  })

  it('submits tool reconciliation through the immutable Runtime command endpoint', async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      type: 'tool.reconcile',
      command_id: 'reconcile-tool-1',
      operation_id: 'toolop-1',
      run_id: 'run-1',
      resolved: true,
      decision: 'retry_not_executed',
      resumed: true,
    }), { status: 200, headers: { 'Content-Type': 'application/json' } }))
    await expect(reconcileLocalToolCommand(
      'reconcile-tool-1',
      'toolop-1',
      'retry_not_executed',
      { baseURL: 'http://127.0.0.1:17371', token: 'local-token' },
      fetcher,
    )).resolves.toMatchObject({ type: 'tool.reconcile', resumed: true })
    expect(fetcher).toHaveBeenCalledWith(
      'http://127.0.0.1:17371/v1/commands',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({
          type: 'tool.reconcile',
          command_id: 'reconcile-tool-1',
          operation_id: 'toolop-1',
          decision: 'retry_not_executed',
        }),
      }),
    )
  })
  it('connects and lists Runtime model services without exposing the API key', async () => {
    const service = {
      id: 'conn_1',
      preset_id: 'deepseek',
      name: 'DeepSeek',
      region: 'cn' as const,
      adapter_id: 'openai_chat' as const,
      base_url: 'https://api.deepseek.com/v1',
      credential_configured: true,
      catalog_status: 'ready' as const,
      models: [{
        model_id: 'deepseek-v4-flash',
        display_name: 'DeepSeek V4 Flash',
        source: 'bundled' as const,
        verification: 'verified' as const,
        recommended: true,
        tool_calling: true,
        streaming: true,
        image_inputs: false,
      }],
      version: 1,
      created_at: '2026-07-12T00:00:00Z',
      updated_at: '2026-07-12T00:00:00Z',
    }
    const fetcher = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify(service), { status: 201 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ services: [service] }), { status: 200 }))
    const config = { baseURL: 'http://127.0.0.1:17371', token: 'local-token' }

    await connectModelService(
      { preset_id: 'deepseek', region: 'cn', api_key: 'secret' },
      config,
      fetcher,
    )
    await expect(listModelServices(config, fetcher)).resolves.toEqual([service])

    expect(JSON.parse(String(fetcher.mock.calls[0][1]?.body))).toEqual({
      preset_id: 'deepseek',
      region: 'cn',
      api_key: 'secret',
    })
  })
  it('discovers the authenticated runtime protocol and capabilities', async () => {
    const fetcher = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          protocol_version: 1,
          runtime_version: '0.1.3',
          capabilities: ['agent.run', 'agent.stream'],
          model_service_configured: true,
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    )

    await expect(
      getLocalRuntimeInfo(
        { baseURL: 'http://127.0.0.1:17371', token: 'local-token' },
        fetcher,
      ),
    ).resolves.toMatchObject({ protocol_version: 1 })
    expect(fetcher).toHaveBeenCalledWith(
      'http://127.0.0.1:17371/v1/runtime',
      expect.objectContaining({
        headers: expect.objectContaining({ Authorization: 'Bearer local-token' }),
      }),
    )
  })
  it('only returns Runtime connection when the Client bridge exposes one', () => {
    expect(getRuntimeConnection(undefined)).toBeUndefined()
    expect(getRuntimeConnection({ platform: 'darwin' })).toBeUndefined()
    expect(
      getRuntimeConnection({
        platform: 'darwin',
        runtime: { baseURL: 'http://127.0.0.1:17371', session: 'client' },
      }),
    ).toEqual({ baseURL: 'http://127.0.0.1:17371', session: 'client' })
  })

  it('probes public health without a pairing token', async () => {
    const fetcher = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          status: 'ok',
          mode: 'runtime',
          worker: 'user',
        }),
        { status: 200 },
      ),
    )

    await expect(probeRuntime('http://127.0.0.1:17371', fetcher)).resolves.toEqual({
      online: true,
      status: 'ok',
      mode: 'runtime',
      worker: 'user',
    })
    expect(fetcher).toHaveBeenCalledWith('http://127.0.0.1:17371/v1/health', {
      signal: expect.any(AbortSignal),
    })
  })

  it('treats failed health checks as offline', async () => {
    await expect(probeRuntime('http://127.0.0.1:17371', vi.fn().mockRejectedValue(new Error('offline')))).resolves.toEqual({
      online: false,
    })
  })

  it('creates local runs with pairing token authorization', async () => {
    const fetcher = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          id: 'run-local',
          goal: 'Inspect workspace',
          status: 'queued',
          created_at: '2026-05-11T00:00:00Z',
          updated_at: '2026-05-11T00:00:00Z',
        }),
        { status: 201, headers: { 'Content-Type': 'application/json' } },
      ),
    )

    await expect(
      createLocalRun(
        {
          ...TEST_COMMAND,
          threadId: 'conversation-1',
          goal: 'Inspect workspace',
          workspacePath: '/tmp/project',
          attachmentPaths: ['/tmp/brief.pdf'],
        },
        { baseURL: 'http://127.0.0.1:17371', token: 'local-token' },
        fetcher,
      ),
    ).resolves.toMatchObject({ id: 'run-local', status: 'queued' })
    expect(fetcher).toHaveBeenCalledWith(
      'http://127.0.0.1:17371/v1/runs',
      expect.objectContaining({
        method: 'POST',
        headers: expect.objectContaining({ Authorization: 'Bearer local-token' }),
        body: JSON.stringify({
          command_id: TEST_COMMAND.commandId,
          client_message_id: TEST_COMMAND.clientMessageId,
          thread_id: 'conversation-1',
          protocol_version: 1,
          required_capabilities: ['agent.run', 'agent.stream', 'attachments', 'hitl', 'mcp', 'memory', 'skills', 'subagents', 'workspace.files'],
          goal: 'Inspect workspace',
          workspace_path: '/tmp/project',
          attachment_paths: ['/tmp/brief.pdf'],
          history: [],
          model: 'local:test:model',
        }),
      }),
    )
  })

  it('retries a transport failure with the same immutable command ids', async () => {
    const fetcher = vi.fn()
      .mockRejectedValueOnce(new TypeError('connection reset'))
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            id: 'run-replayed',
            goal: 'Inspect workspace',
            status: 'queued',
            created_at: '2026-05-11T00:00:00Z',
            updated_at: '2026-05-11T00:00:00Z',
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        ),
      )

    await createLocalRun(
      { commandId: 'cmd_client_1', clientMessageId: 'msg_client_1', goal: 'Inspect workspace', mode: 'local:test:model' },
      { baseURL: 'http://127.0.0.1:17371', token: 'local-token' },
      fetcher,
    )

    expect(fetcher).toHaveBeenCalledTimes(2)
    const firstBody = (fetcher.mock.calls[0]?.[1] as RequestInit).body
    const secondBody = (fetcher.mock.calls[1]?.[1] as RequestInit).body
    expect(secondBody).toBe(firstBody)
    expect(JSON.parse(String(firstBody))).toMatchObject({
      command_id: 'cmd_client_1',
      client_message_id: 'msg_client_1',
    })
  })

  it('redelivers a persisted command with its stable id and acknowledges only success', async () => {
    const fetcher = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          id: 'run-replayed-after-restart',
          goal: 'continue after restart',
          status: 'queued',
          created_at: '2026-05-11T00:00:00Z',
          updated_at: '2026-05-11T00:00:00Z',
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    )
    const acknowledge = vi.fn().mockResolvedValue(undefined)

    await expect(
      deliverPendingRuntimeCommands(
        [{
          type: 'run.start',
          commandId: 'cmd-restart',
          createdAt: '2026-05-10T00:00:00.000Z',
          input: {
            commandId: 'cmd-restart',
            clientMessageId: 'msg-restart',
            threadId: 'conv-restart',
            goal: 'continue after restart',
            mode: 'local:test:model',
          },
        }],
        { baseURL: 'http://127.0.0.1:17371', token: 'local-token' },
        acknowledge,
        fetcher,
      ),
    ).resolves.toMatchObject({ delivered: 1, failures: [] })

    expect(JSON.parse(String((fetcher.mock.calls[0]?.[1] as RequestInit).body))).toMatchObject({
      command_id: 'cmd-restart',
      client_message_id: 'msg-restart',
    })
    expect(acknowledge).toHaveBeenCalledWith(
      expect.objectContaining({ commandId: 'cmd-restart' }),
      expect.objectContaining({ id: 'run-replayed-after-restart' }),
    )
  })

  it('redelivers a persisted checkpoint fork with its original source and checkpoint', async () => {
    const fetcher = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          id: 'run-fork-replayed',
          goal: 'Continue from checkpoint',
          status: 'queued',
          parent_run_id: 'run-source',
          created_at: '2026-06-13T00:00:00Z',
          updated_at: '2026-06-13T00:00:00Z',
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    )
    const acknowledge = vi.fn().mockResolvedValue(undefined)

    await expect(
      deliverPendingRuntimeCommands(
        [{
          type: 'run.fork',
          commandId: 'cmd-fork-restart',
          createdAt: '2026-06-12T00:00:00.000Z',
          input: {
            sourceRunId: 'run-source',
            protocolVersion: 1,
            requiredCapabilities: ['agent.run', 'agent.stream', 'hitl'],
            clientMessageId: 'msg-fork-user',
            assistantMessageId: 'msg-fork-assistant',
            threadId: 'thread-fork',
            checkpointId: 'checkpoint-1',
            goal: 'Continue from checkpoint',
            userInput: 'Continue from checkpoint',
          },
        }],
        { baseURL: 'http://127.0.0.1:17371', token: 'local-token' },
        acknowledge,
        fetcher,
      ),
    ).resolves.toMatchObject({ delivered: 1, failures: [] })

    expect(fetcher).toHaveBeenCalledWith(
      'http://127.0.0.1:17371/v1/runs/run-source/fork',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({
          command_id: 'cmd-fork-restart',
          client_message_id: 'msg-fork-user',
          assistant_message_id: 'msg-fork-assistant',
          thread_id: 'thread-fork',
          protocol_version: 1,
          required_capabilities: ['agent.run', 'agent.stream', 'hitl'],
          checkpoint_id: 'checkpoint-1',
          goal: 'Continue from checkpoint',
          user_input: 'Continue from checkpoint',
        }),
      }),
    )
    expect(acknowledge).toHaveBeenCalledWith(
      expect.objectContaining({ type: 'run.fork', commandId: 'cmd-fork-restart' }),
      expect.objectContaining({ id: 'run-fork-replayed', parent_run_id: 'run-source' }),
    )
  })

  it('redelivers a persisted cancel command through the shared command endpoint', async () => {
    const fetcher = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          type: 'run.cancel',
          command_id: 'cmd-cancel-restart',
          run_id: 'run-cancel-restart',
          canceled: true,
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    )
    const acknowledge = vi.fn().mockResolvedValue(undefined)

    await expect(
      deliverPendingRuntimeCommands(
        [{
          type: 'run.cancel',
          commandId: 'cmd-cancel-restart',
          createdAt: '2026-05-10T00:00:00.000Z',
          input: { runId: 'run-cancel-restart', threadId: 'conv-cancel-restart' },
        }],
        { baseURL: 'http://127.0.0.1:17371', token: 'local-token' },
        acknowledge,
        fetcher,
      ),
    ).resolves.toMatchObject({ delivered: 1, failures: [] })

    expect(fetcher).toHaveBeenCalledWith(
      'http://127.0.0.1:17371/v1/commands',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({
          type: 'run.cancel',
          command_id: 'cmd-cancel-restart',
          run_id: 'run-cancel-restart',
        }),
      }),
    )
    expect(acknowledge).toHaveBeenCalledWith(
      expect.objectContaining({ type: 'run.cancel', commandId: 'cmd-cancel-restart' }),
      expect.objectContaining({ type: 'run.cancel', canceled: true }),
    )
  })

  it('redelivers a persisted question answer with its original answers', async () => {
    const fetcher = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          type: 'question.answer',
          command_id: 'answer-question-restart',
          question_id: 'question-restart',
          run_id: 'run-restart',
          answered: true,
          resumed: true,
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    )
    const acknowledge = vi.fn().mockResolvedValue(undefined)

    await expect(
      deliverPendingRuntimeCommands(
        [{
          type: 'question.answer',
          commandId: 'answer-question-restart',
          createdAt: '2026-05-10T00:00:00.000Z',
          input: {
            questionId: 'question-restart',
            answers: { choice: ['mode X'] },
            runId: 'run-restart',
            threadId: 'thread-restart',
          },
        }],
        { baseURL: 'http://127.0.0.1:17371', token: 'local-token' },
        acknowledge,
        fetcher,
      ),
    ).resolves.toMatchObject({ delivered: 1, failures: [] })

    expect(JSON.parse(String((fetcher.mock.calls[0]?.[1] as RequestInit).body))).toEqual({
      type: 'question.answer',
      command_id: 'answer-question-restart',
      question_id: 'question-restart',
      answers: { choice: ['mode X'] },
    })
    expect(acknowledge).toHaveBeenCalledWith(
      expect.objectContaining({ type: 'question.answer', commandId: 'answer-question-restart' }),
      expect.objectContaining({ type: 'question.answer', resumed: true }),
    )
  })

  it('redelivers a persisted permission decision with its original scope and action', async () => {
    const fetcher = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          type: 'permission.resolve',
          command_id: 'resolve-permission-restart',
          permission_id: 'permission-restart',
          run_id: 'run-restart',
          resolved: true,
          decision: 'edit',
          scope: 'once',
          resumed: true,
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    )
    const acknowledge = vi.fn().mockResolvedValue(undefined)

    await expect(
      deliverPendingRuntimeCommands(
        [{
          type: 'permission.resolve',
          commandId: 'resolve-permission-restart',
          createdAt: '2026-05-10T00:00:00.000Z',
          input: {
            permissionId: 'permission-restart',
            decision: 'edit',
            scope: 'once',
            editedAction: { name: 'execute', args: { command: 'make test' } },
            runId: 'run-restart',
            threadId: 'thread-restart',
          },
        }],
        { baseURL: 'http://127.0.0.1:17371', token: 'local-token' },
        acknowledge,
        fetcher,
      ),
    ).resolves.toMatchObject({ delivered: 1, failures: [] })

    expect(JSON.parse(String((fetcher.mock.calls[0]?.[1] as RequestInit).body))).toEqual({
      type: 'permission.resolve',
      command_id: 'resolve-permission-restart',
      permission_id: 'permission-restart',
      decision: 'edit',
      scope: 'once',
      edited_action: { name: 'execute', args: { command: 'make test' } },
    })
    expect(acknowledge).toHaveBeenCalledWith(
      expect.objectContaining({
        type: 'permission.resolve',
        commandId: 'resolve-permission-restart',
      }),
      expect.objectContaining({ type: 'permission.resolve', resumed: true }),
    )
  })

  it('redelivers a persisted plan decision with its original instructions', async () => {
    const fetcher = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          type: 'plan.resolve',
          command_id: 'resolve-plan-restart',
          approval_id: 'plan-restart',
          run_id: 'run-restart',
          resolved: true,
          decision: 'modify',
          instructions: 'Add verification.',
          resumed: true,
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    )
    const acknowledge = vi.fn().mockResolvedValue(undefined)

    await expect(
      deliverPendingRuntimeCommands(
        [{
          type: 'plan.resolve',
          commandId: 'resolve-plan-restart',
          createdAt: '2026-05-10T00:00:00.000Z',
          input: {
            approvalId: 'plan-restart',
            decision: 'modify',
            instructions: 'Add verification.',
            runId: 'run-restart',
            threadId: 'thread-restart',
          },
        }],
        { baseURL: 'http://127.0.0.1:17371', token: 'local-token' },
        acknowledge,
        fetcher,
      ),
    ).resolves.toMatchObject({ delivered: 1, failures: [] })

    expect(JSON.parse(String((fetcher.mock.calls[0]?.[1] as RequestInit).body))).toEqual({
      type: 'plan.resolve',
      command_id: 'resolve-plan-restart',
      approval_id: 'plan-restart',
      decision: 'modify',
      instructions: 'Add verification.',
    })
    expect(acknowledge).toHaveBeenCalledWith(
      expect.objectContaining({ type: 'plan.resolve', commandId: 'resolve-plan-restart' }),
      expect.objectContaining({ type: 'plan.resolve', resumed: true }),
    )
  })

  it('redelivers a persisted tool reconciliation with its original decision', async () => {
    const fetcher = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          type: 'tool.reconcile',
          command_id: 'reconcile-tool-restart',
          operation_id: 'toolop-restart',
          run_id: 'run-restart',
          resolved: true,
          decision: 'abort',
          resumed: true,
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    )
    const acknowledge = vi.fn().mockResolvedValue(undefined)

    await expect(
      deliverPendingRuntimeCommands(
        [{
          type: 'tool.reconcile',
          commandId: 'reconcile-tool-restart',
          createdAt: '2026-05-10T00:00:00.000Z',
          input: {
            operationId: 'toolop-restart',
            decision: 'abort',
            runId: 'run-restart',
            threadId: 'thread-restart',
          },
        }],
        { baseURL: 'http://127.0.0.1:17371', token: 'local-token' },
        acknowledge,
        fetcher,
      ),
    ).resolves.toMatchObject({ delivered: 1, failures: [] })

    expect(JSON.parse(String((fetcher.mock.calls[0]?.[1] as RequestInit).body))).toEqual({
      type: 'tool.reconcile',
      command_id: 'reconcile-tool-restart',
      operation_id: 'toolop-restart',
      decision: 'abort',
    })
    expect(acknowledge).toHaveBeenCalledWith(
      expect.objectContaining({ type: 'tool.reconcile', commandId: 'reconcile-tool-restart' }),
      expect.objectContaining({ type: 'tool.reconcile', resumed: true }),
    )
  })

  it('reports a permanent rejection while continuing another thread', async () => {
    const fetcher = vi.fn().mockImplementation(async (_url: string, init?: RequestInit) => {
      const body = JSON.parse(String(init?.body ?? '{}')) as { command_id?: string }
      if (body.command_id === 'cmd-rejected') {
        return new Response(JSON.stringify({ detail: 'workspace revoked' }), {
          status: 409,
          headers: { 'Content-Type': 'application/json' },
        })
      }
      return new Response(
        JSON.stringify({
          id: 'run-other-thread',
          goal: 'other thread',
          status: 'queued',
          created_at: '2026-05-11T00:00:00Z',
          updated_at: '2026-05-11T00:00:00Z',
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      )
    })
    const acknowledge = vi.fn().mockResolvedValue(undefined)

    const report = await deliverPendingRuntimeCommands(
        [
          {
            type: 'run.start',
            commandId: 'cmd-rejected',
            createdAt: '2026-05-10T00:00:00.000Z',
            input: {
              commandId: 'cmd-rejected',
              clientMessageId: 'msg-rejected',
              threadId: 'thread-rejected',
              goal: 'rejected',
              mode: 'local:test:model',
            },
          },
          {
            type: 'run.start',
            commandId: 'cmd-other',
            createdAt: '2026-05-10T00:00:01.000Z',
            input: {
              commandId: 'cmd-other',
              clientMessageId: 'msg-other',
              threadId: 'thread-other',
              goal: 'other thread',
              mode: 'local:test:model',
            },
          },
        ],
        { baseURL: 'http://127.0.0.1:17371', token: 'local-token' },
        acknowledge,
        fetcher,
      )

    expect(report).toMatchObject({
      delivered: 1,
      failures: [{
        command: expect.objectContaining({ commandId: 'cmd-rejected' }),
        error: expect.objectContaining({ status: 409 }),
        retryable: false,
      }],
    })

    expect(acknowledge).not.toHaveBeenCalledWith(
      expect.objectContaining({ commandId: 'cmd-rejected' }),
      expect.anything(),
    )
    expect(acknowledge).toHaveBeenCalledWith(
      expect.objectContaining({ commandId: 'cmd-other' }),
      expect.objectContaining({ id: 'run-other-thread' }),
    )
  })

  it('carries per-run agent settings in the run-create payload', async () => {
    const fetcher = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          id: 'run-settings',
          goal: 'Remember things',
          status: 'queued',
          created_at: '2026-05-16T00:00:00Z',
          updated_at: '2026-05-16T00:00:00Z',
        }),
        { status: 201, headers: { 'Content-Type': 'application/json' } },
      ),
    )

    await createLocalRun(
      { ...TEST_COMMAND, goal: 'Remember things', settings: { memory: 'on' } },
      { baseURL: 'http://127.0.0.1:17371', token: 'local-token' },
      fetcher,
    )

    expect(fetcher).toHaveBeenCalledWith(
      'http://127.0.0.1:17371/v1/runs',
      expect.objectContaining({
        body: JSON.stringify({
          command_id: TEST_COMMAND.commandId,
          client_message_id: TEST_COMMAND.clientMessageId,
          protocol_version: 1,
          required_capabilities: ['agent.run', 'agent.stream', 'hitl', 'mcp', 'memory', 'skills', 'subagents'],
          goal: 'Remember things',
          history: [],
          settings: { memory: 'on' },
          model: 'local:test:model',
        }),
      }),
    )
  })

  it('carries the skills agent setting in the run-create payload', async () => {
    const fetcher = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          id: 'run-skills',
          goal: 'Use a skill',
          status: 'queued',
          created_at: '2026-05-16T00:00:00Z',
          updated_at: '2026-05-16T00:00:00Z',
        }),
        { status: 201, headers: { 'Content-Type': 'application/json' } },
      ),
    )

    await createLocalRun(
      { ...TEST_COMMAND, goal: 'Use a skill', settings: { memory: 'off', skills: 'on' } },
      { baseURL: 'http://127.0.0.1:17371', token: 'local-token' },
      fetcher,
    )

    expect(fetcher).toHaveBeenCalledWith(
      'http://127.0.0.1:17371/v1/runs',
      expect.objectContaining({
        body: JSON.stringify({
          command_id: TEST_COMMAND.commandId,
          client_message_id: TEST_COMMAND.clientMessageId,
          protocol_version: 1,
          required_capabilities: ['agent.run', 'agent.stream', 'hitl', 'mcp', 'skills', 'subagents'],
          goal: 'Use a skill',
          history: [],
          settings: { memory: 'off', skills: 'on' },
          model: 'local:test:model',
        }),
      }),
    )
  })

  it('carries run metadata in the run-create payload', async () => {
    const fetcher = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          id: 'run-repair',
          goal: 'Fix the failed task',
          status: 'queued',
          created_at: '2026-05-16T00:00:00Z',
          updated_at: '2026-05-16T00:00:00Z',
        }),
        { status: 201, headers: { 'Content-Type': 'application/json' } },
      ),
    )

    await createLocalRun(
      {
        ...TEST_COMMAND,
        goal: 'Fix the failed task',
        metadata: {
          intent: 'repair',
          source_run_id: 'run-original',
          source_message_id: 'msg-original',
          attempt: 1,
        },
      },
      { baseURL: 'http://127.0.0.1:17371', token: 'local-token' },
      fetcher,
    )

    expect(fetcher).toHaveBeenCalledWith(
      'http://127.0.0.1:17371/v1/runs',
      expect.objectContaining({
        body: JSON.stringify({
          command_id: TEST_COMMAND.commandId,
          client_message_id: TEST_COMMAND.clientMessageId,
          protocol_version: 1,
          required_capabilities: ['agent.run', 'agent.stream', 'hitl', 'mcp', 'memory', 'skills', 'subagents'],
          goal: 'Fix the failed task',
          history: [],
          metadata: {
            intent: 'repair',
            source_run_id: 'run-original',
            source_message_id: 'msg-original',
            attempt: 1,
          },
          model: 'local:test:model',
        }),
      }),
    )
  })

  it('maps advanced agent settings to snake_case wire keys', async () => {
    const fetcher = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          id: 'run-adv',
          goal: 'g',
          status: 'queued',
          created_at: '2026-05-16T00:00:00Z',
          updated_at: '2026-05-16T00:00:00Z',
        }),
        { status: 201, headers: { 'Content-Type': 'application/json' } },
      ),
    )

    await createLocalRun(
      {
        ...TEST_COMMAND,
        goal: 'g',
        settings: {
          advanced: {
            maxModelCalls: 50,
            maxToolRetries: 4,
            researchSearchLimit: 8,
            subagents: false,
            browserHeadless: false,
            planFirst: 'auto',
            // unset knobs must NOT appear on the wire (runtime keeps its default)
          },
        },
      },
      { baseURL: 'http://127.0.0.1:17371', token: 'local-token' },
      fetcher,
    )

    const sent = JSON.parse(String((fetcher.mock.calls[0]?.[1] as { body?: string })?.body ?? '{}'))
    expect(sent.settings).toEqual({
      max_model_calls: 50,
      max_tool_retries: 4,
      research_search_limit: 8,
      subagents: false,
      browser_headless: false,
      plan_first: 'auto',
    })
  })

  it('sends the selected Runtime permission mode with a new run', async () => {
    const fetcher = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          id: 'run-permission-mode',
          goal: 'g',
          status: 'queued',
          created_at: '2026-07-15T00:00:00Z',
          updated_at: '2026-07-15T00:00:00Z',
        }),
        { status: 201, headers: { 'Content-Type': 'application/json' } },
      ),
    )

    await createLocalRun(
      { ...TEST_COMMAND, goal: 'g', permissionMode: 'auto' },
      { baseURL: 'http://127.0.0.1:17371', token: 'local-token' },
      fetcher,
    )

    const sent = JSON.parse(String((fetcher.mock.calls[0]?.[1] as { body?: string })?.body ?? '{}'))
    expect(sent.permission_mode).toBe('auto')
  })

  it('omits settings entirely when only an empty advanced object is given', async () => {
    const fetcher = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          id: 'run-empty',
          goal: 'g',
          status: 'queued',
          created_at: '2026-05-16T00:00:00Z',
          updated_at: '2026-05-16T00:00:00Z',
        }),
        { status: 201, headers: { 'Content-Type': 'application/json' } },
      ),
    )

    await createLocalRun(
      { ...TEST_COMMAND, goal: 'g', settings: { advanced: {} } },
      { baseURL: 'http://127.0.0.1:17371', token: 'local-token' },
      fetcher,
    )

    const sent = JSON.parse(String((fetcher.mock.calls[0]?.[1] as { body?: string })?.body ?? '{}'))
    expect('settings' in sent).toBe(false)
  })

  it('forks a local run from a checkpoint through the protected run API', async () => {
    const fetcher = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          id: 'run-fork',
          goal: 'Retry from checkpoint',
          status: 'queued',
          parent_run_id: 'run-source',
          created_at: '2026-06-13T00:00:00Z',
          updated_at: '2026-06-13T00:00:00Z',
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    )

    await expect(
      forkLocalRun(
        'cmd-fork',
        {
          sourceRunId: 'run-source',
          protocolVersion: 1,
          requiredCapabilities: ['agent.run', 'agent.stream', 'hitl'],
          clientMessageId: 'msg-user',
          assistantMessageId: 'msg-assistant',
          threadId: 'conv-fork',
          checkpointId: 'ckpt-1',
          goal: 'Retry from checkpoint',
          userInput: 'Retry from checkpoint ckpt-1',
          threadTitle: 'Forked conversation',
        },
        { baseURL: 'http://127.0.0.1:17371', token: 'local-token' },
        fetcher,
      ),
    ).resolves.toMatchObject({ id: 'run-fork', parent_run_id: 'run-source' })

    expect(fetcher).toHaveBeenCalledWith(
      'http://127.0.0.1:17371/v1/runs/run-source/fork',
      expect.objectContaining({
        method: 'POST',
        headers: expect.objectContaining({ Authorization: 'Bearer local-token' }),
        body: JSON.stringify({
          command_id: 'cmd-fork',
          client_message_id: 'msg-user',
          assistant_message_id: 'msg-assistant',
          thread_id: 'conv-fork',
          protocol_version: 1,
          required_capabilities: ['agent.run', 'agent.stream', 'hitl'],
          checkpoint_id: 'ckpt-1',
          goal: 'Retry from checkpoint',
          user_input: 'Retry from checkpoint ckpt-1',
          thread_title: 'Forked conversation',
        }),
      }),
    )
  })

  it('lists installed skills and roots from Runtime', async () => {
    const fetcher = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          skills: [{ name: 'hunt', description: 'Diagnose', path: '/p/SKILL.md' }],
          roots: [{ source: 'shejane', path: '/u/.shejane/skills' }],
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    )
    const catalog = await listInstalledSkills(
      { baseURL: 'http://127.0.0.1:17371', token: 'local-token' },
      fetcher,
    )
    expect(catalog.skills).toEqual([
      { name: 'hunt', description: 'Diagnose', path: '/p/SKILL.md' },
    ])
    expect(catalog.roots).toEqual([{ source: 'shejane', path: '/u/.shejane/skills' }])
    expect(fetcher).toHaveBeenCalledWith(
      'http://127.0.0.1:17371/v1/skills',
      expect.objectContaining({ method: 'GET' }),
    )
  })

  it('creates and deletes a SheJane-managed MCP server', async () => {
    const fetcher = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            server: {
              name: 'context7',
              transport: 'stdio',
              source: 'shejane',
              source_path: '/u/.shejane/mcp-servers.json',
              command: 'npx',
              args: ['-y', '@upstash/context7-mcp'],
              env_keys: [],
            },
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        ),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ deleted: true, name: 'context7' }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      )

    await createMcpServer(
      {
        name: 'context7',
        transport: 'stdio',
        command: 'npx',
        args: ['-y', '@upstash/context7-mcp'],
        env: {},
      },
      { baseURL: 'http://127.0.0.1:17371', token: 'local-token' },
      fetcher,
    )
    await deleteMcpServer(
      'context7',
      { baseURL: 'http://127.0.0.1:17371', token: 'local-token' },
      fetcher,
    )

    expect(fetcher).toHaveBeenNthCalledWith(
      1,
      'http://127.0.0.1:17371/v1/mcp-servers',
      expect.objectContaining({
        method: 'POST',
        headers: expect.objectContaining({ Authorization: 'Bearer local-token' }),
        body: JSON.stringify({
          name: 'context7',
          transport: 'stdio',
          command: 'npx',
          args: ['-y', '@upstash/context7-mcp'],
          env: {},
        }),
      }),
    )
    expect(fetcher).toHaveBeenNthCalledWith(
      2,
      'http://127.0.0.1:17371/v1/mcp-servers/context7',
      expect.objectContaining({
        method: 'DELETE',
        headers: expect.objectContaining({ Authorization: 'Bearer local-token' }),
      }),
    )
  })

  it('creates, loads, updates, and deletes a local skill file', async () => {
    const fetcher = vi
      .fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ skill: { name: 'daily', content: '# Daily' } }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ name: 'daily', content: '# Daily' }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ skill: { name: 'daily', content: '# Updated' } }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ deleted: true, name: 'daily' }), { status: 200 }))

    const config = { baseURL: 'http://127.0.0.1:17371', token: 'local-token' }
    await createLocalSkill({ name: 'daily', description: 'Digest', content: '# Daily' }, config, fetcher)
    await getLocalSkillFile('daily', config, fetcher)
    await updateLocalSkill('daily', { description: '', content: '# Updated' }, config, fetcher)
    await deleteLocalSkill('daily', config, fetcher)

    expect(fetcher).toHaveBeenNthCalledWith(
      1,
      'http://127.0.0.1:17371/v1/skills',
      expect.objectContaining({ method: 'POST', body: JSON.stringify({ name: 'daily', description: 'Digest', content: '# Daily' }) }),
    )
    expect(fetcher).toHaveBeenNthCalledWith(
      2,
      'http://127.0.0.1:17371/v1/skills/daily',
      expect.objectContaining({ method: 'GET' }),
    )
    expect(fetcher).toHaveBeenNthCalledWith(
      3,
      'http://127.0.0.1:17371/v1/skills/daily',
      expect.objectContaining({ method: 'PUT', body: JSON.stringify({ description: '', content: '# Updated' }) }),
    )
    expect(fetcher).toHaveBeenNthCalledWith(
      4,
      'http://127.0.0.1:17371/v1/skills/daily',
      expect.objectContaining({ method: 'DELETE' }),
    )
  })

  it('streams local run events and returns completion metadata', async () => {
    const fetcher = vi.fn().mockResolvedValue(
      new Response(
        'event: local.event\n' +
          'data: {"id":"event-1","event_type":"tool.completed","payload":{"tool":"file.read"}}\n\n' +
          'event: local.event\n' +
          'data: {"id":"event-2","event_type":"llm.delta","payload":{"content":"完成"}}\n\n' +
          'data: [DONE]\n\n',
        { status: 200, headers: { 'Content-Type': 'text/event-stream' } },
      ),
    )
    const events: string[] = []
    let content = ''

    await expect(
      streamLocalRun(
        'run-local',
        { baseURL: 'http://127.0.0.1:17371', token: 'local-token' },
        {
          onEvent: (event) => events.push(event.event_type),
          onDelta: (delta) => {
            content += delta
          },
        },
        fetcher,
      ),
    ).resolves.toEqual({ completed: true })
    expect(events).toEqual(['tool.completed', 'llm.delta'])
    expect(content).toBe('完成')
  })

  it('resumes a local run stream after the last projected event sequence', async () => {
    const fetcher = vi.fn().mockResolvedValue(
      new Response('data: [DONE]\n\n', {
        status: 200,
        headers: { 'Content-Type': 'text/event-stream' },
      }),
    )

    await streamLocalRun(
      'run-local',
      { baseURL: 'http://127.0.0.1:17371', token: 'local-token' },
      { afterSeq: 17, onEvent: () => undefined, onDelta: () => undefined },
      fetcher,
    )

    expect(fetcher).toHaveBeenCalledWith(
      'http://127.0.0.1:17371/v1/runs/run-local/stream?after=17',
      expect.anything(),
    )
  })

  it('signals that the caller must rebuild from a Runtime snapshot when the cursor is invalid', async () => {
    const fetcher = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({
        detail: {
          code: 'event_cursor_reset_required',
          message: 'event cursor is outside the retained event window',
          requested_after: 99,
          first_available_seq: 4,
          latest_seq: 8,
        },
      }), {
        status: 409,
        headers: { 'Content-Type': 'application/json' },
      }),
    )

    await expect(streamLocalRun(
      'run-local',
      { baseURL: 'http://127.0.0.1:17371', token: 'local-token' },
      { afterSeq: 99, onEvent: () => undefined, onDelta: () => undefined },
      fetcher,
    )).rejects.toMatchObject({
      name: LocalStreamCursorResetRequiredError.name,
      resumeAfter: 3,
    })
  })

  it('reads artifacts through the protected API', async () => {
    const fetcher = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          id: 'artifact-1',
          title: 'file.read output',
          content: 'artifact content',
          tool_name: 'file.read',
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    )

    await expect(getLocalArtifact('artifact-1', { baseURL: 'http://127.0.0.1:17371', token: 'local-token' }, fetcher)).resolves.toMatchObject({
      id: 'artifact-1',
      content: 'artifact content',
    })
    expect(fetcher).toHaveBeenCalledWith(
      'http://127.0.0.1:17371/v1/artifacts/artifact-1',
      expect.objectContaining({ method: 'GET' }),
    )
  })

  it('injects mid-run steering instructions through the protected run API', async () => {
    const fetcher = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          run_id: 'run-active',
          instruction_id: 'steer-1',
          queued: true,
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    )

    await expect(
      injectLocalRunInstruction(
        'run-active',
        'Focus on the failing tests before editing.',
        { baseURL: 'http://127.0.0.1:17371', token: 'local-token' },
        fetcher,
      ),
    ).resolves.toMatchObject({ queued: true, instruction_id: 'steer-1' })

    expect(fetcher).toHaveBeenCalledWith(
      'http://127.0.0.1:17371/v1/runs/run-active/inject',
      expect.objectContaining({
        method: 'POST',
        headers: expect.objectContaining({ Authorization: 'Bearer local-token' }),
        body: JSON.stringify({ content: 'Focus on the failing tests before editing.' }),
      }),
    )
  })

  it('submits plan decisions through the immutable Runtime command endpoint', async () => {
    const fetcher = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          type: 'plan.resolve',
          command_id: 'resolve-plan-1',
          approval_id: 'plan-1',
          run_id: 'run-1',
          resolved: true,
          decision: 'modify',
          resumed: true,
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    )

    await expect(
      resolveLocalPlanCommand(
        'resolve-plan-1',
        'plan-1',
        'modify',
        'Add a verification step.',
        { baseURL: 'http://127.0.0.1:17371', token: 'local-token' },
        fetcher,
      ),
    ).resolves.toMatchObject({ type: 'plan.resolve', resolved: true, resumed: true })

    expect(fetcher).toHaveBeenCalledWith(
      'http://127.0.0.1:17371/v1/commands',
      expect.objectContaining({
        method: 'POST',
        headers: expect.objectContaining({
          Authorization: 'Bearer local-token',
          'Content-Type': 'application/json',
        }),
        body: JSON.stringify({
          type: 'plan.resolve',
          command_id: 'resolve-plan-1',
          approval_id: 'plan-1',
          decision: 'modify',
          instructions: 'Add a verification step.',
        }),
      }),
    )
  })

  it('lists and authorizes local workspaces through protected APIs', async () => {
    const fetcher = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ workspaces: [{ id: 'workspace-1', path: '/tmp/project', label: 'project' }] }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            id: 'workspace-2',
            path: '/tmp/other',
            label: 'other',
            created_at: '2026-05-11T00:00:00Z',
            last_used_at: '2026-05-11T00:00:00Z',
          }),
          { status: 201, headers: { 'Content-Type': 'application/json' } },
        ),
      )

    await expect(listAuthorizedWorkspaces({ baseURL: 'http://127.0.0.1:17371', token: 'local-token' }, fetcher)).resolves.toEqual([
      { id: 'workspace-1', path: '/tmp/project', label: 'project' },
    ])
    await expect(authorizeLocalWorkspace('/tmp/other', { baseURL: 'http://127.0.0.1:17371', token: 'local-token' }, fetcher)).resolves.toMatchObject({
      id: 'workspace-2',
      path: '/tmp/other',
    })
    expect(fetcher).toHaveBeenNthCalledWith(
      1,
      'http://127.0.0.1:17371/v1/workspaces',
      expect.objectContaining({ method: 'GET', headers: expect.objectContaining({ Authorization: 'Bearer local-token' }) }),
    )
    expect(fetcher).toHaveBeenNthCalledWith(
      2,
      'http://127.0.0.1:17371/v1/workspaces',
      expect.objectContaining({ method: 'POST', body: JSON.stringify({ path: '/tmp/other' }) }),
    )
  })

  it('diagnoses and revokes local workspaces through protected APIs', async () => {
    const fetcher = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            path: '/tmp/project',
            exists: true,
            is_directory: true,
            authorized: true,
            reason: 'authorized',
            workspace: { id: 'workspace-1', path: '/tmp/project', label: 'project' },
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        ),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ id: 'workspace-1', path: '/tmp/project', label: 'project' }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      )

    await expect(diagnoseLocalWorkspace('/tmp/project', { baseURL: 'http://127.0.0.1:17371', token: 'local-token' }, fetcher)).resolves.toMatchObject({
      authorized: true,
      workspace: { id: 'workspace-1' },
    })
    await expect(revokeLocalWorkspace('workspace-1', { baseURL: 'http://127.0.0.1:17371', token: 'local-token' }, fetcher)).resolves.toMatchObject({
      id: 'workspace-1',
      path: '/tmp/project',
    })
    expect(fetcher).toHaveBeenNthCalledWith(
      1,
      'http://127.0.0.1:17371/v1/workspaces/diagnose',
      expect.objectContaining({ method: 'POST', body: JSON.stringify({ path: '/tmp/project' }) }),
    )
    expect(fetcher).toHaveBeenNthCalledWith(
      2,
      'http://127.0.0.1:17371/v1/workspaces/workspace-1',
      expect.objectContaining({ method: 'DELETE', headers: expect.objectContaining({ Authorization: 'Bearer local-token' }) }),
    )
  })

  it('lists local runs and fetches redacted diagnostics through protected APIs', async () => {
    const fetcher = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            runs: [
              {
                id: 'run-1',
                goal: 'Resume this run',
                status: 'running',
                created_at: '2026-05-11T00:00:00Z',
                updated_at: '2026-05-11T00:00:01Z',
                events_count: 3,
              },
            ],
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        ),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            schema_version: 1,
            exported_at: '2026-05-11T00:00:02Z',
            run: { id: 'run-1', goal: 'Resume this run', status: 'running' },
            events: [],
            permissions: [],
            artifacts: [],
            latest_checkpoint: null,
            handoff: {
              status: 'running',
              headline: 'Run is running with 0 persisted events.',
              next_actions: ['Reconnect to the stream or wait for the run to reach a terminal state.'],
              blockers: [],
              recent_event_types: [],
              ledger_state: 'fresh',
              ledger_message: null,
            },
            feature_ledger: {
              summary: 'Wire diagnostics',
              status: 'in_progress',
              acceptance_criteria: ['diagnostics exposes latest ledger'],
              decisions: [],
              files_touched: [],
              validation_commands: [],
              unresolved_risks: [],
              next_actions: ['run tests'],
            },
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        ),
      )

    await expect(listLocalRuns({ baseURL: 'http://127.0.0.1:17371', token: 'local-token' }, fetcher)).resolves.toEqual([
      expect.objectContaining({ id: 'run-1', status: 'running' }),
    ])
    await expect(getLocalRunDiagnostics('run-1', { baseURL: 'http://127.0.0.1:17371', token: 'local-token' }, fetcher)).resolves.toMatchObject({
      schema_version: 1,
      run: { id: 'run-1' },
      handoff: { status: 'running' },
      feature_ledger: { summary: 'Wire diagnostics' },
    })
    expect(fetcher).toHaveBeenNthCalledWith(
      1,
      'http://127.0.0.1:17371/v1/runs',
      expect.objectContaining({ method: 'GET', headers: expect.objectContaining({ Authorization: 'Bearer local-token' }) }),
    )
    expect(fetcher).toHaveBeenNthCalledWith(
      2,
      'http://127.0.0.1:17371/v1/runs/run-1/diagnostics',
      expect.objectContaining({ method: 'GET', headers: expect.objectContaining({ Authorization: 'Bearer local-token' }) }),
    )
  })

  it('reads authoritative Runtime thread snapshots and change cursors', async () => {
    const thread = {
      id: 'conversation-1',
      title: 'Inspect workspace',
      version: 2,
      created_at: '2026-07-12T00:00:00Z',
      updated_at: '2026-07-12T00:00:01Z',
    }
    const fetcher = vi.fn()
      .mockResolvedValueOnce(new Response(
        JSON.stringify({ threads: [thread], cursor: 7 }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ))
      .mockResolvedValueOnce(new Response(
        JSON.stringify({
          thread,
          items: [{
            id: 'item-1',
            thread_id: thread.id,
            item_type: 'assistant_message',
            status: 'completed',
            content: 'done',
            metadata: {},
            position: 2,
            version: 2,
            created_at: thread.created_at,
            updated_at: thread.updated_at,
          }],
          runs: [],
          cursor: 7,
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ))
      .mockResolvedValueOnce(new Response(
        JSON.stringify({
          changes: [{
            cursor: 7,
            thread_id: thread.id,
            thread_version: 2,
            change_type: 'run.completed',
            created_at: thread.updated_at,
          }],
          cursor: 7,
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ))
    const config = { baseURL: 'http://127.0.0.1:17371', token: 'local-token' }

    await expect(listLocalThreads(config, fetcher)).resolves.toMatchObject({ cursor: 7 })
    await expect(getLocalThreadSnapshot(thread.id, config, fetcher)).resolves.toMatchObject({
      thread: { id: thread.id, version: 2 },
      items: [{ content: 'done' }],
    })
    await expect(listLocalThreadChanges(3, config, fetcher)).resolves.toMatchObject({
      cursor: 7,
      changes: [{ change_type: 'run.completed' }],
    })
    expect(fetcher).toHaveBeenNthCalledWith(
      3,
      'http://127.0.0.1:17371/v1/threads/changes?after=3&limit=1000',
      expect.objectContaining({ method: 'GET' }),
    )
  })

  it('drains thread changes through the page containing a later tombstone', async () => {
    const createdAt = '2026-07-12T00:00:00Z'
    const firstPage = Array.from({ length: 1000 }, (_, index) => ({
      cursor: index + 1,
      thread_id: index === 999 ? 'conversation-deleted-later' : `conversation-${index}`,
      thread_version: 1,
      change_type: 'thread.updated',
      created_at: createdAt,
    }))
    const fetcher = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ changes: firstPage, cursor: 1000 }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        changes: [{
          cursor: 1001,
          thread_id: 'conversation-deleted-later',
          thread_version: 2,
          change_type: 'thread.deleted',
          created_at: createdAt,
        }],
        cursor: 1001,
      }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }))

    const result = await listLocalThreadChanges(
      0,
      { baseURL: 'http://127.0.0.1:17371', token: 'local-token' },
      fetcher,
    )
    expect(result).toMatchObject({ cursor: 1001 })
    expect(result.changes).toHaveLength(1001)
    expect(fetcher).toHaveBeenNthCalledWith(
      2,
      'http://127.0.0.1:17371/v1/threads/changes?after=1000&limit=1000',
      expect.objectContaining({ method: 'GET' }),
    )
  })

  it('falls back to a full snapshot sync when the bounded change catch-up is exhausted', async () => {
    const fetcher = vi.fn(async (input: RequestInfo | URL) => {
      const after = Number(new URL(String(input)).searchParams.get('after') ?? 0)
      const changes = Array.from({ length: 1000 }, (_, index) => ({
        cursor: after + index + 1,
        thread_id: `conversation-${after + index + 1}`,
        thread_version: 1,
        change_type: 'thread.updated',
        created_at: '2026-07-12T00:00:00Z',
      }))
      return new Response(JSON.stringify({ changes, cursor: after + 1000 }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    })

    await expect(listLocalThreadChanges(
      0,
      { baseURL: 'http://127.0.0.1:17371', token: 'local-token' },
      fetcher,
    )).resolves.toEqual({ changes: [], cursor: 10_000, resetRequired: true })
    expect(fetcher).toHaveBeenCalledTimes(10)
  })

  it('updates and tombstones Runtime-owned threads', async () => {
    const fetcher = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({
        id: 'conversation-1',
        title: 'Renamed',
        metadata: { pinned: true },
        version: 3,
        created_at: '2026-07-12T00:00:00Z',
        updated_at: '2026-07-12T00:00:03Z',
      }), { status: 200, headers: { 'Content-Type': 'application/json' } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        id: 'conversation-1', deleted: true, version: 4,
      }), { status: 200, headers: { 'Content-Type': 'application/json' } }))
    const config = { baseURL: 'http://127.0.0.1:17371', token: 'local-token' }

    await expect(updateLocalThread(
      'conversation-1',
      { title: 'Renamed', metadata: { pinned: true } },
      config,
      fetcher,
    )).resolves.toMatchObject({ title: 'Renamed', version: 3 })
    await expect(deleteLocalThread('conversation-1', config, fetcher)).resolves.toEqual({
      id: 'conversation-1', deleted: true, version: 4,
    })
    expect(fetcher).toHaveBeenNthCalledWith(
      1,
      'http://127.0.0.1:17371/v1/threads/conversation-1',
      expect.objectContaining({ method: 'PATCH' }),
    )
    expect(fetcher).toHaveBeenNthCalledWith(
      2,
      'http://127.0.0.1:17371/v1/threads/conversation-1',
      expect.objectContaining({ method: 'DELETE' }),
    )
  })

  it('manages scheduled local runs', async () => {
    const fetcher = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            id: 'sched-1',
            goal: '稍后跑',
            status: 'scheduled',
            run_at: '2026-06-13T10:00:00Z',
            created_at: '2026-06-13T09:00:00Z',
            updated_at: '2026-06-13T09:00:00Z',
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        ),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            schedules: [
              {
                id: 'sched-1',
                goal: '稍后跑',
                status: 'completed',
                run_at: '2026-06-13T10:00:00Z',
                result_text: '完成了',
                created_at: '2026-06-13T09:00:00Z',
                updated_at: '2026-06-13T10:01:00Z',
              },
            ],
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        ),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            id: 'sched-1',
            goal: '稍后跑',
            status: 'completed',
            run_at: '2026-06-13T10:00:00Z',
            notified_at: '2026-06-13T10:02:00Z',
            created_at: '2026-06-13T09:00:00Z',
            updated_at: '2026-06-13T10:02:00Z',
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        ),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            id: 'sched-2',
            goal: '取消',
            status: 'canceled',
            run_at: '2026-06-13T11:00:00Z',
            created_at: '2026-06-13T09:00:00Z',
            updated_at: '2026-06-13T09:10:00Z',
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        ),
      )

    await expect(
      createLocalSchedule(
        {
          goal: '稍后跑',
          runAt: '2026-06-13T10:00:00Z',
          mode: 'local:test:model',
          history: [{ role: 'user', content: '背景' }],
          settings: {
            memory: 'on',
            skills: 'on',
            mcp: 'on',
            mcpDisabled: [],
            advanced: {},
          },
        },
        { baseURL: 'http://127.0.0.1:17371', token: 'local-token' },
        fetcher,
      ),
    ).resolves.toMatchObject({ id: 'sched-1', status: 'scheduled' })
    await expect(
      listLocalSchedules(
        { baseURL: 'http://127.0.0.1:17371', token: 'local-token' },
        { notifyPending: true },
        fetcher,
      ),
    ).resolves.toEqual([expect.objectContaining({ id: 'sched-1', result_text: '完成了' })])
    await expect(
      markLocalScheduleNotified('sched-1', { baseURL: 'http://127.0.0.1:17371', token: 'local-token' }, fetcher),
    ).resolves.toMatchObject({ notified_at: '2026-06-13T10:02:00Z' })
    await expect(
      cancelLocalSchedule('sched-2', { baseURL: 'http://127.0.0.1:17371', token: 'local-token' }, fetcher),
    ).resolves.toMatchObject({ status: 'canceled' })

    expect(fetcher).toHaveBeenNthCalledWith(
      1,
      'http://127.0.0.1:17371/v1/schedules',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({
          goal: '稍后跑',
          run_at: '2026-06-13T10:00:00Z',
          model: 'local:test:model',
          history: [{ role: 'user', content: '背景' }],
          settings: {
            memory: 'on',
            skills: 'on',
            mcp: 'on',
          },
        }),
      }),
    )
    expect(fetcher).toHaveBeenNthCalledWith(
      2,
      'http://127.0.0.1:17371/v1/schedules?notify_pending=true',
      expect.objectContaining({ method: 'GET' }),
    )
    expect(fetcher).toHaveBeenNthCalledWith(
      3,
      'http://127.0.0.1:17371/v1/schedules/sched-1/notified',
      expect.objectContaining({ method: 'POST' }),
    )
    expect(fetcher).toHaveBeenNthCalledWith(
      4,
      'http://127.0.0.1:17371/v1/schedules/sched-2',
      expect.objectContaining({ method: 'DELETE' }),
    )
  })

})
