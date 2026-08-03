import 'fake-indexeddb/auto'
import { IDBFactory } from 'fake-indexeddb'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { App } from './App'
import { LocalConversationStore } from './shared/local-data/localConversations'

function truncatedPermissionSnapshot(input: {
  threadID: string
  title: string
  runID: string
  assistantClientID: string
  userClientID?: string
}) {
  const createdAt = '2026-08-02T00:00:00Z'
  return {
    thread: {
      id: input.threadID,
      title: input.title,
      metadata: {},
      version: 2,
      created_at: createdAt,
      updated_at: '2026-08-02T00:00:02Z',
    },
    items: [
      ...(input.userClientID ? [{
        id: `item-user-${input.runID}`,
        thread_id: input.threadID,
        run_id: input.runID,
        client_id: input.userClientID,
        item_type: 'user_message',
        status: 'completed',
        content: 'Run a command',
        metadata: {},
        position: 1,
        version: 1,
        created_at: createdAt,
        updated_at: createdAt,
      }] : []),
      {
        id: `item-assistant-${input.runID}`,
        thread_id: input.threadID,
        run_id: input.runID,
        client_id: input.assistantClientID,
        item_type: 'assistant_message',
        status: 'in_progress',
        content: '',
        metadata: {},
        position: input.userClientID ? 2 : 1,
        version: 2,
        created_at: createdAt,
        updated_at: '2026-08-02T00:00:02Z',
      },
    ],
    runs: [{
      id: input.runID,
      goal: 'Run a command',
      status: 'waiting_permission',
      thread_id: input.threadID,
      assistant_item_id: `item-assistant-${input.runID}`,
      history_json: '[]',
      settings_json: '{}',
      metadata_json: '{}',
      inputs: [],
      subagent_invocations: [],
      created_at: createdAt,
      updated_at: '2026-08-02T00:00:02Z',
    }],
    events: [{
      id: `event-started-${input.runID}`,
      run_id: input.runID,
      seq: 1,
      event_type: 'run.started',
      payload: {},
      created_at: createdAt,
    }],
    event_high_watermarks: { [input.runID]: 1 },
    cursor: 2,
    has_more_items: false,
    next_before_position: null,
    events_truncated: true,
  }
}

function permissionReplayEvent(runID: string) {
  return {
    id: `event-permission-${runID}`,
    run_id: runID,
    seq: 2,
    event_type: 'permission.required',
    payload: {
      request_id: `permission-${runID}`,
      tool: 'execute',
      arguments: { command: 'echo replay' },
      allow_run_scope: true,
    },
    created_at: '2026-08-02T00:00:01Z',
  }
}

describe('desktop shell', () => {
  beforeEach(() => {
    window.localStorage.clear()
    indexedDB = new IDBFactory()
    Object.defineProperty(window, 'shejaneClient', {
      configurable: true,
      value: {
        platform: 'darwin',
        runtime: { baseURL: 'http://127.0.0.1:17371', session: 'client', ready: false },
      },
    })
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('Runtime offline')))
  })

  afterEach(() => {
    cleanup()
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  it('opens the local desktop shell without an account gate', async () => {
    render(<App />)

    expect(await screen.findAllByText('新对话')).not.toHaveLength(0)
    expect(screen.queryByText('登录')).not.toBeInTheDocument()
    expect(screen.queryByText('注册')).not.toBeInTheDocument()
  })

  it('groups Skill, MCP, and installed plugins under one Plugins workspace', async () => {
    render(<App />)

    expect(screen.queryByRole('button', { name: 'Skill' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'MCP' })).not.toBeInTheDocument()
    fireEvent.click(await screen.findByRole('button', { name: '插件' }))

    expect((await screen.findAllByRole('tab')).map((tab) => tab.textContent)).toEqual(['插件', 'Skill', 'MCP'])
    expect(screen.getByRole('tab', { name: '插件' })).toHaveAttribute('aria-selected', 'true')

    fireEvent.click(screen.getByRole('tab', { name: 'MCP' }))
    expect(screen.getByRole('tab', { name: 'MCP' })).toHaveAttribute('aria-selected', 'true')

    fireEvent.click(screen.getByRole('button', { name: '设置' }))
    fireEvent.click(screen.getByRole('button', { name: '插件' }))
    expect(screen.getByRole('tab', { name: 'MCP' })).toHaveAttribute('aria-selected', 'true')
  })

  it('detects Runtime offline and recovery without remounting the Client', async () => {
    vi.useFakeTimers()
    let runtimeOnline = true
    Object.defineProperty(window, 'shejaneClient', {
      configurable: true,
      value: {
        platform: 'darwin',
        runtime: { baseURL: 'http://127.0.0.1:17371', session: 'client', ready: true },
      },
    })
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      if (String(input).endsWith('/v1/health')) {
        if (!runtimeOnline) throw new Error('Runtime offline')
        return new Response(JSON.stringify({ status: 'ok', mode: 'runtime', worker: 'user' }), {
          status: 200,
          headers: { 'content-type': 'application/json' },
        })
      }
      throw new Error('catalog unavailable in health-poll test')
    }))

    render(<App />)
    await vi.waitFor(() => {
      expect(screen.getByLabelText(/Runtime 已连接/)).toBeInTheDocument()
    })

    runtimeOnline = false
    await vi.advanceTimersByTimeAsync(2_000)
    await vi.waitFor(() => {
      expect(screen.getByLabelText(/Runtime 离线/)).toBeInTheDocument()
    })

    runtimeOnline = true
    await vi.advanceTimersByTimeAsync(2_000)
    await vi.waitFor(() => {
      expect(screen.getByLabelText(/Runtime 已连接/)).toBeInTheDocument()
    })
    vi.useRealTimers()
  })

  it('replays an approval event omitted by a truncated Runtime snapshot', async () => {
    Object.defineProperty(window, 'shejaneClient', {
      configurable: true,
      value: {
        platform: 'darwin',
        runtime: { baseURL: 'http://127.0.0.1:17371', session: 'client', ready: true },
      },
    })
    const snapshot = {
      thread: {
        id: 'conversation-replay',
        title: 'Permission replay',
        metadata: {},
        version: 2,
        created_at: '2026-08-02T00:00:00Z',
        updated_at: '2026-08-02T00:00:02Z',
      },
      items: [{
        id: 'assistant-replay',
        thread_id: 'conversation-replay',
        run_id: 'run-replay',
        client_id: 'assistant-replay-client',
        item_type: 'assistant_message',
        status: 'in_progress',
        content: '',
        metadata: {},
        position: 1,
        version: 2,
        created_at: '2026-08-02T00:00:00Z',
        updated_at: '2026-08-02T00:00:02Z',
      }],
      runs: [{
        id: 'run-replay',
        goal: 'Run a command',
        status: 'waiting_permission',
        thread_id: 'conversation-replay',
        assistant_item_id: 'assistant-replay',
        history_json: '[]',
        settings_json: '{}',
        metadata_json: '{}',
        inputs: [],
        subagent_invocations: [],
        created_at: '2026-08-02T00:00:00Z',
        updated_at: '2026-08-02T00:00:02Z',
      }],
      events: [],
      event_high_watermarks: { 'run-replay': 0 },
      cursor: 2,
      has_more_items: false,
      next_before_position: null,
      events_truncated: true,
    }
    const permissionEvent = {
      id: 'event-permission-replay',
      run_id: 'run-replay',
      seq: 1,
      event_type: 'permission.required',
      payload: {
        request_id: 'permission-replay',
        tool: 'execute',
        arguments: { command: 'echo replay' },
        allow_run_scope: true,
      },
      created_at: '2026-08-02T00:00:01Z',
    }
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.endsWith('/v1/health')) {
        return new Response(JSON.stringify({ status: 'ok', mode: 'runtime', worker: 'user' }), {
          status: 200,
          headers: { 'content-type': 'application/json' },
        })
      }
      if (url.endsWith('/v1/threads')) {
        return new Response(JSON.stringify({
          threads: [snapshot.thread],
          cursor: 2,
          has_more: false,
        }), { status: 200, headers: { 'content-type': 'application/json' } })
      }
      if (url.endsWith('/v1/threads/conversation-replay')) {
        return new Response(JSON.stringify(snapshot), {
          status: 200,
          headers: { 'content-type': 'application/json' },
        })
      }
      if (url.endsWith('/v1/runs/run-replay/events?after=0&limit=1000')) {
        return new Response(JSON.stringify({
          events: [permissionEvent],
          has_more: false,
          next_after: 1,
        }), { status: 200, headers: { 'content-type': 'application/json' } })
      }
      return new Response(JSON.stringify({ detail: 'not needed by this test' }), {
        status: 404,
        headers: { 'content-type': 'application/json' },
      })
    }))

    render(<App />)

    await waitFor(() => {
      expect(fetch).toHaveBeenCalledWith(
        'http://127.0.0.1:17371/v1/runs/run-replay/events?after=0&limit=1000',
        expect.objectContaining({ method: 'GET' }),
      )
    })
    fireEvent.click((await screen.findAllByText('Permission replay'))[0])
    expect(await screen.findByText('等待批准：运行命令')).toBeInTheDocument()
  })

  it('replays a truncated approval when a rejected command refreshes the cache', async () => {
    const threadID = 'conversation-rejected-replay'
    const runID = 'run-rejected-replay'
    const assistantClientID = 'assistant-rejected-replay'
    const store = new LocalConversationStore('shejane-local:runtime:local-owner')
    await store.saveWithPendingRuntimeCommand({
      id: threadID,
      title: 'Rejected replay',
      archived: false,
      createdAt: '2026-08-02T00:00:00Z',
      updatedAt: '2026-08-02T00:00:01Z',
      messages: [{
        id: assistantClientID,
        role: 'assistant',
        content: '',
        createdAt: '2026-08-02T00:00:00Z',
        status: 'waiting_permission',
        runId: runID,
        lastEventSeq: 9,
      }],
    }, {
      type: 'permission.resolve',
      commandId: 'cmd-rejected-replay',
      createdAt: '2026-08-02T00:00:01Z',
      input: {
        permissionId: 'permission-old',
        decision: 'approve',
        scope: 'once',
        runId: runID,
        threadId: threadID,
      },
    })
    Object.defineProperty(window, 'shejaneClient', {
      configurable: true,
      value: {
        platform: 'darwin',
        runtime: { baseURL: 'http://127.0.0.1:17371', session: 'client', ready: true },
      },
    })
    const snapshot = truncatedPermissionSnapshot({
      threadID,
      title: 'Rejected replay',
      runID,
      assistantClientID,
    })
    const permissionEvent = permissionReplayEvent(runID)
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/v1/health')) {
        return new Response(JSON.stringify({ status: 'ok', mode: 'runtime', worker: 'user' }), {
          status: 200,
          headers: { 'content-type': 'application/json' },
        })
      }
      if (url.endsWith('/v1/models')) {
        return new Response(JSON.stringify({ models: [] }), {
          status: 200,
          headers: { 'content-type': 'application/json' },
        })
      }
      if (url.endsWith('/v1/threads')) {
        return new Response(JSON.stringify({ threads: [], cursor: 0, has_more: false }), {
          status: 200,
          headers: { 'content-type': 'application/json' },
        })
      }
      if (url.endsWith('/v1/commands') && init?.method === 'POST') {
        return new Response(JSON.stringify({ detail: 'permission no longer exists' }), {
          status: 409,
          headers: { 'content-type': 'application/json' },
        })
      }
      if (url.endsWith(`/v1/threads/${threadID}`)) {
        return new Response(JSON.stringify(snapshot), {
          status: 200,
          headers: { 'content-type': 'application/json' },
        })
      }
      if (url.endsWith(`/v1/runs/${runID}/events?after=1&limit=1000`)) {
        return new Response(JSON.stringify({
          events: [permissionEvent],
          has_more: false,
          next_after: 2,
        }), { status: 200, headers: { 'content-type': 'application/json' } })
      }
      return new Response(JSON.stringify({ detail: 'not needed by this test' }), {
        status: 404,
        headers: { 'content-type': 'application/json' },
      })
    }))

    render(<App />)

    await waitFor(() => {
      expect(fetch).toHaveBeenCalledWith(
        `http://127.0.0.1:17371/v1/runs/${runID}/events?after=1&limit=1000`,
        expect.objectContaining({ method: 'GET' }),
      )
    })
    fireEvent.click((await screen.findAllByText('Rejected replay'))[0])
    expect(await screen.findByText('等待批准：运行命令')).toBeInTheDocument()
  })

  it('replays a truncated approval before the post-stream cache save', async () => {
    Object.defineProperty(window, 'shejaneClient', {
      configurable: true,
      value: {
        platform: 'darwin',
        runtime: { baseURL: 'http://127.0.0.1:17371', session: 'client', ready: true },
      },
    })
    window.localStorage.setItem('shejane.chatMode.v2', 'local:test:model')
    const runID = 'run-post-stream-replay'
    let commandBody: Record<string, unknown> | undefined
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/v1/health')) {
        return new Response(JSON.stringify({ status: 'ok', mode: 'runtime', worker: 'user' }), {
          status: 200,
          headers: { 'content-type': 'application/json' },
        })
      }
      if (url.endsWith('/v1/models')) {
        return new Response(JSON.stringify({ models: [{
          spec: 'local:test:model',
          model_id: 'model',
          display_name: 'Test Model',
          connection_id: 'test',
          service_name: 'Test',
          available: true,
          tool_calling: true,
          streaming: true,
          image_inputs: false,
          verification: 'verified',
          recommended: true,
        }] }), { status: 200, headers: { 'content-type': 'application/json' } })
      }
      if (url.endsWith('/v1/threads')) {
        return new Response(JSON.stringify({ threads: [], cursor: 0, has_more: false }), {
          status: 200,
          headers: { 'content-type': 'application/json' },
        })
      }
      if (url.endsWith('/v1/runs') && init?.method === 'POST') {
        commandBody = JSON.parse(String(init.body)) as Record<string, unknown>
        return new Response(JSON.stringify({
          id: runID,
          goal: 'Run a command',
          status: 'running',
          thread_id: commandBody.thread_id,
          assistant_item_id: `item-assistant-${runID}`,
          history_json: '[]',
          settings_json: '{}',
          metadata_json: '{}',
          inputs: [],
          subagent_invocations: [],
          created_at: '2026-08-02T00:00:00Z',
          updated_at: '2026-08-02T00:00:00Z',
        }), { status: 200, headers: { 'content-type': 'application/json' } })
      }
      if (url.endsWith(`/v1/runs/${runID}/stream`)) {
        return new Response(
          `data: ${JSON.stringify(permissionReplayEvent(runID))}\n\ndata: [DONE]\n\n`,
          { status: 200, headers: { 'content-type': 'text/event-stream' } },
        )
      }
      if (commandBody && url.endsWith(`/v1/threads/${String(commandBody.thread_id)}`)) {
        return new Response(JSON.stringify(truncatedPermissionSnapshot({
          threadID: String(commandBody.thread_id),
          title: 'Post-stream replay',
          runID,
          assistantClientID: String(commandBody.assistant_message_id),
          userClientID: String(commandBody.client_message_id),
        })), { status: 200, headers: { 'content-type': 'application/json' } })
      }
      if (url.endsWith(`/v1/runs/${runID}/events?after=1&limit=1000`)) {
        return new Response(JSON.stringify({
          events: [permissionReplayEvent(runID)],
          has_more: false,
          next_after: 2,
        }), { status: 200, headers: { 'content-type': 'application/json' } })
      }
      return new Response(JSON.stringify({ detail: 'not needed by this test' }), {
        status: 404,
        headers: { 'content-type': 'application/json' },
      })
    }))

    render(<App />)

    await waitFor(() => expect(screen.getByRole('button', { name: '选择模型' })).toHaveTextContent('Test Model'))
    const editor = screen.getByRole('textbox')
    editor.textContent = 'Run a command'
    fireEvent.input(editor, { inputType: 'insertText', data: 'Run a command' })
    const send = screen.getByRole('button', { name: '发送' })
    await waitFor(() => expect(send).toBeEnabled())
    fireEvent.click(send)

    await waitFor(() => {
      expect(fetch).toHaveBeenCalledWith(
        `http://127.0.0.1:17371/v1/runs/${runID}/events?after=1&limit=1000`,
        expect.objectContaining({ method: 'GET' }),
      )
    })
    expect(await screen.findByText('等待批准：运行命令')).toBeInTheDocument()
  })

  it('does not recheck large Runtime Assets when settings rerenders', async () => {
    Object.defineProperty(window, 'shejaneClient', {
      configurable: true,
      value: {
        platform: 'darwin',
        runtime: { baseURL: 'http://127.0.0.1:17371', session: 'client', ready: true },
      },
    })
    const assetRequests: string[] = []
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.endsWith('/v1/health')) {
        return new Response(JSON.stringify({ status: 'ok', mode: 'runtime', worker: 'user' }), {
          status: 200,
          headers: { 'content-type': 'application/json' },
        })
      }
      if (url.endsWith('/runtime-asset')) {
        assetRequests.push(url)
        return new Response(JSON.stringify({
          plugin_id: url.includes('browser-qa') ? 'org.shejane.browser-qa' : 'org.shejane.ocr',
          downloaded: false,
        }), { status: 200, headers: { 'content-type': 'application/json' } })
      }
      throw new Error('catalog unavailable in asset-poll test')
    }))

    render(<App />)
    await vi.waitFor(() => {
      expect(screen.getByLabelText(/Runtime 已连接/)).toBeInTheDocument()
    })
    fireEvent.click(screen.getByRole('button', { name: '设置' }))
    await vi.waitFor(() => expect(assetRequests).toHaveLength(2))

    fireEvent.click(screen.getByRole('combobox', { name: '语言' }))
    fireEvent.click(screen.getByRole('option', { name: 'English' }))
    expect(assetRequests).toHaveLength(2)
  })

  it('does not expose purchase or usage-billing actions', async () => {
    render(<App />)

    await screen.findAllByText('新对话')
    expect(screen.queryByText('充值')).not.toBeInTheDocument()
    expect(screen.queryByText('消费记录')).not.toBeInTheDocument()
  })

  it('asks before downloading diagnostics instead of opening the diagnostics panel', async () => {
    const store = new LocalConversationStore('shejane-local:runtime:local-owner')
    await store.save({
      id: 'conversation-diagnostics',
      title: '诊断测试',
      archived: false,
      createdAt: '2026-07-21T00:00:00.000Z',
      updatedAt: '2026-07-21T00:00:00.000Z',
      messages: [{
        id: 'assistant-diagnostics',
        role: 'assistant',
        content: '任务完成',
        createdAt: '2026-07-21T00:00:00.000Z',
        status: 'done',
        runId: 'run-diagnostics',
      }],
    })
    render(<App />)

    fireEvent.click(await screen.findByRole('button', { name: '诊断' }))

    expect(screen.getByRole('alertdialog')).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '下载诊断信息？' })).toBeInTheDocument()
    expect(screen.getByText('诊断信息可发送给开发者，用于排查问题。')).toBeInTheDocument()
    expect(document.querySelector('.diagnostics-preview')).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '取消' }))
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()
  })

  it('submits a user.ask choice without showing a success toast', async () => {
    const store = new LocalConversationStore('shejane-local:runtime:local-owner')
    window.localStorage.setItem('shejane.chatMode.v2', 'local:test:model')
    await store.save({
      id: 'conversation-question',
      title: '选择风格',
      archived: false,
      createdAt: '2026-07-21T00:00:00.000Z',
      updatedAt: '2026-07-21T00:00:00.000Z',
      messages: [{
        id: 'assistant-question',
        role: 'assistant',
        content: '',
        createdAt: '2026-07-21T00:00:00.000Z',
        status: 'waiting_input',
        runId: 'run-question',
        agentEvents: [{
          type: 'question.asked',
          label: '需要选择',
          questionRequestId: 'question-style',
          questions: [{
            question: '你想要什么风格？',
            header: '风格',
            options: [{ label: '简洁文字' }],
          }],
        }],
      }],
    })
    Object.defineProperty(window, 'shejaneClient', {
      configurable: true,
      value: {
        platform: 'darwin',
        runtime: { baseURL: 'http://127.0.0.1:17371', session: 'client', ready: true },
      },
    })
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/v1/models')) {
        return new Response(JSON.stringify({ models: [] }), {
          status: 200,
          headers: { 'content-type': 'application/json' },
        })
      }
      if (url.endsWith('/v1/commands') && init?.method === 'POST') {
        return new Response(JSON.stringify({
          type: 'question.answer',
          command_id: 'answer_question-style',
          question_id: 'question-style',
          run_id: 'run-question',
          answered: true,
          resumed: true,
        }), { status: 200, headers: { 'content-type': 'application/json' } })
      }
      if (url.includes('/v1/runs/run-question/stream')) {
        return new Response('data: [DONE]\n\n', {
          status: 200,
          headers: { 'content-type': 'text/event-stream' },
        })
      }
      throw new Error(`Unexpected request: ${url}`)
    }))
    render(<App />)

    fireEvent.click((await screen.findAllByText('选择风格'))[0])
    fireEvent.click(await screen.findByText('简洁文字'))

    await waitFor(() => expect(fetch).toHaveBeenCalledWith(
      'http://127.0.0.1:17371/v1/commands',
      expect.objectContaining({ method: 'POST' }),
    ))
    expect(screen.queryByText('答案已提交')).not.toBeInTheDocument()
  })

  it('keeps the sidebar expand control available outside the chat view', async () => {
    render(<App />)

    fireEvent.click(await screen.findByRole('button', { name: '设置' }))
    fireEvent.click(screen.getByRole('button', { name: '收起侧栏' }))

    expect(screen.getByRole('button', { name: '展开侧栏' })).toBeInTheDocument()
  })

  it('opens the model-service picker from the empty composer model state', async () => {
    render(<App />)

    const configureModels = await screen.findByRole('button', { name: '配置模型' })
    expect(configureModels).not.toHaveAttribute('aria-haspopup')
    fireEvent.click(configureModels)

    expect(await screen.findByRole('dialog')).toHaveTextContent('连接已有服务')
  })

  it('uses an available default model for old and new conversations', async () => {
    const store = new LocalConversationStore('shejane-local:runtime:local-owner')
    window.localStorage.setItem('shejane.chatMode.v2', 'local:removed:model')
    await store.save({
      id: 'conversation-with-removed-model',
      title: '旧对话',
      archived: false,
      createdAt: '2026-07-21T00:00:00.000Z',
      updatedAt: '2026-07-21T00:00:00.000Z',
      model: 'local:removed:model',
      messages: [],
    })
    Object.defineProperty(window, 'shejaneClient', {
      configurable: true,
      value: {
        platform: 'darwin',
        runtime: { baseURL: 'http://127.0.0.1:17371', session: 'client', ready: true },
      },
    })
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      if (String(input).endsWith('/v1/models')) {
        return new Response(JSON.stringify({
          models: [{
            spec: 'local:test:standard',
            model_id: 'standard',
            display_name: 'Standard Model',
            connection_id: 'test',
            service_name: 'Test',
            available: true,
            tool_calling: true,
            streaming: true,
            image_inputs: false,
            verification: 'verified',
            recommended: false,
          }, {
            spec: 'local:test:recommended',
            model_id: 'recommended',
            display_name: 'Recommended Model',
            connection_id: 'test',
            service_name: 'Test',
            available: true,
            tool_calling: true,
            streaming: true,
            image_inputs: false,
            verification: 'verified',
            recommended: true,
          }],
        }), { status: 200, headers: { 'content-type': 'application/json' } })
      }
      throw new Error('Runtime offline')
    }))

    render(<App />)

    expect(await screen.findByRole('button', { name: '选择模型' })).toHaveTextContent('Recommended Model')
    expect(window.localStorage.getItem('shejane.chatMode.v2')).toBe('local:test:recommended')
    fireEvent.click(screen.getByRole('button', { name: '新对话' }))
    expect(screen.getByRole('button', { name: '选择模型' })).toHaveTextContent('Recommended Model')
  })

  it('shows verified image models and creates the first Runtime binding explicitly', async () => {
    Object.defineProperty(window, 'shejaneClient', {
      configurable: true,
      value: {
        platform: 'darwin',
        runtime: { baseURL: 'http://127.0.0.1:17371', session: 'client', ready: true },
      },
    })
    const bindingBodies: Array<Record<string, unknown>> = []
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/v1/models')) {
        return new Response(JSON.stringify({
          models: [{
            spec: 'local:official:chat',
            model_id: 'chat',
            display_name: 'Chat Model',
            connection_id: 'official',
            service_name: 'SheJane 官方服务',
            capabilities: [{
              capability: 'agent_chat',
              protocol: 'openai_chat_completions',
              verification: 'verified',
            }],
            available: true,
            tool_calling: true,
            streaming: true,
            image_inputs: false,
            verification: 'verified',
            recommended: true,
          }, ...['gpt-image-2', 'gpt-image-2-vip'].map((modelID) => ({
            spec: `local:official:${modelID}`,
            model_id: modelID,
            display_name: modelID,
            connection_id: 'official',
            service_name: 'SheJane 官方服务',
            capabilities: [{
              capability: 'image_generation',
              protocol: 'openai_images_generations',
              verification: 'verified',
            }],
            available: false,
            tool_calling: false,
            streaming: false,
            image_inputs: false,
            verification: 'verified',
            recommended: modelID === 'gpt-image-2',
          }))],
        }), { status: 200, headers: { 'content-type': 'application/json' } })
      }
      if (url.endsWith('/v1/model-services')) {
        return new Response(JSON.stringify({ services: [{
          id: 'official',
          credential_configured: true,
        }] }), { status: 200, headers: { 'content-type': 'application/json' } })
      }
      if (url.endsWith('/v1/model-capability-bindings') && (!init?.method || init.method === 'GET')) {
        return new Response(JSON.stringify({ bindings: [] }), {
          status: 200,
          headers: { 'content-type': 'application/json' },
        })
      }
      if (url.endsWith('/v1/model-capability-bindings/image_generation') && init?.method === 'PUT') {
        bindingBodies.push(JSON.parse(String(init.body)))
        return new Response(JSON.stringify({
          capability: 'image_generation',
          model_spec: 'local:official:gpt-image-2-vip',
          status: 'ready',
        }), { status: 200, headers: { 'content-type': 'application/json' } })
      }
      throw new Error('Runtime offline')
    }))

    render(<App />)

    const trigger = await screen.findByRole('button', { name: '选择模型' })
    await waitFor(() => expect(trigger).toHaveTextContent('Chat Model'))
    expect(trigger).not.toHaveTextContent('· 图')
    trigger.focus()
    fireEvent.keyDown(trigger, { key: 'Enter', code: 'Enter' })
    fireEvent.click(await screen.findByText('gpt-image-2-vip'))

    await waitFor(() => expect(bindingBodies).toEqual([
      { model_spec: 'local:official:gpt-image-2-vip' },
    ]))
    expect(trigger).toHaveTextContent('Chat Model')
    expect(trigger).not.toHaveTextContent('gpt-image-2-vip')
  })

  it('asks before opening model settings when a message has no model', async () => {
    render(<App />)

    const editor = await screen.findByRole('textbox')
    editor.textContent = '你好'
    fireEvent.input(editor)
    fireEvent.keyDown(editor, { key: 'Enter', code: 'Enter' })

    expect(await screen.findByRole('alertdialog')).toHaveTextContent('需要先连接模型服务')
    expect(screen.queryByRole('heading', { name: '模型服务' })).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '前往设置' }))

    expect(await screen.findByRole('dialog')).toHaveTextContent('连接已有服务')
  })

  it('lets an unsent chat clear its selected workspace', async () => {
    const selectWorkspaceDirectory = vi.fn()
      .mockResolvedValueOnce('/tmp/client-a')
      .mockResolvedValueOnce('/tmp/client-b')
    Object.defineProperty(window, 'shejaneClient', {
      configurable: true,
      value: {
        platform: 'darwin',
        runtime: { baseURL: 'http://127.0.0.1:17371', session: 'client', ready: true },
        selectWorkspaceDirectory,
      },
    })
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).endsWith('/v1/workspaces') && init?.method === 'POST') {
        const path = JSON.parse(String(init.body)).path as string
        const label = path.split('/').pop() ?? path
        return new Response(JSON.stringify({
          id: `workspace-${label}`,
          path,
          label,
          created_at: '2026-07-14T00:00:00.000Z',
          last_used_at: '2026-07-14T00:00:00.000Z',
        }), { status: 200, headers: { 'content-type': 'application/json' } })
      }
      throw new Error('Runtime offline')
    }))
    render(<App />)

    fireEvent.click(await screen.findByRole('button', { name: '添加项目' }))
    fireEvent.click(await screen.findByRole('button', { name: '更换路径：client-a' }))
    fireEvent.click(await screen.findByRole('button', { name: '移除路径：client-b' }))

    expect(await screen.findByRole('button', { name: '添加项目' })).toBeInTheDocument()
    expect(selectWorkspaceDirectory).toHaveBeenCalledTimes(2)
  })

  it('clears workspace metadata from an existing chat', async () => {
    const store = new LocalConversationStore('shejane-local:runtime:local-owner')
    await store.save({
      id: 'conversation-1',
      title: '客户A',
      archived: false,
      createdAt: '2026-07-14T00:00:00.000Z',
      updatedAt: '2026-07-14T00:00:00.000Z',
      project: { name: '客户A' },
      workspace: { path: '/tmp/client-a', label: 'client-a', authorized: true },
      messages: [],
    })
    render(<App />)

    fireEvent.click(await screen.findByRole('button', { name: '移除路径：客户A' }))

    expect(await screen.findByRole('button', { name: '添加项目' })).toBeInTheDocument()
    expect((await store.get('conversation-1'))?.workspace).toBeUndefined()
    expect((await store.get('conversation-1'))?.project).toBeUndefined()
  })

  it('retries a workspace-blocked task after the user chooses a save location', async () => {
    const store = new LocalConversationStore('shejane-local:runtime:local-owner')
    window.localStorage.setItem('shejane.chatMode.v2', 'local:test:model')
    await store.save({
      id: 'conversation-workspace-recovery',
      title: '保存 HTML',
      archived: false,
      createdAt: '2026-07-17T00:00:00.000Z',
      updatedAt: '2026-07-17T00:00:00.000Z',
      messages: [
        {
          id: 'user-save-html',
          role: 'user',
          content: '把这个 HTML 保存下来',
          createdAt: '2026-07-17T00:00:00.000Z',
          status: 'done',
        },
        {
          id: 'assistant-workspace-required',
          role: 'assistant',
          content: 'Authorize a workspace before creating or changing files.',
          createdAt: '2026-07-17T00:00:01.000Z',
          status: 'error',
          runId: 'run-workspace-required',
          agentEvents: [
            {
              type: 'question.asked',
              label: '需要选择',
              questionRequestId: 'question-style',
              questions: [{
                question: '你想要什么风格？',
                header: '风格',
                options: [{ label: '经典数字/符号' }, { label: '简洁文字' }],
              }],
            },
            {
              type: 'question.answered',
              label: '已回答',
              questionRequestId: 'question-style',
              questionAnswers: { '你想要什么风格？': ['经典数字/符号'] },
            },
            {
              type: 'run.failed',
              label: 'Authorize a workspace before creating or changing files.',
              failureCategory: 'workspace',
              failureActionKind: 'user_action',
              failureRecoveryAction: 'workspace',
            },
          ],
        },
      ],
    })
    const runBodies: Array<Record<string, unknown>> = []
    Object.defineProperty(window, 'shejaneClient', {
      configurable: true,
      value: {
        platform: 'darwin',
        runtime: { baseURL: 'http://127.0.0.1:17371', session: 'client', ready: true },
        selectWorkspaceDirectory: vi.fn().mockResolvedValue('/Users/me/Client'),
      },
    })
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/v1/models') && (!init?.method || init.method === 'GET')) {
        return new Response(JSON.stringify({
          models: [{
            spec: 'local:test:model',
            model_id: 'model',
            display_name: 'Test Model',
            connection_id: 'test',
            service_name: 'Test',
            available: true,
            tool_calling: true,
            streaming: true,
            image_inputs: false,
            verification: 'verified',
            recommended: true,
          }],
        }), { status: 200, headers: { 'content-type': 'application/json' } })
      }
      if (url.endsWith('/v1/workspaces') && init?.method === 'POST') {
        return new Response(JSON.stringify({
          id: 'workspace-desktop',
          path: '/Users/me/Client',
          label: 'Client',
          created_at: '2026-07-17T00:00:00.000Z',
          last_used_at: '2026-07-17T00:00:00.000Z',
        }), { status: 200, headers: { 'content-type': 'application/json' } })
      }
      if (url.endsWith('/v1/runs') && init?.method === 'POST') {
        runBodies.push(JSON.parse(String(init.body)))
        throw new Error('stop after observing retry')
      }
      throw new Error('Runtime offline')
    }))
    render(<App />)

    await screen.findByText('Test Model')
    fireEvent.click((await screen.findAllByText('保存 HTML'))[0])
    expect(await screen.findByText('你想要什么风格？')).toBeInTheDocument()
    expect(await screen.findByText('经典数字/符号')).toBeInTheDocument()
    fireEvent.click(await screen.findByRole('button', { name: '选择保存位置' }))

    await waitFor(() => expect(runBodies.length).toBeGreaterThan(0))
    expect(runBodies[0]).toMatchObject({
      user_input: '把这个 HTML 保存下来',
      workspace_path: '/Users/me/Client',
      parent_run_id: 'run-workspace-required',
    })
    expect(runBodies[0]).not.toHaveProperty('replace_from_client_id')
    expect(screen.getAllByText('把这个 HTML 保存下来')).toHaveLength(1)
    expect(await screen.findByText('你想要什么风格？')).toBeInTheDocument()
    expect(await screen.findByText('经典数字/符号')).toBeInTheDocument()
    await waitFor(() => {
      expect(screen.queryByRole('button', { name: '选择保存位置' })).not.toBeInTheDocument()
    })
  })

  it('adds files from the native attachment picker', async () => {
    const selectAttachmentFiles = vi.fn().mockResolvedValue(['/tmp/brief.pdf'])
    Object.defineProperty(window, 'shejaneClient', {
      configurable: true,
      value: {
        platform: 'darwin',
        runtime: { baseURL: 'http://127.0.0.1:17371', session: 'client', ready: true },
        selectAttachmentFiles,
      },
    })
    render(<App />)

    fireEvent.click(await screen.findByRole('button', { name: '添加附件' }))

    expect(await screen.findByText('brief.pdf')).toBeInTheDocument()
    expect(selectAttachmentFiles).toHaveBeenCalledTimes(1)
  })

  it('adds a local file dropped onto the composer', async () => {
    const file = new File(['brief'], 'brief.pdf', { type: 'application/pdf' })
    const getPathForFile = vi.fn().mockReturnValue('/tmp/brief.pdf')
    Object.defineProperty(window, 'shejaneClient', {
      configurable: true,
      value: {
        platform: 'darwin',
        runtime: { baseURL: 'http://127.0.0.1:17371', session: 'client', ready: true },
        getPathForFile,
      },
    })
    render(<App />)
    const editor = screen.getByRole('textbox')

    fireEvent.drop(editor, {
      dataTransfer: {
        types: ['Files'],
        files: [file],
        getData: vi.fn().mockReturnValue(''),
        setData: vi.fn(),
      },
    })

    expect(await screen.findByText('brief.pdf')).toBeInTheDocument()
    expect(getPathForFile).toHaveBeenCalledWith(file)
  })
})
