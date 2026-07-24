import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { PendingApprovalBar } from './PendingApprovalBar'
import { I18nProvider } from '@/shared/i18n/I18nProvider'

afterEach(() => cleanup())

describe('PendingApprovalBar', () => {
  it('renders nothing when there is no pending approval', () => {
    const { container } = render(
      <I18nProvider>
        <PendingApprovalBar approval={null} onDecision={vi.fn()} />
      </I18nProvider>,
    )
    expect(container).toBeEmptyDOMElement()
  })

  it('hides optimistically, forwards decisions, and restores a rejected submission', async () => {
    const onDecision = vi.fn().mockResolvedValue(false)
    render(
      <I18nProvider>
        <PendingApprovalBar
          approval={{ kind: 'approval', messageID: 'm1', requestID: 'p1', tool: '运行命令', toolName: 'execute', arguments: { command: 'make test' }, canGrantForRun: true }}
          onDecision={onDecision}
        />
      </I18nProvider>,
    )

    expect(screen.getByText('等待批准：运行命令')).toBeInTheDocument()

    fireEvent.click(screen.getByText('不再询问'))
    expect(onDecision).toHaveBeenCalledWith('m1', 'p1', 'approve', 'run')
    expect(screen.queryByText('等待批准：运行命令')).not.toBeInTheDocument()
    await waitFor(() => expect(screen.getByText('等待批准：运行命令')).toBeInTheDocument())

    fireEvent.click(screen.getByText('允许一次'))
    expect(onDecision).toHaveBeenCalledWith('m1', 'p1', 'approve', 'once')
    await waitFor(() => expect(screen.getByText('等待批准：运行命令')).toBeInTheDocument())

    fireEvent.click(screen.getByText('拒绝'))
    expect(onDecision).toHaveBeenCalledWith('m1', 'p1', 'deny', undefined)
    await waitFor(() => expect(screen.getByText('等待批准：运行命令')).toBeInTheDocument())
    expect(screen.queryByText('修改参数')).not.toBeInTheDocument()
  })

  it('does not offer a lasting grant for an irreversible operation', () => {
    render(
      <I18nProvider>
        <PendingApprovalBar
          approval={{ kind: 'approval', messageID: 'm1', requestID: 'p-delete', tool: '删除幻灯片', toolName: 'office.delete_slide', arguments: {}, canGrantForRun: false }}
          onDecision={vi.fn()}
        />
      </I18nProvider>,
    )

    expect(screen.getByText('允许一次')).toBeInTheDocument()
    expect(screen.getByText('拒绝')).toBeInTheDocument()
    expect(screen.queryByText('不再询问')).not.toBeInTheDocument()
  })

  it('requires an explicit reconciliation outcome', () => {
    const onReconcile = vi.fn()
    render(
      <I18nProvider>
        <PendingApprovalBar
          approval={{ kind: 'reconciliation', messageID: 'm1', requestID: 'toolop-1', tool: '运行命令', toolName: 'execute', arguments: {} }}
          onDecision={vi.fn()}
          onReconcile={onReconcile}
        />
      </I18nProvider>,
    )
    fireEvent.click(screen.getByText('确认未执行，重新尝试'))
    expect(onReconcile).toHaveBeenCalledWith('m1', 'toolop-1', 'retry_not_executed')
  })

  it('explains when intelligent approval falls back to the user', () => {
    render(
      <I18nProvider>
        <PendingApprovalBar
          approval={{ kind: 'approval', messageID: 'm1', requestID: 'p1', tool: '运行命令', toolName: 'execute', arguments: {}, source: 'fallback' }}
          onDecision={vi.fn()}
        />
      </I18nProvider>,
    )

    expect(screen.getByText('智能审批暂时不可用，已切换为人工确认。')).toBeInTheDocument()
  })
})
