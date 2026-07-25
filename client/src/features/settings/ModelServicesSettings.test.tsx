import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { I18nProvider } from '@/shared/i18n/I18nProvider'
import { ModelServicesSettings } from './ModelServicesSettings'

const api = vi.hoisted(() => ({
  addModelServiceModel: vi.fn(),
  connectModelService: vi.fn(),
  deleteModelService: vi.fn(),
  listModelServicePresets: vi.fn(),
  listModelServices: vi.fn(),
  reconnectModelService: vi.fn(),
  refreshModelService: vi.fn(),
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

describe('ModelServicesSettings', () => {
  afterEach(cleanup)

  beforeEach(() => {
    vi.clearAllMocks()
    api.listModelServicePresets.mockResolvedValue([deepseek])
    api.listModelServices.mockResolvedValue([])
    api.connectModelService.mockResolvedValue({})
    api.reconnectModelService.mockResolvedValue({})
  })

  it('connects an official service with an editable API address', async () => {
    render(
      <I18nProvider>
        <ModelServicesSettings config={config} />
      </I18nProvider>,
    )

    fireEvent.click(await screen.findByRole('button', { name: '添加模型服务' }))
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

    fireEvent.click(await screen.findByRole('button', { name: '添加模型服务' }))
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

    fireEvent.click(await screen.findByRole('button', { name: '添加模型服务' }))
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

    fireEvent.click(screen.getByRole('button', { name: '添加模型服务' }))

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

    expect(await screen.findByText('DeepSeek V4 Flash')).toBeInTheDocument()
    expect(screen.getByText('中国站 · 可用')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '打开 DeepSeek 控制台' })).toBeInTheDocument()
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

    fireEvent.click(await screen.findByRole('button', { name: '编辑 DeepSeek 连接' }))
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
})
