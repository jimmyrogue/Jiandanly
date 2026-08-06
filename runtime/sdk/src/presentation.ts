import type { RunPresentationItem, RunPresentationSnapshot } from './client.js'
import type { RunPresentationChange } from './sse.js'

export interface RunPresentationState {
  snapshot: RunPresentationSnapshot
  drafts: Record<string, string>
}

export function createRunPresentationState(
  snapshot: RunPresentationSnapshot,
): RunPresentationState {
  return {
    snapshot: { ...snapshot, items: [...(snapshot.items ?? [])] },
    drafts: {},
  }
}

export function applyRunPresentationChange(
  state: RunPresentationState,
  change: RunPresentationChange,
): RunPresentationState {
  if (change.kind === 'draft.delta') {
    return {
      ...state,
      drafts: {
        ...state.drafts,
        [change.round_id]: `${state.drafts[change.round_id] ?? ''}${change.content}`,
      },
    }
  }
  if (change.kind === 'draft.closed') {
    const drafts = { ...state.drafts }
    delete drafts[change.round_id]
    return { ...state, drafts }
  }

  const items = new Map<string, RunPresentationItem>(
    (state.snapshot.items ?? []).map(item => [item.id, item]),
  )
  const current = items.get(change.item.id)
  if (!current || change.item.revision >= current.revision) {
    items.set(change.item.id, change.item)
  }
  return {
    ...state,
    drafts: change.item.kind === 'final_answer' ? {} : state.drafts,
    snapshot: {
      ...state.snapshot,
      items: [...items.values()].sort(comparePresentationItems),
      event_high_watermark: Math.max(
        state.snapshot.event_high_watermark,
        change.item.revision,
      ),
    },
  }
}

function comparePresentationItems(left: RunPresentationItem, right: RunPresentationItem): number {
  return left.order.event_seq - right.order.event_seq
    || left.order.slot - right.order.slot
    || left.id.localeCompare(right.id)
}
