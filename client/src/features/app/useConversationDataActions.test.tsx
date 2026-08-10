import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { LocalConversationStore } from '@/shared/local-data/localConversations'
import type { Conversation } from '@/shared/local-data/types'
import type { AgentSettings, RuntimeConnection } from '@/runtime/client'
import { useConversationDataActions } from './useConversationDataActions'

const hasRuntimeAuthorization = vi.hoisted(() => vi.fn(() => true))
const updateLocalThread = vi.hoisted(() => vi.fn())

vi.mock('@/runtime/client', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/runtime/client')>(),
  hasRuntimeAuthorization,
  updateLocalThread,
}))

const runtimeConnection: RuntimeConnection = {
  baseURL: 'http://127.0.0.1:17371',
  session: 'client',
}

describe('useConversationDataActions', () => {
  afterEach(() => {
    cleanup()
    hasRuntimeAuthorization.mockClear()
    updateLocalThread.mockReset()
  })

  it('persists Runtime-owned metadata before refreshing the local projection', async () => {
    const conversation: Conversation = {
      id: 'thread-1',
      title: 'Before',
      archived: false,
      createdAt: '2026-08-10T00:00:00.000Z',
      updatedAt: '2026-08-10T00:00:00.000Z',
      messages: [],
    }
    const localData = {
      get: vi.fn().mockResolvedValue(conversation),
      save: vi.fn().mockResolvedValue(undefined),
    } as unknown as LocalConversationStore
    const refreshConversations = vi.fn().mockResolvedValue(undefined)
    const { result } = renderHook(() => useConversationDataActions({
      activeIDRef: { current: conversation.id },
      agentSettings: {} as Required<AgentSettings>,
      localData,
      locale: 'zh',
      mode: '' as never,
      refreshConversations,
      runtimeConnection,
      runtimeThreadIDsRef: { current: new Set([conversation.id]) },
      setNotice: vi.fn(),
      t: ((key: string) => key) as never,
    }))

    await act(async () => {
      await result.current.updateConversationMetadata(conversation.id, (item) => {
        item.title = 'After'
      })
    })

    expect(updateLocalThread).toHaveBeenCalledWith(
      conversation.id,
      expect.objectContaining({ title: 'After' }),
      runtimeConnection,
    )
    expect(localData.save).toHaveBeenCalledWith(expect.objectContaining({ title: 'After' }))
    expect(refreshConversations).toHaveBeenCalledWith(conversation.id, {
      preserveEmptyActive: false,
    })
  })
})
