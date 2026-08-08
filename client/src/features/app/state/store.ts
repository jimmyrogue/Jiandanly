import { useSyncExternalStore } from 'react'

export interface Store<S> {
  getState: () => S
  setState: (updater: S | ((previous: S) => S)) => void
  subscribe: (listener: () => void) => () => void
}

export function createStore<S>(initialState: S): Store<S> {
  let state = initialState
  const listeners = new Set<() => void>()
  return {
    getState: () => state,
    setState: (updater) => {
      const next = typeof updater === 'function'
        ? (updater as (previous: S) => S)(state)
        : updater
      if (next === state) return
      state = next
      for (const listener of listeners) listener()
    },
    subscribe: (listener) => {
      listeners.add(listener)
      return () => listeners.delete(listener)
    },
  }
}

export function useStore<S>(store: Store<S>): S {
  return useSyncExternalStore(store.subscribe, store.getState, store.getState)
}
