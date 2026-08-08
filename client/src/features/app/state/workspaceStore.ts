import type {
  ConversationProject,
  ConversationWorkspace,
  LocalAttachmentRef,
} from '@/shared/local-data/types'
import type { LocalRun, LocalWorkspaceAuthorization } from '@/runtime/client'
import { createStore } from './store'

export interface WorkspaceStoreState {
  /** Project/workspace picked in the composer before a conversation existed. */
  pendingWorkspace: ConversationWorkspace | undefined
  pendingProject: ConversationProject | undefined
  pendingAttachments: LocalAttachmentRef[]
  /** Workspaces the Runtime has authorized for this Client. */
  authorizedWorkspaces: LocalWorkspaceAuthorization[]
  /** Local harness runs surfaced in the UI. */
  localRuns: LocalRun[]
  /** Bumped when pending-command state changes so delivery re-runs. */
  pendingCommandDeliveryVersion: number
}

const initialState: WorkspaceStoreState = {
  pendingWorkspace: undefined,
  pendingProject: undefined,
  pendingAttachments: [],
  authorizedWorkspaces: [],
  localRuns: [],
  pendingCommandDeliveryVersion: 0,
}

export const workspaceStore = createStore<WorkspaceStoreState>(initialState)

type Updater<T> = T | ((previous: T) => T)

function resolve<T>(updater: Updater<T>, previous: T): T {
  return typeof updater === 'function' ? (updater as (previous: T) => T)(previous) : updater
}

export const workspaceStoreActions = {
  setPendingWorkspace: (updater: Updater<ConversationWorkspace | undefined>): void => {
    workspaceStore.setState((state) => ({
      ...state,
      pendingWorkspace: resolve(updater, state.pendingWorkspace),
    }))
  },
  setPendingProject: (updater: Updater<ConversationProject | undefined>): void => {
    workspaceStore.setState((state) => ({
      ...state,
      pendingProject: resolve(updater, state.pendingProject),
    }))
  },
  setPendingAttachments: (updater: Updater<LocalAttachmentRef[]>): void => {
    workspaceStore.setState((state) => ({
      ...state,
      pendingAttachments: resolve(updater, state.pendingAttachments),
    }))
  },
  setAuthorizedWorkspaces: (updater: Updater<LocalWorkspaceAuthorization[]>): void => {
    workspaceStore.setState((state) => ({
      ...state,
      authorizedWorkspaces: resolve(updater, state.authorizedWorkspaces),
    }))
  },
  setLocalRuns: (updater: Updater<LocalRun[]>): void => {
    workspaceStore.setState((state) => ({
      ...state,
      localRuns: resolve(updater, state.localRuns),
    }))
  },
  setPendingCommandDeliveryVersion: (updater: Updater<number>): void => {
    workspaceStore.setState((state) => ({
      ...state,
      pendingCommandDeliveryVersion: resolve(updater, state.pendingCommandDeliveryVersion),
    }))
  },
  /** Reset module state between test cases. */
  resetForTests: (): void => {
    workspaceStore.setState(initialState)
  },
}
