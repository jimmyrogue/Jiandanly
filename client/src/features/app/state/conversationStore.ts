import type { Conversation } from '@/shared/local-data/types'
import { createStore } from './store'

export interface ConversationStoreState {
  conversations: Conversation[]
  /** The conversation currently bound to the chat workspace. */
  activeID: string | undefined
}

const initialState: ConversationStoreState = {
  conversations: [],
  activeID: undefined,
}

export const conversationStore = createStore<ConversationStoreState>(initialState)

type Updater<T> = T | ((previous: T) => T)

function resolve<T>(updater: Updater<T>, previous: T): T {
  return typeof updater === 'function' ? (updater as (previous: T) => T)(previous) : updater
}

export const conversationStoreActions = {
  setConversations: (updater: Updater<Conversation[]>): void => {
    conversationStore.setState((state) => ({
      ...state,
      conversations: resolve(updater, state.conversations),
    }))
  },
  setActiveID: (updater: Updater<string | undefined>): void => {
    conversationStore.setState((state) => ({
      ...state,
      activeID: resolve(updater, state.activeID),
    }))
  },
  /** Reset module state between test cases. */
  resetForTests: (): void => {
    conversationStore.setState(initialState)
  },
}
