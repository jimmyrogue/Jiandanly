import { describe, expect, it } from 'vitest'
import type { Conversation } from '@/shared/local-data/types'
import { upsertConversation } from './conversationState'

function conversation(id: string, updatedAt: string, pinned = false): Conversation {
  return {
    id,
    title: id,
    archived: false,
    pinned,
    createdAt: updatedAt,
    updatedAt,
    messages: [],
  }
}

describe('conversationState', () => {
  it('keeps live projection upserts sorted by pinned state and update time', () => {
    const items = [
      conversation('pinned-newer', '2026-08-09T12:00:00.000Z', true),
      conversation('regular-newer', '2026-08-09T11:00:00.000Z'),
      conversation('regular-older', '2026-08-09T09:00:00.000Z'),
    ]

    const result = upsertConversation(
      items,
      conversation('pinned-older', '2026-08-09T08:00:00.000Z', true),
    )

    expect(result.map((item) => item.id)).toEqual([
      'pinned-newer',
      'pinned-older',
      'regular-newer',
      'regular-older',
    ])
  })
})
