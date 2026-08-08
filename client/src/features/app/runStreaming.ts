import {
  appendLocalDelta,
  latestRunFailedLabel,
  processStreamEvent,
  projectRuntimeThread,
  recordLocalEventCursor,
  type ToolArgsByCallId,
} from '@/features/chat/runtimeProjection'
import type { Translator } from '@/shared/i18n/i18n'
import type { ChatMessage, Conversation, LocalFileRef } from '@/shared/local-data/types'
import {
  getLocalThreadSnapshot,
  LocalStreamCursorResetRequiredError,
  streamLocalRun,
  type LocalRunDiagnostics,
  type LocalScheduledRun,
  type RuntimeConnection,
} from '@/runtime/client'

export async function streamLocalMessage(
  runID: string,
  config: RuntimeConnection,
  conversation: Conversation,
  message: ChatMessage,
  t: Translator,
  onOfficeFileOpened: (ref: LocalFileRef) => void,
  onUpdate: () => void,
) {
  let seenEventIDs = new Set((message.agentEvents ?? []).flatMap((event) => event.eventId ? [event.eventId] : []))
  const toolArgsByCallId: ToolArgsByCallId = new Map()
  const subscribe = () => streamLocalRun(runID, config, {
    afterSeq: message.lastEventSeq,
    onEvent: (event) => {
      recordLocalEventCursor(message, event)
      processStreamEvent(message, event, seenEventIDs, toolArgsByCallId, t, onOfficeFileOpened)
      onUpdate()
    },
    onDelta: (delta, event) => {
      recordLocalEventCursor(message, event)
      appendLocalDelta(message, delta, event, seenEventIDs)
      onUpdate()
    },
  })

  let reconnects = 0
  let cursorResets = 0
  while (true) {
    try {
      const result = await subscribe()
      if (result.completed) return result
    } catch (error) {
      if (error instanceof LocalStreamCursorResetRequiredError) {
        if (cursorResets >= 1) throw error
        cursorResets += 1
        const rebuilt = projectRuntimeThread(await getLocalThreadSnapshot(conversation.id, config), undefined, t)
        const projectedMessage = rebuilt.messages.find((item) => item.runId === runID)
        if (!projectedMessage) throw error
        for (const key of Object.keys(message)) Reflect.deleteProperty(message, key)
        Object.assign(message, projectedMessage)
        message.lastEventSeq = Math.max(message.lastEventSeq ?? 0, error.resumeAfter)
        rebuilt.messages = rebuilt.messages.map((item) => item.id === projectedMessage.id ? message : item)
        for (const key of Object.keys(conversation)) Reflect.deleteProperty(conversation, key)
        Object.assign(conversation, rebuilt)
        seenEventIDs = new Set((message.agentEvents ?? []).flatMap((event) => event.eventId ? [event.eventId] : []))
        toolArgsByCallId.clear()
        onUpdate()
        continue
      }
      if (reconnects >= 5) throw error
    }
    if (reconnects >= 5) {
      throw new Error('Runtime event stream ended before its completion marker.')
    }
    const delay = 100 * (2 ** reconnects)
    reconnects += 1
    await new Promise((resolve) => window.setTimeout(resolve, delay))
  }
}

function notify(title: string, raw: string, fallback: string): void {
  const bridge = window.shejaneClient
  if (!bridge?.notify) return
  const normalized = raw.trim().replace(/\s+/g, ' ')
  void bridge.notify({ title, body: normalized.length > 140 ? `${normalized.slice(0, 140)}…` : normalized || fallback })
}

export function notifyAgentCompleted(message: ChatMessage, t: Translator): void {
  notify(t('notify.agentCompleted.title'), message.content || '', t('notify.agentCompleted.empty'))
}

export function notifyAgentFailed(message: ChatMessage, t: Translator): void {
  notify(t('notify.agentFailed.title'), latestRunFailedLabel(message) || message.content || '', t('notify.agentFailed.empty'))
}

export function notifyScheduledRun(schedule: LocalScheduledRun, t: Translator): void {
  const raw = (schedule.status === 'failed' ? schedule.error_message : schedule.result_text) || schedule.goal || ''
  notify(
    schedule.status === 'failed' ? t('notify.scheduledRunFailed.title') : t('notify.scheduledRunCompleted.title'),
    raw,
    t('notify.scheduledRun.empty'),
  )
}

export function downloadLocalRunDiagnostics(diagnostics: LocalRunDiagnostics): void {
  const url = URL.createObjectURL(new Blob([JSON.stringify(diagnostics, null, 2)], { type: 'application/json' }))
  const link = document.createElement('a')
  link.href = url
  link.download = `shejane-local-run-${diagnostics.run.id}-diagnostics.json`
  link.click()
  URL.revokeObjectURL(url)
}
