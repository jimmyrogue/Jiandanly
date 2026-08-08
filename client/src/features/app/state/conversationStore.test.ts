import { afterEach, describe, expect, it } from 'vitest'
import { conversationStore, conversationStoreActions } from './conversationStore'

describe('conversationStore', () => {
  afterEach(() => {
    conversationStoreActions.resetForTests()
  })

  it('starts with an empty conversation list', () => {
    expect(conversationStore.getState().conversations).toEqual([])
    expect(conversationStore.getState().activeID).toBeUndefined()
  })

  it('upserts conversations through functional updates', () => {
    conversationStoreActions.setConversations([{ id: 'conv-1', title: 'A' } as never])
    conversationStoreActions.setConversations((items) => [
      ...items,
      { id: 'conv-2', title: 'B' } as never,
    ])
    expect(conversationStore.getState().conversations.map((item) => item.id)).toEqual(['conv-1', 'conv-2'])
  })

  it('tracks the active conversation id', () => {
    conversationStoreActions.setActiveID('conv-1')
    expect(conversationStore.getState().activeID).toBe('conv-1')
    conversationStoreActions.setActiveID(undefined)
    expect(conversationStore.getState().activeID).toBeUndefined()
  })

  it('notifies subscribers on state change', () => {
    const seen: Array<string | undefined> = []
    const unsubscribe = conversationStore.subscribe(() => {
      seen.push(conversationStore.getState().activeID)
    })
    conversationStoreActions.setActiveID('conv-2')
    unsubscribe()
    conversationStoreActions.setActiveID('conv-3')
    expect(seen).toEqual(['conv-2'])
  })
})
