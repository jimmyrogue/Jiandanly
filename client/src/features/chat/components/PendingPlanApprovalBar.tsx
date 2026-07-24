import { useState } from 'react'
import { IconCheck, IconEdit, IconX } from '@tabler/icons-react'
import { Button } from '@/components/ui/button'
import { useI18n } from '@/shared/i18n/i18n'
import type { PendingPlanApproval } from '../pendingPlanApproval'

export function PendingPlanApprovalBar({
  plan,
  onDecision,
}: {
  plan: PendingPlanApproval | null
  onDecision: (
    messageID: string,
    requestID: string,
    decision: 'approve' | 'modify' | 'reject',
    instructions?: string,
  ) => void
}) {
  const { t } = useI18n()
  const [instructions, setInstructions] = useState('')

  if (!plan) {
    return null
  }

  const trimmed = instructions.trim()
  const decide = (decision: 'approve' | 'modify' | 'reject', note?: string) =>
    onDecision(plan.messageID, plan.requestID, decision, note)

  return (
    <div className="plan-bar" role="region" aria-label={t('agent.planApproval.title')}>
      <div className="plan-bar-head">
        <div className="plan-bar-copy">
          <span className="plan-bar-title">{t('agent.planApproval.title')}</span>
          <span className="plan-bar-detail">{t('agent.planApproval.detail')}</span>
        </div>
        <div className="plan-bar-actions">
          <Button size="sm" onClick={() => decide('approve')}>
            <IconCheck size={14} />
            {t('agent.planApproval.approve')}
          </Button>
          <Button size="sm" variant="outline" onClick={() => decide('reject')}>
            <IconX size={14} />
            {t('agent.planApproval.reject')}
          </Button>
        </div>
      </div>
      {plan.todos.length > 0 ? (
        <ol className="plan-bar-todos">
          {plan.todos.slice(0, 6).map((todo) => (
            <li key={`${todo.status}:${todo.content}`}>{todo.content}</li>
          ))}
        </ol>
      ) : null}
      <div className="plan-bar-revise">
        <textarea
          className="plan-bar-input"
          value={instructions}
          rows={2}
          placeholder={t('agent.planApproval.modifyPlaceholder')}
          onChange={(event) => setInstructions(event.target.value)}
        />
        <Button
          size="sm"
          variant="secondary"
          disabled={!trimmed}
          onClick={() => decide('modify', trimmed)}
        >
          <IconEdit size={14} />
          {t('agent.planApproval.modify')}
        </Button>
      </div>
    </div>
  )
}
