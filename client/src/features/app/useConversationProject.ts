import {
  useCallback,
  useEffect,
  useRef,
  type Dispatch,
  type MutableRefObject,
  type SetStateAction,
} from 'react'
import { toast } from 'sonner'
import type { ChatMode } from '@/shared/local-data/types'
import { recentRecoverableFailures } from '@/features/chat/recoverableFailures'
import type {
  Conversation,
  ConversationProject,
  ConversationWorkspace,
  LocalAttachmentRef,
} from '@/shared/local-data/types'
import {
  getLocalThreadSnapshot,
  hasRuntimeAuthorization,
  listLocalThreadChanges,
  listLocalThreads,
  type RuntimeConnection,
} from '@/runtime/client'
import type { LocalConversationStore } from '@/shared/local-data/localConversations'
import { projectRuntimeThreadCache } from '@/features/chat/runtimeProjection'
import type { Translator } from '@/shared/i18n/i18n'
import {
  cloneConversation,
  mapWithConcurrency,
  sortConversationsForSidebar,
  upsertConversation,
} from './conversationState'
import { chooseAvailableMode } from './modelSelection'
import { createConversation } from './runExecution'
import { conversationStore, conversationStoreActions } from './state/conversationStore'
import { runtimeStore } from './state/runtimeStore'
import { workspaceStoreActions } from './state/workspaceStore'
import { useStore } from './state/store'

// A notice callback does not need to expose the toast internals, but should
// accept the same option shape that App-level callers already use.
type NoticeOptions = Omit<NonNullable<Parameters<typeof toast.message>[1]>, 'id'>

export type ConversationRenderContext = {
  navigationVersionAtStart: number
}

type PendingConversationRender = {
  conversation: Conversation
  context: ConversationRenderContext
}

interface ConversationProjectContext {
  localData: LocalConversationStore
  isDesktop: boolean
  t: Translator
  setNotice: (message: string, options?: NoticeOptions) => void
  setMainView: (view: 'chat' | 'plugins' | 'settings') => void
  setDraft: Dispatch<SetStateAction<string>>
  setMode: Dispatch<SetStateAction<ChatMode>>
  readChatMode: () => ChatMode
  navigationVersionRef: MutableRefObject<number>
  runtimeThreadCursorRef: MutableRefObject<number>
  runtimeThreadIDsRef: MutableRefObject<Set<string>>
  runtimeThreadStorageLoad: () => Set<string>
  runtimeThreadStorageSave: (ids: Set<string>) => void
  detachVisibleSend: () => void
}

interface ConversationProjectRuntime {
  conversations: Conversation[]
  setConversations: Dispatch<SetStateAction<Conversation[]>>
  activeID: string | undefined
  setActiveID: Dispatch<SetStateAction<string | undefined>>
  activeIDRef: MutableRefObject<string | undefined>
  activeConversation: Conversation | undefined
  refreshConversations: (nextActiveID?: string, options?: { preserveEmptyActive?: boolean }) => Promise<void>
  refreshConversationsAfterStream: (conversationID: string, context: ConversationRenderContext) => Promise<void>
  createConversationRenderContext: () => ConversationRenderContext
  scheduleConversationRender: (conversation: Conversation, context: ConversationRenderContext) => void
  syncRuntimeThreadCache: (config: RuntimeConnection) => Promise<Conversation[]>
  setActiveConversationID: (nextActiveID: string | undefined) => void
  saveActiveConversationWorkspace: (
    workspace: ConversationWorkspace | undefined,
  ) => Promise<void>
  startNewConversation: () => void
  selectConversation: (id: string) => void
}

export function useConversationProject(context: ConversationProjectContext): ConversationProjectRuntime {
  const {
    localData,
    isDesktop,
    t,
    setNotice,
    setMainView,
    setDraft,
    setMode,
    readChatMode,
    navigationVersionRef,
    runtimeThreadCursorRef,
    runtimeThreadIDsRef,
    runtimeThreadStorageLoad,
    runtimeThreadStorageSave,
    detachVisibleSend,
  } = context
  const { runtime, connection: runtimeConnection } = useStore(runtimeStore)

  const pendingConversationRendersRef = useRef<Map<string, PendingConversationRender>>(new Map())
  const liveRenderTimerRef = useRef<number>()
  const activeIDRef = useRef<string | undefined>()
  const conversationInitializationCompleteRef = useRef(false)
  const startupRecoveryNoticeShownRef = useRef(false)

  const { conversations, activeID } = useStore(conversationStore)
  const setConversations = conversationStoreActions.setConversations
  const setActiveID = conversationStoreActions.setActiveID

  const setActiveConversationID = useCallback((nextActiveID: string | undefined): void => {
    activeIDRef.current = nextActiveID
    setActiveID(nextActiveID)
    if (!nextActiveID) {
      setMode(chooseAvailableMode(runtimeStore.getState().models, readChatMode()))
      return
    }
    void localData.get(nextActiveID).then((conversation) => {
      if (activeIDRef.current === nextActiveID) {
        setMode(chooseAvailableMode(runtimeStore.getState().models, conversation?.model ?? '', readChatMode()))
      }
    })
  }, [localData, readChatMode, setMode])
  const refreshConversations = useCallback(async (
    nextActiveID?: string,
    options: { preserveEmptyActive?: boolean } = {},
  ): Promise<void> => {
    const items = await localData.list()
    setConversations(items)
    setActiveConversationID(nextActiveID ?? (options.preserveEmptyActive ? undefined : items[0]?.id))
  }, [localData, setActiveConversationID])

  const syncRuntimeThreadCache = useCallback(async (config: RuntimeConnection): Promise<Conversation[]> => {
    const { threads, cursor } = await listLocalThreads(config)
    const nextThreadIDs = new Set(threads.map((thread) => thread.id))
    const removedThreadIDs = [...runtimeThreadStorageLoad()].filter((id) => !nextThreadIDs.has(id))
    await Promise.all(removedThreadIDs.map((id) => localData.delete(id)))
    const existing = new Map((await localData.list()).map((item) => [item.id, item]))
    const snapshots = await mapWithConcurrency(
      threads,
      4,
      (thread) => getLocalThreadSnapshot(thread.id, config),
    )
    const projected = await mapWithConcurrency(
      snapshots,
      4,
      (snapshot) => projectRuntimeThreadCache(snapshot, existing.get(snapshot.thread.id), config, t),
    )
    const saved = await Promise.all(projected.map((conversation) => localData.saveRuntimeProjection(conversation)))
    const visibleProjected = projected.filter((_conversation, index) => saved[index])
    runtimeThreadStorageSave(nextThreadIDs)
    runtimeThreadIDsRef.current = nextThreadIDs
    runtimeThreadCursorRef.current = Math.max(runtimeThreadCursorRef.current, cursor)
    return visibleProjected
  }, [localData, runtimeThreadCursorRef, runtimeThreadIDsRef, runtimeThreadStorageLoad, runtimeThreadStorageSave, t])

  const createConversationRenderContext = useCallback(() => ({
    navigationVersionAtStart: navigationVersionRef.current,
  }), [navigationVersionRef])

  const refreshConversationsAfterStream = useCallback(async (conversationID: string, context: ConversationRenderContext) => {
    const userNavigatedWhileStreaming = navigationVersionRef.current !== context.navigationVersionAtStart
    await refreshConversations(userNavigatedWhileStreaming ? activeIDRef.current : conversationID, {
      preserveEmptyActive: userNavigatedWhileStreaming && !activeIDRef.current,
    })
  }, [navigationVersionRef, refreshConversations])

  const scheduleConversationRender = useCallback((conversation: Conversation, context: ConversationRenderContext) => {
    pendingConversationRendersRef.current.set(conversation.id, {
      conversation: cloneConversation(conversation),
      context,
    })
    if (liveRenderTimerRef.current !== undefined) {
      return
    }
    liveRenderTimerRef.current = window.setTimeout(() => {
      liveRenderTimerRef.current = undefined
      const pending = Array.from(pendingConversationRendersRef.current.values())
      pendingConversationRendersRef.current.clear()
      if (!pending.length) {
        return
      }
      setConversations((items) => pending.reduce((nextItems, item) => upsertConversation(nextItems, item.conversation), items))
      const focusTarget = pending.find(
        (item) =>
          activeIDRef.current === item.conversation.id ||
          navigationVersionRef.current === item.context.navigationVersionAtStart,
      )
      if (focusTarget) {
        setActiveConversationID(focusTarget.conversation.id)
      }
    }, 33)
  }, [navigationVersionRef, setActiveConversationID])

  const startNewConversation = useCallback(() => {
    detachVisibleSend()
    navigationVersionRef.current += 1
    setActiveConversationID(undefined)
    workspaceStoreActions.setPendingWorkspace(undefined)
    workspaceStoreActions.setPendingProject(undefined)
    workspaceStoreActions.setPendingAttachments([])
    setDraft('')
    setMainView('chat')
  }, [
    detachVisibleSend,
    navigationVersionRef,
    setDraft,
    setMainView,
    setActiveConversationID,
  ])

  const selectConversation = useCallback((id: string) => {
    detachVisibleSend()
    navigationVersionRef.current += 1
    workspaceStoreActions.setPendingWorkspace(undefined)
    workspaceStoreActions.setPendingProject(undefined)
    workspaceStoreActions.setPendingAttachments([])
    setActiveConversationID(id)
    setMainView('chat')
  }, [
    detachVisibleSend,
    navigationVersionRef,
    setMainView,
    setActiveConversationID,
  ])

  useEffect(() => {
    const navigationVersion = navigationVersionRef.current
    const maySelectInitialConversation = !conversationInitializationCompleteRef.current
    let disposed = false
    void localData.list().then((items) => {
      if (disposed) {
        return
      }
      conversationInitializationCompleteRef.current = true
      setConversations((current) => {
        if (!isDesktop) {
          return items
        }
        const merged = new Map(current.map((item) => [item.id, item]))
        for (const item of items) {
          const existing = merged.get(item.id)
          if (!existing || item.updatedAt > existing.updatedAt) {
            merged.set(item.id, item)
          }
        }
        return sortConversationsForSidebar(Array.from(merged.values()))
      })
      if (maySelectInitialConversation && navigationVersionRef.current === navigationVersion) {
        setActiveConversationID(items[0]?.id)
      }
      const [failure] = !startupRecoveryNoticeShownRef.current
        ? recentRecoverableFailures(items, 1)
        : []
      if (failure) {
        startupRecoveryNoticeShownRef.current = true
        setNotice(t('app.notice.recoverableFailureAfterRestart'), {
          duration: 8000,
          action: {
            label: t('agent.failureAction.openChat'),
            onClick: () => {
              setActiveConversationID(failure.target.conversationID)
              setMainView('chat')
            },
          },
        })
      }
    })
    return () => {
      disposed = true
    }
  }, [
    isDesktop,
    localData,
    navigationVersionRef,
    runtimeThreadCursorRef,
    runtimeThreadIDsRef,
    runtimeThreadStorageLoad,
    runtimeThreadStorageSave,
    setActiveConversationID,
    setMainView,
    setNotice,
    t,
  ])

  useEffect(() => {
    activeIDRef.current = activeID
  }, [activeID])

  useEffect(() => {
    return () => {
      if (liveRenderTimerRef.current !== undefined) {
        window.clearTimeout(liveRenderTimerRef.current)
      }
      pendingConversationRendersRef.current.clear()
    }
  }, [])

  useEffect(() => {
    if (!isDesktop || !runtime?.online || !hasRuntimeAuthorization(runtimeConnection)) {
      return
    }
    let disposed = false
    let polling = false
    let interval: number | undefined
    const applyProjected = (projected: Conversation[], deleted = new Set<string>()) => {
      if (disposed || (projected.length === 0 && deleted.size === 0)) {
        return
      }
      setConversations((current) => {
        const merged = new Map<string, Conversation>()
        for (const item of current) {
          if (!deleted.has(item.id)) merged.set(item.id, item)
        }
        for (const item of projected) merged.set(item.id, item)
        return sortConversationsForSidebar(Array.from(merged.values()))
      })
    }
    const pollChanges = async () => {
      if (polling) return
      polling = true
      try {
        const result = await listLocalThreadChanges(runtimeThreadCursorRef.current, runtimeConnection)
        if (disposed) return
        if (result.resetRequired) {
          const previousThreadIDs = new Set(runtimeThreadIDsRef.current)
          const projected = await syncRuntimeThreadCache(runtimeConnection)
          const deleted = new Set(
            [...previousThreadIDs].filter((threadID) => !runtimeThreadIDsRef.current.has(threadID)),
          )
          applyProjected(projected, deleted)
          return
        }
        const latest = new Map(result.changes.map((change) => [change.thread_id, change]))
        const deleted = new Set(
          [...latest.values()].flatMap((change) =>
            change.change_type === 'thread.deleted' ? [change.thread_id] : []),
        )
        await Promise.all([...deleted].map((threadID) => localData.delete(threadID)))
        const existing = new Map((await localData.list()).map((item) => [item.id, item]))
        const snapshots = await mapWithConcurrency(
          [...latest.keys()].filter((threadID) => !deleted.has(threadID)),
          4,
          (threadID) => getLocalThreadSnapshot(threadID, runtimeConnection),
        )
        const projected = await mapWithConcurrency(
          snapshots,
          4,
          (snapshot) => projectRuntimeThreadCache(snapshot, existing.get(snapshot.thread.id), runtimeConnection, t),
        )
        const saved = await Promise.all(projected.map((conversation) => localData.saveRuntimeProjection(conversation)))
        const visibleProjected = projected.filter((_conversation, index) => saved[index])
        const nextRuntimeThreadIDs = new Set(runtimeThreadIDsRef.current)
        for (const threadID of latest.keys()) nextRuntimeThreadIDs.add(threadID)
        for (const threadID of deleted) nextRuntimeThreadIDs.delete(threadID)
        runtimeThreadStorageSave(nextRuntimeThreadIDs)
        runtimeThreadIDsRef.current = nextRuntimeThreadIDs
        runtimeThreadCursorRef.current = Math.max(runtimeThreadCursorRef.current, result.cursor)
        applyProjected(visibleProjected, deleted)
      } catch {
        // Cursor polling is a cache refresh. The next pass retries from the
        // last committed cursor; it never changes Runtime truth.
      } finally {
        polling = false
      }
    }
    void syncRuntimeThreadCache(runtimeConnection)
      .then((projected) => {
        applyProjected(projected)
        if (!disposed) {
          interval = window.setInterval(() => void pollChanges(), 2000)
        }
      })
      .catch(() => {
        if (!disposed) {
          interval = window.setInterval(() => void pollChanges(), 2000)
        }
      })
    return () => {
      disposed = true
      if (interval !== undefined) window.clearInterval(interval)
    }
  }, [
    isDesktop,
    runtime?.online,
    runtimeConnection,
    localData,
    runtimeThreadCursorRef,
    runtimeThreadIDsRef,
    runtimeThreadStorageSave,
    syncRuntimeThreadCache,
    t,
  ])

  const activeConversation = conversations.find((conversation) => conversation.id === activeID)
  const saveActiveConversationWorkspace = useCallback(async (
    workspace: ConversationWorkspace | undefined,
  ): Promise<void> => {
    if (!activeID) {
      workspaceStoreActions.setPendingWorkspace(workspace)
      return
    }
    const timestamp = new Date().toISOString()
    const conversation = (await localData.get(activeID))
      ?? createConversation(t('chat.newConversation'), timestamp, t('chat.newConversation'))
    if (workspace) conversation.workspace = workspace
    else delete conversation.workspace
    conversation.updatedAt = timestamp
    await localData.save(conversation)
    setActiveConversationID(conversation.id)
    setConversations((items) => sortConversationsForSidebar(
      upsertConversation(items, cloneConversation(conversation)),
    ))
  }, [activeID, localData, setActiveConversationID, t])

  return {
    conversations,
    setConversations,
    activeID,
    setActiveID,
    activeIDRef,
    activeConversation,
    refreshConversations,
    refreshConversationsAfterStream,
    createConversationRenderContext,
    scheduleConversationRender,
    syncRuntimeThreadCache,
    setActiveConversationID,
    saveActiveConversationWorkspace,
    startNewConversation,
    selectConversation,
  }
}
