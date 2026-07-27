import 'fake-indexeddb/auto'
import { IDBFactory } from 'fake-indexeddb'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { App } from './App'
import { LocalConversationStore } from './shared/local-data/localConversations'

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
