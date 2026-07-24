import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import type { ReactNode } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { TooltipProvider } from '@/components/ui/tooltip'
import { I18nProvider } from '@/shared/i18n/I18nProvider'
import type { ChatMode } from '@/shared/local-data/types'
import { ModeSelector, type ModelOption } from './ModeSelector'

const MODELS: ModelOption[] = [
  { id: 'local:openai:gpt-4o', label: 'GPT-4o', vendor: 'OpenAI', imageInputs: true, recommended: true },
  { id: 'local:ollama:qwen3', label: 'Qwen 3', vendor: 'Ollama', imageInputs: false },
]

function withProviders(children: ReactNode) {
  return <I18nProvider><TooltipProvider>{children}</TooltipProvider></I18nProvider>
}

function renderSelector(mode: ChatMode, onChange = vi.fn()) {
  render(withProviders(<ModeSelector mode={mode} models={MODELS} onChange={onChange} />))
  return onChange
}

function openMenu() {
  const trigger = screen.getByRole('button')
  trigger.focus()
  fireEvent.keyDown(trigger, { key: 'Enter', code: 'Enter' })
}

describe('ModeSelector (Runtime catalog)', () => {
  afterEach(cleanup)

  it('shows the selected Runtime model', () => {
    renderSelector('local:openai:gpt-4o')
    expect(screen.getByRole('button')).toHaveTextContent('GPT-4o')
  })

  it('shows a model-selection prompt for a stale selection', () => {
    renderSelector('local:removed:model')
    expect(screen.getByRole('button')).toHaveTextContent('选择具体模型')
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

  it('separates recommended and more models', async () => {
    renderSelector('local:openai:gpt-4o')
    openMenu()
    expect(await screen.findByText('更多模型')).toBeInTheDocument()
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

})
