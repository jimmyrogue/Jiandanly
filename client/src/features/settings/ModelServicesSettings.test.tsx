import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { I18nProvider } from '@/shared/i18n/I18nProvider'
import { ModelServicesSettings } from './ModelServicesSettings'

const api = vi.hoisted(() => ({
  addModelServiceModel: vi.fn(),
  connectModelService: vi.fn(),
  deleteModelService: vi.fn(),
  getCentralDiagnostics: vi.fn(),
  getSheJaneAuthorization: vi.fn(),
  listModelCapabilityBindings: vi.fn(),
  listModelServicePresets: vi.fn(),
  listModelServices: vi.fn(),
  reconnectModelService: vi.fn(),
  refreshModelService: vi.fn(),
  setModelCapabilityBinding: vi.fn(),
  startSheJaneAuthorization: vi.fn(),
  updateCentralDiagnostics: vi.fn(),
  verifyModelServiceModel: vi.fn(),
}))

vi.mock('@/runtime/client', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/runtime/client')>(),
  ...api,
}))

const config = { baseURL: 'http://127.0.0.1:17371', token: 'tok' }
const deepseek = {
  id: 'deepseek',
  name: 'DeepSeek',
  description: '推理和通用任务',
  connection_method: 'api_key',
  api_key_url: 'https://platform.deepseek.com/api_keys',
  billing_url: 'https://platform.deepseek.com/usage',
  regions: [{
    id: 'cn',
    name: '中国站',
    default: true,
    base_url: 'https://api.deepseek.com/v1',
  }],
}

const shejaneOfficial = {
  id: 'shejane-official',
  name: 'SheJane 官方服务（推荐）',
  description: '登录 SheJane Cloud 使用官方托管的模型服务。',
  connection_method: 'browser_authorization',
  api_key_url: null,
  billing_url: null,
  regions: [],
}

const tuziConnection = {
  id: 'conn_1',
  preset_id: 'custom',
  name: '兔子',
  region: 'custom',
  adapter_id: 'openai_chat',
  base_url: 'https://api.tu-zi.com/v1',
  credential_configured: true,
  catalog_status: 'ready',
  models: [{
    model_id: 'gpt-5.6-luna',
    display_name: 'gpt-5.6-luna',
    capabilities: [],
    source: 'discovered',
    verification: 'unverified',
    recommended: false,
    tool_calling: false,
    streaming: false,
    image_inputs: false,
  }, {
    model_id: 'gpt-image-2',
    display_name: 'gpt-image-2',
    capabilities: [],
    source: 'discovered',
    verification: 'unverified',
    recommended: false,
    tool_calling: false,
    streaming: false,
    image_inputs: false,
  }],
  version: 1,
  created_at: 'now',
  updated_at: 'now',
}

describe('ModelServicesSettings', () => {
  afterEach(cleanup)

  beforeEach(() => {
    vi.clearAllMocks()
    delete window.shejaneClient
    api.listModelServicePresets.mockResolvedValue([deepseek])
    api.listModelServices.mockResolvedValue([])
    api.listModelCapabilityBindings.mockResolvedValue([])
    api.getCentralDiagnostics.mockResolvedValue({
      enabled: false,
      connection_id: null,
      success_sample_rate: 0,
      credential_configured: false,
    })
    api.connectModelService.mockResolvedValue({})
    api.reconnectModelService.mockResolvedValue({})
    api.verifyModelServiceModel.mockResolvedValue({})
    api.setModelCapabilityBinding.mockResolvedValue({})
  })

  it('opens the system browser and connects the official service without credential editing', async () => {
    const connection = {
      ...tuziConnection,
      id: `conn_${'a'.repeat(32)}`,
      preset_id: 'shejane-official',
      name: 'SheJane 官方服务（推荐）',
      region: 'official',
      base_url: 'https://cloud.example.test',
      models: [{
        ...tuziConnection.models[0],
        model_id: 'official-flash',
        display_name: 'official-flash',
        verification: 'unverified',
        recommended: true,
      }, {
        ...tuziConnection.models[0],
        model_id: 'official-pro',
        display_name: 'official-pro',
        verification: 'unverified',
      }],
    }
    api.listModelServicePresets.mockResolvedValue([shejaneOfficial, deepseek])
    api.listModelServices
      .mockResolvedValueOnce([])
      .mockResolvedValue([connection])
    api.startSheJaneAuthorization.mockResolvedValue({
      authorization_id: `auth_${'b'.repeat(32)}`,
      authorization_url: 'https://cloud.example.test/shejane/authorize?state=safe',
      expires_at: '2026-07-29T00:10:00Z',
    })
    api.getSheJaneAuthorization.mockResolvedValue({
      authorization_id: `auth_${'b'.repeat(32)}`,
      status: 'succeeded',
      connection,
      error_code: null,
    })
    api.updateCentralDiagnostics.mockResolvedValue({
      enabled: true,
      connection_id: connection.id,
      success_sample_rate: 0,
      credential_configured: true,
    })
    api.getCentralDiagnostics
      .mockResolvedValueOnce({
        enabled: false,
        connection_id: null,
        success_sample_rate: 0,
        credential_configured: false,
      })
      .mockResolvedValue({
        enabled: true,
        connection_id: connection.id,
        success_sample_rate: 0,
        credential_configured: true,
      })
    const openExternal = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(window, 'shejaneClient', {
      configurable: true,
      value: { openExternal },
    })

    render(
      <I18nProvider>
        <ModelServicesSettings config={config} />
      </I18nProvider>,
    )

    fireEvent.click(await screen.findByRole('button', { name: '连接已有服务' }))
    fireEvent.click(screen.getByRole('button', { name: /SheJane 官方服务/ }))

    await waitFor(() => expect(api.startSheJaneAuthorization).toHaveBeenCalledWith(config))
    expect(openExternal).toHaveBeenCalledWith(
      'https://cloud.example.test/shejane/authorize?state=safe',
    )
    await waitFor(() => expect(api.getSheJaneAuthorization).toHaveBeenCalledWith(
      `auth_${'b'.repeat(32)}`,
      config,
    ))
    expect(api.updateCentralDiagnostics).toHaveBeenCalledWith({
      enabled: true,
      connection_id: connection.id,
      success_sample_rate: 0,
    }, config)
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(screen.getByRole('switch', { name: '共享运行诊断' })).toBeChecked()
    expect(api.verifyModelServiceModel).not.toHaveBeenCalled()
    const modelInfo = screen.getByRole('button', {
      name: '查看 SheJane 官方服务（推荐） 的模型',
    })
    fireEvent.click(modelInfo)
    expect(await screen.findByText('official-flash')).toBeInTheDocument()
    expect(screen.getByText('official-pro')).toBeInTheDocument()
    expect(screen.getByText('共 2 个模型')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: '测试模型' }))
    expect(await screen.findByRole('heading', { name: '手动测试模型兼容性' })).toBeInTheDocument()
    expect(screen.getByRole('checkbox', { name: '选择 official-flash' })).toBeInTheDocument()
    expect(screen.queryByLabelText('API Key')).not.toBeInTheDocument()
  })

  it('keeps visible progress and preserves the connection if default diagnostics fail', async () => {
    const connection = {
      ...tuziConnection,
      id: `conn_${'a'.repeat(32)}`,
      preset_id: 'shejane-official',
      name: 'SheJane 官方服务（推荐）',
      region: 'official',
      base_url: 'https://cloud.example.test/v1',
    }
    let finishAuthorization!: () => void
    let finishReload!: () => void
    let serviceLoadCount = 0
    api.listModelServicePresets.mockResolvedValue([shejaneOfficial])
    api.listModelServices.mockImplementation(() => {
      serviceLoadCount += 1
      if (serviceLoadCount === 1) return Promise.resolve([])
      return new Promise((resolve) => {
        finishReload = () => resolve([connection])
      })
    })
    api.startSheJaneAuthorization.mockResolvedValue({
      authorization_id: `auth_${'b'.repeat(32)}`,
      authorization_url: 'https://cloud.example.test/shejane/authorize?state=safe',
      expires_at: '2026-07-29T00:10:00Z',
    })
    api.getSheJaneAuthorization.mockImplementation(() => new Promise((resolve) => {
      finishAuthorization = () => resolve({
        authorization_id: `auth_${'b'.repeat(32)}`,
        status: 'succeeded',
        connection,
        error_code: null,
      })
    }))
    api.updateCentralDiagnostics.mockRejectedValue(new Error('diagnostics unavailable'))
    Object.defineProperty(window, 'shejaneClient', {
      configurable: true,
      value: { openExternal: vi.fn().mockResolvedValue(undefined) },
    })

    render(
      <I18nProvider>
        <ModelServicesSettings config={config} />
      </I18nProvider>,
    )

    fireEvent.click(await screen.findByRole('button', { name: '连接已有服务' }))
    fireEvent.click(screen.getByRole('button', { name: /SheJane 官方服务/ }))

    const progress = await screen.findByRole('status')
    expect(progress).toHaveAttribute('aria-busy', 'true')
    expect(progress).toHaveTextContent('正在等待浏览器授权，完成后将自动连接…')

    finishAuthorization()
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('授权成功，正在同步模型…'))

    finishReload()
    expect(await screen.findByText('官方服务 · 可用')).toBeInTheDocument()
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(screen.getByRole('alert')).toHaveTextContent('官方服务已连接，但运行诊断未能自动开启，你可以稍后重试。')
  })

  it.each([
    ['denied', '你已取消授权。'],
    ['expired', '授权已超时，请重试。'],
    ['failed', '授权失败，请重试。'],
  ] as const)('shows a retry action when official authorization is %s', async (status, message) => {
    api.listModelServicePresets.mockResolvedValue([shejaneOfficial])
    api.startSheJaneAuthorization.mockResolvedValue({
      authorization_id: `auth_${'b'.repeat(32)}`,
      authorization_url: 'https://cloud.example.test/shejane/authorize?state=safe',
      expires_at: '2026-07-29T00:10:00Z',
    })
    api.getSheJaneAuthorization.mockResolvedValue({
      authorization_id: `auth_${'b'.repeat(32)}`,
      status,
      connection: null,
      error_code: status,
    })
    Object.defineProperty(window, 'shejaneClient', {
      configurable: true,
      value: { openExternal: vi.fn().mockResolvedValue(undefined) },
    })

    render(
      <I18nProvider>
        <ModelServicesSettings config={config} />
      </I18nProvider>,
    )

    fireEvent.click(await screen.findByRole('button', { name: '连接已有服务' }))
    fireEvent.click(screen.getByRole('button', { name: /SheJane 官方服务/ }))

    expect(await screen.findByRole('alert')).toHaveTextContent(message)
    expect(screen.queryByText('授权未完成，可以重新打开浏览器。')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '重新授权' })).toBeInTheDocument()
  })

  it('does not offer API-key replacement for a managed official connection', async () => {
    api.listModelServicePresets.mockResolvedValue([shejaneOfficial])
    api.listModelServices.mockResolvedValue([{
      ...tuziConnection,
      id: `conn_${'a'.repeat(32)}`,
      preset_id: 'shejane-official',
      name: 'SheJane 官方服务（推荐）',
      region: 'official',
      base_url: 'https://cloud.example.test',
    }])

    render(
      <I18nProvider>
        <ModelServicesSettings config={config} />
      </I18nProvider>,
    )

    expect(await screen.findByText('官方服务 · 可用')).toBeInTheDocument()
    const trigger = screen.getByRole('button', { name: 'SheJane 官方服务（推荐） 更多操作' })
    trigger.focus()
    fireEvent.keyDown(trigger, { key: 'Enter', code: 'Enter' })

    expect(screen.queryByRole('menuitem', { name: /编辑 SheJane 官方服务/ })).not.toBeInTheDocument()
    expect(await screen.findByRole('menuitem', { name: '刷新模型' })).toBeInTheDocument()
  })

  it('lets users re-enable metadata-only diagnostics after opting out', async () => {
    const connectionID = `conn_${'a'.repeat(32)}`
    api.listModelServicePresets.mockResolvedValue([shejaneOfficial])
    api.listModelServices.mockResolvedValue([{
      ...tuziConnection,
      id: connectionID,
      preset_id: 'shejane-official',
      name: 'SheJane 官方服务（推荐）',
      region: 'official',
      base_url: 'https://cloud.example.test',
    }])
    api.updateCentralDiagnostics.mockResolvedValue({
      enabled: true,
      connection_id: connectionID,
      success_sample_rate: 0,
      credential_configured: true,
    })

    render(
      <I18nProvider>
        <ModelServicesSettings config={config} />
      </I18nProvider>,
    )

    const consent = await screen.findByRole('switch', { name: '共享运行诊断' })
    expect(consent).not.toBeChecked()
    expect(consent).toHaveAccessibleDescription('默认开启，可随时关闭。仅上传失败状态、耗时、Token 数和工具名称；不上传提示词、输出或本地文件内容。')
    expect(screen.getByText('默认开启，可随时关闭。仅上传失败状态、耗时、Token 数和工具名称；不上传提示词、输出或本地文件内容。')).toBeInTheDocument()
    expect(api.updateCentralDiagnostics).not.toHaveBeenCalled()

    fireEvent.click(consent)

    await waitFor(() => expect(api.updateCentralDiagnostics).toHaveBeenCalledWith(
      {
        enabled: true,
        connection_id: connectionID,
        success_sample_rate: 0,
      },
      config,
    ))
    expect(consent).toBeChecked()
    expect(document.body.textContent).not.toContain('st-')
  })

  it('keeps BYOK services usable when diagnostics status is unavailable', async () => {
    api.listModelServices.mockResolvedValue([tuziConnection])
    api.getCentralDiagnostics.mockRejectedValue(new Error('diagnostics unavailable'))

    render(
      <I18nProvider>
        <ModelServicesSettings config={config} />
      </I18nProvider>,
    )

    expect(await screen.findByText('兔子')).toBeInTheDocument()
    expect(screen.queryByText('diagnostics unavailable')).not.toBeInTheDocument()
  })

  it('connects an official service with an editable API address', async () => {
    render(
      <I18nProvider>
        <ModelServicesSettings config={config} />
      </I18nProvider>,
    )

    fireEvent.click(await screen.findByRole('button', { name: '连接已有服务' }))
    fireEvent.click(screen.getByRole('button', { name: /DeepSeek/ }))
    expect(screen.getByLabelText('API 地址')).toHaveValue('')
    expect(screen.getByLabelText('API 地址')).toHaveAttribute(
      'placeholder',
      'https://api.deepseek.com/v1',
    )
    expect(screen.getByRole('button', { name: '获取 API Key' })).toHaveAttribute('data-size', 'lg')
    expect(screen.getByRole('button', { name: '连接' })).toHaveClass('h-11')
    fireEvent.change(screen.getByLabelText('API 地址'), {
      target: { value: 'https://gateway.example/v1' },
    })
    fireEvent.change(screen.getByLabelText('API Key'), { target: { value: 'secret' } })
    fireEvent.click(screen.getByRole('button', { name: '连接' }))

    await waitFor(() => expect(api.connectModelService).toHaveBeenCalledWith(
      {
        preset_id: 'deepseek',
        region: 'cn',
        api_key: 'secret',
        base_url: 'https://gateway.example/v1',
      },
      config,
    ))
  })

  it('shows persistent configuration progress while the service is being verified', async () => {
    let finishConnect!: () => void
    api.connectModelService.mockImplementation(() => new Promise((resolve) => {
      finishConnect = () => resolve({})
    }))
    render(
      <I18nProvider>
        <ModelServicesSettings config={config} />
      </I18nProvider>,
    )

    fireEvent.click(await screen.findByRole('button', { name: '连接已有服务' }))
    fireEvent.click(screen.getByRole('button', { name: /DeepSeek/ }))
    fireEvent.change(screen.getByLabelText('API Key'), { target: { value: 'secret' } })
    fireEvent.click(screen.getByRole('button', { name: '连接' }))

    expect(await screen.findByRole('status')).toHaveTextContent('正在连接服务并读取模型列表')
    expect(screen.getByRole('button', { name: '配置中…' })).toHaveAttribute('aria-busy', 'true')

    finishConnect()
    await waitFor(() => expect(screen.queryByRole('status')).not.toBeInTheDocument())
  })

  it('uses the first official API address as the visible and submitted fallback', async () => {
    api.listModelServicePresets.mockResolvedValue([{
      ...deepseek,
      regions: [{ ...deepseek.regions[0], default: false }],
    }])

    render(
      <I18nProvider>
        <ModelServicesSettings config={config} />
      </I18nProvider>,
    )

    fireEvent.click(await screen.findByRole('button', { name: '连接已有服务' }))
    fireEvent.click(screen.getByRole('button', { name: /DeepSeek/ }))

    expect(screen.getByLabelText('API 地址')).toHaveValue('')
    expect(screen.getByLabelText('API 地址')).toHaveAttribute(
      'placeholder',
      'https://api.deepseek.com/v1',
    )
    fireEvent.change(screen.getByLabelText('API Key'), { target: { value: 'secret' } })
    fireEvent.click(screen.getByRole('button', { name: '连接' }))

    await waitFor(() => expect(api.connectModelService).toHaveBeenCalledWith(
      {
        preset_id: 'deepseek',
        region: 'cn',
        api_key: 'secret',
        base_url: 'https://api.deepseek.com/v1',
      },
      config,
    ))
  })

  it('uses localized validation and can return to the service picker', async () => {
    render(
      <I18nProvider>
        <ModelServicesSettings config={config} />
      </I18nProvider>,
    )

    fireEvent.click(await screen.findByRole('button', { name: '连接已有服务' }))
    fireEvent.click(screen.getByRole('button', { name: /DeepSeek/ }))
    fireEvent.click(screen.getByRole('button', { name: '连接' }))

    expect(screen.getByRole('alert')).toHaveTextContent('请输入 API Key')
    expect(api.connectModelService).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: '返回选择模型服务' }))
    expect(screen.getByRole('button', { name: /DeepSeek/ })).toBeInTheDocument()
    expect(screen.queryByLabelText('API Key')).not.toBeInTheDocument()
  })

  it('keeps the service catalog behind the add button', async () => {
    const { container } = render(
      <I18nProvider>
        <ModelServicesSettings config={config} />
      </I18nProvider>,
    )

    await waitFor(() => expect(api.listModelServicePresets).toHaveBeenCalled())
    expect(screen.queryByRole('button', { name: /DeepSeek/ })).not.toBeInTheDocument()
    expect(container.querySelector('.settings-card')).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '连接已有服务' }))

    expect(screen.getByRole('dialog')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /DeepSeek/ })).toBeInTheDocument()
    expect(screen.queryByText('选择要连接的服务。')).not.toBeInTheDocument()
  })

  it('renders cached connections without opening an editor on row click', async () => {
    api.listModelServices.mockResolvedValue([{
      id: 'conn_1',
      preset_id: 'deepseek',
      name: 'DeepSeek',
      region: 'cn',
      adapter_id: 'openai_chat',
      base_url: 'https://api.deepseek.com/v1',
      credential_configured: true,
      catalog_status: 'ready',
      models: [{
        model_id: 'deepseek-v4-flash',
        display_name: 'DeepSeek V4 Flash',
        source: 'bundled',
        verification: 'verified',
        recommended: true,
        tool_calling: true,
        streaming: true,
        image_inputs: false,
        capabilities: [{
          capability: 'agent_chat',
          protocol: 'openai_chat_completions',
          verification: 'verified',
        }],
      }, {
        model_id: 'deepseek-v4-pro',
        display_name: 'DeepSeek V4 Pro',
        source: 'bundled',
        verification: 'verified',
        recommended: false,
        tool_calling: true,
        streaming: true,
        image_inputs: false,
        capabilities: [{
          capability: 'agent_chat',
          protocol: 'openai_chat_completions',
          verification: 'verified',
        }],
      }],
      version: 1,
      created_at: 'now',
      updated_at: 'now',
    }])

    const { container } = render(
      <I18nProvider>
        <ModelServicesSettings config={config} />
      </I18nProvider>,
    )

    const modelInfo = await screen.findByRole('button', { name: '查看 DeepSeek 的模型' })
    expect(screen.queryByText('DeepSeek V4 Flash')).not.toBeInTheDocument()
    fireEvent.click(modelInfo)
    expect(await screen.findByText('DeepSeek V4 Flash')).toBeInTheDocument()
    expect(screen.getByText('DeepSeek V4 Pro')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(screen.getByText('中国站 · 可用')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '测试模型' })).toBeInTheDocument()
    const actions = container.querySelector('.settings-model-service-actions')
    expect(actions?.querySelectorAll('button')).toHaveLength(2)
    expect(actions?.lastElementChild).toHaveAttribute('aria-label', 'DeepSeek 更多操作')
    const actionsTrigger = screen.getByRole('button', { name: 'DeepSeek 更多操作' })
    actionsTrigger.focus()
    fireEvent.keyDown(actionsTrigger, { key: 'Enter', code: 'Enter' })
    expect(await screen.findByRole('menuitem', { name: '打开 DeepSeek 控制台' })).toBeInTheDocument()
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('offers manual compatibility testing without running it automatically', async () => {
    api.listModelServices.mockResolvedValue([tuziConnection])

    render(
      <I18nProvider>
        <ModelServicesSettings config={config} />
      </I18nProvider>,
    )

    fireEvent.click(await screen.findByRole('button', { name: '测试模型' }))
    expect(screen.getByText('连接成功后，Agent 对话模型即可使用；请选择需要手动测试兼容性的模型。')).toBeInTheDocument()
    expect(api.verifyModelServiceModel).not.toHaveBeenCalled()
  })

  it('shows persistent progress on the service card while models refresh', async () => {
    let finishRefresh!: () => void
    api.listModelServices.mockResolvedValue([tuziConnection])
    api.refreshModelService.mockImplementation(() => new Promise((resolve) => {
      finishRefresh = () => resolve(tuziConnection)
    }))

    const { container } = render(
      <I18nProvider>
        <ModelServicesSettings config={config} />
      </I18nProvider>,
    )

    const actionsTrigger = await screen.findByRole('button', { name: '兔子 更多操作' })
    actionsTrigger.focus()
    fireEvent.keyDown(actionsTrigger, { key: 'Enter', code: 'Enter' })
    fireEvent.click(await screen.findByRole('menuitem', { name: '刷新模型' }))

    expect(screen.queryByRole('menuitem', { name: '刷新模型' })).not.toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveTextContent('正在刷新模型…')
    expect(actionsTrigger).toHaveAttribute('aria-busy', 'true')
    expect(container.querySelectorAll('.settings-model-service .animate-spin')).toHaveLength(1)

    finishRefresh()
    await waitFor(() => expect(screen.queryByRole('status')).not.toBeInTheDocument())
    expect(screen.getByText('已有服务 · 可用')).toBeInTheDocument()
  })

  it('reconnects an existing service by replacing only its API key', async () => {
    api.listModelServices.mockResolvedValue([{
      id: 'conn_1',
      preset_id: 'deepseek',
      name: 'DeepSeek',
      region: 'cn',
      adapter_id: 'openai_chat',
      base_url: 'https://api.deepseek.com/v1',
      credential_configured: false,
      catalog_status: 'ready',
      models: [],
      version: 1,
      created_at: 'now',
      updated_at: 'now',
    }])

    render(
      <I18nProvider>
        <ModelServicesSettings config={config} />
      </I18nProvider>,
    )

    const actionsTrigger = await screen.findByRole('button', { name: 'DeepSeek 更多操作' })
    actionsTrigger.focus()
    fireEvent.keyDown(actionsTrigger, { key: 'Enter', code: 'Enter' })
    fireEvent.click(await screen.findByRole('menuitem', { name: '编辑 DeepSeek 连接' }))
    expect(screen.getByText('需要 API Key')).toBeInTheDocument()
    expect(screen.getByLabelText('API 地址')).toHaveValue('https://api.deepseek.com/v1')
    fireEvent.change(screen.getByLabelText('API 地址'), {
      target: { value: 'https://gateway.example/v1' },
    })
    fireEvent.change(screen.getByLabelText('API Key'), { target: { value: 'new-secret' } })
    fireEvent.click(screen.getByRole('button', { name: '更新连接' }))

    await waitFor(() => expect(api.reconnectModelService).toHaveBeenCalledWith(
      'conn_1',
      { api_key: 'new-secret', base_url: 'https://gateway.example/v1' },
      config,
    ))
  })

  it.each(['discovered', 'bundled'] as const)(
    'lets users verify a %s model for its selected use',
    async (source) => {
    api.listModelServices.mockResolvedValue([{
      id: 'conn_1',
      preset_id: 'custom',
      name: 'Tuzi',
      region: 'custom',
      adapter_id: 'openai_chat',
      base_url: 'https://api.tu-zi.com/v1',
      credential_configured: true,
      catalog_status: 'ready',
      models: [{
        model_id: 'gpt-image-1',
        display_name: 'GPT Image 1',
        capabilities: [],
        source,
        verification: 'unverified',
        recommended: false,
        tool_calling: false,
        streaming: false,
        image_inputs: false,
      }],
      version: 1,
      created_at: 'now',
      updated_at: 'now',
    }])

    render(
      <I18nProvider>
        <ModelServicesSettings config={config} />
      </I18nProvider>,
    )

    fireEvent.click(await screen.findByRole('button', { name: '测试模型' }))
    fireEvent.click(screen.getByRole('checkbox', { name: '选择 GPT Image 1' }))
    fireEvent.click(screen.getByRole('combobox', { name: 'GPT Image 1 用途' }))
    fireEvent.click(screen.getByRole('option', { name: '图片生成' }))
    fireEvent.click(screen.getByRole('button', { name: '测试 1 个模型' }))

    await waitFor(() => expect(api.verifyModelServiceModel).toHaveBeenCalledWith(
      'conn_1',
      'gpt-image-1',
      { capability: 'image_generation', protocol: 'openai_images_generations' },
      config,
    ))
    await waitFor(() => expect(api.setModelCapabilityBinding).toHaveBeenCalledWith(
      'image_generation',
      { model_spec: 'local:conn_1:gpt-image-1' },
      config,
    ))
    },
  )

  it('opens a searchable model picker after connecting an existing service', async () => {
    api.listModelServicePresets.mockResolvedValue([{
      id: 'custom',
      name: '已有服务',
      description: '兼容服务',
      regions: [],
    }])
    api.connectModelService.mockResolvedValue(tuziConnection)
    api.listModelServices
      .mockResolvedValueOnce([])
      .mockResolvedValueOnce([tuziConnection])

    render(
      <I18nProvider>
        <ModelServicesSettings config={config} />
      </I18nProvider>,
    )

    fireEvent.click(await screen.findByRole('button', { name: '连接已有服务' }))
    fireEvent.click(screen.getByRole('button', { name: /已有服务/ }))
    fireEvent.change(screen.getByLabelText('服务名称'), { target: { value: '兔子' } })
    fireEvent.change(screen.getByLabelText('API 地址'), { target: { value: 'https://api.tu-zi.com/v1' } })
    fireEvent.change(screen.getByLabelText('API Key'), { target: { value: 'secret' } })
    fireEvent.click(screen.getByRole('button', { name: '连接' }))

    expect(await screen.findByRole('heading', { name: '手动测试模型兼容性' })).toBeInTheDocument()
    const search = screen.getByRole('searchbox', { name: '筛选模型' })
    fireEvent.change(search, { target: { value: 'image' } })
    expect(screen.getByText('gpt-image-2')).toBeInTheDocument()
    expect(screen.queryByText('gpt-5.6-luna')).not.toBeInTheDocument()
  })

  it('keeps the catalog in the picker and tests only selected models', async () => {
    api.listModelServices.mockResolvedValue([tuziConnection])

    render(
      <I18nProvider>
        <ModelServicesSettings config={config} />
      </I18nProvider>,
    )

    fireEvent.click(await screen.findByRole('button', { name: '查看 兔子 的模型' }))
    expect(await screen.findByText('共 2 个模型')).toBeInTheDocument()
    expect(screen.getByText('gpt-image-2')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: '测试模型' }))
    fireEvent.click(screen.getByRole('checkbox', { name: '选择 gpt-image-2' }))
    fireEvent.click(screen.getByRole('combobox', { name: 'gpt-image-2 用途' }))
    fireEvent.click(screen.getByRole('option', { name: '图片生成' }))
    fireEvent.click(screen.getByRole('button', { name: '测试 1 个模型' }))

    await waitFor(() => expect(api.verifyModelServiceModel).toHaveBeenCalledWith(
      'conn_1',
      'gpt-image-2',
      { capability: 'image_generation', protocol: 'openai_images_generations' },
      config,
    ))
  })
})
