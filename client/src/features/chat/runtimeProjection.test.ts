import { describe, expect, it } from 'vitest'
import type { LocalThreadSnapshot } from '../../runtime/client'
import { projectRuntimeThread } from './runtimeProjection'

describe('Runtime thread projection', () => {
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
