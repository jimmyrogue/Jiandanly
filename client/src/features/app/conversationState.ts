import type { Conversation, LocalAttachmentRef } from '@/shared/local-data/types'

export function cloneConversation(conversation: Conversation): Conversation {
  return {
    ...conversation,
    project: conversation.project ? { ...conversation.project } : undefined,
    workspace: conversation.workspace ? { ...conversation.workspace } : undefined,
    messages: conversation.messages.map((message) => ({
      ...message,
      attachments: message.attachments?.map((attachment) => ({ ...attachment })),
      agentEvents: message.agentEvents ? [...message.agentEvents] : undefined,
    })),
  }
}

export function upsertConversation(items: Conversation[], conversation: Conversation): Conversation[] {
  return [conversation, ...items.filter((item) => item.id !== conversation.id)]
}

export function sortConversationsForSidebar(items: Conversation[]): Conversation[] {
  return [...items].sort((a, b) => {
    if (Boolean(a.pinned) !== Boolean(b.pinned)) return a.pinned ? -1 : 1
    return b.updatedAt.localeCompare(a.updatedAt)
  })
}

export function mergeAttachments(
  current: LocalAttachmentRef[],
  additions: LocalAttachmentRef[],
): LocalAttachmentRef[] {
  const byPath = new Map(current.map((item) => [item.path, item]))
  for (const item of additions) byPath.set(item.path, item)
  return [...byPath.values()].slice(0, 10)
}

export async function mapWithConcurrency<T, R>(
  values: T[],
  concurrency: number,
  map: (value: T) => Promise<R>,
): Promise<R[]> {
  const results = new Array<R>(values.length)
  let next = 0
  async function worker() {
    while (next < values.length) {
      const index = next
      next += 1
      results[index] = await map(values[index])
    }
  }
  await Promise.all(Array.from({ length: Math.min(Math.max(1, concurrency), values.length) }, () => worker()))
  return results
}
