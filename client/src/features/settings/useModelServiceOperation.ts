import { useEffect, useRef, useState } from 'react'
import type { RuntimeConnection } from '@/runtime/client'

type OperationState = {
  key: string
  nextRun: number
  completionRevision: number
  activeRun?: number
  owner?: symbol
  label: string
  listeners: Set<(label: string, completionRevision: number, owner?: symbol) => void>
}

const operations = new Map<string, OperationState>()

function operationKey(config: RuntimeConnection | null | undefined) {
  if (!config) return ''
  return `${config.baseURL}\u0000${config.session ?? config.token ?? ''}`
}

function stateFor(key: string) {
  if (!key) return undefined
  let state = operations.get(key)
  if (!state) {
    state = { key, nextRun: 0, completionRevision: 0, label: '', listeners: new Set() }
    operations.set(key, state)
  }
  return state
}

function publish(state: OperationState) {
  for (const listener of state.listeners) {
    listener(state.label, state.completionRevision, state.owner)
  }
  if (state.activeRun === undefined && state.listeners.size === 0) {
    queueMicrotask(() => {
      if (
        state.activeRun === undefined
        && state.listeners.size === 0
        && operations.get(state.key) === state
      ) operations.delete(state.key)
    })
  }
}

export function useModelServiceOperation(
  config: RuntimeConnection | null | undefined,
  onExternalCompletion: () => void,
) {
  const state = stateFor(operationKey(config))
  const [owner] = useState(() => Symbol('model-service-settings'))
  const [busy, setBusy] = useState(() => state?.label ?? '')
  const observedCompletionRevision = useRef(state?.completionRevision ?? 0)

  useEffect(() => {
    if (!state) {
      setBusy('')
      return
    }
    observedCompletionRevision.current = state.completionRevision
    const listener = (label: string, completionRevision: number, completionOwner?: symbol) => {
      setBusy(label)
      if (completionRevision === observedCompletionRevision.current) return
      observedCompletionRevision.current = completionRevision
      if (completionOwner !== owner) onExternalCompletion()
    }
    state.listeners.add(listener)
    setBusy(state.label)
    return () => {
      state.listeners.delete(listener)
      publish(state)
    }
  }, [onExternalCompletion, owner, state])

  function begin(label: string) {
    if (!state || state.activeRun !== undefined) return undefined
    const run = ++state.nextRun
    state.activeRun = run
    state.owner = owner
    state.label = label
    publish(state)
    return run
  }

  function update(run: number, label: string) {
    if (!state || state.activeRun !== run) return
    state.label = label
    publish(state)
  }

  function finish(run: number) {
    if (!state || state.activeRun !== run) return
    state.activeRun = undefined
    state.label = ''
    state.completionRevision += 1
    publish(state)
    state.owner = undefined
  }

  return {
    active: state?.activeRun !== undefined,
    begin,
    busy,
    finish,
    update,
  }
}

export type ModelServiceOperationController = Pick<
  ReturnType<typeof useModelServiceOperation>,
  'begin' | 'busy' | 'finish'
>
