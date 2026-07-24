import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest'
import { I18nProvider } from '@/shared/i18n/I18nProvider'
import { ModelProvidersSettings } from './ModelProvidersSettings'

const { discoverLocalModels, listLocalModelProviders, upsertLocalModelProvider } = vi.hoisted(() => ({
  discoverLocalModels: vi.fn(),
  listLocalModelProviders: vi.fn(),
  upsertLocalModelProvider: vi.fn(),
}))

vi.mock('@/runtime/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/runtime/client')>()),
  discoverLocalModels,
  listLocalModelProviders,
  upsertLocalModelProvider,
  deleteLocalModelProvider: vi.fn(),
}))

beforeAll(() => {
  Element.prototype.scrollIntoView = vi.fn()
})

describe('ModelProvidersSettings', () => {
  afterEach(() => {
    cleanup()
    vi.clearAllMocks()
  })

  it('offers OpenAI and Anthropic presets without local-server presets', async () => {
    listLocalModelProviders.mockResolvedValue([])
    upsertLocalModelProvider.mockResolvedValue({})
    render(
      <I18nProvider>
        <ModelProvidersSettings config={{ baseURL: 'http://127.0.0.1:17371', token: 'tok' }} />
      </I18nProvider>,
    )

    fireEvent.click(screen.getByRole('button', { name: '添加供应商' }))
    fireEvent.click(screen.getByRole('combobox', { name: '服务' }))

    const anthropic = await screen.findByRole('option', { name: 'Anthropic' })
    expect(anthropic).toBeInTheDocument()
    expect(screen.getByRole('option', { name: '自定义 OpenAI' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: '自定义 Anthropic' })).toBeInTheDocument()
    expect(screen.queryByRole('option', { name: 'Ollama' })).not.toBeInTheDocument()
    expect(screen.queryByRole('option', { name: 'LM Studio' })).not.toBeInTheDocument()

    fireEvent.click(anthropic)
    expect(screen.getByLabelText('API 地址')).toHaveValue('https://api.anthropic.com')
    fireEvent.change(screen.getByLabelText('API Key'), { target: { value: 'anthropic-secret' } })
    fireEvent.change(screen.getByRole('textbox', { name: '模型 ID 1' }), {
      target: { value: 'claude-sonnet-4-6' },
    })
    fireEvent.click(screen.getByRole('button', { name: '保存' }))

    await waitFor(() => expect(upsertLocalModelProvider).toHaveBeenCalledWith(
      'anthropic',
      expect.objectContaining({
        name: 'Anthropic',
        kind: 'anthropic',
        base_url: 'https://api.anthropic.com',
      }),
      expect.objectContaining({ token: 'tok' }),
    ))
  })

  it('uses a provider preset and keeps advanced fields out of the default flow', async () => {
    listLocalModelProviders.mockResolvedValue([])
    upsertLocalModelProvider.mockResolvedValue({})
    const onChanged = vi.fn()
    render(
      <I18nProvider>
        <ModelProvidersSettings
          config={{ baseURL: 'http://127.0.0.1:17371', token: 'tok' }}
          onChanged={onChanged}
        />
      </I18nProvider>,
    )

    fireEvent.click(screen.getByRole('button', { name: '添加供应商' }))

    expect(screen.queryByLabelText('最大输入 Token（可选）')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('模型 ID')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '获取模型' })).not.toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('API Key'), { target: { value: 'secret-key' } })
    fireEvent.change(screen.getByRole('textbox', { name: '模型 ID 1' }), { target: { value: 'gpt-4.1' } })
    fireEvent.click(screen.getByRole('button', { name: '添加模型' }))
    fireEvent.change(screen.getByRole('textbox', { name: '模型 ID 2' }), { target: { value: 'gpt-4o' } })
    expect(screen.getByRole('button', { name: '获取模型' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '保存' }))

    await waitFor(() => expect(upsertLocalModelProvider).toHaveBeenCalledWith(
      'openai',
      expect.objectContaining({
        name: 'OpenAI',
        base_url: 'https://api.openai.com/v1',
        api_key: 'secret-key',
        models: [
          expect.objectContaining({ model_id: 'gpt-4.1' }),
          expect.objectContaining({ model_id: 'gpt-4o' }),
        ],
      }),
      expect.objectContaining({ token: 'tok' }),
    ))
    expect(onChanged).toHaveBeenCalled()
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('discovers provider model names and saves multiple selected models', async () => {
    listLocalModelProviders.mockResolvedValue([])
    discoverLocalModels.mockResolvedValue([
      {
        model_id: 'openai/gpt-4.1',
        display_name: 'GPT-4.1',
        tool_calling: true,
        streaming: true,
        image_inputs: true,
        max_input_tokens: 1_000_000,
        max_output_tokens: 32_768,
      },
      {
        model_id: 'anthropic/claude-sonnet-4',
        display_name: 'Claude Sonnet 4',
        tool_calling: true,
        streaming: true,
        image_inputs: false,
        max_input_tokens: 200_000,
        max_output_tokens: 64_000,
      },
    ])
    upsertLocalModelProvider.mockResolvedValue({})
    render(
      <I18nProvider>
        <ModelProvidersSettings
          config={{ baseURL: 'http://127.0.0.1:17371', token: 'tok' }}
        />
      </I18nProvider>,
    )

    fireEvent.click(screen.getByRole('button', { name: '添加供应商' }))
    fireEvent.change(screen.getByLabelText('API Key'), { target: { value: 'secret-key' } })
    fireEvent.click(screen.getByRole('button', { name: '获取模型' }))

    await waitFor(() => expect(discoverLocalModels).toHaveBeenCalledWith(
      expect.objectContaining({
        provider_id: 'openai',
        base_url: 'https://api.openai.com/v1',
        api_key: 'secret-key',
      }),
      expect.objectContaining({ token: 'tok' }),
    ))
    await screen.findByRole('group', { name: '模型' })
    expect(screen.getByText('1M 上下文')).toBeInTheDocument()
    expect(screen.getAllByText('图片')).toHaveLength(1)
    expect(screen.getAllByText('工具')).toHaveLength(2)
    fireEvent.click(screen.getByRole('checkbox', { name: 'GPT-4.1 (openai/gpt-4.1)' }))
    fireEvent.click(screen.getByRole('checkbox', { name: 'Claude Sonnet 4 (anthropic/claude-sonnet-4)' }))
    expect(screen.getByText('已选择 2 个模型')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '保存' }))

    await waitFor(() => expect(upsertLocalModelProvider).toHaveBeenCalledWith(
      'openai',
      expect.objectContaining({
        models: [
          expect.objectContaining({
            model_id: 'openai/gpt-4.1',
            display_name: 'GPT-4.1',
            image_inputs: true,
            max_input_tokens: 1_000_000,
            max_output_tokens: 32_768,
          }),
          expect.objectContaining({
            model_id: 'anthropic/claude-sonnet-4',
            display_name: 'Claude Sonnet 4',
          }),
        ],
      }),
      expect.objectContaining({ token: 'tok' }),
    ))
  })
})
