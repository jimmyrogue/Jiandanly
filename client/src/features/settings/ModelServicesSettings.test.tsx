import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { I18nProvider } from '@/shared/i18n/I18nProvider'
import { ModelServicesSettings } from './ModelServicesSettings'

const api = vi.hoisted(() => ({
  addModelServiceModel: vi.fn(),
  connectModelService: vi.fn(),
  deleteModelService: vi.fn(),
  listModelCapabilityBindings: vi.fn(),
  listModelServicePresets: vi.fn(),
  listModelServices: vi.fn(),
  reconnectModelService: vi.fn(),
  refreshModelService: vi.fn(),
  setModelCapabilityBinding: vi.fn(),
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
  api_key_url: 'https://platform.deepseek.com/api_keys',
  billing_url: 'https://platform.deepseek.com/usage',
  regions: [{
    id: 'cn',
    name: '中国站',
    default: true,
    base_url: 'https://api.deepseek.com/v1',
  }],
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
    api.listModelServicePresets.mockResolvedValue([deepseek])
    api.listModelServices.mockResolvedValue([])
    api.listModelCapabilityBindings.mockResolvedValue([])
    api.connectModelService.mockResolvedValue({})
    api.reconnectModelService.mockResolvedValue({})
    api.verifyModelServiceModel.mockResolvedValue({})
    api.setModelCapabilityBinding.mockResolvedValue({})
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

    expect(await screen.findByRole('status')).toHaveTextContent('正在配置并验证模型兼容性')
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

    expect(await screen.findByText('DeepSeek V4 Flash、DeepSeek V4 Pro')).toBeInTheDocument()
    expect(screen.getByText('中国站 · 可用')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '选择模型' })).toBeInTheDocument()
    const actions = container.querySelector('.settings-model-service-actions')
    expect(actions?.querySelectorAll('button')).toHaveLength(2)
    expect(actions?.lastElementChild).toHaveAttribute('aria-label', 'DeepSeek 更多操作')
    const actionsTrigger = screen.getByRole('button', { name: 'DeepSeek 更多操作' })
    actionsTrigger.focus()
    fireEvent.keyDown(actionsTrigger, { key: 'Enter', code: 'Enter' })
    expect(await screen.findByRole('menuitem', { name: '打开 DeepSeek 控制台' })).toBeInTheDocument()
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
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

    fireEvent.click(await screen.findByRole('button', { name: '选择模型' }))
    fireEvent.click(screen.getByRole('checkbox', { name: '选择 GPT Image 1' }))
    fireEvent.click(screen.getByRole('combobox', { name: 'GPT Image 1 用途' }))
    fireEvent.click(screen.getByRole('option', { name: '图片生成' }))
    fireEvent.click(screen.getByRole('button', { name: '测试并启用 1 个模型' }))

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

    expect(await screen.findByRole('heading', { name: '选择要使用的模型' })).toBeInTheDocument()
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

    expect(await screen.findByText('尚未启用模型')).toBeInTheDocument()
    expect(screen.queryByText('gpt-image-2')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '选择模型' }))
    fireEvent.click(screen.getByRole('checkbox', { name: '选择 gpt-image-2' }))
    fireEvent.click(screen.getByRole('combobox', { name: 'gpt-image-2 用途' }))
    fireEvent.click(screen.getByRole('option', { name: '图片生成' }))
    fireEvent.click(screen.getByRole('button', { name: '测试并启用 1 个模型' }))

    await waitFor(() => expect(api.verifyModelServiceModel).toHaveBeenCalledWith(
      'conn_1',
      'gpt-image-2',
      { capability: 'image_generation', protocol: 'openai_images_generations' },
      config,
    ))
  })
})
