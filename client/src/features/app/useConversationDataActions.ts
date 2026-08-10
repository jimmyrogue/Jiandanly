import { useCallback, type MutableRefObject } from 'react'
import type { Locale, Translator } from '@/shared/i18n/i18n'
import type {
  ChatMode,
  Conversation,
  ExportedModelService,
} from '@/shared/local-data/types'
import type { LocalConversationStore } from '@/shared/local-data/localConversations'
import {
  deleteLocalThread,
  hasRuntimeAuthorization,
  importModelService,
  listModelServices,
  updateLocalThread,
  type AgentSettings,
  type RuntimeConnection,
} from '@/runtime/client'
import { storeRuntimeThreadIDs } from './appStorage'
import { runtimeStoreActions } from './state/runtimeStore'
import { workspaceStoreActions } from './state/workspaceStore'

interface ConversationDataActionsContext {
  activeIDRef: MutableRefObject<string | undefined>
  agentSettings: Required<AgentSettings>
  localData: LocalConversationStore
  locale: Locale
  mode: ChatMode
  refreshConversations: (
    nextActiveID?: string,
    options?: { preserveEmptyActive?: boolean },
  ) => Promise<void>
  runtimeConnection: RuntimeConnection | null
  runtimeThreadIDsRef: MutableRefObject<Set<string>>
  setNotice: (message: string) => void
  t: Translator
}

export function useConversationDataActions({
  activeIDRef,
  agentSettings,
  localData,
  locale,
  mode,
  refreshConversations,
  runtimeConnection,
  runtimeThreadIDsRef,
  setNotice,
  t,
}: ConversationDataActionsContext) {
  const updateConversationMetadata = useCallback(async (
    conversationID: string,
    update: (conversation: Conversation) => void,
    options: { touch?: boolean } = {},
  ): Promise<Conversation | undefined> => {
    const conversation = await localData.get(conversationID)
    if (!conversation) {
      setNotice(t('app.notice.conversationMissing'))
      return undefined
    }
    update(conversation)
    if (options.touch ?? true) {
      conversation.updatedAt = new Date().toISOString()
    }
    const runtimeOwnsThread = runtimeThreadIDsRef.current.has(conversationID)
    if (runtimeOwnsThread && hasRuntimeAuthorization(runtimeConnection)) {
      try {
        await updateLocalThread(
          conversationID,
          {
            title: conversation.title,
            archived: conversation.archived,
            metadata: {
              pinned: conversation.pinned ?? false,
              model: conversation.model,
              project: conversation.project,
              workspace: conversation.workspace,
            },
          },
          runtimeConnection,
        )
      } catch (error) {
        setNotice(error instanceof Error ? error.message : t('app.notice.localRunFailed'))
        return undefined
      }
    }
    await localData.save(conversation)
    await refreshConversations(activeIDRef.current ?? undefined, {
      preserveEmptyActive: !activeIDRef.current,
    })
    return conversation
  }, [activeIDRef, localData, refreshConversations, runtimeConnection, runtimeThreadIDsRef, setNotice, t])

  const togglePinConversation = useCallback(async (conversationID: string) => {
    const conversation = await updateConversationMetadata(
      conversationID,
      (item) => {
        item.pinned = !item.pinned
      },
      { touch: false },
    )
    if (conversation) {
      setNotice(t(conversation.pinned ? 'app.notice.conversationPinned' : 'app.notice.conversationUnpinned', { title: conversation.title }))
    }
  }, [setNotice, t, updateConversationMetadata])

  const renameConversation = useCallback(async (conversationID: string, title: string) => {
    const nextTitle = title.trim()
    if (!nextTitle) return
    const conversation = await updateConversationMetadata(conversationID, (item) => {
      item.title = nextTitle
    })
    if (conversation) {
      setNotice(t('app.notice.conversationRenamed', { title: conversation.title }))
    }
  }, [setNotice, t, updateConversationMetadata])

  const deleteConversationData = useCallback(async (conversationID: string) => {
    const conversation = await localData.get(conversationID)
    if (!conversation) {
      setNotice(t('app.notice.conversationMissing'))
      return
    }
    const deletedActive = activeIDRef.current === conversationID
    const runtimeOwnsThread = runtimeThreadIDsRef.current.has(conversationID)
    if (runtimeOwnsThread && hasRuntimeAuthorization(runtimeConnection)) {
      try {
        await deleteLocalThread(conversationID, runtimeConnection)
        const nextRuntimeThreadIDs = new Set(runtimeThreadIDsRef.current)
        nextRuntimeThreadIDs.delete(conversationID)
        storeRuntimeThreadIDs(nextRuntimeThreadIDs)
        runtimeThreadIDsRef.current = nextRuntimeThreadIDs
      } catch (error) {
        setNotice(error instanceof Error ? error.message : t('app.notice.localRunFailed'))
        return
      }
    }
    await localData.delete(conversationID)
    if (deletedActive) {
      workspaceStoreActions.setPendingWorkspace(undefined)
      workspaceStoreActions.setPendingProject(undefined)
    }
    await refreshConversations(deletedActive ? undefined : activeIDRef.current ?? undefined, {
      preserveEmptyActive: !deletedActive && !activeIDRef.current,
    })
    setNotice(t('app.notice.conversationDeleted', { title: conversation.title }))
  }, [activeIDRef, localData, refreshConversations, runtimeConnection, runtimeThreadIDsRef, setNotice, t])

  const exportConversationData = useCallback(async (conversationID: string) => {
    const conversation = await localData.get(conversationID)
    if (!conversation) {
      setNotice(t('app.notice.conversationMissing'))
      return
    }
    const payload = {
      version: 1,
      exportedAt: new Date().toISOString(),
      conversations: [conversation],
    } as const
    downloadJson(
      payload,
      `shejane-conversation-${safeFilename(conversation.title)}-${new Date().toISOString().slice(0, 10)}.json`,
    )
    setNotice(t('app.notice.conversationExported', { title: conversation.title }))
  }, [localData, setNotice, t])

  const importLocalData = useCallback(async (file: File | undefined) => {
    if (!file) return
    const modelServices = await localData.importAll(await file.text())
    if (runtimeConnection && modelServices.length > 0) {
      const existing = new Set(
        (await listModelServices(runtimeConnection)).map((service) => service.id),
      )
      for (const service of modelServices) {
        if (
          service.preset_id !== 'custom'
          && service.preset_id !== 'shejane-official'
          && service.region !== 'official'
          && !existing.has(service.id)
        ) {
          await importModelService({ ...service, region: service.region }, runtimeConnection)
        }
      }
      runtimeStoreActions.bumpCatalogVersion()
    }
    await refreshConversations()
    setNotice(t('app.notice.localDataImported'))
  }, [localData, refreshConversations, runtimeConnection, setNotice, t])

  const exportLocalData = useCallback(async () => {
    const modelServices: ExportedModelService[] = runtimeConnection
      ? (await listModelServices(runtimeConnection)).map((service) => ({
          id: service.id,
          preset_id: service.preset_id,
          name: service.name,
          region: service.region,
          adapter_id: service.adapter_id,
          base_url: service.base_url,
          models: service.models,
        }))
      : []
    const conversationExport = await localData.exportAll(modelServices)
    downloadJson(
      {
        ...conversationExport,
        settings: { agentSettings, chatMode: mode, locale },
      },
      `shejane-local-data-${new Date().toISOString().slice(0, 10)}.json`,
    )
    setNotice(t('app.notice.localDataExported'))
  }, [agentSettings, localData, locale, mode, runtimeConnection, setNotice, t])

  return {
    deleteConversationData,
    exportConversationData,
    exportLocalData,
    importLocalData,
    renameConversation,
    togglePinConversation,
    updateConversationMetadata,
  }
}

function downloadJson(payload: unknown, filename: string): void {
  const url = URL.createObjectURL(new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' }))
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  link.click()
  URL.revokeObjectURL(url)
}

function safeFilename(value: string): string {
  return value.trim().replace(/[^\p{L}\p{N}_-]+/gu, '-').replace(/^-+|-+$/gu, '').slice(0, 48) || 'conversation'
}
