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
            status: 'completed',
            order: { event_seq: 2, slot: 0 },
            revision: 3,
            source: { kind: 'tool_receipt', id: 'tool-1' },
            tool_call_id: 'call-1',
            tool_name: 'read_file',
            risk: 'read_only',
            created_at: '2026-08-04T00:00:01Z',
            updated_at: '2026-08-04T00:00:02Z',
            completed_at: '2026-08-04T00:00:02Z',
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
    expect(screen.getByText('read_file')).toBeInTheDocument()
  })

  it('shows current narrative and activity while the run is active', () => {
    render(<I18nProvider><RunProcess message={message('streaming')} /></I18nProvider>)

    expect(screen.getByText('正在处理')).toBeInTheDocument()
    expect(screen.getByText('正在核对结果…')).toBeInTheDocument()
    expect(screen.getByText('read_file')).toBeInTheDocument()
  })
})
