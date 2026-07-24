export function withStableTimelineKeys<T extends { eventId?: string }>(
  items: T[],
): Array<{ item: T; key: string }> {
  const occurrences = new Map<string, number>()
  return items.map((item) => {
    const identity = item.eventId ?? JSON.stringify(item)
    const occurrence = occurrences.get(identity) ?? 0
    occurrences.set(identity, occurrence + 1)
    return {
      item,
      key: occurrence ? `${identity}:${occurrence}` : identity,
    }
  })
}
