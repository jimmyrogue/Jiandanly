import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { I18nProvider } from '@/shared/i18n/I18nProvider'
import type { ChatMessage } from '@/shared/local-data/types'
import { RunProcess } from './RunProcess'

afterEach(cleanup)

function message(status: ChatMessage['status']): ChatMessage {
  return {
    id: 'assistant-1',
    role: 'assistant',
    content: status === 'done' ? 'Final answer.' : '',
    createdAt: '2026-08-04T00:00:00Z',
    status,
    runId: 'run-1',
    presentation: {
      snapshot: {
        schema_version: 1,
        run_id: 'run-1',
        event_high_watermark: 4,
        items: [
          {
            id: 'round:model-call-1:progress',
            kind: 'progress',
            status: 'completed',
            order: { event_seq: 1, slot: 0 },
            revision: 1,
            source: { kind: 'run_event', id: 'event-1' },
            text: '先检查相关文件。',
            created_at: '2026-08-04T00:00:00Z',
          },
          {
            id: 'receipt:tool-1',
            kind: 'tool',
            status: status === 'streaming' ? 'in_progress' : 'completed',
            order: { event_seq: 2, slot: 0 },
            revision: 3,
            source: { kind: 'tool_receipt', id: 'tool-1' },
            tool_call_id: 'call-1',
            tool_name: 'read_file',
            risk: 'read_only',
            display_target: 'README.md',
            display_target_kind: 'text',
            created_at: '2026-08-04T00:00:01Z',
            updated_at: '2026-08-04T00:00:02Z',
            completed_at: status === 'streaming' ? null : '2026-08-04T00:00:02Z',
          },
          {
            id: 'answer:assistant-1',
            kind: 'final_answer',
            status: 'completed',
            order: { event_seq: 4, slot: 0 },
            revision: 4,
            source: { kind: 'thread_item', id: 'assistant-1' },
            content: 'Final answer.',
            created_at: '2026-08-04T00:00:00Z',
            completed_at: '2026-08-04T00:00:03Z',
          },
        ],
      },
      drafts: status === 'streaming' ? { 'model-call-2': '正在核对结果…' } : {},
    },
  }
}

describe('RunProcess', () => {
  it('keeps completed process available behind a collapsed summary', () => {
    render(<I18nProvider><RunProcess message={message('done')} /></I18nProvider>)

    expect(screen.getByRole('button', { name: /过程 · 2 步 · 1 个工具 · 已完成/ }))
      .toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByText('先检查相关文件。')).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /过程 · 2 步/ }))
    expect(screen.getByText('先检查相关文件。')).toBeInTheDocument()
    expect(screen.getByText('读取文件')).toBeInTheDocument()
    expect(screen.getByText('README.md')).toBeInTheDocument()
    expect(screen.queryByText('read_file')).not.toBeInTheDocument()
  })

  it('shows current narrative and activity while the run is active', () => {
    render(<I18nProvider><RunProcess message={message('streaming')} /></I18nProvider>)

    expect(screen.getByText('读取文件 · README.md')).toBeInTheDocument()
    expect(screen.getByText('正在核对结果…')).toBeInTheDocument()
    expect(screen.queryByText('已完成')).not.toBeInTheDocument()
  })

  it('keeps phase elapsed time visible beside an existing presentation', () => {
    const activeMessage = message('streaming')
    const tool = activeMessage.presentation?.snapshot.items?.[1]
    if (!tool || tool.kind !== 'tool') throw new Error('expected tool fixture')
    tool.status = 'completed'
    activeMessage.modelPhase = 'reasoning'
    activeMessage.modelPhaseStartedAt = new Date(Date.now() - 1_500).toISOString()

    render(<I18nProvider><RunProcess message={activeMessage} /></I18nProvider>)

    expect(screen.getByText(/正在深度分析… · 1 秒/)).toBeInTheDocument()
  })

  it('groups adjacent successful tools with the same action and target', () => {
    const completedMessage = message('done')
    const presentation = completedMessage.presentation
    const items = presentation?.snapshot.items
    const firstTool = items?.[1]
    if (!items || !firstTool || firstTool.kind !== 'tool') throw new Error('expected tool fixture')
    items.splice(2, 0, {
      ...firstTool,
      id: 'receipt:tool-2',
      tool_call_id: 'call-2',
      order: { event_seq: 3, slot: 0 },
      revision: 3,
    })

    render(<I18nProvider><RunProcess message={completedMessage} /></I18nProvider>)
    fireEvent.click(screen.getByRole('button', { name: /过程 · 3 步/ }))

    expect(screen.getAllByText('读取文件')).toHaveLength(1)
    expect(screen.getByText('× 2')).toBeInTheDocument()
  })

  it('shows a failed tool as a human action with its target and reason', () => {
    const activeMessage = message('streaming')
    const tool = activeMessage.presentation?.snapshot.items?.[1]
    if (!tool || tool.kind !== 'tool') throw new Error('expected tool fixture')
    tool.tool_name = 'web.fetch'
    tool.status = 'failed'
    tool.display_target = 'bochk.com'
    tool.display_target_kind = 'host'
    tool.failure_detail = '404 Not Found'

    render(<I18nProvider><RunProcess message={activeMessage} /></I18nProvider>)

    expect(screen.getByText('读取网页')).toBeInTheDocument()
    expect(screen.getByText('bochk.com')).toBeInTheDocument()
    expect(screen.getByText('失败 · 404 Not Found')).toBeInTheDocument()
    expect(screen.queryByText('web.fetch')).not.toBeInTheDocument()
  })

  it('keeps failed runs expanded when an action needs attention', () => {
    const failedMessage = message('error')
    const tool = failedMessage.presentation?.snapshot.items?.[1]
    if (!tool || tool.kind !== 'tool') throw new Error('expected tool fixture')
    tool.status = 'unknown'

    render(<I18nProvider><RunProcess message={failedMessage} /></I18nProvider>)

    expect(screen.getByRole('button', { name: /过程 · 2 步/ }))
      .toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByText('状态未知')).toBeInTheDocument()
  })

  it('collapses recovered failures when the run ultimately succeeds', () => {
    const activeMessage = message('streaming')
    const tool = activeMessage.presentation?.snapshot.items?.[1]
    if (!tool || tool.kind !== 'tool') throw new Error('expected tool fixture')
    tool.status = 'failed'
    tool.failure_detail = 'SSLError'

    const view = render(
      <I18nProvider><RunProcess message={activeMessage} /></I18nProvider>,
    )
    expect(screen.getByText('失败 · SSLError')).toBeInTheDocument()

    view.rerender(
      <I18nProvider>
        <RunProcess message={{ ...activeMessage, status: 'done', content: 'Recovered answer.' }} />
      </I18nProvider>,
    )

    expect(screen.getByRole('button', { name: /过程 · 2 步/ }))
      .toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByText('失败 · SSLError')).not.toBeInTheDocument()
  })

  it('collapses a long live narrative without discarding the full text', () => {
    const activeMessage = message('streaming')
    const longDraft = '正在逐项核对网页来源、地址、营业时间和交通信息，避免把搜索摘要当作官方结论。'.repeat(4)
    if (!activeMessage.presentation) throw new Error('expected presentation fixture')
    activeMessage.presentation.drafts = { 'model-call-2': longDraft }

    const { container } = render(
      <I18nProvider><RunProcess message={activeMessage} /></I18nProvider>,
    )

    const details = container.querySelector('.run-process-narrative-details')
    expect(details).not.toHaveAttribute('open')
    fireEvent.click(details!.querySelector('summary')!)
    expect(details).toHaveAttribute('open')
    expect(screen.getByText(longDraft)).toBeInTheDocument()
  })
})
