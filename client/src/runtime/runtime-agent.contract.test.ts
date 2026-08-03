import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'

import {
  answerLocalQuestionCommand,
  authorizeLocalWorkspace,
  cancelLocalRunCommand,
  createLocalSkill,
  createLocalRun,
  deleteLocalSkill,
  forkLocalRun,
  getLocalThreadSnapshot,
  getLocalRunDiagnostics,
  resolveLocalPermissionCommand,
  revokeLocalWorkspace,
  streamLocalRun,
  updateLocalSkill,
} from './client'

const BASE_URL = process.env.VITE_TEST_RUNTIME_URL
const TOKEN = process.env.VITE_TEST_RUNTIME_TOKEN ?? 'dev-local-token'
const REAL_LLM_MODEL = process.env.VITE_TEST_REAL_LLM_MODEL
const RUN_MODEL = (REAL_LLM_MODEL ?? 'local:test:model') as `local:${string}:${string}`
const SETTINGS = { memory: 'off', skills: 'off', mcp: 'off' } as const

function realGoal(fakeGoal: string, realGoal: string): string {
  return REAL_LLM_MODEL ? realGoal : fakeGoal
}

describe.skipIf(!BASE_URL)('flow:P5-P12 > contract: Runtime agent loop (live runtime)', () => {
  const config = { baseURL: BASE_URL!, token: TOKEN }

  it('tool:read_file > family:filesystem > effect:read-only > risk:low > traits:read-only,workspace > flow:P8-P9 attachment', async () => {
    const directory = mkdtempSync(join(tmpdir(), 'shejane-e2e-pdf-'))
    const suffix = Date.now().toString(36)
    const attachment = join(directory, `e2e-receipt-${suffix}.pdf`)
    writeFileSync(attachment, minimalPdf('E2E rental receipt'))
    try {
      const run = await createLocalRun({
        commandId: `cmd_e2e_pdf_${suffix}`,
        clientMessageId: `msg_e2e_pdf_${suffix}`,
        goal: realGoal(
          '[[e2e:read-attachment]] summarize the attached PDF',
          'Use read_file on the provided /attachments/ path, then include the exact receipt text in your answer.',
        ),
        attachmentPaths: [attachment],
        mode: RUN_MODEL,
        settings: SETTINGS,
      }, config)
      const events: Array<{ type: string; payload: Record<string, unknown> }> = []
      await streamLocalRun(run.id, config, {
        onEvent: (event) => events.push({ type: event.event_type, payload: event.payload ?? {} }),
        onDelta: () => undefined,
      })

      const eventTypes = events.map(event => event.type)
      expect(eventTypes).toEqual(expect.arrayContaining([
        'tool.requested',
        'tool.completed',
        'run.completed',
      ]))
      expect(eventTypes).not.toContain('question.asked')
      const requested = events.find(event =>
        event.type === 'tool.requested' && event.payload.name === 'read_file')
      const arguments_ = requested?.payload.arguments as Record<string, unknown> | undefined
      expect(String(arguments_?.file_path ?? '')).toMatch(/^\/attachments\//)
      expect(arguments_?.file_path).not.toBe(attachment)
      const completed = events.find((event) => event.type === 'run.completed')
      expect(String(completed?.payload.final_text ?? '')).toContain('E2E rental receipt')
    } finally {
      rmSync(directory, { recursive: true, force: true })
    }
  })

  it('flow:P6 > binds an enabled Skill and loads it through read_file', async () => {
    const name = 'e2e-active-skill'
    const suffix = Date.now().toString(36)
    await deleteLocalSkill(name, config).catch(() => undefined)
    try {
      await createLocalSkill({
        name,
        description: 'Proves that Runtime Skill instructions reach the Agent.',
        content: '# E2E Active Skill\n\nReply with the exact token E2E_SKILL_ACTIVE.',
      }, config)
      const run = await createLocalRun({
        commandId: `cmd_e2e_skill_${suffix}`,
        clientMessageId: `msg_e2e_skill_${suffix}`,
        goal: realGoal(
          'Use e2e-active-skill for this answer. [[e2e:skill]]',
          'Use the e2e-active-skill. Read its SKILL.md with read_file and follow it exactly.',
        ),
        mode: RUN_MODEL,
        settings: { ...SETTINGS, skills: 'on' },
      }, config)
      const events: Array<{ type: string; payload: Record<string, unknown> }> = []
      await streamLocalRun(run.id, config, {
        onEvent: event => events.push({ type: event.event_type, payload: event.payload ?? {} }),
        onDelta: () => undefined,
      })

      expect(events).toEqual(expect.arrayContaining([
        expect.objectContaining({
          type: 'tool.requested',
          payload: expect.objectContaining({ name: 'read_file' }),
        }),
        expect.objectContaining({
          type: 'tool.completed',
          payload: expect.objectContaining({ name: 'read_file' }),
        }),
        expect.objectContaining({ type: 'run.completed' }),
      ]))
      const completed = [...events].reverse().find(event => event.type === 'run.completed')
      expect(String(completed?.payload.final_text ?? '')).toContain('E2E_SKILL_ACTIVE')
    } finally {
      await deleteLocalSkill(name, config).catch(() => undefined)
    }
  })

  it('fails a waiting Run closed when its admitted Skill catalog changes', async () => {
    const name = 'e2e-active-skill'
    const workspace = mkdtempSync(join(tmpdir(), 'shejane-e2e-skill-drift-'))
    const suffix = Date.now().toString(36)
    let workspaceID = ''
    await deleteLocalSkill(name, config).catch(() => undefined)
    try {
      workspaceID = (await authorizeLocalWorkspace(workspace, config)).id
      await createLocalSkill({
        name,
        description: 'Proves that a Run cannot silently switch Skill content while waiting.',
        content: '# E2E Active Skill\n\nReply with the exact token E2E_SKILL_VERSION_ONE.',
      }, config)
      const run = await createLocalRun({
        commandId: `cmd_e2e_skill_drift_${suffix}`,
        clientMessageId: `msg_e2e_skill_drift_${suffix}`,
        goal: 'Pause before reading e2e-active-skill. [[e2e:skill-drift]]',
        workspacePath: workspace,
        mode: RUN_MODEL,
        settings: { ...SETTINGS, skills: 'on' },
      }, config)
      const first: Array<{ type: string; payload: Record<string, unknown> }> = []
      await streamLocalRun(run.id, config, {
        onEvent: event => first.push({ type: event.event_type, payload: event.payload ?? {} }),
        onDelta: () => undefined,
      })
      expect(first.map(event => event.type)).toEqual(expect.arrayContaining([
        'permission.required',
        'run.waiting',
      ]))

      await updateLocalSkill(name, {
        name,
        description: 'Changed after the old Run was admitted.',
        content: '# E2E Active Skill\n\nReply with the exact token E2E_SKILL_VERSION_TWO.',
      }, config)
      const permissionID = String(
        first.find(event => event.type === 'permission.required')?.payload.request_id ?? '',
      )
      await resolveLocalPermissionCommand(
        `cmd_e2e_skill_drift_approve_${suffix}`,
        permissionID,
        'approve',
        { scope: 'once' },
        config,
      )
      const resumed: Array<{ type: string; payload: Record<string, unknown> }> = []
      await streamLocalRun(run.id, config, {
        onEvent: event => resumed.push({ type: event.event_type, payload: event.payload ?? {} }),
        onDelta: () => undefined,
      })

      const failed = [...resumed].reverse().find(event => event.type === 'run.failed')
      expect(failed?.payload).toMatchObject({
        type: 'ExecutionSkillBindingError',
        category: 'configuration',
        recoverable: true,
        retryable: false,
      })
      expect(resumed.some(event => event.type === 'tool.completed')).toBe(false)
      expect(JSON.stringify(resumed)).not.toContain('E2E_SKILL_VERSION_TWO')

      const freshRun = await createLocalRun({
        commandId: `cmd_e2e_skill_fresh_${suffix}`,
        clientMessageId: `msg_e2e_skill_fresh_${suffix}`,
        goal: 'Use e2e-active-skill for this answer. [[e2e:skill]]',
        mode: RUN_MODEL,
        settings: { ...SETTINGS, skills: 'on' },
      }, config)
      const fresh: Array<{ type: string; payload: Record<string, unknown> }> = []
      await streamLocalRun(freshRun.id, config, {
        onEvent: event => fresh.push({ type: event.event_type, payload: event.payload ?? {} }),
        onDelta: () => undefined,
      })
      expect(String(
        [...fresh].reverse().find(event => event.type === 'run.completed')?.payload.final_text ?? '',
      )).toContain('E2E_SKILL_VERSION_TWO')

      const freshDiagnostics = await getLocalRunDiagnostics(freshRun.id, config)
      const checkpointID = String(freshDiagnostics.latest_checkpoint?.id ?? '')
      expect(checkpointID).toBeTruthy()
      await updateLocalSkill(name, {
        name,
        description: 'Changed after the source Run completed.',
        content: '# E2E Active Skill\n\nReply with the exact token E2E_SKILL_VERSION_THREE.',
      }, config)
      const fork = await forkLocalRun(`cmd_e2e_skill_fork_${suffix}`, {
        sourceRunId: freshRun.id,
        protocolVersion: 1,
        requiredCapabilities: [],
        clientMessageId: `msg_e2e_skill_fork_user_${suffix}`,
        assistantMessageId: `msg_e2e_skill_fork_assistant_${suffix}`,
        threadId: `thread_e2e_skill_fork_${suffix}`,
        checkpointId: checkpointID,
        goal: 'Use the same admitted Skill snapshot. [[e2e:skill]]',
        userInput: 'Fork from the completed Skill Run.',
      }, config)
      const forkEvents: Array<{ type: string; payload: Record<string, unknown> }> = []
      await streamLocalRun(fork.id, config, {
        onEvent: event => forkEvents.push({ type: event.event_type, payload: event.payload ?? {} }),
        onDelta: () => undefined,
      })
      expect([...forkEvents].reverse().find(event => event.type === 'run.failed')?.payload)
        .toMatchObject({
          type: 'ExecutionSkillBindingError',
          category: 'configuration',
          retryable: false,
        })
      expect(JSON.stringify(forkEvents)).not.toContain('E2E_SKILL_VERSION_THREE')
    } finally {
      await deleteLocalSkill(name, config).catch(() => undefined)
      if (workspaceID) await revokeLocalWorkspace(workspaceID, config).catch(() => undefined)
      rmSync(workspace, { recursive: true, force: true })
    }
  })

  it('runs a Subagent and returns its result to the parent Agent', async () => {
    const suffix = Date.now().toString(36)
    const run = await createLocalRun({
      commandId: `cmd_e2e_subagent_${suffix}`,
      clientMessageId: `msg_e2e_subagent_${suffix}`,
      goal: realGoal(
        '[[e2e:subagent]] delegate this deterministic task',
        'You must call the task tool with subagent_type "writer". Ask the subagent to return exactly E2E_SUBAGENT_RESULT, then include that exact token in your final answer.',
      ),
      mode: RUN_MODEL,
      settings: SETTINGS,
    }, config)
    const events: Array<{ type: string; payload: Record<string, unknown> }> = []
    await streamLocalRun(run.id, config, {
      onEvent: event => events.push({ type: event.event_type, payload: event.payload ?? {} }),
      onDelta: () => undefined,
    })

    const lifecycle = events.filter(event => event.type.startsWith('subagent.'))
    expect(lifecycle.map(event => event.type)).toEqual([
      'subagent.spawned',
      'subagent.started',
      'subagent.completed',
    ])
    expect(new Set(lifecycle.map(event => event.payload.operation_id)).size).toBe(1)
    expect(new Set(lifecycle.map(event => event.payload.tool_call_id)).size).toBe(1)
    expect(lifecycle.every(event => event.payload.parent_run_id === run.id)).toBe(true)
    expect(lifecycle.every(event => event.payload.subagent_type === 'writer')).toBe(true)
    expect(lifecycle.at(-1)?.payload).toMatchObject({
      status: 'completed',
      receipt_status: 'completed',
      usage: expect.objectContaining({ model_calls: expect.any(Number) }),
    })
    expect(events).toEqual(expect.arrayContaining([
      expect.objectContaining({ type: 'run.completed' }),
    ]))
    const completed = [...events].reverse().find(event => event.type === 'run.completed')
    expect(String(completed?.payload.final_text ?? '')).toContain('E2E_SUBAGENT_RESULT')

    const replay: Array<{ type: string; payload: Record<string, unknown> }> = []
    await streamLocalRun(run.id, config, {
      onEvent: event => replay.push({ type: event.event_type, payload: event.payload ?? {} }),
      onDelta: () => undefined,
    })
    expect(replay.filter(event => event.type.startsWith('subagent.'))).toEqual(lifecycle)

    expect(run.thread_id).toBeTruthy()
    const snapshot = await getLocalThreadSnapshot(String(run.thread_id), config)
    const invocation = snapshot.runs.find(item => item.id === run.id)?.subagent_invocations?.[0]
    expect(invocation).toMatchObject({
      operation_id: lifecycle[0]?.payload.operation_id,
      parent_run_id: run.id,
      status: 'completed',
      receipt_status: 'completed',
      subagent_type: 'writer',
    })
  })

  it('updates the injected Todo state before completing', async () => {
    const suffix = Date.now().toString(36)
    const run = await createLocalRun({
      commandId: `cmd_e2e_todos_${suffix}`,
      clientMessageId: `msg_e2e_todos_${suffix}`,
      goal: realGoal(
        '[[e2e:write-todos]] create the deterministic Todo list',
        'Call write_todos with one todo whose content is E2E_TODO_ACTIVE and status is in_progress. Include E2E_TODO_ACTIVE in the final answer.',
      ),
      mode: RUN_MODEL,
      settings: SETTINGS,
    }, config)
    const events: Array<{ type: string; payload: Record<string, unknown> }> = []
    await streamLocalRun(run.id, config, {
      onEvent: event => events.push({ type: event.event_type, payload: event.payload ?? {} }),
      onDelta: () => undefined,
    })

    expect(events).toEqual(expect.arrayContaining([
      expect.objectContaining({
        type: 'tool.requested',
        payload: expect.objectContaining({ name: 'write_todos' }),
      }),
      expect.objectContaining({
        type: 'tool.completed',
        payload: expect.objectContaining({ name: 'write_todos' }),
      }),
      expect.objectContaining({ type: 'run.completed' }),
    ]))
    const completed = [...events].reverse().find(event => event.type === 'run.completed')
    expect(String(completed?.payload.final_text ?? '')).toContain('E2E_TODO_ACTIVE')
  })

  it('tool:write_file > family:filesystem > effect:workspace-write > risk:workspace > traits:permission,side-effect,workspace > flow:P7-P10 resume', async () => {
    const workspace = mkdtempSync(join(tmpdir(), 'shejane-e2e-write-'))
    let workspaceID = ''
    try {
      workspaceID = (await authorizeLocalWorkspace(workspace, config)).id
      const suffix = Date.now().toString(36)
      const run = await createLocalRun({
        commandId: `cmd_e2e_write_${suffix}`,
        clientMessageId: `msg_e2e_write_${suffix}`,
        goal: realGoal(
          '[[e2e:write-file]] create the approved file',
          'Call write_file exactly once with file_path "approved.txt" and content "approved by E2E". The successful write_file result is sufficient verification: do not call task.verify or any other tool afterward. After approval, reply with exactly E2E_WRITE_APPROVED.',
        ),
        workspacePath: workspace,
        mode: RUN_MODEL,
        settings: SETTINGS,
      }, config)
      const first: Array<{ type: string; payload: Record<string, unknown> }> = []
      await streamLocalRun(run.id, config, {
        onEvent: (event) => first.push({ type: event.event_type, payload: event.payload ?? {} }),
        onDelta: () => undefined,
      })
      expect(first.map((event) => event.type)).toEqual(expect.arrayContaining([
        'permission.required',
        'run.waiting',
      ]))
      const permission = first.find((event) => event.type === 'permission.required')
      const permissionID = String(permission?.payload.request_id ?? '')
      expect(permissionID).toBeTruthy()

      const approvalCommandID = `cmd_e2e_approve_${suffix}`
      const approval = await resolveLocalPermissionCommand(
        approvalCommandID,
        permissionID,
        'approve',
        { scope: 'once' },
        config,
      )
      expect(approval).toMatchObject({ resolved: true, resumed: true })
      await expect(resolveLocalPermissionCommand(
        approvalCommandID,
        permissionID,
        'approve',
        { scope: 'once' },
        config,
      )).resolves.toEqual(approval)

      const second: Array<{ type: string; payload: Record<string, unknown> }> = []
      await streamLocalRun(run.id, config, {
        onEvent: (event) => second.push({ type: event.event_type, payload: event.payload ?? {} }),
        onDelta: () => undefined,
      })
      expect(second.map((event) => event.type)).toEqual(expect.arrayContaining([
        'permission.resolved',
        'tool.completed',
        'run.completed',
      ]))
      const output = join(workspace, 'approved.txt')
      expect(existsSync(output)).toBe(true)
      expect(readFileSync(output, 'utf8')).toBe('approved by E2E')
      if (REAL_LLM_MODEL) {
        const completed = [...second].reverse().find(event => event.type === 'run.completed')
        expect(String(completed?.payload.final_text ?? '')).toContain('E2E_WRITE_APPROVED')
      }
      const diagnostics = await getLocalRunDiagnostics(run.id, config)
      expect((diagnostics.tool_receipts ?? []).filter(receipt => receipt.tool_name === 'write_file')).toEqual([
        expect.objectContaining({ status: 'completed', attempt_count: 1 }),
      ])
    } finally {
      if (workspaceID) await revokeLocalWorkspace(workspaceID, config).catch(() => undefined)
      rmSync(workspace, { recursive: true, force: true })
    }
  })

  it('records a denied Tool as rejected without executing its side effect', async () => {
    const workspace = mkdtempSync(join(tmpdir(), 'shejane-e2e-deny-'))
    let workspaceID = ''
    try {
      workspaceID = (await authorizeLocalWorkspace(workspace, config)).id
      const suffix = Date.now().toString(36)
      const run = await createLocalRun({
        commandId: `cmd_e2e_deny_run_${suffix}`,
        clientMessageId: `msg_e2e_deny_run_${suffix}`,
        goal: '[[e2e:write-file]] attempt the denied file',
        workspacePath: workspace,
        mode: RUN_MODEL,
        settings: SETTINGS,
      }, config)
      const first: Array<{ type: string; payload: Record<string, unknown> }> = []
      await streamLocalRun(run.id, config, {
        onEvent: event => first.push({ type: event.event_type, payload: event.payload ?? {} }),
        onDelta: () => undefined,
      })
      const permissionID = String(
        first.find(event => event.type === 'permission.required')?.payload.request_id ?? '',
      )
      expect(permissionID).toBeTruthy()

      await expect(resolveLocalPermissionCommand(
        `cmd_e2e_deny_${suffix}`,
        permissionID,
        'deny',
        { scope: 'once' },
        config,
      )).resolves.toMatchObject({ resolved: true, decision: 'deny', resumed: true })

      const second: Array<{ type: string; payload: Record<string, unknown> }> = []
      await streamLocalRun(run.id, config, {
        onEvent: event => second.push({ type: event.event_type, payload: event.payload ?? {} }),
        onDelta: () => undefined,
      })
      expect(second.map(event => event.type)).toEqual(expect.arrayContaining([
        'permission.resolved',
        'tool.failed',
        'run.completed',
      ]))
      expect(existsSync(join(workspace, 'approved.txt'))).toBe(false)
      const diagnostics = await getLocalRunDiagnostics(run.id, config)
      expect((diagnostics.tool_receipts ?? []).filter(receipt => receipt.tool_name === 'write_file')).toEqual([
        expect.objectContaining({ status: 'rejected', attempt_count: 0 }),
      ])
    } finally {
      if (workspaceID) await revokeLocalWorkspace(workspaceID, config).catch(() => undefined)
      rmSync(workspace, { recursive: true, force: true })
    }
  })

  it('executes edited Tool arguments and rejects a conflicting stale decision', async () => {
    const workspace = mkdtempSync(join(tmpdir(), 'shejane-e2e-edit-'))
    let workspaceID = ''
    try {
      workspaceID = (await authorizeLocalWorkspace(workspace, config)).id
      const suffix = Date.now().toString(36)
      const run = await createLocalRun({
        commandId: `cmd_e2e_edit_run_${suffix}`,
        clientMessageId: `msg_e2e_edit_run_${suffix}`,
        goal: '[[e2e:write-file]] propose a file that will be edited',
        workspacePath: workspace,
        mode: RUN_MODEL,
        settings: SETTINGS,
      }, config)
      const first: Array<{ type: string; payload: Record<string, unknown> }> = []
      await streamLocalRun(run.id, config, {
        onEvent: event => first.push({ type: event.event_type, payload: event.payload ?? {} }),
        onDelta: () => undefined,
      })
      const permissionID = String(
        first.find(event => event.type === 'permission.required')?.payload.request_id ?? '',
      )
      expect(permissionID).toBeTruthy()
      await expect(resolveLocalPermissionCommand(
        `cmd_e2e_edit_${suffix}`,
        permissionID,
        'edit',
        {
          scope: 'once',
          editedAction: {
            name: 'write_file',
            args: { file_path: '/edited.txt', content: 'edited by E2E' },
          },
        },
        config,
      )).resolves.toMatchObject({ decision: 'edit', resolved: true, resumed: true })
      await expect(resolveLocalPermissionCommand(
        `cmd_e2e_edit_conflict_${suffix}`,
        permissionID,
        'deny',
        { scope: 'once' },
        config,
      )).rejects.toThrow()

      const second: Array<{ type: string; payload: Record<string, unknown> }> = []
      await streamLocalRun(run.id, config, {
        onEvent: event => second.push({ type: event.event_type, payload: event.payload ?? {} }),
        onDelta: () => undefined,
      })
      expect(second.map(event => event.type)).toEqual(expect.arrayContaining([
        'permission.resolved',
        'tool.completed',
        'run.completed',
      ]))
      expect(existsSync(join(workspace, 'approved.txt'))).toBe(false)
      expect(readFileSync(join(workspace, 'edited.txt'), 'utf8')).toBe('edited by E2E')
    } finally {
      if (workspaceID) await revokeLocalWorkspace(workspaceID, config).catch(() => undefined)
      rmSync(workspace, { recursive: true, force: true })
    }
  })

  it('waits for every permission in a Tool batch before applying mixed decisions', async () => {
    const workspace = mkdtempSync(join(tmpdir(), 'shejane-e2e-multi-permission-'))
    let workspaceID = ''
    try {
      workspaceID = (await authorizeLocalWorkspace(workspace, config)).id
      const suffix = Date.now().toString(36)
      const run = await createLocalRun({
        commandId: `cmd_e2e_multi_permission_${suffix}`,
        clientMessageId: `msg_e2e_multi_permission_${suffix}`,
        goal: '[[e2e:multi-write-batch]] propose two independent writes',
        workspacePath: workspace,
        mode: RUN_MODEL,
        settings: SETTINGS,
      }, config)
      const first: Array<{ type: string; payload: Record<string, unknown> }> = []
      await streamLocalRun(run.id, config, {
        onEvent: event => first.push({ type: event.event_type, payload: event.payload ?? {} }),
        onDelta: () => undefined,
      })
      const permissionIDs = first
        .filter(event => event.type === 'permission.required')
        .map(event => String(event.payload.request_id ?? ''))
      expect(permissionIDs).toHaveLength(2)
      expect(new Set(permissionIDs).size).toBe(2)

      await expect(resolveLocalPermissionCommand(
        `cmd_e2e_multi_first_${suffix}`,
        permissionIDs[0],
        'approve',
        { scope: 'once' },
        config,
      )).resolves.toMatchObject({ resolved: true, resumed: false })
      expect(existsSync(join(workspace, 'first.txt'))).toBe(false)
      expect(existsSync(join(workspace, 'second.txt'))).toBe(false)

      await expect(resolveLocalPermissionCommand(
        `cmd_e2e_multi_second_${suffix}`,
        permissionIDs[1],
        'deny',
        { scope: 'once' },
        config,
      )).resolves.toMatchObject({ resolved: true, resumed: true })
      const second: Array<{ type: string; payload: Record<string, unknown> }> = []
      await streamLocalRun(run.id, config, {
        onEvent: event => second.push({ type: event.event_type, payload: event.payload ?? {} }),
        onDelta: () => undefined,
      })
      expect(second.map(event => event.type)).toEqual(expect.arrayContaining([
        'tool.completed',
        'tool.failed',
        'run.completed',
      ]))
      expect(readFileSync(join(workspace, 'first.txt'), 'utf8')).toBe('first approved')
      expect(existsSync(join(workspace, 'second.txt'))).toBe(false)
      const diagnostics = await getLocalRunDiagnostics(run.id, config)
      expect((diagnostics.tool_receipts ?? []).filter(receipt => receipt.tool_name === 'write_file'))
        .toEqual(expect.arrayContaining([
          expect.objectContaining({ status: 'completed', attempt_count: 1 }),
          expect.objectContaining({ status: 'rejected', attempt_count: 0 }),
        ]))
    } finally {
      if (workspaceID) await revokeLocalWorkspace(workspaceID, config).catch(() => undefined)
      rmSync(workspace, { recursive: true, force: true })
    }
  })

  it('serializes conflicting writes in model order without overwriting the first result', async () => {
    const workspace = mkdtempSync(join(tmpdir(), 'shejane-e2e-write-conflict-'))
    let workspaceID = ''
    try {
      workspaceID = (await authorizeLocalWorkspace(workspace, config)).id
      const suffix = Date.now().toString(36)
      const run = await createLocalRun({
        commandId: `cmd_e2e_write_conflict_${suffix}`,
        clientMessageId: `msg_e2e_write_conflict_${suffix}`,
        goal: '[[e2e:conflicting-write-batch]] write the same path twice',
        workspacePath: workspace,
        mode: RUN_MODEL,
        settings: SETTINGS,
      }, config)
      const first: Array<{ type: string; payload: Record<string, unknown> }> = []
      await streamLocalRun(run.id, config, {
        onEvent: event => first.push({ type: event.event_type, payload: event.payload ?? {} }),
        onDelta: () => undefined,
      })
      const permissionIDs = first
        .filter(event => event.type === 'permission.required')
        .map(event => String(event.payload.request_id ?? ''))
      expect(permissionIDs).toHaveLength(2)

      await expect(resolveLocalPermissionCommand(
        `cmd_e2e_write_conflict_first_${suffix}`,
        permissionIDs[0],
        'approve',
        { scope: 'once' },
        config,
      )).resolves.toMatchObject({ resolved: true, resumed: false })
      await expect(resolveLocalPermissionCommand(
        `cmd_e2e_write_conflict_second_${suffix}`,
        permissionIDs[1],
        'approve',
        { scope: 'once' },
        config,
      )).resolves.toMatchObject({ resolved: true, resumed: true })

      const second: Array<{ type: string; payload: Record<string, unknown> }> = []
      await streamLocalRun(run.id, config, {
        onEvent: event => second.push({ type: event.event_type, payload: event.payload ?? {} }),
        onDelta: () => undefined,
      })
      expect(second.map(event => event.type)).toEqual(expect.arrayContaining([
        'tool.completed',
        'tool.failed',
        'run.completed',
      ]))
      expect(readFileSync(join(workspace, 'conflict.txt'), 'utf8')).toBe('first write wins')

      const diagnostics = await getLocalRunDiagnostics(run.id, config)
      expect((diagnostics.tool_receipts ?? []).filter(receipt =>
        receipt.tool_call_id?.startsWith('call_e2e_conflicting_write_')
      )).toEqual([
        expect.objectContaining({
          tool_call_id: 'call_e2e_conflicting_write_first',
          status: 'completed',
          attempt_count: 1,
        }),
        expect.objectContaining({
          tool_call_id: 'call_e2e_conflicting_write_second',
          status: 'failed',
          attempt_count: 1,
        }),
      ])
    } finally {
      if (workspaceID) await revokeLocalWorkspace(workspaceID, config).catch(() => undefined)
      rmSync(workspace, { recursive: true, force: true })
    }
  })

  it('reuses a run-scoped grant for new validated arguments to the same ordinary tool', async () => {
    const workspace = mkdtempSync(join(tmpdir(), 'shejane-e2e-run-grant-'))
    let workspaceID = ''
    try {
      workspaceID = (await authorizeLocalWorkspace(workspace, config)).id
      const suffix = Date.now().toString(36)
      const run = await createLocalRun({
        commandId: `cmd_e2e_run_grant_${suffix}`,
        clientMessageId: `msg_e2e_run_grant_${suffix}`,
        goal: '[[e2e:run-scope-grant]] write two files with one tool grant',
        workspacePath: workspace,
        mode: RUN_MODEL,
        settings: SETTINGS,
      }, config)
      const first: Array<{ type: string; payload: Record<string, unknown> }> = []
      await streamLocalRun(run.id, config, {
        onEvent: event => first.push({ type: event.event_type, payload: event.payload ?? {} }),
        onDelta: () => undefined,
      })
      const permissions = first.filter(event => event.type === 'permission.required')
      expect(permissions).toHaveLength(1)
      const permissionID = String(permissions[0]?.payload.request_id ?? '')
      expect(permissionID).toBeTruthy()

      await expect(resolveLocalPermissionCommand(
        `cmd_e2e_run_grant_approve_${suffix}`,
        permissionID,
        'approve',
        { scope: 'run' },
        config,
      )).resolves.toMatchObject({ resolved: true, resumed: true })

      const second: Array<{ type: string; payload: Record<string, unknown> }> = []
      await streamLocalRun(run.id, config, {
        onEvent: event => second.push({ type: event.event_type, payload: event.payload ?? {} }),
        onDelta: () => undefined,
      })
      const all = [...first, ...second]
      const uniquePermissionIDs = new Set(
        all
          .filter(event => event.type === 'permission.required')
          .map(event => String(event.payload.request_id ?? '')),
      )
      expect(uniquePermissionIDs).toEqual(new Set([permissionID]))
      expect(all).toEqual(expect.arrayContaining([
        expect.objectContaining({
          type: 'permission.auto_approved',
          payload: expect.objectContaining({ source: 'run_grant' }),
        }),
        expect.objectContaining({ type: 'run.completed' }),
      ]))
      expect(readFileSync(join(workspace, 'run-grant-a.txt'), 'utf8')).toBe('a')
      expect(readFileSync(join(workspace, 'run-grant-b.txt'), 'utf8')).toBe('b')
      const diagnostics = await getLocalRunDiagnostics(run.id, config)
      expect((diagnostics.tool_receipts ?? []).filter(receipt => receipt.tool_name === 'write_file'))
        .toEqual([
          expect.objectContaining({ status: 'completed', attempt_count: 1 }),
          expect.objectContaining({ status: 'completed', attempt_count: 1 }),
        ])
    } finally {
      if (workspaceID) await revokeLocalWorkspace(workspaceID, config).catch(() => undefined)
      rmSync(workspace, { recursive: true, force: true })
    }
  })

  it('pauses a mixed Tool batch before any sibling executes', async () => {
    const workspace = mkdtempSync(join(tmpdir(), 'shejane-e2e-batch-'))
    let workspaceID = ''
    try {
      writeFileSync(join(workspace, 'source.txt'), 'batch source')
      workspaceID = (await authorizeLocalWorkspace(workspace, config)).id
      const suffix = Date.now().toString(36)
      const run = await createLocalRun({
        commandId: `cmd_e2e_batch_${suffix}`,
        clientMessageId: `msg_e2e_batch_${suffix}`,
        goal: '[[e2e:mixed-batch]] read source.txt and write batch.txt',
        workspacePath: workspace,
        mode: RUN_MODEL,
        settings: SETTINGS,
      }, config)
      const first: Array<{ type: string; payload: Record<string, unknown> }> = []
      await streamLocalRun(run.id, config, {
        onEvent: event => first.push({ type: event.event_type, payload: event.payload ?? {} }),
        onDelta: () => undefined,
      })
      expect(first.map(event => event.type)).toEqual(expect.arrayContaining([
        'permission.required',
        'run.waiting',
      ]))
      expect(existsSync(join(workspace, 'batch.txt'))).toBe(false)
      const paused = await getLocalRunDiagnostics(run.id, config)
      expect((paused.tool_receipts ?? []).map(receipt => ({
        tool: receipt.tool_name,
        status: receipt.status,
        attempts: receipt.attempt_count,
      }))).toEqual(expect.arrayContaining([
        { tool: 'read_file', status: 'prepared', attempts: 0 },
        { tool: 'write_file', status: 'prepared', attempts: 0 },
      ]))

      const permissionID = String(
        first.find(event => event.type === 'permission.required')?.payload.request_id ?? '',
      )
      await resolveLocalPermissionCommand(
        `cmd_e2e_batch_approve_${suffix}`,
        permissionID,
        'approve',
        { scope: 'once' },
        config,
      )
      const second: Array<{ type: string; payload: Record<string, unknown> }> = []
      await streamLocalRun(run.id, config, {
        onEvent: event => second.push({ type: event.event_type, payload: event.payload ?? {} }),
        onDelta: () => undefined,
      })
      expect(second.map(event => event.type)).toContain('run.completed')
      expect(readFileSync(join(workspace, 'batch.txt'), 'utf8')).toBe('batch output')
      const completed = await getLocalRunDiagnostics(run.id, config)
      expect((completed.tool_receipts ?? []).filter(receipt =>
        receipt.tool_name === 'read_file' || receipt.tool_name === 'write_file'
      )).toEqual(expect.arrayContaining([
        expect.objectContaining({ tool_name: 'read_file', status: 'completed', attempt_count: 1 }),
        expect.objectContaining({ tool_name: 'write_file', status: 'completed', attempt_count: 1 }),
      ]))
    } finally {
      if (workspaceID) await revokeLocalWorkspace(workspaceID, config).catch(() => undefined)
      rmSync(workspace, { recursive: true, force: true })
    }
  })

  it('tool:user.ask > family:human-in-the-loop > effect:human-interaction > risk:human > traits:interrupt,resume > flow:P10 answer', async () => {
    const suffix = Date.now().toString(36)
    const run = await createLocalRun({
      commandId: `cmd_e2e_question_${suffix}`,
      clientMessageId: `msg_e2e_question_${suffix}`,
      goal: realGoal(
        '[[e2e:ask]] ask which option to use',
        'Call user.ask exactly once with question "Choose an E2E option" and options ["Option A", "Option B"]. After the answer, include the selected option in the final response.',
      ),
      mode: RUN_MODEL,
      settings: SETTINGS,
    }, config)
    const first: Array<{ type: string; payload: Record<string, unknown> }> = []
    await streamLocalRun(run.id, config, {
      onEvent: (event) => first.push({ type: event.event_type, payload: event.payload ?? {} }),
      onDelta: () => undefined,
    })
    const question = first.find((event) => event.type === 'question.asked')
    const questionID = String(question?.payload.request_id ?? '')
    expect(questionID).toBeTruthy()
    expect(question?.payload.questions).toEqual([
      {
        id: questionID,
        question: 'Choose an E2E option',
        options: [{ label: 'Option A' }, { label: 'Option B' }],
      },
    ])
    expect(first.map((event) => event.type)).toContain('run.waiting')

    await expect(answerLocalQuestionCommand(
      `cmd_e2e_answer_${suffix}`,
      questionID,
      { [questionID]: ['Option B'] },
      config,
    )).resolves.toMatchObject({ answered: true, resumed: true })

    const second: Array<{ type: string; payload: Record<string, unknown> }> = []
    await streamLocalRun(run.id, config, {
      onEvent: (event) => second.push({ type: event.event_type, payload: event.payload ?? {} }),
      onDelta: () => undefined,
    })
    expect(second.map((event) => event.type)).toEqual(expect.arrayContaining([
      'question.answered',
      'run.completed',
    ]))
    const completed = second.find((event) => event.type === 'run.completed')
    expect(String(completed?.payload.final_text ?? '')).toContain('Option B')
  })

  it('flow:P11 > cancels an in-flight model call and reaches a durable terminal state', async () => {
    const suffix = Date.now().toString(36)
    const run = await createLocalRun({
      commandId: `cmd_e2e_slow_${suffix}`,
      clientMessageId: `msg_e2e_slow_${suffix}`,
      goal: '[[e2e:slow]] keep running until canceled',
      mode: RUN_MODEL,
      settings: SETTINGS,
    }, config)
    const events: string[] = []
    let markStarted: () => void = () => undefined
    const started = new Promise<void>((resolve) => { markStarted = resolve })
    const stream = streamLocalRun(run.id, config, {
      onEvent: (event) => {
        events.push(event.event_type)
        if (event.event_type === 'run.started') markStarted()
      },
      onDelta: () => undefined,
    })
    await started
    await expect(cancelLocalRunCommand(
      `cmd_e2e_cancel_${suffix}`,
      run.id,
      config,
    )).resolves.toMatchObject({ canceled: true })
    await stream

    expect(events).toContain('run.canceled')
    expect(events).not.toContain('run.completed')
  })
})

function minimalPdf(text: string): Buffer {
  const stream = Buffer.from(`BT /F1 12 Tf 72 720 Td (${text}) Tj ET`, 'ascii')
  const objects = [
    Buffer.from('<< /Type /Catalog /Pages 2 0 R >>', 'ascii'),
    Buffer.from('<< /Type /Pages /Kids [3 0 R] /Count 1 >>', 'ascii'),
    Buffer.from('<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>', 'ascii'),
    Buffer.from('<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>', 'ascii'),
    Buffer.concat([
      Buffer.from(`<< /Length ${stream.length} >>\nstream\n`, 'ascii'),
      stream,
      Buffer.from('\nendstream', 'ascii'),
    ]),
  ]
  const chunks = [Buffer.from('%PDF-1.4\n', 'ascii')]
  const offsets = [0]
  let length = chunks[0].length
  objects.forEach((object, index) => {
    offsets.push(length)
    const chunk = Buffer.concat([
      Buffer.from(`${index + 1} 0 obj\n`, 'ascii'),
      object,
      Buffer.from('\nendobj\n', 'ascii'),
    ])
    chunks.push(chunk)
    length += chunk.length
  })
  const xref = [
    `xref\n0 ${objects.length + 1}\n`,
    '0000000000 65535 f \n',
    ...offsets.slice(1).map((offset) => `${offset.toString().padStart(10, '0')} 00000 n \n`),
    `trailer\n<< /Size ${objects.length + 1} /Root 1 0 R >>\nstartxref\n${length}\n%%EOF\n`,
  ].join('')
  chunks.push(Buffer.from(xref, 'ascii'))
  return Buffer.concat(chunks)
}
