import type { ModelOption } from '@/features/chat/components/ModeSelector'
import type { ChatMode } from '@/shared/local-data/types'

export function chooseAvailableMode(models: ModelOption[], ...candidates: ChatMode[]): ChatMode {
  return candidates.find((candidate) => models.some((model) => model.id === candidate))
    ?? models.find((model) => model.recommended)?.id
    ?? models[0]?.id
    ?? ''
}
