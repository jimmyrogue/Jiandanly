import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { I18nProvider } from '@/shared/i18n/I18nProvider'
import { PendingPlanApprovalBar } from './PendingPlanApprovalBar'

afterEach(() => cleanup())

describe('PendingPlanApprovalBar', () => {
  it('renders nothing without a pending plan', () => {
    const { container } = render(
      <I18nProvider>
        <PendingPlanApprovalBar plan={null} onDecision={vi.fn()} />
      </I18nProvider>,
    )
    expect(container).toBeEmptyDOMElement()
  })

  it('shows todos and forwards approve / reject decisions', () => {
    const onDecision = vi.fn()
    render(
      <I18nProvider>
        <PendingPlanApprovalBar
          plan={{
            messageID: 'm1',
            requestID: 'plan-1',
            todos: [
              { content: 'Write focused tests', status: 'pending' },
              { content: 'Implement the fix', status: 'pending' },
            ],
          }}
          onDecision={onDecision}
        />
      </I18nProvider>,
    )

    expect(screen.getByText('等待你批准计划')).toBeInTheDocument()
    expect(screen.getByText('Write focused tests')).toBeInTheDocument()
    expect(screen.getByText('Implement the fix')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '批准计划' }))
    expect(onDecision).toHaveBeenCalledWith('m1', 'plan-1', 'approve', undefined)

    fireEvent.click(screen.getByRole('button', { name: '拒绝' }))
    expect(onDecision).toHaveBeenCalledWith('m1', 'plan-1', 'reject', undefined)
  })

  it('requires instructions before sending a modify decision', () => {
    const onDecision = vi.fn()
    render(
      <I18nProvider>
        <PendingPlanApprovalBar
          plan={{
            messageID: 'm1',
            requestID: 'plan-1',
            todos: [{ content: 'Write focused tests', status: 'pending' }],
          }}
          onDecision={onDecision}
        />
      </I18nProvider>,
    )

    const revise = screen.getByRole('button', { name: '要求修改' })
    expect(revise).toBeDisabled()

    fireEvent.change(screen.getByPlaceholderText('告诉助手要怎么改计划…'), {
      target: { value: '先补测试，再改实现。' },
    })
    expect(revise).toBeEnabled()

    fireEvent.click(revise)
    expect(onDecision).toHaveBeenCalledWith('m1', 'plan-1', 'modify', '先补测试，再改实现。')
  })
})
