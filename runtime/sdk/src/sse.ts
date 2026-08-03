export interface AgentRunEvent {
  event_type: string
  payload?: Record<string, unknown>
  id?: string
  run_id?: string
  seq?: number
  created_at?: string
}

export const SUBAGENT_LIFECYCLE_EVENT_TYPES = [
  'subagent.spawned',
  'subagent.started',
  'subagent.waiting',
  'subagent.completed',
  'subagent.failed',
  'subagent.canceled',
  'subagent.outcome_unknown',
  'child.spawned',
  'child.started',
  'child.waiting',
  'child.completed',
  'child.failed',
  'child.canceled',
  'child.cleanup_required',
] as const

export type SubagentLifecycleEventType = typeof SUBAGENT_LIFECYCLE_EVENT_TYPES[number]
export type SubagentInvocationStatus =
  | 'queued'
  | 'running'
  | 'waiting'
  | 'completed'
  | 'failed'
  | 'canceled'
  | 'unknown'
export type SubagentReceiptStatus =
  | 'prepared'
  | 'running'
  | 'paused'
  | 'completed'
  | 'failed'
  | 'outcome_unknown'
  | 'rejected'
  | 'canceled'

export interface SubagentUsage {
  model_calls: number
  input_tokens: number
  output_tokens: number
  unmetered_calls: number
  outcome_unknown_calls: number
}

export interface SubagentLifecyclePayload extends Record<string, unknown> {
  operation_id: string
  parent_run_id: string
  parent_operation_id: string | null
  tool_call_id: string
  subagent_type: string
  description: string
  status: SubagentInvocationStatus
  receipt_status: SubagentReceiptStatus
  attempt_count: number
  usage: SubagentUsage
  error_type: string | null
  created_at: string
  started_at: string | null
  completed_at: string | null
  updated_at: string
}

export interface SubagentLifecycleEvent extends AgentRunEvent {
  event_type: SubagentLifecycleEventType
  payload: SubagentLifecyclePayload
}

export function isSubagentLifecycleEvent(
  event: AgentRunEvent,
): event is SubagentLifecycleEvent {
  if (!(SUBAGENT_LIFECYCLE_EVENT_TYPES as readonly string[]).includes(event.event_type)) {
    return false
  }
  const payload = event.payload
  if (!payload) return false
  const usage = payload.usage
  return typeof payload.operation_id === 'string'
    && Boolean(payload.operation_id)
    && typeof payload.parent_run_id === 'string'
    && Boolean(payload.parent_run_id)
    && (payload.parent_operation_id === null || typeof payload.parent_operation_id === 'string')
    && typeof payload.tool_call_id === 'string'
    && typeof payload.subagent_type === 'string'
    && typeof payload.description === 'string'
    && isSubagentInvocationStatus(payload.status)
    && isSubagentReceiptStatus(payload.receipt_status)
    && nonNegativeInteger(payload.attempt_count)
    && typeof usage === 'object'
    && usage !== null
    && !Array.isArray(usage)
    && nonNegativeInteger((usage as Record<string, unknown>).model_calls)
    && nonNegativeInteger((usage as Record<string, unknown>).input_tokens)
    && nonNegativeInteger((usage as Record<string, unknown>).output_tokens)
    && nonNegativeInteger((usage as Record<string, unknown>).unmetered_calls)
    && nonNegativeInteger((usage as Record<string, unknown>).outcome_unknown_calls)
    && typeof payload.created_at === 'string'
    && (payload.started_at === null || typeof payload.started_at === 'string')
    && (payload.completed_at === null || typeof payload.completed_at === 'string')
    && typeof payload.updated_at === 'string'
    && (payload.error_type === null || typeof payload.error_type === 'string')
}

export type AgentSSEEvent =
  | { type: 'agent'; event: AgentRunEvent }
  | { type: 'done' }
  | { type: 'ignore' }

export interface StreamTransportHandlers {
  onDelta: (content: string, event: AgentRunEvent) => void
  onEvent?: (event: AgentRunEvent) => void
}

export interface AgentStreamResult {
  requestId: string
  inputTokens: number
  outputTokens: number
  completed: boolean
}

export function parseAgentSSEBuffer(buffer: string): { events: AgentSSEEvent[]; rest: string } {
  const chunks = buffer.split(/\n\n/)
  const rest = chunks.pop() ?? ''
  const events = chunks.map(parseAgentSSEChunk).filter((event) => event.type !== 'ignore')
  return { events, rest }
}

export async function streamAgentSSE(
  response: Response,
  handlers: StreamTransportHandlers,
  signal?: AbortSignal,
): Promise<AgentStreamResult> {
  if (signal?.aborted) throw new Error('Stream transport aborted')
  if (!response.ok || !response.body) {
    throw new Error(`Stream transport HTTP ${response.status}`)
  }

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let done = false
  let completed = false
  let requestId = response.headers.get('X-Request-ID') ?? ''
  let inputTokens = 0
  let outputTokens = 0

  while (!done) {
    if (signal?.aborted) throw new Error('Stream transport aborted')
    const result = await reader.read()
    done = result.done
    buffer += decoder.decode(result.value ?? new Uint8Array(), { stream: !done })
    let parsed: ReturnType<typeof parseAgentSSEBuffer>
    try {
      parsed = parseAgentSSEBuffer(buffer)
    } catch (error) {
      throw new Error(`Malformed Agent SSE: ${error instanceof Error ? error.message : String(error)}`)
    }
    buffer = parsed.rest
    for (const parsedEvent of parsed.events) {
      if (parsedEvent.type === 'done') {
        completed = true
        continue
      }
      if (parsedEvent.type !== 'agent') continue
      handlers.onEvent?.(parsedEvent.event)
      if (parsedEvent.event.event_type === 'llm.delta') {
        const content = parsedEvent.event.payload?.content
        if (typeof content === 'string') handlers.onDelta(content, parsedEvent.event)
      }
      if (parsedEvent.event.event_type === 'run.completed') {
        requestId = stringPayload(parsedEvent.event, 'request_id') || requestId
        inputTokens = numberPayload(parsedEvent.event, 'input_tokens')
        outputTokens = numberPayload(parsedEvent.event, 'output_tokens')
      }
    }
  }

  return { requestId, inputTokens, outputTokens, completed }
}

function parseAgentSSEChunk(chunk: string): AgentSSEEvent {
  const dataLines = chunk
    .split(/\n/)
    .map((line) => line.trim())
    .filter((line) => line.startsWith('data:'))
    .map((line) => line.slice('data:'.length).trim())
  if (dataLines.length === 0) return { type: 'ignore' }
  const data = dataLines.join('\n')
  if (data === '[DONE]') return { type: 'done' }
  const event = JSON.parse(data) as unknown
  if (
    !event
    || typeof event !== 'object'
    || typeof (event as { event_type?: unknown }).event_type !== 'string'
    || !(event as { event_type: string }).event_type
  ) {
    throw new Error('Runtime event envelope requires event_type')
  }
  return { type: 'agent', event: event as AgentRunEvent }
}

function stringPayload(event: AgentRunEvent, key: string): string {
  const value = event.payload?.[key]
  return typeof value === 'string' ? value : ''
}

function numberPayload(event: AgentRunEvent, key: string): number {
  const value = event.payload?.[key]
  return typeof value === 'number' ? value : 0
}

function nonNegativeInteger(value: unknown): boolean {
  return typeof value === 'number' && Number.isInteger(value) && value >= 0
}

function isSubagentInvocationStatus(value: unknown): value is SubagentInvocationStatus {
  return value === 'queued'
    || value === 'running'
    || value === 'waiting'
    || value === 'completed'
    || value === 'failed'
    || value === 'canceled'
    || value === 'unknown'
}

function isSubagentReceiptStatus(value: unknown): value is SubagentReceiptStatus {
  return value === 'prepared'
    || value === 'running'
    || value === 'paused'
    || value === 'completed'
    || value === 'failed'
    || value === 'outcome_unknown'
    || value === 'rejected'
    || value === 'canceled'
}
