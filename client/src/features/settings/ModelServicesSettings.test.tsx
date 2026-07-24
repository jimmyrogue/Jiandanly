import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
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
  regions: [{ id: 'cn', name: '中国站', default: true }],
}

describe('ModelServicesSettings', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    api.listModelServicePresets.mockResolvedValue([deepseek])
    api.listModelServices.mockResolvedValue([])
    api.connectModelService.mockResolvedValue({})
    api.reconnectModelService.mockResolvedValue({})
  })

  it('connects an official service with only an API key', async () => {
    render(
      <I18nProvider>
        <ModelServicesSettings config={config} />
      </I18nProvider>,
    )

    fireEvent.click(await screen.findByRole('button', { name: /DeepSeek/ }))
    fireEvent.change(screen.getByLabelText('API Key'), { target: { value: 'secret' } })
    fireEvent.click(screen.getByRole('button', { name: '连接' }))

    await waitFor(() => expect(api.connectModelService).toHaveBeenCalledWith(
      { preset_id: 'deepseek', region: 'cn', api_key: 'secret' },
      config,
    ))
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

    fireEvent.click(await screen.findByRole('button', { name: '更新 DeepSeek 的 API Key' }))
    expect(screen.getByText('需要 API Key')).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('API Key'), { target: { value: 'new-secret' } })
    fireEvent.click(screen.getByRole('button', { name: '更新连接' }))

    await waitFor(() => expect(api.reconnectModelService).toHaveBeenCalledWith(
      'conn_1',
      { api_key: 'new-secret' },
      config,
    ))
  })
})
