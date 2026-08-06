import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'

import {
  authorizeLocalWorkspace,
  cancelLocalSchedule,
  clearLocalMemory,
  createLocalRun,
  createLocalSchedule,
  deleteLocalThread,
  diagnoseLocalWorkspace,
  fetchWorkspaceFile,
  getLocalRunDiagnostics,
  getLocalThreadSnapshot,
  listLocalSchedules,
  listLocalThreadChanges,
  listLocalThreads,
  markLocalScheduleNotified,
  revokeLocalWorkspace,
  streamLocalRun,
  updateLocalThread,
} from './client'

const BASE_URL = process.env.VITE_TEST_RUNTIME_URL
const TOKEN = process.env.VITE_TEST_RUNTIME_TOKEN ?? 'dev-local-token'
const MODE = 'local:test:model'
const SETTINGS = { memory: 'off', skills: 'off', mcp: 'off' } as const

describe.skipIf(!BASE_URL)('flow:P1/P4/P12 > contract: Runtime-owned state (live runtime)', () => {
  const config = { baseURL: BASE_URL!, token: TOKEN }

  it('authorizes, diagnoses, reads, and revokes a workspace', async () => {
    const path = mkdtempSync(join(tmpdir(), 'shejane-e2e-files-'))
    const file = join(path, 'note.txt')
    writeFileSync(file, 'workspace round trip', 'utf8')
    let workspaceID = ''
    try {
      const workspace = await authorizeLocalWorkspace(path, config)
      workspaceID = workspace.id
      await expect(diagnoseLocalWorkspace(path, config)).resolves.toMatchObject({
        authorized: true,
        reason: 'authorized',
        workspace: { id: workspace.id },
      })
      const bytes = await fetchWorkspaceFile(file, config)
      expect(Buffer.from(bytes).toString('utf8')).toBe(readFileSync(file, 'utf8'))

      const outside = join(tmpdir(), `shejane-e2e-outside-${Date.now()}.txt`)
      writeFileSync(outside, 'denied', 'utf8')
      try {
        await expect(fetchWorkspaceFile(outside, config)).rejects.toThrow(/403/)
      } finally {
        rmSync(outside, { force: true })
      }
    } finally {
      if (workspaceID) await revokeLocalWorkspace(workspaceID, config).catch(() => undefined)
      rmSync(path, { recursive: true, force: true })
    }
    await expect(diagnoseLocalWorkspace(path, config)).resolves.toMatchObject({
      authorized: false,
      reason: 'not_found',
    })
  })

  it('flow:P12 > commits a complete run into the authoritative thread and diagnostics', async () => {
    const suffix = Date.now().toString(36)
    const threadID = `e2e-thread-${suffix}`
    const run = await createLocalRun({
      commandId: `cmd_e2e_state_${suffix}`,
      clientMessageId: `msg_e2e_state_${suffix}`,
      threadId: threadID,
      threadTitle: 'E2E Thread',
      goal: 'complete the state round trip',
      mode: MODE,
      settings: SETTINGS,
    }, config)
    const events: string[] = []
    await streamLocalRun(run.id, config, {
      onEvent: (event) => events.push(event.event_type),
      onDelta: () => undefined,
    })
    expect(events).toEqual(expect.arrayContaining(['run.started', 'run.completed']))

    const diagnostics = await getLocalRunDiagnostics(run.id, config)
    expect(diagnostics).toMatchObject({
      schema_version: 2,
      run: { id: run.id, status: 'completed' },
    })
    expect(diagnostics.events.map((event) => event.event_type)).toEqual(
      expect.arrayContaining(['run.started', 'run.completed']),
    )

    const snapshot = await getLocalThreadSnapshot(threadID, config)
    expect(snapshot.thread).toMatchObject({ id: threadID, title: 'State round trip' })
    expect(snapshot.runs).toEqual(expect.arrayContaining([
      expect.objectContaining({ id: run.id, status: 'completed' }),
    ]))
    expect(snapshot.items.some((item) => item.item_type === 'assistant_message')).toBe(true)

    const listed = await listLocalThreads(config)
    expect(listed.threads.some((thread) => thread.id === threadID)).toBe(true)
    const changes = await listLocalThreadChanges(0, config)
    expect(changes.changes.some((change) => change.thread_id === threadID)).toBe(true)

    await expect(updateLocalThread(threadID, {
      title: 'Renamed E2E Thread',
      metadata: { pinned: true },
    }, config)).resolves.toMatchObject({
      title: 'Renamed E2E Thread',
      metadata: { pinned: true },
    })
    await expect(deleteLocalThread(threadID, config)).resolves.toMatchObject({
      id: threadID,
      deleted: true,
    })
    await expect(getLocalThreadSnapshot(threadID, config)).rejects.toThrow(/not found/i)
  })

  it('runs a due schedule, marks its notification, and cancels a future schedule', async () => {
    const suffix = Date.now().toString(36)
    const due = await createLocalSchedule({
      goal: 'scheduled E2E run',
      runAt: new Date(Date.now() - 1_000).toISOString(),
      mode: MODE,
      settings: SETTINGS,
    }, config)
    const completed = await waitForSchedule(due.id, 'completed')
    expect(completed.result_text).toContain('Fake runtime reply')
    await expect(markLocalScheduleNotified(due.id, config)).resolves.toMatchObject({
      id: due.id,
      notified_at: expect.any(String),
    })

    const future = await createLocalSchedule({
      goal: `future E2E run ${suffix}`,
      runAt: new Date(Date.now() + 60 * 60 * 1_000).toISOString(),
      mode: MODE,
      settings: SETTINGS,
    }, config)
    await expect(cancelLocalSchedule(future.id, config)).resolves.toMatchObject({
      id: future.id,
      status: 'canceled',
    })
  })

  it('clears long-term memory idempotently', async () => {
    await expect(clearLocalMemory(config)).resolves.toMatchObject({ cleared: true })
    await expect(clearLocalMemory(config)).resolves.toEqual({ cleared: true, deleted_count: 0 })
  })

  async function waitForSchedule(id: string, status: 'completed' | 'failed') {
    const deadline = Date.now() + 15_000
    while (Date.now() < deadline) {
      const item = (await listLocalSchedules(config)).find((schedule) => schedule.id === id)
      if (item?.status === status) return item
      if (item?.status === 'failed') throw new Error(item.error_message || 'scheduled run failed')
      await new Promise((resolve) => setTimeout(resolve, 100))
    }
    throw new Error(`schedule ${id} did not reach ${status}`)
  }
})
