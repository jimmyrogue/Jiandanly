import { cleanup, render } from '@testing-library/react'
import type { ReactElement } from 'react'
import { afterEach, describe, expect, it } from 'vitest'
import { ThinkingIndicator } from './ThinkingIndicator'
import { I18nProvider } from '@/shared/i18n/I18nProvider'
import type { ChatMessage } from '@/shared/local-data/types'

afterEach(() => cleanup())

function message(overrides: Partial<ChatMessage> = {}): ChatMessage {
  return {
    id: 'm1',
    role: 'assistant',
    content: '',
    createdAt: new Date(Date.now() - 65_000).toISOString(),
    status: 'streaming',
    runId: 'run-1',
    agentEvents: [],
    ...overrides,
  }
}

function renderIndicator(node: ReactElement) {
  return render(<I18nProvider>{node}</I18nProvider>)
}

describe('ThinkingIndicator', () => {
  it('renders nothing for user messages', () => {
    const { container } = renderIndicator(
      <ThinkingIndicator message={message({ role: 'user', status: 'done' })} />,
    )
    expect(container).toBeEmptyDOMElement()
  })

  it('renders the pulsing icon while the assistant message is streaming', () => {
    const { container } = renderIndicator(<ThinkingIndicator message={message({ status: 'streaming' })} />)
    const indicator = container.querySelector('.thinking-indicator')
    expect(indicator).toBeInTheDocument()
    expect(container.querySelector('.thinking-pulse')).toBeInTheDocument()
    expect(indicator).toHaveTextContent('正在思考…')
  })

  it('renders nothing once answer text starts streaming', () => {
    const { container } = renderIndicator(
      <ThinkingIndicator message={message({ status: 'streaming', content: '回答已经开始' })} />,
    )
    expect(container).toBeEmptyDOMElement()
  })

  it('renders the pulsing icon for the pending status as well', () => {
    const { container } = renderIndicator(<ThinkingIndicator message={message({ status: 'pending' })} />)
    expect(container.querySelector('.thinking-pulse')).toBeInTheDocument()
  })

  it('renders nothing while waiting for permission or input (the bars take over)', () => {
    const { container } = renderIndicator(
      <ThinkingIndicator message={message({ status: 'waiting_input' })} />,
    )
    expect(container).toBeEmptyDOMElement()
  })

  it('renders nothing once the run has finished', () => {
    const { container } = renderIndicator(<ThinkingIndicator message={message({ status: 'done' })} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('renders nothing on error status', () => {
    const { container } = renderIndicator(<ThinkingIndicator message={message({ status: 'error' })} />)
    expect(container).toBeEmptyDOMElement()
  })
})
