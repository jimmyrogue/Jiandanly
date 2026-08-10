import { useRef, useState } from 'react'
import type {
  LocalPermissionScope,
  LocalPlanApprovalDecision,
  LocalToolReconciliationDecision,
} from '@/runtime/client'
import type { RunDecisionCommandHandlers } from './useRunDecisionCommands'

export function useRunDecisionGuards(handlers: RunDecisionCommandHandlers) {
  const questionAnswersInFlightRef = useRef(new Set<string>())
  const permissionDecisionsInFlightRef = useRef(new Set<string>())
  const planDecisionsInFlightRef = useRef(new Set<string>())
  const toolReconciliationsInFlightRef = useRef(new Set<string>())
  const [submittedPermissionRequestIDs, setSubmittedPermissionRequestIDs] = useState<
    ReadonlySet<string>
  >(() => new Set())

  async function handlePermissionDecision(
    messageID: string,
    requestID: string,
    decision: 'approve' | 'edit' | 'deny',
    scope: LocalPermissionScope = 'once',
    editedAction?: { name: string; args: Record<string, unknown> },
  ) {
    if (permissionDecisionsInFlightRef.current.has(requestID)) return false
    permissionDecisionsInFlightRef.current.add(requestID)
    setSubmittedPermissionRequestIDs((current) => new Set(current).add(requestID))
    let commandAccepted = false
    try {
      commandAccepted = await handlers.handlePermissionDecisionOnce(
        messageID,
        requestID,
        decision,
        scope,
        editedAction,
      )
    } finally {
      permissionDecisionsInFlightRef.current.delete(requestID)
      if (!commandAccepted) {
        setSubmittedPermissionRequestIDs((current) => {
          const next = new Set(current)
          next.delete(requestID)
          return next
        })
      }
    }
    return commandAccepted
  }

  async function handleToolReconciliation(
    messageID: string,
    requestID: string,
    decision: LocalToolReconciliationDecision,
  ) {
    if (toolReconciliationsInFlightRef.current.has(requestID)) return
    toolReconciliationsInFlightRef.current.add(requestID)
    try {
      await handlers.handleToolReconciliationOnce(messageID, requestID, decision)
    } finally {
      toolReconciliationsInFlightRef.current.delete(requestID)
    }
  }

  async function handleQuestionAnswer(
    messageID: string,
    requestID: string,
    answers: Record<string, string[]>,
  ) {
    if (questionAnswersInFlightRef.current.has(requestID)) return
    questionAnswersInFlightRef.current.add(requestID)
    try {
      await handlers.handleQuestionAnswerOnce(messageID, requestID, answers)
    } finally {
      questionAnswersInFlightRef.current.delete(requestID)
    }
  }

  async function handlePlanApprovalDecision(
    messageID: string,
    requestID: string,
    decision: LocalPlanApprovalDecision,
    instructions?: string,
  ) {
    if (planDecisionsInFlightRef.current.has(requestID)) return
    planDecisionsInFlightRef.current.add(requestID)
    try {
      await handlers.handlePlanApprovalDecisionOnce(
        messageID,
        requestID,
        decision,
        instructions,
      )
    } finally {
      planDecisionsInFlightRef.current.delete(requestID)
    }
  }

  return {
    submittedPermissionRequestIDs,
    handlePermissionDecision,
    handleToolReconciliation,
    handleQuestionAnswer,
    handlePlanApprovalDecision,
  }
}
