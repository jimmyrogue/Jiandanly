import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { TooltipProvider } from '@/components/ui/tooltip'
import { I18nProvider } from '@/shared/i18n/I18nProvider'
import type { ChatMode } from '@/shared/local-data/types'
import { readReasoningMode, writeReasoningMode } from '@/features/app/appStorage'
import { ModeSelector, type ModelOption } from './ModeSelector'

const MODELS: ModelOption[] = [
  { id: 'local:openai:gpt-4o', label: 'GPT-4o', vendor: 'OpenAI', imageInputs: true, recommended: true },
  { id: 'local:ollama:qwen3', label: 'Qwen 3', vendor: 'Ollama', imageInputs: false },
]

const IMAGE_MODELS: ModelOption[] = [
  { id: 'local:official:gpt-image-2', label: 'gpt-image-2', vendor: 'SheJane 官方服务', imageInputs: false },
  { id: 'local:official:gpt-image-2-vip', label: 'gpt-image-2-vip', vendor: 'SheJane 官方服务', imageInputs: false },
]

function withProviders(children: ReactNode) {
  return <I18nProvider><TooltipProvider>{children}</TooltipProvider></I18nProvider>
}

function renderSelector(mode: ChatMode, onChange = vi.fn()) {
  render(withProviders(<ModeSelector mode={mode} models={MODELS} onChange={onChange} />))
  return onChange
}

function openMenu() {
  const trigger = screen.getByRole('button', { name: '选择模型' })
  trigger.focus()
  fireEvent.keyDown(trigger, { key: 'Enter', code: 'Enter' })
}

describe('ModeSelector (Runtime catalog)', () => {
  afterEach(() => {
    cleanup()
    localStorage.clear()
  })

  it('shows the selected Runtime model', () => {
    renderSelector('local:openai:gpt-4o')
    expect(screen.getByRole('button', { name: '选择模型' })).toHaveTextContent('GPT-4o')
  })

  it('shows and switches the Runtime-owned image model binding', async () => {
    const onImageModeChange = vi.fn()
    render(withProviders(
      <ModeSelector
        mode="local:openai:gpt-4o"
        models={MODELS}
        onChange={vi.fn()}
        imageMode="local:official:gpt-image-2"
        imageModels={IMAGE_MODELS}
        onImageModeChange={onImageModeChange}
      />,
    ))

    const trigger = screen.getByRole('button', { name: '选择模型' })
    expect(trigger).toHaveTextContent('GPT-4o')
    expect(trigger).not.toHaveTextContent('gpt-image-2')
    openMenu()
    expect(await screen.findByText('图片生成模型')).toBeInTheDocument()
    fireEvent.click(screen.getByText('gpt-image-2-vip'))
    expect(onImageModeChange).toHaveBeenCalledWith('local:official:gpt-image-2-vip')
  })

  it('shows a model-selection prompt for a stale selection', () => {
    renderSelector('local:removed:model')
    expect(screen.getByRole('button', { name: '选择模型' })).toHaveTextContent('选择具体模型')
  })

  it('lists concrete Runtime models directly', async () => {
    renderSelector('local:openai:gpt-4o')
    openMenu()
    expect((await screen.findAllByText('GPT-4o')).length).toBeGreaterThanOrEqual(2)
    expect(screen.getByText('Qwen 3')).toBeInTheDocument()
    expect(screen.getByText('支持图片')).toBeInTheDocument()
    expect(screen.getByText('仅文本')).toBeInTheDocument()
    expect(screen.queryByText('自动')).not.toBeInTheDocument()
  })

  it('selects a concrete model', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    const onChange = renderSelector('local:openai:gpt-4o')
    openMenu()
    fireEvent.click(await screen.findByText('Qwen 3'))
    expect(onChange).toHaveBeenCalledWith('local:ollama:qwen3')
    expect(window.confirm).toHaveBeenCalledOnce()
  })

  it('keeps provider groups without an extra more-models heading', async () => {
    renderSelector('local:openai:gpt-4o')
    openMenu()
    expect(await screen.findByText('OpenAI')).toBeInTheDocument()
    expect(screen.getByText('Ollama')).toBeInTheDocument()
    expect(screen.queryByText('更多模型')).not.toBeInTheDocument()
  })

  it('keeps recommended and additional models from one vendor together', async () => {
    render(withProviders(
      <ModeSelector
        mode="local:deepseek:deepseek-v4-flash"
        models={[
          {
            id: 'local:deepseek:deepseek-v4-flash',
            label: 'DeepSeek V4 Flash',
            vendor: 'DeepSeek',
            imageInputs: false,
            recommended: true,
          },
          {
            id: 'local:deepseek:deepseek-v4-pro',
            label: 'DeepSeek V4 Pro',
            vendor: 'DeepSeek',
            imageInputs: false,
          },
        ]}
        onChange={vi.fn()}
      />,
    ))
    openMenu()

    expect(await screen.findByText('DeepSeek V4 Pro')).toBeInTheDocument()
    expect(screen.getAllByText('DeepSeek')).toHaveLength(1)
  })

  it('refreshes only when the model menu opens', () => {
    const onRefreshCurrent = vi.fn()
    render(withProviders(
      <ModeSelector
        mode="local:openai:gpt-4o"
        models={MODELS}
        onChange={vi.fn()}
        onRefreshCurrent={onRefreshCurrent}
      />,
    ))
    openMenu()
    expect(onRefreshCurrent).toHaveBeenCalledOnce()
  })

  it('keeps thinking effort outside the model menu and persists the selection', async () => {
    render(withProviders(
      <ModeSelector
        mode="local:deepseek:deepseek-v4-flash"
        models={[{
          id: 'local:deepseek:deepseek-v4-flash',
          label: 'DeepSeek V4 Flash',
          vendor: 'DeepSeek',
          imageInputs: false,
          reasoningModes: ['off', 'high', 'max'],
          defaultReasoningMode: 'off',
        }]}
        onChange={vi.fn()}
      />,
    ))

    expect(screen.getByRole('button', { name: '思考强度：快速' })).toHaveTextContent('快速')
    openMenu()
    expect(screen.queryByRole('menuitem', { name: '高' })).not.toBeInTheDocument()
    fireEvent.keyDown(document.activeElement ?? document.body, { key: 'Escape', code: 'Escape' })

    const reasoningTrigger = screen.getByRole('button', { name: '思考强度：快速' })
    reasoningTrigger.focus()
    fireEvent.keyDown(reasoningTrigger, { key: 'Enter', code: 'Enter' })
    fireEvent.click(await screen.findByRole('menuitem', { name: '高' }))

    expect(screen.getByRole('button', { name: '思考强度：高' })).toHaveTextContent('高')
    expect(readReasoningMode()).toBe('high')
  })

  it('uses the model default when a fixed reasoning alias is selected', async () => {
    writeReasoningMode('off')
    render(withProviders(
      <ModeSelector
        mode="local:official:deepseek-v4-flash-max"
        models={[{
          id: 'local:official:deepseek-v4-flash-max',
          label: 'DeepSeek V4 Flash Max',
          vendor: 'DeepSeek',
          imageInputs: false,
          reasoningModes: ['max'],
          defaultReasoningMode: 'max',
        }]}
        onChange={vi.fn()}
      />,
    ))

    await waitFor(() => expect(readReasoningMode()).toBe('max'))
    expect(screen.getByRole('button', { name: '思考强度：极高' })).toHaveTextContent('极高')
  })

})
