import { useEffect, useState } from 'react'
import type { Translator } from '@/shared/i18n/i18n'
import type { ModelPhase } from '@/shared/local-data/types'

export function modelPhaseLabel(phase: ModelPhase | undefined, t: Translator): string {
  switch (phase) {
    case 'waiting_provider': return t('agent.phase.waiting_provider')
    case 'reasoning': return t('agent.phase.reasoning')
    case 'answering': return t('agent.phase.answering')
    case 'tool_calling': return t('agent.phase.tool_calling')
    default: return t('agent.thinkingStreaming')
  }
}

export function useModelPhaseText(
  phase: ModelPhase | undefined,
  startedAt: string | undefined,
  active: boolean,
  t: Translator,
): string {
  const [now, setNow] = useState(Date.now)
  useEffect(() => {
    if (!active) return
    setNow(Date.now())
    const timer = window.setInterval(() => setNow(Date.now()), 1_000)
    return () => window.clearInterval(timer)
  }, [active, startedAt])
  const parsedStartedAt = Date.parse(startedAt ?? '')
  const elapsedSeconds = Number.isFinite(parsedStartedAt)
    ? Math.max(0, Math.floor((now - parsedStartedAt) / 1_000))
    : 0
  const label = modelPhaseLabel(phase, t)
  return elapsedSeconds > 0
    ? `${label} · ${t('agent.phase.elapsed', { seconds: elapsedSeconds })}`
    : label
}
