import { describe, expect, it } from 'vitest'
import type { LocalThreadSnapshot } from '../../runtime/client'
import { createTranslator } from '../../shared/i18n/i18n'
import { timelineItem } from './chatStore'
import { findConversationPendingApproval } from './pendingApproval'
import { applySubagentLifecycleEvent, projectRuntimeThread } from './runtimeProjection'

describe('Runtime thread projection', () => {
  it('tracks live lifecycle events by operation_id without treating outcome_unknown as failed', () => {
    const base = {
      operation_id: 'toolop-live',
      parent_run_id: 'run-live',
      parent_operation_id: 'toolop-parent',
      tool_call_id: 'call-live',
      subagent_type: 'researcher',
      description: 'Check primary sources',
      attempt_count: 1,
      usage: {
        model_calls: 2,
        input_tokens: 100,
        output_tokens: 20,
        unmetered_calls: 0,
        outcome_unknown_calls: 0,
      },
      error_type: null,
      created_at: '2026-08-02T00:00:01Z',
      started_at: null,
      completed_at: null,
      updated_at: '2026-08-02T00:00:01Z',
    }
    let current = applySubagentLifecycleEvent([], {
      id: 'event-spawned',
      run_id: 'run-live',
      seq: 1,
      event_type: 'subagent.spawned',
      payload: { ...base, status: 'queued', receipt_status: 'prepared' },
      created_at: '2026-08-02T00:00:01Z',
    })
    current = applySubagentLifecycleEvent(current, {
      id: 'event-started',
      run_id: 'run-live',
      seq: 2,
      event_type: 'subagent.started',
      payload: {
        ...base,
        status: 'running',
        receipt_status: 'running',
        started_at: '2026-08-02T00:00:02Z',
        updated_at: '2026-08-02T00:00:02Z',
      },
      created_at: '2026-08-02T00:00:02Z',
    })
    current = applySubagentLifecycleEvent(current, {
      id: 'event-unknown',
      run_id: 'run-live',
      seq: 3,
      event_type: 'subagent.failed',
      payload: {
        ...base,
        status: 'unknown',
        receipt_status: 'outcome_unknown',
        error_type: 'execution_lease_expired',
        completed_at: '2026-08-02T00:00:03Z',
        updated_at: '2026-08-02T00:00:03Z',
        usage: { ...base.usage, outcome_unknown_calls: 1 },
      },
      created_at: '2026-08-02T00:00:03Z',
    })

    expect(current).toEqual([
      expect.objectContaining({
        operationId: 'toolop-live',
        parentOperationId: 'toolop-parent',
        status: 'unknown',
        receiptStatus: 'outcome_unknown',
        errorType: 'execution_lease_expired',
        usage: expect.objectContaining({ outcomeUnknownCalls: 1 }),
      }),
    ])

    current = applySubagentLifecycleEvent(current, {
      id: 'event-completed-after-reconciliation',
      run_id: 'run-live',
      seq: 4,
      event_type: 'subagent.completed',
      payload: {
        ...base,
        status: 'completed',
        receipt_status: 'completed',
        error_type: null,
        completed_at: '2026-08-02T00:00:04Z',
        updated_at: '2026-08-02T00:00:04Z',
      },
      created_at: '2026-08-02T00:00:04Z',
    })
    expect(current[0]).toEqual(expect.objectContaining({ status: 'completed', receiptStatus: 'completed' }))
    expect(current[0]).not.toHaveProperty('errorType')

    current = applySubagentLifecycleEvent(current, {
      id: 'event-requeued',
      run_id: 'run-live',
      seq: 5,
      event_type: 'subagent.spawned',
      payload: {
        ...base,
        status: 'queued',
        receipt_status: 'prepared',
        error_type: null,
        started_at: null,
        completed_at: null,
        updated_at: '2026-08-02T00:00:05Z',
      },
      created_at: '2026-08-02T00:00:05Z',
    })
    expect(current[0]).toEqual(expect.objectContaining({ status: 'queued', receiptStatus: 'prepared' }))
    expect(current[0]).not.toHaveProperty('errorType')
    expect(current[0]).not.toHaveProperty('startedAt')
    expect(current[0]).not.toHaveProperty('completedAt')
  })

  it('keeps durable operations distinct when nested scopes reuse a tool_call_id', () => {
    const lifecycle = (operationId: string, parentOperationId: string) => ({
      id: `event-${operationId}`,
      run_id: 'run-shared-call',
      event_type: 'subagent.started',
      payload: {
        operation_id: operationId,
        parent_run_id: 'run-shared-call',
        parent_operation_id: parentOperationId,
        tool_call_id: 'call-reused',
        subagent_type: 'researcher',
        description: operationId,
        status: 'running',
        receipt_status: 'running',
        attempt_count: 1,
        usage: {
          model_calls: 0,
          input_tokens: 0,
          output_tokens: 0,
          unmetered_calls: 0,
          outcome_unknown_calls: 0,
        },
        error_type: null,
        created_at: '2026-08-02T00:00:01Z',
        started_at: '2026-08-02T00:00:01Z',
        completed_at: null,
        updated_at: '2026-08-02T00:00:01Z',
      },
      created_at: '2026-08-02T00:00:01Z',
    })
    let current = applySubagentLifecycleEvent([], lifecycle('toolop-a', 'parent-a'))
    current = applySubagentLifecycleEvent(current, lifecycle('toolop-b', 'parent-b'))

    expect(current.map((item) => item.operationId)).toEqual(['toolop-a', 'toolop-b'])

    const completedA = lifecycle('toolop-a', 'parent-a')
    current = applySubagentLifecycleEvent(current, {
      ...completedA,
      id: 'event-toolop-a-completed',
      event_type: 'subagent.completed',
      payload: {
        ...completedA.payload,
        status: 'completed',
        receipt_status: 'completed',
        completed_at: '2026-08-02T00:00:03Z',
        updated_at: '2026-08-02T00:00:03Z',
      },
      created_at: '2026-08-02T00:00:03Z',
    })
    expect(current.map((item) => item.operationId)).toEqual(['toolop-a', 'toolop-b'])
    expect(current[0]?.status).toBe('completed')
  })

  it('falls back to generic task events for an older Runtime and replaces the temporary identity once available', () => {
    let current = applySubagentLifecycleEvent([], {
      id: 'legacy-request',
      run_id: 'run-legacy',
      seq: 1,
      event_type: 'tool.requested',
      payload: {
        tool: 'task',
        tool_call_id: 'call-legacy',
        arguments: { subagent_type: 'researcher', description: 'Find sources' },
      },
      created_at: '2026-08-02T00:00:01Z',
    })
    expect(current).toEqual([
      expect.objectContaining({
        operationId: 'legacy-task:run-legacy:call-legacy',
        status: 'running',
      }),
    ])

    const oldRuntimeFailure = applySubagentLifecycleEvent(current, {
      id: 'legacy-completed-with-error',
      run_id: 'run-legacy',
      seq: 2,
      event_type: 'subagent.completed',
      payload: {
        tool_call_id: 'call-legacy',
        status: 'error',
      },
      created_at: '2026-08-02T00:00:02Z',
    })
    expect(oldRuntimeFailure).toEqual([
      expect.objectContaining({ status: 'failed', receiptStatus: 'failed' }),
    ])

    const oldRuntimeUnknown = applySubagentLifecycleEvent(current, {
      id: 'legacy-tool-outcome-unknown',
      run_id: 'run-legacy',
      seq: 2,
      event_type: 'tool.failed',
      payload: {
        tool: 'task',
        tool_call_id: 'call-legacy',
        error_code: 'tool_outcome_unknown',
      },
      created_at: '2026-08-02T00:00:02Z',
    })
    expect(oldRuntimeUnknown).toEqual([
      expect.objectContaining({ status: 'unknown', receiptStatus: 'outcome_unknown' }),
    ])

    current = applySubagentLifecycleEvent(current, {
      id: 'durable-spawn',
      run_id: 'run-legacy',
      seq: 2,
      event_type: 'subagent.spawned',
      payload: {
        operation_id: 'toolop-durable',
        parent_run_id: 'run-legacy',
        tool_call_id: 'call-legacy',
        subagent_type: 'researcher',
        description: 'Find sources',
        status: 'queued',
        receipt_status: 'prepared',
        attempt_count: 0,
        usage: {
          model_calls: 0,
          input_tokens: 0,
          output_tokens: 0,
          unmetered_calls: 0,
          outcome_unknown_calls: 0,
        },
        parent_operation_id: null,
        error_type: null,
        created_at: '2026-08-02T00:00:01Z',
        started_at: null,
        completed_at: null,
        updated_at: '2026-08-02T00:00:02Z',
      },
      created_at: '2026-08-02T00:00:02Z',
    })
    expect(current).toEqual([
      expect.objectContaining({ operationId: 'toolop-durable', status: 'queued' }),
    ])
  })

  it('rebuilds current subagent state from the Run snapshot when lifecycle events are truncated', () => {
    const snapshot = {
      thread: {
        id: 'conversation-subagents',
        title: 'Subagents',
        metadata: {},
        version: 1,
        created_at: '2026-08-02T00:00:00Z',
        updated_at: '2026-08-02T00:00:03Z',
      },
      items: [{
        id: 'assistant-subagents',
        thread_id: 'conversation-subagents',
        run_id: 'run-subagents',
        client_id: 'assistant-subagents-client',
        item_type: 'assistant_message',
        status: 'in_progress',
        content: '',
        metadata: {},
        position: 1,
        version: 1,
        created_at: '2026-08-02T00:00:00Z',
        updated_at: '2026-08-02T00:00:03Z',
      }],
      runs: [{
        id: 'run-subagents',
        goal: 'Research in parallel',
        status: 'running',
        thread_id: 'conversation-subagents',
        history_json: '[]',
        settings_json: '{}',
        metadata_json: '{}',
        inputs: [],
        created_at: '2026-08-02T00:00:00Z',
        updated_at: '2026-08-02T00:00:03Z',
        subagent_invocations: [
          {
            operation_id: 'toolop_running',
            parent_run_id: 'run-subagents',
            tool_call_id: 'call-running',
            subagent_type: 'researcher',
            description: 'Collect primary sources',
            status: 'running',
            receipt_status: 'running',
            attempt_count: 1,
            usage: {
              model_calls: 2,
              input_tokens: 120,
              output_tokens: 40,
              unmetered_calls: 1,
              outcome_unknown_calls: 0,
            },
            error_type: null,
            created_at: '2026-08-02T00:00:01Z',
            started_at: '2026-08-02T00:00:01Z',
            completed_at: null,
            updated_at: '2026-08-02T00:00:02Z',
          },
          {
            operation_id: 'toolop_unknown',
            parent_run_id: 'run-subagents',
            tool_call_id: 'call-unknown',
            subagent_type: 'writer',
            description: 'Draft the answer',
            status: 'unknown',
            receipt_status: 'outcome_unknown',
            attempt_count: 1,
            usage: {
              model_calls: 1,
              input_tokens: 80,
              output_tokens: 10,
              unmetered_calls: 0,
              outcome_unknown_calls: 1,
            },
            error_type: 'execution_lease_expired',
            created_at: '2026-08-02T00:00:01Z',
            started_at: '2026-08-02T00:00:01Z',
            completed_at: '2026-08-02T00:00:03Z',
            updated_at: '2026-08-02T00:00:03Z',
          },
        ],
      }],
      events: [
        {
          id: 'event-stale-terminal',
          run_id: 'run-subagents',
          seq: 7,
          event_type: 'subagent.completed',
          payload: {
            operation_id: 'toolop_running',
            parent_run_id: 'run-subagents',
            tool_call_id: 'call-running',
            subagent_type: 'researcher',
            description: 'Collect primary sources',
            status: 'completed',
            receipt_status: 'completed',
            attempt_count: 1,
            usage: {
              model_calls: 0,
              input_tokens: 0,
              output_tokens: 0,
              unmetered_calls: 0,
              outcome_unknown_calls: 0,
            },
            parent_operation_id: null,
            error_type: null,
            created_at: '2026-08-02T00:00:01Z',
            started_at: '2026-08-02T00:00:01Z',
            completed_at: '2026-08-02T00:00:02Z',
            updated_at: '2026-08-02T00:00:02Z',
          },
          created_at: '2026-08-02T00:00:02Z',
        },
        {
          id: 'event-supplement',
          run_id: 'run-subagents',
          seq: 8,
          event_type: 'subagent.waiting',
          payload: {
            operation_id: 'toolop_supplement',
            parent_run_id: 'run-subagents',
            tool_call_id: 'call-running',
            subagent_type: 'reviewer',
            description: 'Wait for source collection',
            status: 'waiting',
            receipt_status: 'paused',
            attempt_count: 1,
            usage: {
              model_calls: 0,
              input_tokens: 0,
              output_tokens: 0,
              unmetered_calls: 0,
              outcome_unknown_calls: 0,
            },
            parent_operation_id: null,
            error_type: null,
            created_at: '2026-08-02T00:00:01Z',
            started_at: '2026-08-02T00:00:01Z',
            completed_at: null,
            updated_at: '2026-08-02T00:00:03Z',
          },
          created_at: '2026-08-02T00:00:03Z',
        },
      ],
      event_high_watermarks: { 'run-subagents': 8 },
      cursor: 1,
      has_more_items: false,
      events_truncated: true,
    } as unknown as LocalThreadSnapshot

    const projected = projectRuntimeThread(snapshot).messages[0]?.subagents
    expect(projected).toHaveLength(2)
    expect(projected?.find((item) => item.operationId === 'toolop_running')).toEqual(
      expect.objectContaining({
        operationId: 'toolop_running',
        subagentType: 'researcher',
        description: 'Collect primary sources',
        status: 'running',
        receiptStatus: 'running',
        usage: {
          modelCalls: 2,
          inputTokens: 120,
          outputTokens: 40,
          unmeteredCalls: 1,
          outcomeUnknownCalls: 0,
        },
      }),
    )
    expect(projected?.find((item) => item.operationId === 'toolop_unknown')).toEqual(
      expect.objectContaining({
        operationId: 'toolop_unknown',
        subagentType: 'writer',
        status: 'unknown',
        receiptStatus: 'outcome_unknown',
        usage: expect.objectContaining({ outcomeUnknownCalls: 1 }),
        errorType: 'execution_lease_expired',
      }),
    )
    expect(projected?.find((item) => item.operationId === 'toolop_supplement')).toBeUndefined()
  })

  it('rebuilds visible messages, metadata, statuses, and timeline from Runtime truth', () => {
    const snapshot: LocalThreadSnapshot = {
      thread: {
        id: 'conversation-1',
        title: 'Visible title',
        metadata: {
          pinned: true,
          model: 'local:conn_1:deepseek-v4-flash',
          workspace: { path: '/tmp/project', label: 'project', authorized: true },
        },
        version: 2,
        created_at: '2026-07-12T00:00:00Z',
        updated_at: '2026-07-12T00:00:02Z',
      },
      items: [
        {
          id: 'runtime-user',
          thread_id: 'conversation-1',
          run_id: 'run-1',
          client_id: 'user-1',
          item_type: 'user_message',
          status: 'completed',
          content: 'Visible question',
          metadata: {
            attachments: [{
              path: '/tmp/brief.pdf',
              name: 'brief.pdf',
            }],
            plugin_selection: {
              references: [{
                plugin_id: 'dev.shejane.fixture.archive',
                name: 'Archive fixture',
                digest: `sha256:${'a'.repeat(64)}`,
              }],
              command: {
                plugin_id: 'dev.shejane.fixture.archive',
                plugin_name: 'Archive fixture',
                command_id: 'extract',
                title: 'Extract archive',
                digest: `sha256:${'a'.repeat(64)}`,
              },
            },
          },
          position: 1,
          version: 1,
          created_at: '2026-07-12T00:00:00Z',
          updated_at: '2026-07-12T00:00:00Z',
        },
        {
          id: 'assistant-1',
          thread_id: 'conversation-1',
          run_id: 'run-1',
          client_id: 'assistant-client-1',
          item_type: 'assistant_message',
          status: 'completed',
          content: 'Done',
          metadata: {},
          position: 2,
          version: 2,
          created_at: '2026-07-12T00:00:01Z',
          updated_at: '2026-07-12T00:00:02Z',
          completed_at: '2026-07-12T00:00:02Z',
        },
      ],
      runs: [{
        id: 'run-1',
        run_kind: 'turn',
        root_run_id: 'run-1',
        agent_definition_id: 'shejane.default',
        agent_definition_version: '1',
        collaboration_depth: 0,
        collaboration_policy_json: '{}',
        goal: 'Internal directive\nVisible question',
        user_input: 'Visible question',
        status: 'completed',
        thread_id: 'conversation-1',
        assistant_item_id: 'assistant-1',
        command_id: 'cmd-1',
        history_json: '[]',
        settings_json: '{}',
        metadata_json: '{}',
        inputs: [{
          client_index: 0,
          input_id: 'source',
          virtual_path: '/attachments/brief.pdf',
          original_name: 'brief.pdf',
          media_type: 'application/pdf',
          bytes: 123,
          sha256: 'a'.repeat(64),
        }],
        created_at: '2026-07-12T00:00:00Z',
        updated_at: '2026-07-12T00:00:02Z',
      }],
      events: [{
        id: 'event-1',
        run_id: 'run-1',
        seq: 1,
        event_type: 'run.completed',
        payload: { final_text: 'Done' },
        created_at: '2026-07-12T00:00:02Z',
      }],
      event_high_watermarks: { 'run-1': 9 },
      cursor: 2,
      has_more_items: false,
      events_truncated: false,
    }

    const conversation = projectRuntimeThread(snapshot)

    expect(conversation).toMatchObject({
      id: 'conversation-1',
      title: 'Visible title',
      pinned: true,
      model: 'local:conn_1:deepseek-v4-flash',
      workspace: { path: '/tmp/project' },
    })
    expect(conversation.messages).toMatchObject([
      {
        id: 'user-1',
        role: 'user',
        content: 'Visible question',
        runId: 'run-1',
        attachments: [{
          path: '/tmp/brief.pdf',
          name: 'brief.pdf',
          runId: 'run-1',
          inputId: 'source',
        }],
        pluginReferences: [{
          pluginId: 'dev.shejane.fixture.archive',
          name: 'Archive fixture',
          digest: `sha256:${'a'.repeat(64)}`,
        }],
        pluginCommand: {
          pluginId: 'dev.shejane.fixture.archive',
          pluginName: 'Archive fixture',
          commandId: 'extract',
          title: 'Extract archive',
          digest: `sha256:${'a'.repeat(64)}`,
        },
      },
      {
        id: 'assistant-client-1',
        role: 'assistant',
        content: 'Done',
        status: 'done',
        runId: 'run-1',
        commandId: 'cmd-1',
        lastEventSeq: 9,
        agentEvents: [{ type: 'run.completed' }],
      },
    ])
  })

  it('does not move a live client cursor backwards when an older snapshot arrives', () => {
    const snapshot: LocalThreadSnapshot = {
      thread: {
        id: 'conversation-live',
        title: 'Live',
        metadata: {},
        version: 1,
        created_at: '2026-07-12T00:00:00Z',
        updated_at: '2026-07-12T00:00:01Z',
      },
      items: [{
        id: 'assistant-live',
        thread_id: 'conversation-live',
        run_id: 'run-live',
        client_id: 'assistant-live-client',
        item_type: 'assistant_message',
        status: 'in_progress',
        content: '',
        metadata: {},
        position: 1,
        version: 1,
        created_at: '2026-07-12T00:00:00Z',
        updated_at: '2026-07-12T00:00:01Z',
      }],
      runs: [{
        id: 'run-live',
        run_kind: 'turn',
        root_run_id: 'run-live',
        agent_definition_id: 'shejane.default',
        agent_definition_version: '1',
        collaboration_depth: 0,
        collaboration_policy_json: '{}',
        goal: 'Live',
        status: 'running',
        thread_id: 'conversation-live',
        history_json: '[]',
        settings_json: '{}',
        metadata_json: '{}',
        inputs: [],
        created_at: '2026-07-12T00:00:00Z',
        updated_at: '2026-07-12T00:00:01Z',
      }],
      events: [],
      event_high_watermarks: { 'run-live': 0 },
      cursor: 1,
      has_more_items: false,
      events_truncated: false,
    }
    const existing = {
      id: 'conversation-live',
      title: 'Live',
      archived: false,
      createdAt: '2026-07-12T00:00:00Z',
      updatedAt: '2026-07-12T00:00:01Z',
      messages: [{
        id: 'assistant-live-client',
        role: 'assistant' as const,
        content: 'newer live text',
        createdAt: '2026-07-12T00:00:00Z',
        status: 'streaming' as const,
        runId: 'run-live',
        lastEventSeq: 8,
      }],
    }

    const conversation = projectRuntimeThread(snapshot, existing)

    expect(conversation.messages[0]?.lastEventSeq).toBe(8)
  })

  it('replays a permission event omitted by a truncated snapshot', () => {
    const t = createTranslator('zh')
    const snapshot: LocalThreadSnapshot = {
      thread: {
        id: 'conversation-permission-replay',
        title: 'Permission replay',
        metadata: {},
        version: 1,
        created_at: '2026-08-02T00:00:00Z',
        updated_at: '2026-08-02T00:00:01Z',
      },
      items: [{
        id: 'assistant-permission-replay',
        thread_id: 'conversation-permission-replay',
        run_id: 'run-permission-replay',
        client_id: 'assistant-permission-replay-client',
        item_type: 'assistant_message',
        status: 'waiting_permission',
        content: '',
        metadata: {},
        position: 1,
        version: 1,
        created_at: '2026-08-02T00:00:00Z',
        updated_at: '2026-08-02T00:00:01Z',
      }],
      runs: [{
        id: 'run-permission-replay',
        run_kind: 'turn',
        root_run_id: 'run-permission-replay',
        agent_definition_id: 'shejane.default',
        agent_definition_version: '1',
        collaboration_depth: 0,
        collaboration_policy_json: '{}',
        goal: 'Run a command',
        status: 'waiting_permission',
        thread_id: 'conversation-permission-replay',
        history_json: '[]',
        settings_json: '{}',
        metadata_json: '{}',
        inputs: [],
        created_at: '2026-08-02T00:00:00Z',
        updated_at: '2026-08-02T00:00:01Z',
      }],
      events: [],
      event_high_watermarks: { 'run-permission-replay': 0 },
      cursor: 1,
      has_more_items: false,
      events_truncated: true,
    }

    const conversation = projectRuntimeThread(snapshot, {
      id: 'conversation-permission-replay',
      title: 'Permission replay',
      archived: false,
      createdAt: '2026-08-02T00:00:00Z',
      updatedAt: '2026-08-02T00:00:01Z',
      messages: [{
        id: 'assistant-permission-replay-client',
        role: 'assistant',
        content: '',
        createdAt: '2026-08-02T00:00:00Z',
        status: 'waiting_permission',
        runId: 'run-permission-replay',
        lastEventSeq: 9,
      }],
    })
    const message = conversation.messages[0]
    expect(message?.lastEventSeq).toBe(0)
    expect(findConversationPendingApproval(conversation, t)).toBeNull()

    const replayed = timelineItem({
      id: 'event-permission-replay',
      run_id: 'run-permission-replay',
      seq: 1,
      event_type: 'permission.required',
      payload: { request_id: 'permission-replay', tool: 'execute' },
      created_at: '2026-08-02T00:00:01Z',
    })
    expect(replayed).not.toBeNull()
    message!.agentEvents = [replayed!]
    message!.lastEventSeq = 1
    expect(findConversationPendingApproval(conversation, t)).toMatchObject({
      requestID: 'permission-replay',
    })
  })

  it('omits an internal retry input while keeping its assistant result', () => {
    const snapshot: LocalThreadSnapshot = {
      thread: {
        id: 'conversation-retry',
        title: 'Retry',
        metadata: {},
        version: 2,
        created_at: '2026-07-12T00:00:00Z',
        updated_at: '2026-07-12T00:00:02Z',
      },
      items: [
        {
          id: 'internal-retry-user',
          thread_id: 'conversation-retry',
          run_id: 'run-retry',
          client_id: 'internal-retry-user-client',
          item_type: 'user_message',
          status: 'completed',
          content: 'Do not show this duplicate prompt',
          metadata: { hidden_from_transcript: true },
          position: 1,
          version: 1,
          created_at: '2026-07-12T00:00:01Z',
          updated_at: '2026-07-12T00:00:01Z',
        },
        {
          id: 'retry-assistant',
          thread_id: 'conversation-retry',
          run_id: 'run-retry',
          client_id: 'retry-assistant-client',
          item_type: 'assistant_message',
          status: 'completed',
          content: 'Recovered',
          metadata: {},
          position: 2,
          version: 1,
          created_at: '2026-07-12T00:00:01Z',
          updated_at: '2026-07-12T00:00:02Z',
        },
      ],
      runs: [{
        id: 'run-retry',
        run_kind: 'turn',
        root_run_id: 'run-retry',
        agent_definition_id: 'shejane.default',
        agent_definition_version: '1',
        collaboration_depth: 0,
        collaboration_policy_json: '{}',
        goal: 'Do not show this duplicate prompt',
        status: 'completed',
        thread_id: 'conversation-retry',
        assistant_item_id: 'retry-assistant',
        history_json: '[]',
        settings_json: '{}',
        metadata_json: '{"intent":"retry"}',
        inputs: [],
        created_at: '2026-07-12T00:00:01Z',
        updated_at: '2026-07-12T00:00:02Z',
      }],
      events: [],
      event_high_watermarks: { 'run-retry': 0 },
      cursor: 2,
      has_more_items: false,
      events_truncated: false,
    }

    expect(projectRuntimeThread(snapshot).messages).toMatchObject([
      { role: 'assistant', content: 'Recovered' },
    ])
  })
})
