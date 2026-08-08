import { useEffect } from 'react'
import { toast } from 'sonner'
import type { Translator } from '@/shared/i18n/i18n'
import { runtimeCommandErrorMessage } from './runtimeCommandError'
import type { LocalConversationStore } from '@/shared/local-data/localConversations'
import {
  deliverPendingRuntimeCommands,
  hasRuntimeAuthorization,
  type PendingRuntimeCommand,
  type PendingRuntimeCommandFailure,
  type RuntimeCommandResult,
  type RuntimeConnection,
} from '@/runtime/client'
import { runtimeStore } from './state/runtimeStore'
import { workspaceStore } from './state/workspaceStore'
import { useStore } from './state/store'

type NoticeOptions = Omit<NonNullable<Parameters<typeof toast.message>[1]>, 'id'>

interface RuntimeDeliveryContext {
  localData: LocalConversationStore
  isDesktop: boolean
  settleDeliveredLocalRunCommand: (
    command: PendingRuntimeCommand,
    result: RuntimeCommandResult,
    config: RuntimeConnection,
  ) => Promise<boolean>
  settleRejectedPendingRuntimeCommand: (
    failure: PendingRuntimeCommandFailure,
    config: RuntimeConnection,
  ) => Promise<void>
  setNotice: (message: string, options?: NoticeOptions) => void
  consumeRuntimeCommandFailureNotice: (commandId: string, message: string) => boolean
  t: Translator
  retryDelayMs?: number
}

export function useRuntimeDelivery(context: RuntimeDeliveryContext): void {
  const {
    localData,
    isDesktop,
    settleDeliveredLocalRunCommand,
    settleRejectedPendingRuntimeCommand,
    setNotice,
    consumeRuntimeCommandFailureNotice,
    t,
    retryDelayMs = 2000,
  } = context
  const { runtime, connection: runtimeConnection } = useStore(runtimeStore)
  const { pendingCommandDeliveryVersion } = useStore(workspaceStore)

  useEffect(() => {
    if (!isDesktop || !runtime?.online || !hasRuntimeAuthorization(runtimeConnection)) {
      return
    }
    let disposed = false
    let retryTimer: number | undefined
    const config = runtimeConnection
    const deliver = async () => {
      try {
        const commands = await localData.listPendingRuntimeCommands()
        if (disposed || commands.length === 0) return
        const report = await deliverPendingRuntimeCommands(
          commands,
          config,
          (command, run) => settleDeliveredLocalRunCommand(command, run, config).then(() => undefined),
        )
        const rejectedSettlements: Promise<void>[] = []
        for (const failure of report.failures) {
          if (!failure.retryable) {
            rejectedSettlements.push(settleRejectedPendingRuntimeCommand(failure, config))
          }
        }
        await Promise.all(rejectedSettlements)
        const blocked = report.failures.find((item) => !item.retryable)
        if (!disposed && blocked) {
          const blockedMessage = runtimeCommandErrorMessage(blocked.error, t)
          if (!consumeRuntimeCommandFailureNotice(blocked.command.commandId, blockedMessage)) {
            setNotice(blockedMessage)
          }
        }
        if (!disposed && report.failures.some((item) => item.retryable)) {
          retryTimer = window.setTimeout(() => void deliver(), retryDelayMs)
        }
      } catch {
        if (!disposed) {
          retryTimer = window.setTimeout(() => void deliver(), retryDelayMs)
        }
      }
    }
    void deliver()
    return () => {
      disposed = true
      if (retryTimer !== undefined) window.clearTimeout(retryTimer)
    }
  }, [
    isDesktop,
    localData,
    pendingCommandDeliveryVersion,
    runtime?.online,
    runtimeConnection,
    settleDeliveredLocalRunCommand,
    settleRejectedPendingRuntimeCommand,
    consumeRuntimeCommandFailureNotice,
    setNotice,
    t,
    retryDelayMs,
  ])
}
