import { useState } from 'react'
import { IconCircleCheck, IconShieldCheck, IconX } from '@tabler/icons-react'
import { Button } from '@/components/ui/button'
import { useI18n } from '@/shared/i18n/i18n'
import type { LocalPermissionDecision, LocalPermissionScope } from '@/runtime/client'
import type { LocalToolReconciliationDecision } from '@/runtime/client'
import type { PendingApproval } from '../pendingApproval'

export function PendingApprovalBar({
  approval,
  onDecision,
  onReconcile,
}: {
  approval: PendingApproval | null
  onDecision: (
    messageID: string,
    requestID: string,
    decision: LocalPermissionDecision,
    scope?: LocalPermissionScope,
  ) => boolean | Promise<boolean>
  onReconcile?: (messageID: string, requestID: string, decision: LocalToolReconciliationDecision) => void
}) {
  if (!approval) {
    return null
  }
  return (
    <ActivePendingApprovalBar
      key={approval.requestID}
      approval={approval}
      onDecision={onDecision}
      onReconcile={onReconcile}
    />
  )
}

function ActivePendingApprovalBar({
  approval,
  onDecision,
  onReconcile,
}: {
  approval: PendingApproval
  onDecision: (
    messageID: string,
    requestID: string,
    decision: LocalPermissionDecision,
    scope?: LocalPermissionScope,
  ) => boolean | Promise<boolean>
  onReconcile?: (messageID: string, requestID: string, decision: LocalToolReconciliationDecision) => void
}) {
  const { t } = useI18n()
  const [locallySubmittedRequestID, setLocallySubmittedRequestID] = useState<string | null>(null)
  if (approval.requestID === locallySubmittedRequestID) {
    return null
  }
  if (approval.kind === 'reconciliation') {
    return (
      <div className="approval-bar" role="region" aria-label={t('agent.reconciliationTitle', { tool: approval.tool })}>
        <div className="approval-bar-copy">
          <span className="approval-bar-title">{t('agent.reconciliationTitle', { tool: approval.tool })}</span>
          <span className="approval-bar-detail">{t('agent.reconciliationDetail')}</span>
        </div>
        <div className="approval-bar-actions">
          <Button size="sm" onClick={() => onReconcile?.(approval.messageID, approval.requestID, 'confirmed_completed')}>
            <IconCircleCheck size={14} />
            {t('agent.reconciliationCompleted')}
          </Button>
          <Button size="sm" variant="secondary" onClick={() => onReconcile?.(approval.messageID, approval.requestID, 'retry_not_executed')}>
            <IconShieldCheck size={14} />
            {t('agent.reconciliationRetry')}
          </Button>
          <Button size="sm" variant="outline" onClick={() => onReconcile?.(approval.messageID, approval.requestID, 'abort')}>
            <IconX size={14} />
            {t('agent.reconciliationAbort')}
          </Button>
        </div>
      </div>
    )
  }
  const decide = (
    decision: LocalPermissionDecision,
    scope?: LocalPermissionScope,
  ) => {
    const requestID = approval.requestID
    setLocallySubmittedRequestID(requestID)
    void Promise.resolve(
      onDecision(approval.messageID, requestID, decision, scope),
    ).then((accepted) => {
      if (!accepted) setLocallySubmittedRequestID(null)
    }, () => setLocallySubmittedRequestID(null))
  }
  const permissionDetail = approval.source === 'fallback'
    ? t('agent.permissionFallbackDetail')
    : approval.source === 'llm'
      ? t('agent.permissionLlmDetail')
      : t('agent.permissionDetail')

  return (
    <div className="approval-bar" role="region" aria-label={t('agent.waitingApproval', { tool: approval.tool })}>
      <div className="approval-bar-copy">
        <span className="approval-bar-title">{t('agent.waitingApproval', { tool: approval.tool })}</span>
        <span className="approval-bar-detail">{permissionDetail}</span>
      </div>
      <div className="approval-bar-actions">
        <Button size="sm" onClick={() => decide('approve', 'once')}>
          <IconCircleCheck size={14} />
          {t('agent.allowOnce')}
        </Button>
        {approval.canGrantForRun ? (
          <Button size="sm" variant="secondary" onClick={() => decide('approve', 'run')}>
            <IconShieldCheck size={14} />
            {t('agent.allowRun')}
          </Button>
        ) : null}
        <Button size="sm" variant="outline" onClick={() => decide('deny')}>
          <IconX size={14} />
          {t('agent.deny')}
        </Button>
      </div>
    </div>
  )
}
