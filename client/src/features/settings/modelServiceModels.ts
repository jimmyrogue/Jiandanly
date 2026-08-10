import type {
  ModelServiceConnection,
  VerifyModelServiceModelRequest,
} from '@/runtime/client'

export type ModelCapabilityName = VerifyModelServiceModelRequest['capability']
export type ModelProtocol = VerifyModelServiceModelRequest['protocol']

export function defaultModelProtocol(
  service: ModelServiceConnection,
  capability: ModelCapabilityName,
): ModelProtocol {
  if (capability === 'image_generation') return 'openai_images_generations'
  if (capability === 'image_editing') return 'openai_images_edits'
  if (service.adapter_id === 'google_genai') return 'google_generate_content'
  if (service.preset_id === 'openai') return 'openai_responses'
  return service.adapter_id === 'anthropic_messages'
    ? 'anthropic_messages'
    : 'openai_chat_completions'
}
