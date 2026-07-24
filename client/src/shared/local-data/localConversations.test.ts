import 'fake-indexeddb/auto'
import { beforeAll, describe, expect, it } from 'vitest'
import { LocalConversationStore } from './localConversations'
import type { Conversation } from './types'
import type { PendingRunStartCommand } from '../../runtime/client'

describe('LocalConversationStore', () => {
  beforeAll(async () => {
    await Promise.all([
      deleteDatabase('shejane-test'),
      deleteDatabase('shejane-test-pinned'),
      deleteDatabase('shejane-test-pending-command'),
      deleteDatabase('shejane-test-delete-pending-command'),
      deleteDatabase('shejane-test-rejected-command'),
      deleteDatabase('shejane-test-pending-plugin-command'),
    ])
  })

  it('saves, lists, exports, imports, and deletes local conversations', async () => {
    const store = new LocalConversationStore('shejane-test')
    const conversation: Conversation = {
      id: 'conv-1',
      title: '客户跟进',
      archived: false,
      createdAt: '2026-05-10T00:00:00.000Z',
      updatedAt: '2026-05-10T00:01:00.000Z',
      messages: [
        {
          id: 'msg-1',
          role: 'user',
          content: '帮我写一封客户跟进邮件',
          createdAt: '2026-05-10T00:00:00.000Z',
          status: 'done',
        },
      ],
    }

    await store.save(conversation)
    expect(await store.list()).toHaveLength(1)
    expect(await store.get('conv-1')).toMatchObject({ title: '客户跟进' })

    const modelServices = [{
      id: `conn_${'a'.repeat(32)}`,
      preset_id: 'deepseek',
      name: 'DeepSeek',
      region: 'cn' as const,
      adapter_id: 'openai_chat' as const,
      base_url: 'https://api.deepseek.com/v1',
      models: [],
    }]
    const exported = await store.exportAll(modelServices)
    expect(JSON.stringify(exported)).not.toContain('api_key')
    await store.delete('conv-1')
    expect(await store.list()).toHaveLength(0)

    expect(await store.importAll(exported)).toEqual(modelServices)
    expect(await store.list()).toHaveLength(1)
  })

  it('keeps pinned conversations before regular recent conversations', async () => {
    const store = new LocalConversationStore('shejane-test-pinned')
    await store.save(conversation('older-pinned', '固定会话', '2026-05-10T00:00:00.000Z', true))
    await store.save(conversation('newer-regular', '普通会话', '2026-05-11T00:00:00.000Z'))
    await store.save(conversation('newest-pinned', '更新固定会话', '2026-05-12T00:00:00.000Z', true))

    expect((await store.list()).map((item) => item.id)).toEqual(['newest-pinned', 'older-pinned', 'newer-regular'])
  })

  it('keeps an unacknowledged Runtime command with its optimistic conversation across restart', async () => {
    const command: PendingRunStartCommand = {
      type: 'run.start',
      commandId: 'cmd-restart',
      createdAt: '2026-05-10T00:00:00.000Z',
      input: {
        commandId: 'cmd-restart',
        clientMessageId: 'msg-restart',
        threadId: 'conv-restart',
        goal: 'continue after restart',
        mode: 'local:test:model',
      },
    }
    const store = new LocalConversationStore('shejane-test-pending-command')

    await store.saveWithPendingRuntimeCommand(
      conversation('conv-restart', '恢复投递', '2026-05-10T00:00:00.000Z'),
      command,
    )

    const reopened = new LocalConversationStore('shejane-test-pending-command')
    expect(await reopened.get('conv-restart')).toMatchObject({ title: '恢复投递' })
    expect(await reopened.listPendingRuntimeCommands()).toEqual([command])
  })

  it('keeps a cancel command across restart without creating another conversation', async () => {
    const store = new LocalConversationStore('shejane-test-pending-cancel-command')
    const command = {
      type: 'run.cancel' as const,
      commandId: 'cmd-cancel-restart',
      createdAt: '2026-05-10T00:00:00.000Z',
      input: { runId: 'run-cancel-restart', threadId: 'conv-cancel-restart' },
    }

    await store.savePendingRuntimeCommand(command)

    const reopened = new LocalConversationStore('shejane-test-pending-cancel-command')
    expect(await reopened.list()).toEqual([])
    expect(await reopened.listPendingRuntimeCommands()).toEqual([command])
  })

  it('keeps a plugin command across restart without attaching it to a conversation', async () => {
    const store = new LocalConversationStore('shejane-test-pending-plugin-command')
    const command = {
      type: 'plugin.update' as const,
      commandId: 'cmd-plugin-update-restart',
      createdAt: '2026-07-16T00:00:00.000Z',
      input: {
        pluginId: 'dev.shejane.fixture.archive',
        sourcePath: '/tmp/archive.shejane-plugin',
        expectedDigest: `sha256:${'a'.repeat(64)}`,
        allowUnsigned: true,
      },
    }

    await store.savePendingRuntimeCommand(command)

    const reopened = new LocalConversationStore('shejane-test-pending-plugin-command')
    expect(await reopened.list()).toEqual([])
    expect(await reopened.listPendingRuntimeCommands()).toEqual([command])
  })

  it('deletes a conversation and its unacknowledged Runtime commands together', async () => {
    const store = new LocalConversationStore('shejane-test-delete-pending-command')
    await store.saveWithPendingRuntimeCommand(
      conversation('conv-delete', '删除任务', '2026-05-10T00:00:00.000Z'),
      {
        type: 'run.start',
        commandId: 'cmd-delete',
        createdAt: '2026-05-10T00:00:00.000Z',
        input: {
          commandId: 'cmd-delete',
          clientMessageId: 'msg-delete',
          threadId: 'conv-delete',
          goal: 'must not reappear',
          mode: 'local:test:model',
        },
      },
    )

    await store.delete('conv-delete')

    expect(await store.get('conv-delete')).toBeUndefined()
    expect(await store.listPendingRuntimeCommands()).toEqual([
      expect.objectContaining({ commandId: 'cmd-delete', canceledAt: expect.any(String) }),
    ])

    await store.settleCanceledLocalRunCommand('conv-delete', 'cmd-delete')
    expect(await store.listPendingRuntimeCommands()).toEqual([])
    expect(await store.getPendingRuntimeCommand('cmd-delete')).toEqual(
      expect.objectContaining({ settledAt: expect.any(String) }),
    )
    expect(
      await store.saveRuntimeProjection(
        conversation('conv-delete', '过期投影', '2026-05-10T00:00:01.000Z'),
      ),
    ).toBe(false)
    expect(await store.get('conv-delete')).toBeUndefined()
  })

  it('atomically settles a rejected command with its failed local projection', async () => {
    const store = new LocalConversationStore('shejane-test-rejected-command')
    const item = conversation('conv-rejected', '拒绝任务', '2026-05-10T00:00:00.000Z')
    item.messages = [{
      id: 'msg-assistant',
      role: 'assistant',
      content: '',
      createdAt: item.createdAt,
      status: 'pending',
    }]
    await store.saveWithPendingRuntimeCommand(item, {
      type: 'run.start',
      commandId: 'cmd-rejected',
      createdAt: item.createdAt,
      input: {
        commandId: 'cmd-rejected',
        clientMessageId: 'msg-user',
        assistantMessageId: 'msg-assistant',
        threadId: item.id,
        goal: 'rejected',
        mode: 'local:test:model',
      },
    })

    item.messages[0].status = 'error'
    await store.settleRejectedRuntimeCommand('cmd-rejected', item)

    expect(await store.listPendingRuntimeCommands()).toEqual([])
    expect((await store.get(item.id))?.messages[0].status).toBe('error')
  })
})

function conversation(id: string, title: string, updatedAt: string, pinned = false): Conversation {
  return {
    id,
    title,
    archived: false,
    pinned,
    createdAt: '2026-05-10T00:00:00.000Z',
    updatedAt,
    messages: [],
  }
}

function deleteDatabase(name: string): Promise<void> {
  return new Promise((resolve, reject) => {
    const request = indexedDB.deleteDatabase(name)
    request.onsuccess = () => resolve()
    request.onerror = () => reject(request.error)
    request.onblocked = () => resolve()
  })
}
