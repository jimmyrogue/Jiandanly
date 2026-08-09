import { execFileSync } from 'node:child_process'
import { copyFileSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, symlinkSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { afterAll, beforeAll, describe, expect, it } from 'vitest'

import {
  authorizeLocalWorkspace,
  cancelLocalRunCommand,
  createMcpServer,
  createLocalRun,
  deleteMcpServer,
  getLocalArtifactContent,
  getLocalRunDiagnostics,
  listMcpServers,
  reconcileLocalToolCommand,
  resolveLocalPermissionCommand,
  revokeLocalWorkspace,
  streamLocalRun,
} from './client'

const BASE_URL = process.env.VITE_TEST_RUNTIME_URL
const TOKEN = process.env.VITE_TEST_RUNTIME_TOKEN ?? 'dev-local-token'
const MCP_HTTP_URL = process.env.VITE_TEST_MCP_HTTP_URL
const REAL_LLM_MODEL = process.env.VITE_TEST_REAL_LLM_MODEL
const RUN_MODEL = (REAL_LLM_MODEL ?? 'local:test:model') as `local:${string}:${string}`
const DEFAULT_SETTINGS = { memory: 'off', skills: 'off', mcp: 'off' } as const
const MEMORY_SETTINGS = { ...DEFAULT_SETTINGS, memory: 'on' } as const
const EXHAUSTIVE_SCHEMA_TIMEOUT_MS = 60_000
const DEDICATED_TOOL_TESTS = [
  { name: 'read_file', category: 'filesystem', effect: 'read-only', risk: 'low', traits: 'read-only,workspace' },
  { name: 'write_file', category: 'filesystem', effect: 'workspace-write', risk: 'workspace', traits: 'permission,side-effect,workspace' },
  { name: 'user.ask', category: 'human-in-the-loop', effect: 'human-interaction', risk: 'human', traits: 'interrupt,resume' },
] as const
const HOST_GUARD_CASES = [
  { name: 'open.url', args: { url: 'file:///tmp/blocked' }, expected: 'only http(s)' },
  { name: 'open.url', args: { url: 'https://' }, expected: 'include a hostname' },
  {
    name: 'open.url',
    args: { url: 'https://user@example.invalid/private' },
    expected: 'credentials are not allowed',
  },
  { name: 'open.file', args: { path: '/missing-e2e-file' }, expected: 'file not found' },
] as const

type ToolCase = {
  name: string
  args: (workspace: string) => Record<string, unknown>
  expected: string
  assertFinalText?: boolean
  instruction?: string
  goal?: string
  outcome?: 'completed' | 'failed'
  settings?: typeof DEFAULT_SETTINGS | typeof MEMORY_SETTINGS
  verifyBeforeApproval?: (workspace: string) => void
  verifyEvents?: (events: RuntimeEvent[]) => void
  verify?: (workspace: string) => void
}

type CategorizedToolCase = ToolCase & {
  category: string
  effect: ToolEffect
  risk: ToolRisk
  traits: string
  expectedOutcome: 'success' | 'guarded-failure'
}

type ToolEffect =
  | 'read-only'
  | 'workspace-write'
  | 'runtime-state'
  | 'host-interaction'
  | 'human-interaction'

type ToolRisk = 'low' | 'workspace' | 'network' | 'host' | 'human'

const TOOL_CASES: ToolCase[] = [
  { name: 'time.now', args: () => ({ timezone: 'UTC' }), expected: 'UTC' },
  { name: 'environment.observe', args: () => ({}), expected: 'python' },
  { name: 'ls', args: () => ({ path: '/' }), expected: 'notes.txt' },
  { name: 'glob', args: () => ({ pattern: '*.txt', path: '/' }), expected: 'notes.txt' },
  {
    name: 'grep',
    args: () => ({ pattern: 'E2E needle', path: '/', output_mode: 'content' }),
    expected: 'E2E needle',
  },
  {
    name: 'edit_file',
    args: () => ({ file_path: '/edit.txt', old_string: 'before', new_string: 'after' }),
    expected: 'Successfully replaced',
    verifyBeforeApproval: workspace => expect(readFileSync(join(workspace, 'edit.txt'), 'utf8')).toBe('before'),
    verify: workspace => expect(readFileSync(join(workspace, 'edit.txt'), 'utf8')).toBe('after'),
  },
  { name: 'execute', args: () => ({ command: 'printf e2e-execute' }), expected: 'e2e-execute' },
  {
    name: 'task.verify',
    args: () => ({
      checks: [{ kind: 'file_contains', path: 'notes.txt', substring: 'E2E needle' }],
    }),
    expected: 'pass_count',
  },
  {
    name: 'task.progress',
    args: () => ({ summary: 'E2E progress recorded', status: 'verified' }),
    expected: 'E2E progress recorded',
  },
  {
    name: 'memory.write',
    args: () => ({
      fact: REAL_LLM_MODEL
        ? "SheJane's real E2E test color is cobalt"
        : 'E2E memory fact.',
    }),
    expected: 'saved',
    assertFinalText: !REAL_LLM_MODEL,
    goal: REAL_LLM_MODEL
      ? "Please remember that SheJane's real E2E test color is cobalt"
      : 'Please remember that E2E memory fact.',
    settings: MEMORY_SETTINGS,
    verifyEvents: REAL_LLM_MODEL
      ? events => expect(events).toEqual(expect.arrayContaining([
          expect.objectContaining({
            type: 'tool.requested',
            payload: expect.objectContaining({
              name: 'memory.write',
              arguments: expect.objectContaining({
                fact: "SheJane's real E2E test color is cobalt",
              }),
            }),
          }),
          expect.objectContaining({
            type: 'tool.completed',
            payload: expect.objectContaining({
              name: 'memory.write',
              content: expect.stringContaining('"saved": true'),
            }),
          }),
        ]))
      : undefined,
  },
  {
    name: 'memory.search',
    args: () => ({
      query: REAL_LLM_MODEL
        ? "SheJane's real E2E test color is cobalt"
        : 'E2E memory fact.',
    }),
    expected: REAL_LLM_MODEL
      ? "SheJane's real E2E test color is cobalt"
      : 'E2E memory fact.',
    settings: MEMORY_SETTINGS,
  },
  {
    name: 'web.fetch',
    args: () => ({ url: 'http://127.0.0.1/private' }),
    expected: 'refusing private/loopback address',
    outcome: 'failed',
  },
  {
    name: 'open.url',
    args: () => ({ url: 'https://example.invalid/shejane-e2e' }),
    expected: 'example.invalid/shejane-e2e',
  },
  {
    name: 'open.file',
    args: workspace => ({ path: join(workspace, 'notes.txt') }),
    expected: 'notes.txt',
  },
  { name: 'clipboard.write', args: () => ({ text: 'E2E' }), expected: 'bytes_written' },
  { name: 'clipboard.read', args: () => ({}), expected: 'text' },
  {
    name: 'office.read',
    args: workspace => ({ path: join(workspace, 'base.docx') }),
    expected: 'E2E heading',
  },
  {
    name: 'office.outline',
    args: workspace => ({ path: join(workspace, 'base.docx') }),
    expected: 'paragraph_count',
  },
  {
    name: 'office.read_range',
    args: workspace => ({ path: join(workspace, 'base.xlsx'), sheet: 'Data', range: 'A1:B2' }),
    expected: 'Needle',
  },
  {
    name: 'office.read_slides',
    args: workspace => ({ path: join(workspace, 'base.pptx') }),
    expected: 'E2E Deck',
  },
  officeWriteCase('office.find_replace', 'docx', workspace => ({
    path: freshOfficeFile(workspace, 'office.find_replace', 'docx'),
    find: 'Replace target',
    replace: 'Replaced E2E',
  }), '"replaced": 1'),
  officeWriteCase('office.insert_paragraph', 'docx', workspace => ({
    path: freshOfficeFile(workspace, 'office.insert_paragraph', 'docx'),
    anchor: 'Replace target',
    content: 'Inserted E2E',
    position: 'after',
  }), 'after anchor'),
  officeWriteCase('office.update_paragraph', 'docx', workspace => ({
    path: freshOfficeFile(workspace, 'office.update_paragraph', 'docx'),
    target: 'Update target',
    content: 'Updated E2E',
  }), 'Update target'),
  officeWriteCase('office.delete_paragraph', 'docx', workspace => ({
    path: freshOfficeFile(workspace, 'office.delete_paragraph', 'docx'),
    target: 'Delete target',
  }), 'Delete target'),
  officeWriteCase('office.apply_style', 'docx', workspace => ({
    path: freshOfficeFile(workspace, 'office.apply_style', 'docx'),
    target: 'Style target',
    style: 'Heading 2',
  }), 'Heading 2'),
  officeWriteCase('office.set_cells', 'xlsx', workspace => ({
    path: freshOfficeFile(workspace, 'office.set_cells', 'xlsx'),
    sheet: 'Data',
    range: 'C1:C2',
    values: [['Extra'], ['E2E']],
  }), 'C1:C2'),
  officeWriteCase('office.set_formula', 'xlsx', workspace => ({
    path: freshOfficeFile(workspace, 'office.set_formula', 'xlsx'),
    sheet: 'Data',
    cell: 'C2',
    formula: '=B2*2',
  }), '=B2*2'),
  officeWriteCase('office.set_cell_format', 'xlsx', workspace => ({
    path: freshOfficeFile(workspace, 'office.set_cell_format', 'xlsx'),
    sheet: 'Data',
    range: 'A1:B1',
    bold: true,
    bg_color: '#FFF2CC',
  }), 'cells_formatted'),
  officeWriteCase('office.merge_cells', 'xlsx', workspace => ({
    path: freshOfficeFile(workspace, 'office.merge_cells', 'xlsx'),
    sheet: 'Data',
    range: 'A3:B3',
  }), 'A3:B3'),
  officeWriteCase('office.add_row', 'xlsx', workspace => ({
    path: freshOfficeFile(workspace, 'office.add_row', 'xlsx'),
    sheet: 'Data',
    values: ['Added', 3],
  }), 'cells_written'),
  {
    name: 'office.create_pptx',
    args: (workspace) => {
      const path = officePath(workspace, 'office.create_pptx', 'pptx')
      rmSync(path, { force: true })
      return { path, title: 'E2E Created Deck' }
    },
    expected: 'E2E Created Deck',
    verifyBeforeApproval: workspace => expect(existsSync(officePath(workspace, 'office.create_pptx', 'pptx'))).toBe(false),
    verify: workspace => expect(existsSync(officePath(workspace, 'office.create_pptx', 'pptx'))).toBe(true),
  },
  officeWriteCase('office.add_slide', 'pptx', workspace => ({
    path: freshOfficeFile(workspace, 'office.add_slide', 'pptx'),
    title: 'Added slide',
    bullets: ['One', 'Two'],
  }), 'Title and Content'),
  officeWriteCase('office.update_slide', 'pptx', workspace => ({
    path: freshOfficeFile(workspace, 'office.update_slide', 'pptx'),
    index: 0,
    title: 'Updated slide',
  }), '"index": 0'),
  officeWriteCase('office.delete_slide', 'pptx', workspace => ({
    path: freshOfficeFile(workspace, 'office.delete_slide', 'pptx'),
    index: 1,
  }), 'Second slide'),
  officeWriteCase('office.reorder_slides', 'pptx', workspace => ({
    path: freshOfficeFile(workspace, 'office.reorder_slides', 'pptx'),
    from_index: 0,
    to_index: 1,
  }), 'from_index'),
  officeWriteCase('office.set_slide_title', 'pptx', workspace => ({
    path: freshOfficeFile(workspace, 'office.set_slide_title', 'pptx'),
    index: 0,
    title: 'Retitled E2E',
  }), '"index": 0'),
  officeWriteCase('office.set_slide_bullets', 'pptx', workspace => ({
    path: freshOfficeFile(workspace, 'office.set_slide_bullets', 'pptx'),
    index: 1,
    bullets: ['Updated bullet'],
  }), '"index": 1'),
  officeWriteCase('office.set_slide_notes', 'pptx', workspace => ({
    path: freshOfficeFile(workspace, 'office.set_slide_notes', 'pptx'),
    index: 0,
    notes: 'E2E speaker notes',
  }), 'E2E speaker notes'),
  officeWriteCase('office.add_image_to_slide', 'pptx', workspace => ({
    path: freshOfficeFile(workspace, 'office.add_image_to_slide', 'pptx'),
    index: 0,
    image_path: join(workspace, 'pixel.png'),
    width_in: 1,
  }), 'pixel.png'),
]

const CATEGORIZED_TOOL_CASES: CategorizedToolCase[] = TOOL_CASES.map((toolCase) => {
  const effect = toolEffect(toolCase.name)
  return {
    ...toolCase,
    category: toolCategory(toolCase.name),
    effect,
    risk: toolRisk(toolCase.name, effect),
    traits: toolTraits(toolCase.name, effect),
    expectedOutcome: toolCase.outcome === 'failed' ? 'guarded-failure' : 'success',
  }
})

describe.skipIf(!BASE_URL)('flow:P10 > contract: every Runtime Tool (live runtime)', () => {
  const config = { baseURL: BASE_URL!, token: TOKEN }
  const workspace = mkdtempSync(join(tmpdir(), 'shejane-e2e-tools-'))
  let workspaceID = ''

  beforeAll(async () => {
    writeFileSync(join(workspace, 'notes.txt'), 'E2E needle\n')
    writeFileSync(join(workspace, 'edit.txt'), 'before')
    createOfficeFixtures(workspace)
    workspaceID = (await authorizeLocalWorkspace(workspace, config)).id
  })

  afterAll(async () => {
    if (workspaceID) await revokeLocalWorkspace(workspaceID, config).catch(() => undefined)
    rmSync(workspace, { recursive: true, force: true })
  })

  it('keeps every published Tool on an executed E2E path', async () => {
    const response = await fetch(`${BASE_URL}/v1/tools`, {
      headers: { Authorization: `Bearer ${TOKEN}` },
    })
    expect(response.status).toBe(200)
    const body = await response.json() as { tools: Array<{ name: string }> }
    const published = body.tools.map((tool) => tool.name).sort()
    const executed = [
      ...CATEGORIZED_TOOL_CASES.map(tool => tool.name),
      ...DEDICATED_TOOL_TESTS.map(tool => tool.name),
    ].sort()
    expect(executed).toEqual(published)
  })

  it('rejects a missing required field for every published Tool schema before execution', async () => {
    const response = await fetch(`${BASE_URL}/v1/tools`, {
      headers: { Authorization: `Bearer ${TOKEN}` },
    })
    expect(response.status).toBe(200)
    const body = await response.json() as {
      tools: Array<{
        name: string
        args_schema?: { required?: unknown }
      }>
    }
    const validArgsByName = new Map(
      CATEGORIZED_TOOL_CASES.map(toolCase => [toolCase.name, toolCase.args(workspace)]),
    )
    validArgsByName.set('read_file', { file_path: '/notes.txt' })
    validArgsByName.set('write_file', {
      file_path: '/schema-invalid-must-not-exist.txt',
      content: 'must not execute',
    })
    validArgsByName.set('user.ask', {
      question: 'Schema validation must stop before this question is shown',
      options: ['A', 'B'],
    })

    const tested: string[] = []
    const notApplicable: string[] = []
    for (const published of body.tools) {
      const required = Array.isArray(published.args_schema?.required)
        ? published.args_schema.required.filter((item): item is string => typeof item === 'string')
        : []
      if (required.length === 0) {
        notApplicable.push(`${published.name}: no required inputs`)
        continue
      }
      const validArgs = validArgsByName.get(published.name)
      expect(validArgs, `missing valid E2E arguments for ${published.name}`).toBeDefined()
      const missingField = required[0]
      const invalidArgs = { ...validArgs }
      delete invalidArgs[missingField]
      const suffix = `${Date.now().toString(36)}_${published.name.replaceAll('.', '_')}`
      const run = await createLocalRun({
        commandId: `cmd_e2e_required_field_${suffix}`,
        clientMessageId: `msg_e2e_required_field_${suffix}`,
        goal: encodedToolGoal(published.name, invalidArgs),
        workspacePath: workspace,
        mode: RUN_MODEL,
        settings: published.name.startsWith('memory.') ? MEMORY_SETTINGS : DEFAULT_SETTINGS,
      }, config)
      const events: RuntimeEvent[] = []
      await collectRunEvents(run.id, config, events)

      expect(
        events.map(event => event.type),
        `${published.name} accepted missing required field ${missingField}`,
      ).not.toContain('permission.required')
      expect(events).toEqual(expect.arrayContaining([
        expect.objectContaining({
          type: 'tool.failed',
          payload: expect.objectContaining({ name: published.name }),
        }),
      ]))
      expect(
        events.some(event => ['run.completed', 'run.failed'].includes(event.type)),
        `${published.name} did not reach a durable terminal state after validation failed`,
      ).toBe(true)
      const diagnostics = await getLocalRunDiagnostics(run.id, config)
      expect(diagnostics.tool_receipts).toEqual(expect.arrayContaining([
        expect.objectContaining({
          tool_name: published.name,
          status: 'failed',
          attempt_count: 0,
          error_type: 'ToolInputValidationError',
        }),
      ]))
      tested.push(`${published.name}.${missingField}`)
    }

    expect(tested.length).toBeGreaterThan(0)
    expect(tested.length + notApplicable.length).toBe(body.tools.length)
    expect(existsSync(join(workspace, 'schema-invalid-must-not-exist.txt'))).toBe(false)
  }, EXHAUSTIVE_SCHEMA_TIMEOUT_MS)

  it('rejects unknown fields for every closed published Tool schema before execution', async () => {
    const response = await fetch(`${BASE_URL}/v1/tools`, {
      headers: { Authorization: `Bearer ${TOKEN}` },
    })
    expect(response.status).toBe(200)
    const body = await response.json() as {
      tools: Array<{
        name: string
        args_schema?: { additionalProperties?: unknown }
      }>
    }
    const validArgsByName = new Map(
      CATEGORIZED_TOOL_CASES.map(toolCase => [toolCase.name, toolCase.args(workspace)]),
    )
    validArgsByName.set('read_file', { file_path: '/notes.txt' })
    validArgsByName.set('write_file', {
      file_path: '/schema-extra-must-not-exist.txt',
      content: 'must not execute',
    })
    validArgsByName.set('user.ask', {
      question: 'Schema validation must stop before this question is shown',
      options: ['A', 'B'],
    })

    const tested: string[] = []
    const notApplicable: string[] = []
    for (const published of body.tools) {
      if (published.args_schema?.additionalProperties !== false) {
        notApplicable.push(`${published.name}: schema permits or does not constrain extra fields`)
        continue
      }
      const validArgs = validArgsByName.get(published.name)
      expect(validArgs, `missing valid E2E arguments for ${published.name}`).toBeDefined()
      const invalidArgs = { ...validArgs, __e2e_unexpected_field__: 'must be rejected' }
      const suffix = `${Date.now().toString(36)}_${published.name.replaceAll('.', '_')}`
      const run = await createLocalRun({
        commandId: `cmd_e2e_extra_field_${suffix}`,
        clientMessageId: `msg_e2e_extra_field_${suffix}`,
        goal: encodedToolGoal(published.name, invalidArgs),
        workspacePath: workspace,
        mode: RUN_MODEL,
        settings: published.name.startsWith('memory.') ? MEMORY_SETTINGS : DEFAULT_SETTINGS,
      }, config)
      const events: RuntimeEvent[] = []
      await collectRunEvents(run.id, config, events)

      expect(
        events.map(event => event.type),
        `${published.name} accepted an undeclared input field`,
      ).not.toContain('permission.required')
      expect(events).toEqual(expect.arrayContaining([
        expect.objectContaining({
          type: 'tool.failed',
          payload: expect.objectContaining({ name: published.name }),
        }),
      ]))
      expect(
        events.some(event => ['run.completed', 'run.failed'].includes(event.type)),
        `${published.name} did not reach a durable terminal state after extra-field validation failed`,
      ).toBe(true)
      const diagnostics = await getLocalRunDiagnostics(run.id, config)
      expect(diagnostics.tool_receipts).toEqual(expect.arrayContaining([
        expect.objectContaining({
          tool_name: published.name,
          status: 'failed',
          attempt_count: 0,
          error_type: 'ToolInputValidationError',
        }),
      ]))
      tested.push(published.name)
    }

    expect(tested.length).toBeGreaterThan(0)
    expect(tested.length + notApplicable.length).toBe(body.tools.length)
    expect(existsSync(join(workspace, 'schema-extra-must-not-exist.txt'))).toBe(false)
  }, EXHAUSTIVE_SCHEMA_TIMEOUT_MS)

  it('persists an explicitly stated name in one turn and recalls it in a new thread', async () => {
    const suffix = Date.now().toString(36)
    const fact = `我的名字是 Jimmy-${suffix}`
    const threadID = `thread_e2e_memory_name_${suffix}`
    const firstRun = await createLocalRun({
      commandId: `cmd_e2e_memory_fact_${suffix}`,
      clientMessageId: `msg_e2e_memory_fact_${suffix}`,
      threadId: threadID,
      userInput: fact,
      goal: REAL_LLM_MODEL
        ? fact
        : encodedToolGoal('memory.write', { fact }),
      mode: RUN_MODEL,
      settings: MEMORY_SETTINGS,
    }, config)
    const firstEvents: RuntimeEvent[] = []
    await collectRunEvents(firstRun.id, config, firstEvents)
    expect(firstEvents).toEqual(expect.arrayContaining([
      expect.objectContaining({
        type: 'tool.completed',
        payload: expect.objectContaining({
          name: 'memory.write',
          content: expect.stringContaining('"saved": true'),
        }),
      }),
      expect.objectContaining({ type: 'run.completed' }),
    ]))

    const recallRun = await createLocalRun({
      commandId: `cmd_e2e_memory_recall_${suffix}`,
      clientMessageId: `msg_e2e_memory_recall_${suffix}`,
      threadId: `thread_e2e_memory_recall_${suffix}`,
      userInput: '我的名字是什么？',
      goal: REAL_LLM_MODEL
        ? '我的名字是什么？请先查询长期记忆。'
        : encodedToolGoal('memory.search', { query: '名字' }, undefined, fact),
      mode: RUN_MODEL,
      settings: MEMORY_SETTINGS,
    }, config)
    const recallEvents: RuntimeEvent[] = []
    await collectRunEvents(recallRun.id, config, recallEvents)
    expect(recallEvents).toEqual(expect.arrayContaining([
      expect.objectContaining({
        type: 'tool.completed',
        payload: expect.objectContaining({
          name: 'memory.search',
          content: expect.stringContaining(fact),
        }),
      }),
      expect.objectContaining({
        type: 'run.completed',
        payload: expect.objectContaining({ final_text: expect.stringContaining(fact) }),
      }),
    ]))
  })

  it('resolves remember-my-name against the previous user turn over HTTP/SSE', async () => {
    const suffix = Date.now().toString(36)
    const fact = `我的名字是 Reference-${suffix}`
    const threadID = `thread_e2e_memory_reference_${suffix}`
    const factRun = await createLocalRun({
      commandId: `cmd_e2e_memory_reference_fact_${suffix}`,
      clientMessageId: `msg_e2e_memory_reference_fact_${suffix}`,
      threadId: threadID,
      userInput: fact,
      goal: fact,
      mode: RUN_MODEL,
      settings: DEFAULT_SETTINGS,
    }, config)
    const factEvents: RuntimeEvent[] = []
    await collectRunEvents(factRun.id, config, factEvents)
    expect(factEvents).toEqual(expect.arrayContaining([
      expect.objectContaining({ type: 'run.completed' }),
    ]))

    const rememberRun = await createLocalRun({
      commandId: `cmd_e2e_memory_reference_save_${suffix}`,
      clientMessageId: `msg_e2e_memory_reference_save_${suffix}`,
      threadId: threadID,
      userInput: '记住我的名字',
      goal: REAL_LLM_MODEL
        ? '记住我的名字'
        : encodedToolGoal('memory.write', { fact }),
      mode: RUN_MODEL,
      settings: MEMORY_SETTINGS,
    }, config)
    const rememberEvents: RuntimeEvent[] = []
    await collectRunEvents(rememberRun.id, config, rememberEvents)
    expect(rememberEvents).toEqual(expect.arrayContaining([
      expect.objectContaining({
        type: 'tool.completed',
        payload: expect.objectContaining({
          name: 'memory.write',
          content: expect.stringContaining('"saved": true'),
        }),
      }),
      expect.objectContaining({ type: 'run.completed' }),
    ]))
  })

  it.each(CATEGORIZED_TOOL_CASES)(
    'tool:$name > family:$category > effect:$effect > risk:$risk > traits:$traits > outcome:$expectedOutcome',
    async (toolCase) => {
      const args = toolCase.args(workspace)
      const suffix = `${Date.now().toString(36)}_${toolCase.name.replaceAll('.', '_')}`
      const run = await createLocalRun({
        commandId: `cmd_e2e_tool_${suffix}`,
        clientMessageId: `msg_e2e_tool_${suffix}`,
        goal: toolCase.goal ?? encodedToolGoal(
          toolCase.name,
          args,
          toolCase.instruction,
          toolCase.expected,
        ),
        workspacePath: workspace,
        mode: RUN_MODEL,
        settings: toolCase.settings ?? DEFAULT_SETTINGS,
      }, config)
      const events = await streamThroughPermission(
        run.id,
        suffix,
        config,
        toolCase.verifyBeforeApproval
          ? () => toolCase.verifyBeforeApproval?.(workspace)
          : undefined,
      )
      const eventTypes = events.map(event => event.type)

      if (toolCase.traits.split(',').includes('permission')) {
        expect(eventTypes, `${toolCase.name} bypassed its declared permission gate`)
          .toContain('permission.required')
      }
      if (toolCase.traits.split(',').includes('read-only')) {
        expect(eventTypes, `${toolCase.name} unexpectedly requested permission`)
          .not.toContain('permission.required')
      }

      expect(events).toEqual(expect.arrayContaining([
        expect.objectContaining({
          type: 'tool.requested',
          payload: expect.objectContaining({ name: toolCase.name }),
        }),
        expect.objectContaining({
          type: `tool.${toolCase.outcome ?? 'completed'}`,
          payload: expect.objectContaining({ name: toolCase.name }),
        }),
        expect.objectContaining({ type: 'run.completed' }),
      ]))
      const completed = [...events].reverse().find(event => event.type === 'run.completed')
      if (toolCase.assertFinalText !== false) {
        expect(String(completed?.payload.final_text ?? '')).toContain(toolCase.expected)
      }
      toolCase.verifyEvents?.(events)
      const diagnostics = await getLocalRunDiagnostics(run.id, config)
      expect(diagnostics.tool_receipts).toEqual(expect.arrayContaining([
        expect.objectContaining({
          tool_name: toolCase.name,
          status: toolCase.outcome ?? 'completed',
          attempt_count: 1,
        }),
      ]))
      toolCase.verify?.(workspace)
    },
  )

  it.each(HOST_GUARD_CASES)(
    'tool:$name > family:host-integration > risk:host > edge:guarded-failure',
    async ({ name, args, expected }) => {
      const suffix = `${Date.now().toString(36)}_${name.replaceAll('.', '_')}`
      const run = await createLocalRun({
        commandId: `cmd_e2e_host_guard_${suffix}`,
        clientMessageId: `msg_e2e_host_guard_${suffix}`,
        goal: encodedToolGoal(name, args, undefined, expected),
        workspacePath: workspace,
        mode: RUN_MODEL,
        settings: DEFAULT_SETTINGS,
      }, config)
      const events = await streamThroughPermission(run.id, suffix, config)

      expect(events).toEqual(expect.arrayContaining([
        expect.objectContaining({
          type: 'tool.failed',
          payload: expect.objectContaining({ name }),
        }),
        expect.objectContaining({ type: 'run.completed' }),
      ]))
      const completed = [...events].reverse().find(event => event.type === 'run.completed')
      expect(String(completed?.payload.final_text ?? '')).toContain(expected)
    },
  )

  describe('flow: validation and failure containment', () => {
    it('rejects invalid arguments before permission or execution', async () => {
      const suffix = Date.now().toString(36)
      const run = await createLocalRun({
        commandId: `cmd_e2e_invalid_args_${suffix}`,
        clientMessageId: `msg_e2e_invalid_args_${suffix}`,
        goal: encodedToolGoal('write_file', { file_path: 'invalid.txt', text: 'wrong key' }),
        workspacePath: workspace,
        mode: RUN_MODEL,
        settings: DEFAULT_SETTINGS,
      }, config)
      const events: RuntimeEvent[] = []
      await collectRunEvents(run.id, config, events)

      expect(events.map(event => event.type)).not.toContain('permission.required')
      expect(events).toEqual(expect.arrayContaining([
        expect.objectContaining({
          type: 'tool.failed',
          payload: expect.objectContaining({ name: 'write_file' }),
        }),
        expect.objectContaining({ type: 'run.completed' }),
      ]))
      const diagnostics = await getLocalRunDiagnostics(run.id, config)
      expect(diagnostics.tool_receipts).toEqual(expect.arrayContaining([
        expect.objectContaining({
          tool_name: 'write_file',
          status: 'failed',
          attempt_count: 0,
          error_type: 'ToolInputValidationError',
        }),
      ]))
      expect(existsSync(join(workspace, 'invalid.txt'))).toBe(false)
    })

    it('returns an observable failure for an unknown Tool name', async () => {
      const suffix = Date.now().toString(36)
      const run = await createLocalRun({
        commandId: `cmd_e2e_unknown_tool_${suffix}`,
        clientMessageId: `msg_e2e_unknown_tool_${suffix}`,
        goal: encodedToolGoal('unknown.e2e_tool', { value: 'ignored' }),
        workspacePath: workspace,
        mode: RUN_MODEL,
        settings: DEFAULT_SETTINGS,
      }, config)
      const events: RuntimeEvent[] = []
      await collectRunEvents(run.id, config, events)

      expect(events).toEqual(expect.arrayContaining([
        expect.objectContaining({
          type: 'tool.failed',
          payload: expect.objectContaining({ name: 'unknown.e2e_tool' }),
        }),
        expect.objectContaining({ type: 'run.completed' }),
      ]))
      expect(JSON.stringify(events)).toContain('not available in this Runtime definition')
    })

    it('stores oversized Tool output as an Artifact with a bounded model handoff', async () => {
      const suffix = Date.now().toString(36)
      const run = await createLocalRun({
        commandId: `cmd_e2e_large_output_${suffix}`,
        clientMessageId: `msg_e2e_large_output_${suffix}`,
        goal: encodedToolGoal('execute', { command: 'yes x | head -c 70000' }),
        workspacePath: workspace,
        mode: RUN_MODEL,
        settings: DEFAULT_SETTINGS,
      }, config)
      const events = await streamThroughPermission(run.id, suffix, config)
      expect(events).toEqual(expect.arrayContaining([
        expect.objectContaining({ type: 'tool.completed' }),
        expect.objectContaining({ type: 'run.completed' }),
      ]))

      const diagnostics = await getLocalRunDiagnostics(run.id, config)
      const artifact = diagnostics.artifacts.find(item => item.tool_name === 'execute')
      expect(artifact).toMatchObject({ kind: 'tool_output' })
      expect(artifact?.bytes).toBeGreaterThan(64 * 1024)
      const content = await getLocalArtifactContent(artifact!.id, config)
      expect(content.size).toBeGreaterThan(64 * 1024)
      const completed = [...events].reverse().find(event => event.type === 'run.completed')
      expect(String(completed?.payload.final_text ?? '').length).toBeLessThan(70_000)
      expect(String(completed?.payload.final_text ?? '')).toContain(artifact!.id)
    })

    it('preserves execute cwd, stdout, stderr, and a non-zero exit code', async () => {
      const suffix = Date.now().toString(36)
      const run = await createLocalRun({
        commandId: `cmd_e2e_execute_nonzero_${suffix}`,
        clientMessageId: `msg_e2e_execute_nonzero_${suffix}`,
        goal: encodedToolGoal('execute', {
          command: "pwd; printf 'stdout marker\\n'; printf 'stderr marker\\n' >&2; exit 7",
        }),
        workspacePath: workspace,
        mode: RUN_MODEL,
        settings: DEFAULT_SETTINGS,
      }, config)
      const events = await streamThroughPermission(run.id, `execute_nonzero_${suffix}`, config)
      const serialized = JSON.stringify(events)

      expect(events).toEqual(expect.arrayContaining([
        expect.objectContaining({
          type: 'tool.completed',
          payload: expect.objectContaining({ name: 'execute' }),
        }),
        expect.objectContaining({ type: 'run.completed' }),
      ]))
      expect(serialized).toContain(workspace)
      expect(serialized).toContain('stdout marker')
      expect(serialized).toContain('[stderr] stderr marker')
      expect(serialized).toContain('exit code 7')
      const diagnostics = await getLocalRunDiagnostics(run.id, config)
      expect(diagnostics.tool_receipts).toEqual(expect.arrayContaining([
        expect.objectContaining({
          tool_name: 'execute',
          status: 'completed',
          attempt_count: 1,
        }),
      ]))
    })

    it('contains invalid UTF-8 output and reads quoted paths with spaces', async () => {
      const suffix = Date.now().toString(36)
      const directory = join(workspace, `space ' quoted ${suffix}`)
      const outputPath = join(directory, 'result file.txt')
      mkdirSync(directory, { recursive: true })
      writeFileSync(outputPath, 'quoted path ok')
      const run = await createLocalRun({
        commandId: `cmd_e2e_execute_encoding_${suffix}`,
        clientMessageId: `msg_e2e_execute_encoding_${suffix}`,
        goal: encodedToolGoal('execute', {
          command: [
            `cat ${shellQuote(outputPath)}`,
            "printf '\\377binary-tail\\n'",
          ].join('; '),
        }),
        workspacePath: workspace,
        mode: RUN_MODEL,
        settings: DEFAULT_SETTINGS,
      }, config)
      const events = await streamThroughPermission(run.id, `execute_encoding_${suffix}`, config)

      expect(events).toEqual(expect.arrayContaining([
        expect.objectContaining({
          type: 'tool.completed',
          payload: expect.objectContaining({ name: 'execute' }),
        }),
        expect.objectContaining({ type: 'run.completed' }),
      ]))
      expect(readFileSync(outputPath, 'utf8')).toBe('quoted path ok')
      expect(JSON.stringify(events)).toContain('binary-tail')
    })

    it('terminates the complete shell process group when execute times out', async () => {
      const suffix = Date.now().toString(36)
      const marker = `shejane-timeout-${suffix}`
      const run = await createLocalRun({
        commandId: `cmd_e2e_execute_timeout_${suffix}`,
        clientMessageId: `msg_e2e_execute_timeout_${suffix}`,
        goal: encodedToolGoal('execute', {
          command: `/bin/sh -c 'sleep 30' ${marker} & wait`,
          timeout: 1,
        }),
        workspacePath: workspace,
        mode: RUN_MODEL,
        settings: DEFAULT_SETTINGS,
      }, config)
      const events = await streamThroughPermission(run.id, `execute_timeout_${suffix}`, config)

      expect(events).toEqual(expect.arrayContaining([
        expect.objectContaining({
          type: 'tool.completed',
          payload: expect.objectContaining({ name: 'execute' }),
        }),
        expect.objectContaining({ type: 'run.completed' }),
      ]))
      expect(JSON.stringify(events)).toContain('timed out after 1 seconds')
      expect(processesForMarker(marker)).toEqual([])
    })

    it('cancels execute and reaps the complete shell process group', async () => {
      const suffix = Date.now().toString(36)
      const marker = `shejane-cancel-${suffix}`
      const run = await createLocalRun({
        commandId: `cmd_e2e_execute_cancel_${suffix}`,
        clientMessageId: `msg_e2e_execute_cancel_${suffix}`,
        goal: encodedToolGoal('execute', {
          command: `/bin/sh -c 'sleep 30' ${marker} & wait`,
        }),
        workspacePath: workspace,
        mode: RUN_MODEL,
        settings: DEFAULT_SETTINGS,
      }, config)
      const events: RuntimeEvent[] = []
      await collectRunEvents(run.id, config, events)
      const permission = events.find(event => event.type === 'permission.required')
      expect(permission).toBeDefined()
      await resolveLocalPermissionCommand(
        `cmd_e2e_execute_cancel_approve_${suffix}`,
        String(permission?.payload.request_id ?? ''),
        'approve',
        { scope: 'once' },
        config,
      )

      const activeStream = collectRunEvents(run.id, config, events)
      const childPIDs = await waitForProcessMarker(marker)
      const cancelReceipt = await cancelLocalRunCommand(
        `cmd_e2e_execute_cancel_run_${suffix}`,
        run.id,
        config,
      )
      expect(cancelReceipt).toMatchObject({ canceled: true })
      await activeStream

      const diagnostics = await getLocalRunDiagnostics(run.id, config)
      expect(
        events.map(event => event.type),
        JSON.stringify({ cancelReceipt, run: diagnostics.run, events }, null, 2),
      ).toContain('run.canceled')
      expect(events.map(event => event.type)).not.toContain('run.completed')
      for (const childPID of childPIDs) await expectProcessGone(childPID)
    })
  })

  describe('flow: workspace isolation', () => {
    it('round-trips a Unicode path and content through approval and execution', async () => {
      const suffix = Date.now().toString(36)
      const fileName = `石间-${suffix}.txt`
      const content = '你好，世界 🌱\n第二行'
      const run = await createLocalRun({
        commandId: `cmd_e2e_unicode_${suffix}`,
        clientMessageId: `msg_e2e_unicode_${suffix}`,
        goal: encodedToolGoal('write_file', { file_path: `/${fileName}`, content }),
        workspacePath: workspace,
        mode: RUN_MODEL,
        settings: DEFAULT_SETTINGS,
      }, config)
      const events = await streamThroughPermission(run.id, suffix, config)

      expect(events.map(event => event.type)).toContain('run.completed')
      expect(readFileSync(join(workspace, fileName), 'utf8')).toBe(content)
      const diagnostics = await getLocalRunDiagnostics(run.id, config)
      expect(diagnostics.tool_receipts).toEqual(expect.arrayContaining([
        expect.objectContaining({ tool_name: 'write_file', status: 'completed' }),
      ]))
    })

    it('does not follow an in-workspace symlink outside the authorized root', async () => {
      const outside = mkdtempSync(join(tmpdir(), 'shejane-e2e-outside-'))
      const link = join(workspace, `escape-${Date.now().toString(36)}.txt`)
      writeFileSync(join(outside, 'secret.txt'), 'must-not-cross-workspace-boundary')
      symlinkSync(join(outside, 'secret.txt'), link)
      try {
        const suffix = Date.now().toString(36)
        const run = await createLocalRun({
          commandId: `cmd_e2e_symlink_${suffix}`,
          clientMessageId: `msg_e2e_symlink_${suffix}`,
          goal: encodedToolGoal('read_file', { file_path: `/${link.split('/').at(-1)}` }),
          workspacePath: workspace,
          mode: RUN_MODEL,
          settings: DEFAULT_SETTINGS,
        }, config)
        const events: RuntimeEvent[] = []
        await collectRunEvents(run.id, config, events)

        const failed = events.find(event => event.type === 'tool.failed')
        expect(failed?.payload.name).toBe('read_file')
        expect(JSON.stringify(events)).not.toContain('must-not-cross-workspace-boundary')
        expect(events.map(event => event.type)).toContain('run.completed')
      } finally {
        rmSync(link, { force: true })
        rmSync(outside, { recursive: true, force: true })
      }
    })

    it('round-trips an empty file and reports its empty state as a successful read', async () => {
      const suffix = Date.now().toString(36)
      const fileName = `empty-${suffix}.txt`
      const writeRun = await createLocalRun({
        commandId: `cmd_e2e_empty_write_${suffix}`,
        clientMessageId: `msg_e2e_empty_write_${suffix}`,
        goal: encodedToolGoal('write_file', { file_path: `/${fileName}`, content: '' }),
        workspacePath: workspace,
        mode: RUN_MODEL,
        settings: DEFAULT_SETTINGS,
      }, config)
      const writeEvents = await streamThroughPermission(writeRun.id, `empty_write_${suffix}`, config)
      expect(writeEvents.map(event => event.type)).toEqual(expect.arrayContaining([
        'tool.completed',
        'run.completed',
      ]))
      expect(readFileSync(join(workspace, fileName), 'utf8')).toBe('')

      const readRun = await createLocalRun({
        commandId: `cmd_e2e_empty_read_${suffix}`,
        clientMessageId: `msg_e2e_empty_read_${suffix}`,
        goal: encodedToolGoal('read_file', { file_path: `/${fileName}` }),
        workspacePath: workspace,
        mode: RUN_MODEL,
        settings: DEFAULT_SETTINGS,
      }, config)
      const readEvents: RuntimeEvent[] = []
      await collectRunEvents(readRun.id, config, readEvents)
      expect(readEvents).toEqual(expect.arrayContaining([
        expect.objectContaining({ type: 'tool.completed' }),
        expect.objectContaining({ type: 'run.completed' }),
      ]))
      expect(JSON.stringify(readEvents)).toContain('File exists but has empty contents')
    })

    it('honors read_file offset and limit without leaking adjacent lines', async () => {
      const suffix = Date.now().toString(36)
      const fileName = `pagination-${suffix}.txt`
      writeFileSync(
        join(workspace, fileName),
        ['PAGE_ONE', 'PAGE_TWO', 'PAGE_THREE', 'PAGE_FOUR', 'PAGE_FIVE'].join('\n'),
      )
      const run = await createLocalRun({
        commandId: `cmd_e2e_pagination_${suffix}`,
        clientMessageId: `msg_e2e_pagination_${suffix}`,
        goal: encodedToolGoal('read_file', { file_path: `/${fileName}`, offset: 1, limit: 2 }),
        workspacePath: workspace,
        mode: RUN_MODEL,
        settings: DEFAULT_SETTINGS,
      }, config)
      const events: RuntimeEvent[] = []
      await collectRunEvents(run.id, config, events)
      const serialized = JSON.stringify(events)

      expect(events.map(event => event.type)).toEqual(expect.arrayContaining([
        'tool.completed',
        'run.completed',
      ]))
      expect(serialized).toContain('PAGE_TWO')
      expect(serialized).toContain('PAGE_THREE')
      expect(serialized).not.toContain('PAGE_ONE')
      expect(serialized).not.toContain('PAGE_FOUR')
      expect(serialized).not.toContain('PAGE_FIVE')
    })

    it('rejects a workspace file above the Runtime read-size limit without loading it', async () => {
      const suffix = Date.now().toString(36)
      const fileName = `oversized-${suffix}.txt`
      writeFileSync(join(workspace, fileName), Buffer.alloc(21 * 1024 * 1024, 0x78))
      const run = await createLocalRun({
        commandId: `cmd_e2e_oversized_read_${suffix}`,
        clientMessageId: `msg_e2e_oversized_read_${suffix}`,
        goal: encodedToolGoal('read_file', { file_path: `/${fileName}` }),
        workspacePath: workspace,
        mode: RUN_MODEL,
        settings: DEFAULT_SETTINGS,
      }, config)
      const events: RuntimeEvent[] = []
      await collectRunEvents(run.id, config, events)

      expect(events.map(event => event.type)).not.toContain('permission.required')
      expect(events).toEqual(expect.arrayContaining([
        expect.objectContaining({
          type: 'tool.failed',
          payload: expect.objectContaining({ name: 'read_file' }),
        }),
        expect.objectContaining({ type: 'run.completed' }),
      ]))
      expect(JSON.stringify(events)).toMatch(/too large|size|20 MB/i)
      const diagnostics = await getLocalRunDiagnostics(run.id, config)
      expect(diagnostics.tool_receipts).toEqual(expect.arrayContaining([
        expect.objectContaining({
          tool_name: 'read_file',
          status: 'failed',
          attempt_count: 1,
        }),
      ]))
    })

    it.each([
      {
        label: 'missing match',
        content: 'alpha\nbeta\n',
        oldString: 'gamma',
        expected: 'String not found in file',
      },
      {
        label: 'ambiguous match',
        content: 'repeat\nrepeat\n',
        oldString: 'repeat',
        expected: 'appears 2 times in file',
      },
    ])('contains edit_file $label without changing the file', async ({
      label,
      content,
      oldString,
      expected,
    }) => {
      const suffix = `${Date.now().toString(36)}_${label.replaceAll(' ', '_')}`
      const fileName = `edit-edge-${suffix}.txt`
      const path = join(workspace, fileName)
      writeFileSync(path, content)
      const run = await createLocalRun({
        commandId: `cmd_e2e_edit_edge_${suffix}`,
        clientMessageId: `msg_e2e_edit_edge_${suffix}`,
        goal: encodedToolGoal('edit_file', {
          file_path: `/${fileName}`,
          old_string: oldString,
          new_string: 'replacement',
        }),
        workspacePath: workspace,
        mode: RUN_MODEL,
        settings: DEFAULT_SETTINGS,
      }, config)
      const events = await streamThroughPermission(run.id, `edit_edge_${suffix}`, config)

      expect(events).toEqual(expect.arrayContaining([
        expect.objectContaining({
          type: 'tool.failed',
          payload: expect.objectContaining({ name: 'edit_file' }),
        }),
        expect.objectContaining({ type: 'run.completed' }),
      ]))
      expect(JSON.stringify(events)).toContain(expected)
      expect(readFileSync(path, 'utf8')).toBe(content)
      const diagnostics = await getLocalRunDiagnostics(run.id, config)
      expect(diagnostics.tool_receipts).toEqual(expect.arrayContaining([
        expect.objectContaining({
          tool_name: 'edit_file',
          status: 'failed',
          attempt_count: 1,
        }),
      ]))
    })
  })

  it('searches and executes successful and failing stdio MCP Tools', async () => {
    const serverName = 'e2e-runtime-mcp'
    const toolName = `${serverName}_echo`
    const runtimeRoot = resolve(process.cwd(), '../runtime')
    const suffix = Date.now().toString(36)
    const pidLog = join(workspace, `mcp-pids-${suffix}.log`)
    const cursorLog = join(workspace, `mcp-cursors-${suffix}.log`)
    await deleteMcpServer(serverName, config).catch(() => undefined)
    try {
      await createMcpServer({
        name: serverName,
        transport: 'stdio',
        command: 'uv',
        args: ['run', 'python', 'tests/fixtures/e2e_mcp_server.py'],
        env: { E2E_MCP_PID_LOG: pidLog, E2E_MCP_CURSOR_LOG: cursorLog },
        cwd: runtimeRoot,
      }, config)
      await waitForMcpServer(serverName, config)
      await waitForFile(cursorLog)
      const discoveryCursors = readFileSync(cursorLog, 'utf8').trim().split('\n')
      expect(discoveryCursors[0]).toBe('<start>')
      expect(discoveryCursors).toContain('page:4')

      const searchRun = await createLocalRun({
        commandId: `cmd_e2e_mcp_search_${suffix}`,
        clientMessageId: `msg_e2e_mcp_search_${suffix}`,
        goal: encodedToolGoal('mcp.search_tools', { query: 'echo', limit: 3 }),
        workspacePath: workspace,
        mode: RUN_MODEL,
        settings: { ...DEFAULT_SETTINGS, mcp: 'on' },
      }, config)
      const searchEvents = await streamThroughPermission(searchRun.id, `search_${suffix}`, config)
      expect(searchEvents).toEqual(expect.arrayContaining([
        expect.objectContaining({
          type: 'tool.requested',
          payload: expect.objectContaining({ name: 'mcp.search_tools' }),
        }),
        expect.objectContaining({
          type: 'tool.completed',
          payload: expect.objectContaining({ name: 'mcp.search_tools' }),
        }),
        expect.objectContaining({ type: 'run.completed' }),
      ]))
      const searchCompleted = [...searchEvents]
        .reverse()
        .find(event => event.type === 'run.completed')
      expect(String(searchCompleted?.payload.final_text ?? '')).toContain(toolName)

      const run = await createLocalRun({
        commandId: `cmd_e2e_mcp_${suffix}`,
        clientMessageId: `msg_e2e_mcp_${suffix}`,
        goal: encodedToolGoal(toolName, { value: 'ping' }),
        workspacePath: workspace,
        mode: RUN_MODEL,
        settings: { ...DEFAULT_SETTINGS, mcp: 'on' },
      }, config)
      const events = await streamThroughPermission(run.id, suffix, config)

      expect(events).toEqual(expect.arrayContaining([
        expect.objectContaining({
          type: 'tool.requested',
          payload: expect.objectContaining({ name: toolName }),
        }),
        expect.objectContaining({
          type: 'tool.completed',
          payload: expect.objectContaining({ name: toolName }),
        }),
        expect.objectContaining({ type: 'run.completed' }),
      ]))
      const completed = [...events].reverse().find(event => event.type === 'run.completed')
      expect(String(completed?.payload.final_text ?? '')).toContain('E2E_MCP_OK:ping')

      const structuredToolName = `${serverName}_structured`
      const structuredRun = await createLocalRun({
        commandId: `cmd_e2e_mcp_structured_${suffix}`,
        clientMessageId: `msg_e2e_mcp_structured_${suffix}`,
        goal: encodedToolGoal(structuredToolName, { value: 'structured-ping' }),
        workspacePath: workspace,
        mode: RUN_MODEL,
        settings: { ...DEFAULT_SETTINGS, mcp: 'on' },
      }, config)
      const structuredEvents = await streamThroughPermission(
        structuredRun.id,
        `structured_${suffix}`,
        config,
      )
      expect(structuredEvents).toEqual(expect.arrayContaining([
        expect.objectContaining({
          type: 'tool.completed',
          payload: expect.objectContaining({ name: structuredToolName }),
        }),
        expect.objectContaining({ type: 'run.completed' }),
      ]))
      expect(JSON.stringify(structuredEvents)).toContain('structured-ping')
      expect(JSON.stringify(structuredEvents)).toContain('length')

      const longRunningToolName = `${serverName}_long_running`
      const progressRun = await createLocalRun({
        commandId: `cmd_e2e_mcp_progress_${suffix}`,
        clientMessageId: `msg_e2e_mcp_progress_${suffix}`,
        goal: encodedToolGoal(longRunningToolName, { steps: 3, delay_seconds: 0.02 }),
        workspacePath: workspace,
        mode: RUN_MODEL,
        settings: { ...DEFAULT_SETTINGS, mcp: 'on' },
      }, config)
      const progressEvents = await streamThroughPermission(
        progressRun.id,
        `progress_${suffix}`,
        config,
      )
      const progressPayloads = progressEvents
        .filter(event => event.type === 'tool.progress')
        .map(event => event.payload)
      expect(progressPayloads).toEqual([
        expect.objectContaining({ tool: longRunningToolName, progress: 1, total: 3 }),
        expect.objectContaining({ tool: longRunningToolName, progress: 2, total: 3 }),
        expect.objectContaining({ tool: longRunningToolName, progress: 3, total: 3 }),
      ])
      expect(progressEvents.map(event => event.type)).toContain('run.completed')
      expect(JSON.stringify(progressEvents)).toContain('E2E_MCP_PROGRESS:3')

      const cancelPID = Number(lastMcpPID(pidLog))
      const cancelRun = await createLocalRun({
        commandId: `cmd_e2e_mcp_cancel_${suffix}`,
        clientMessageId: `msg_e2e_mcp_cancel_${suffix}`,
        goal: encodedToolGoal(longRunningToolName, { steps: 600, delay_seconds: 0.05 }),
        workspacePath: workspace,
        mode: RUN_MODEL,
        settings: { ...DEFAULT_SETTINGS, mcp: 'on' },
      }, config)
      const cancelEvents: RuntimeEvent[] = []
      await collectRunEvents(cancelRun.id, config, cancelEvents)
      const cancelPermission = cancelEvents.find(event => event.type === 'permission.required')
      expect(cancelPermission).toBeDefined()
      await resolveLocalPermissionCommand(
        `cmd_e2e_mcp_cancel_approve_${suffix}`,
        String(cancelPermission?.payload.request_id ?? ''),
        'approve',
        { scope: 'once' },
        config,
      )
      const cancelStream = collectRunEvents(cancelRun.id, config, cancelEvents)
      await waitForEvent(cancelEvents, 'tool.progress')
      await cancelLocalRunCommand(`cmd_e2e_mcp_cancel_run_${suffix}`, cancelRun.id, config)
      await cancelStream
      expect(cancelEvents.map(event => event.type)).toContain('run.canceled')
      expect(cancelEvents.map(event => event.type)).not.toContain('tool.completed')
      const cancelDiagnostics = await getLocalRunDiagnostics(cancelRun.id, config)
      expect(cancelDiagnostics.tool_receipts).toEqual(expect.arrayContaining([
        expect.objectContaining({
          tool_name: longRunningToolName,
          status: 'outcome_unknown',
          attempt_count: 1,
        }),
      ]))
      await expectProcessGone(cancelPID)
      await waitForMcpProcessRestart(pidLog, String(cancelPID))

      const invalidRun = await createLocalRun({
        commandId: `cmd_e2e_mcp_invalid_${suffix}`,
        clientMessageId: `msg_e2e_mcp_invalid_${suffix}`,
        goal: encodedToolGoal(toolName, { wrong_key: 'must not reach the MCP server' }),
        workspacePath: workspace,
        mode: RUN_MODEL,
        settings: { ...DEFAULT_SETTINGS, mcp: 'on' },
      }, config)
      const invalidEvents: RuntimeEvent[] = []
      await collectRunEvents(invalidRun.id, config, invalidEvents)
      expect(invalidEvents.map(event => event.type)).not.toContain('permission.required')
      expect(invalidEvents).toEqual(expect.arrayContaining([
        expect.objectContaining({
          type: 'tool.failed',
          payload: expect.objectContaining({ name: toolName }),
        }),
        expect.objectContaining({ type: 'run.completed' }),
      ]))
      const invalidDiagnostics = await getLocalRunDiagnostics(invalidRun.id, config)
      expect(invalidDiagnostics.tool_receipts).toEqual(expect.arrayContaining([
        expect.objectContaining({
          tool_name: toolName,
          status: 'failed',
          attempt_count: 0,
          error_type: 'ToolInputValidationError',
        }),
      ]))

      await deleteMcpServer(serverName, config)
      await createMcpServer({
        name: serverName,
        transport: 'stdio',
        command: 'uv',
        args: ['run', 'python', 'tests/fixtures/e2e_mcp_server.py'],
        env: { E2E_MCP_PREFIX: 'E2E_MCP_OLD', E2E_MCP_PID_LOG: pidLog },
        cwd: runtimeRoot,
      }, config)
      await waitForMcpServer(serverName, config)
      const frozenRun = await createLocalRun({
        commandId: `cmd_e2e_mcp_frozen_${suffix}`,
        clientMessageId: `msg_e2e_mcp_frozen_${suffix}`,
        goal: encodedToolGoal(toolName, { value: 'frozen' }),
        workspacePath: workspace,
        mode: RUN_MODEL,
        settings: { ...DEFAULT_SETTINGS, mcp: 'on' },
      }, config)
      const frozenEvents: RuntimeEvent[] = []
      await collectRunEvents(frozenRun.id, config, frozenEvents)
      const frozenPermission = frozenEvents.find(event => event.type === 'permission.required')
      expect(frozenPermission).toBeDefined()

      await deleteMcpServer(serverName, config)
      await createMcpServer({
        name: serverName,
        transport: 'stdio',
        command: 'uv',
        args: ['run', 'python', 'tests/fixtures/e2e_mcp_server.py'],
        env: { E2E_MCP_PREFIX: 'E2E_MCP_NEW', E2E_MCP_PID_LOG: pidLog },
        cwd: runtimeRoot,
      }, config)
      await waitForMcpServer(serverName, config)
      await resolveLocalPermissionCommand(
        `cmd_e2e_mcp_frozen_approve_${suffix}`,
        String(frozenPermission?.payload.request_id ?? ''),
        'approve',
        { scope: 'once' },
        config,
      )
      await collectRunEvents(frozenRun.id, config, frozenEvents)
      expect(frozenEvents).toEqual(expect.arrayContaining([
        expect.objectContaining({
          type: 'run.failed',
          payload: expect.objectContaining({
            code: 'tool_receipt_conflict',
            recoverable: false,
            retryable: false,
          }),
        }),
      ]))
      expect(JSON.stringify(frozenEvents)).not.toContain('E2E_MCP_NEW:frozen')
      expect(frozenEvents.map(event => event.type)).not.toContain('tool.completed')
      expect(frozenEvents.map(event => event.type)).not.toContain('run.completed')

      const refreshedRun = await createLocalRun({
        commandId: `cmd_e2e_mcp_refreshed_${suffix}`,
        clientMessageId: `msg_e2e_mcp_refreshed_${suffix}`,
        goal: encodedToolGoal(toolName, { value: 'refreshed' }),
        workspacePath: workspace,
        mode: RUN_MODEL,
        settings: { ...DEFAULT_SETTINGS, mcp: 'on' },
      }, config)
      const refreshedEvents = await streamThroughPermission(
        refreshedRun.id,
        `refreshed_${suffix}`,
        config,
      )
      const refreshedCompleted = [...refreshedEvents]
        .reverse()
        .find(event => event.type === 'run.completed')
      expect(String(refreshedCompleted?.payload.final_text ?? '')).toContain(
        'E2E_MCP_NEW:refreshed',
      )

      const failingToolName = `${serverName}_fail`
      const failingRun = await createLocalRun({
        commandId: `cmd_e2e_mcp_fail_${suffix}`,
        clientMessageId: `msg_e2e_mcp_fail_${suffix}`,
        goal: encodedToolGoal(failingToolName, { value: 'expected' }),
        workspacePath: workspace,
        mode: RUN_MODEL,
        settings: { ...DEFAULT_SETTINGS, mcp: 'on' },
      }, config)
      const failingEvents = await streamThroughPermission(failingRun.id, `fail_${suffix}`, config)
      expect(failingEvents).toEqual(expect.arrayContaining([
        expect.objectContaining({
          type: 'tool.failed',
          payload: expect.objectContaining({ name: failingToolName }),
        }),
        expect.objectContaining({ type: 'run.completed' }),
      ]))
      expect(JSON.stringify(failingEvents)).toContain('E2E_MCP_FAILURE:expected')
      const failingDiagnostics = await getLocalRunDiagnostics(failingRun.id, config)
      expect(failingDiagnostics.tool_receipts).toEqual(expect.arrayContaining([
        expect.objectContaining({
          tool_name: failingToolName,
          status: 'failed',
          attempt_count: 1,
        }),
      ]))

      const hangingToolName = `${serverName}_hang`
      const timedOutPID = lastMcpPID(pidLog)
      const hangingRun = await createLocalRun({
        commandId: `cmd_e2e_mcp_hang_${suffix}`,
        clientMessageId: `msg_e2e_mcp_hang_${suffix}`,
        goal: encodedToolGoal(hangingToolName, { seconds: 120 }),
        workspacePath: workspace,
        mode: RUN_MODEL,
        settings: { ...DEFAULT_SETTINGS, mcp: 'on' },
      }, config)
      const hangingEvents = await streamThroughPermission(
        hangingRun.id,
        `hang_${suffix}`,
        config,
      )
      expect(hangingEvents).toEqual(expect.arrayContaining([
        expect.objectContaining({
          type: 'tool.reconciliation_required',
          payload: expect.objectContaining({ tool_name: hangingToolName }),
        }),
      ]))
      expect(hangingEvents.map(event => event.type)).not.toContain('tool.completed')
      const hangingDiagnostics = await getLocalRunDiagnostics(hangingRun.id, config)
      expect(hangingDiagnostics.tool_receipts).toEqual(expect.arrayContaining([
        expect.objectContaining({
          tool_name: hangingToolName,
          status: 'outcome_unknown',
          attempt_count: 1,
          error_type: 'TimeoutError',
        }),
      ]))
      const timeoutReconciliation = hangingEvents.find(
        event => event.type === 'tool.reconciliation_required',
      )
      await reconcileLocalToolCommand(
        `cmd_e2e_mcp_hang_abort_${suffix}`,
        String(timeoutReconciliation?.payload.operation_id ?? ''),
        'abort',
        config,
      )
      await collectRunEvents(hangingRun.id, config, hangingEvents)
      expect(hangingEvents).toEqual(expect.arrayContaining([
        expect.objectContaining({
          type: 'tool.failed',
          payload: expect.objectContaining({ name: hangingToolName }),
        }),
        expect.objectContaining({ type: 'run.completed' }),
      ]))
      await waitForMcpProcessRestart(pidLog, timedOutPID)

      const crashingToolName = `${serverName}_crash`
      const crashedPID = lastMcpPID(pidLog)
      const crashingRun = await createLocalRun({
        commandId: `cmd_e2e_mcp_crash_${suffix}`,
        clientMessageId: `msg_e2e_mcp_crash_${suffix}`,
        goal: encodedToolGoal(crashingToolName, {}),
        workspacePath: workspace,
        mode: RUN_MODEL,
        settings: { ...DEFAULT_SETTINGS, mcp: 'on' },
      }, config)
      const crashingEvents = await streamThroughPermission(crashingRun.id, `crash_${suffix}`, config)
      expect(crashingEvents).toEqual(expect.arrayContaining([
        expect.objectContaining({
          type: 'tool.reconciliation_required',
          payload: expect.objectContaining({ tool_name: crashingToolName }),
        }),
      ]))
      expect(crashingEvents.map(event => event.type)).not.toContain('run.completed')
      const crashingDiagnostics = await getLocalRunDiagnostics(crashingRun.id, config)
      expect(crashingDiagnostics.tool_receipts).toEqual(expect.arrayContaining([
        expect.objectContaining({
          tool_name: crashingToolName,
          status: 'outcome_unknown',
          attempt_count: 1,
        }),
      ]))
      const reconciliation = crashingEvents.find(
        event => event.type === 'tool.reconciliation_required',
      )
      const operationID = String(reconciliation?.payload.operation_id ?? '')
      expect(operationID).toBeTruthy()
      await reconcileLocalToolCommand(
        `cmd_e2e_mcp_abort_${suffix}`,
        operationID,
        'abort',
        config,
      )
      await collectRunEvents(crashingRun.id, config, crashingEvents)
      expect(crashingEvents).toEqual(expect.arrayContaining([
        expect.objectContaining({
          type: 'tool.failed',
          payload: expect.objectContaining({ name: crashingToolName }),
        }),
        expect.objectContaining({ type: 'run.completed' }),
      ]))
      const abortedDiagnostics = await getLocalRunDiagnostics(crashingRun.id, config)
      expect(abortedDiagnostics.tool_receipts).toEqual(expect.arrayContaining([
        expect.objectContaining({
          tool_name: crashingToolName,
          status: 'failed',
          attempt_count: 1,
        }),
      ]))

      await waitForMcpProcessRestart(pidLog, crashedPID)
      const recoveredRun = await createLocalRun({
        commandId: `cmd_e2e_mcp_recovered_${suffix}`,
        clientMessageId: `msg_e2e_mcp_recovered_${suffix}`,
        goal: encodedToolGoal(toolName, { value: 'recovered' }),
        workspacePath: workspace,
        mode: RUN_MODEL,
        settings: { ...DEFAULT_SETTINGS, mcp: 'on' },
      }, config)
      const recoveredEvents = await streamThroughPermission(recoveredRun.id, `recovered_${suffix}`, config)
      expect(recoveredEvents).toEqual(expect.arrayContaining([
        expect.objectContaining({
          type: 'tool.completed',
          payload: expect.objectContaining({ name: toolName }),
        }),
        expect.objectContaining({ type: 'run.completed' }),
      ]))
      const recoveredCompleted = [...recoveredEvents]
        .reverse()
        .find(event => event.type === 'run.completed')
      expect(String(recoveredCompleted?.payload.final_text ?? '')).toContain('E2E_MCP_NEW:recovered')
    } finally {
      await deleteMcpServer(serverName, config).catch(() => undefined)
    }
  })

  it.runIf(MCP_HTTP_URL)('recovers with a new Streamable HTTP session after the server expires one', async () => {
    const serverName = 'e2e-runtime-mcp-http'
    const toolName = `${serverName}_echo`
    const suffix = Date.now().toString(36)
    await deleteMcpServer(serverName, config).catch(() => undefined)
    try {
      await createMcpServer({
        name: serverName,
        transport: 'streamable_http',
        url: MCP_HTTP_URL,
        args: [],
        env: {},
      }, config)
      await waitForMcpServer(serverName, config)
      await waitForHttpMcpSessions(MCP_HTTP_URL!, 1)

      const expiredRun = await createLocalRun({
        commandId: `cmd_e2e_mcp_http_expired_${suffix}`,
        clientMessageId: `msg_e2e_mcp_http_expired_${suffix}`,
        goal: encodedToolGoal(toolName, { value: 'expired' }),
        workspacePath: workspace,
        mode: RUN_MODEL,
        settings: { ...DEFAULT_SETTINGS, mcp: 'on' },
      }, config)
      const expiredEvents = await streamThroughPermission(
        expiredRun.id,
        `http_expired_${suffix}`,
        config,
      )
      expect(expiredEvents).toEqual(expect.arrayContaining([
        expect.objectContaining({
          type: 'tool.reconciliation_required',
          payload: expect.objectContaining({ tool_name: toolName }),
        }),
      ]))
      expect(expiredEvents.map(event => event.type)).not.toContain('tool.completed')
      const reconciliation = expiredEvents.find(
        event => event.type === 'tool.reconciliation_required',
      )
      await reconcileLocalToolCommand(
        `cmd_e2e_mcp_http_abort_${suffix}`,
        String(reconciliation?.payload.operation_id ?? ''),
        'abort',
        config,
      )
      await collectRunEvents(expiredRun.id, config, expiredEvents)
      expect(expiredEvents.map(event => event.type)).toContain('run.completed')

      await waitForHttpMcpSessions(MCP_HTTP_URL!, 2)
      const recoveredRun = await createLocalRun({
        commandId: `cmd_e2e_mcp_http_recovered_${suffix}`,
        clientMessageId: `msg_e2e_mcp_http_recovered_${suffix}`,
        goal: encodedToolGoal(toolName, { value: 'recovered' }),
        workspacePath: workspace,
        mode: RUN_MODEL,
        settings: { ...DEFAULT_SETTINGS, mcp: 'on' },
      }, config)
      const recoveredEvents = await streamThroughPermission(
        recoveredRun.id,
        `http_recovered_${suffix}`,
        config,
      )
      expect(recoveredEvents).toEqual(expect.arrayContaining([
        expect.objectContaining({
          type: 'tool.completed',
          payload: expect.objectContaining({ name: toolName }),
        }),
        expect.objectContaining({ type: 'run.completed' }),
      ]))
      expect(JSON.stringify(recoveredEvents)).toContain('E2E_MCP_HTTP_OK:recovered')
    } finally {
      await deleteMcpServer(serverName, config).catch(() => undefined)
    }
  })
})

type RuntimeEvent = { type: string; payload: Record<string, unknown> }
type RuntimeConnection = { baseURL: string; token: string }

async function waitForMcpServer(name: string, config: RuntimeConnection): Promise<void> {
  for (let attempt = 0; attempt < 50; attempt += 1) {
    const server = (await listMcpServers(config)).servers.find(item => item.name === name)
    if (server?.status === 'ready' && server.tool_count > 0) return
    if (server?.status === 'error') throw new Error(`MCP discovery failed: ${server.error_type}`)
    await new Promise(resolve => setTimeout(resolve, 100))
  }
  throw new Error(`MCP server ${name} did not become ready`)
}

function lastMcpPID(pidLog: string): string {
  const pids = readFileSync(pidLog, 'utf8').trim().split('\n').filter(Boolean)
  expect(pids.length).toBeGreaterThan(0)
  return pids.at(-1) ?? ''
}

async function waitForMcpProcessRestart(pidLog: string, previousPID: string): Promise<void> {
  for (let attempt = 0; attempt < 50; attempt += 1) {
    if (existsSync(pidLog) && lastMcpPID(pidLog) !== previousPID) return
    await new Promise(resolve => setTimeout(resolve, 100))
  }
  throw new Error(`MCP server process ${previousPID} was not restarted`)
}

async function waitForHttpMcpSessions(mcpURL: string, expected: number): Promise<void> {
  const statusURL = new URL('/status', mcpURL)
  for (let attempt = 0; attempt < 50; attempt += 1) {
    const response = await fetch(statusURL)
    const status = await response.json() as { sessions?: number }
    if ((status.sessions ?? 0) >= expected) return
    await new Promise(resolve => setTimeout(resolve, 100))
  }
  throw new Error(`MCP HTTP fixture did not establish ${expected} sessions`)
}

async function waitForFile(path: string): Promise<void> {
  for (let attempt = 0; attempt < 100; attempt += 1) {
    if (existsSync(path)) return
    await new Promise(resolve => setTimeout(resolve, 20))
  }
  throw new Error(`Timed out waiting for ${path}`)
}

async function waitForEvent(events: RuntimeEvent[], type: string): Promise<void> {
  for (let attempt = 0; attempt < 100; attempt += 1) {
    if (events.some(event => event.type === type)) return
    await new Promise(resolve => setTimeout(resolve, 20))
  }
  throw new Error(`Timed out waiting for ${type}`)
}

async function expectProcessGone(pid: number): Promise<void> {
  for (let attempt = 0; attempt < 100; attempt += 1) {
    try {
      process.kill(pid, 0)
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === 'ESRCH') return
      throw error
    }
    await new Promise(resolve => setTimeout(resolve, 20))
  }
  throw new Error(`Shell child process ${pid} remained alive`)
}

function processesForMarker(marker: string): number[] {
  try {
    return execFileSync('pgrep', ['-f', marker], { encoding: 'utf8' })
      .trim()
      .split(/\s+/)
      .filter(Boolean)
      .map(Number)
      .filter(Number.isSafeInteger)
  } catch (error) {
    if ((error as NodeJS.ErrnoException & { status?: number }).status === 1) return []
    throw error
  }
}

async function waitForProcessMarker(marker: string): Promise<number[]> {
  for (let attempt = 0; attempt < 100; attempt += 1) {
    const processes = processesForMarker(marker)
    if (processes.length > 0) return processes
    await new Promise(resolve => setTimeout(resolve, 20))
  }
  throw new Error(`Timed out waiting for shell process marker ${marker}`)
}

function shellQuote(value: string): string {
  return `'${value.replaceAll("'", `'"'"'`)}'`
}

async function streamThroughPermission(
  runID: string,
  suffix: string,
  config: RuntimeConnection,
  beforeApprove?: () => void,
): Promise<RuntimeEvent[]> {
  const events: RuntimeEvent[] = []
  await collectRunEvents(runID, config, events)
  const permission = events.find(event => event.type === 'permission.required')
  if (permission) {
    beforeApprove?.()
    const permissionID = String(permission.payload.request_id ?? '')
    expect(permissionID).toBeTruthy()
    await resolveLocalPermissionCommand(
      `cmd_e2e_tool_approve_${suffix}`,
      permissionID,
      'approve',
      { scope: 'once' },
      config,
    )
    await collectRunEvents(runID, config, events)
  }
  return events
}

async function collectRunEvents(
  runID: string,
  config: RuntimeConnection,
  events: RuntimeEvent[],
): Promise<void> {
  await streamLocalRun(runID, config, {
    onEvent: event => events.push({ type: event.event_type, payload: event.payload ?? {} }),
    onDelta: () => undefined,
  })
}

function encodedToolGoal(
  name: string,
  args: Record<string, unknown>,
  instruction = `Use the ${name} Tool.`,
  expectedMarker?: string,
): string {
  if (REAL_LLM_MODEL) {
    return [
      instruction,
      `Call exactly the ${name} tool once with exactly these JSON arguments: ${JSON.stringify(args)}.`,
      'Do not substitute another tool or change any argument.',
      expectedMarker
        ? `After the tool returns, include the exact success marker ${JSON.stringify(expectedMarker)} in your final answer.`
        : 'After the tool returns, briefly report its result in your final answer.',
    ].join('\n')
  }
  const payload = Buffer.from(JSON.stringify({ name, args }), 'utf8').toString('base64url')
  return `${instruction}\n[[e2e:tool:${payload}]]`
}

function toolCategory(name: string): string {
  if (!name.includes('.')) return 'filesystem'
  if (name.startsWith('office.')) return 'office'
  if (name.startsWith('memory.')) return 'memory'
  if (name.startsWith('task.')) return 'task-state'
  if (name.startsWith('web.')) return 'network'
  if (name.startsWith('open.') || name.startsWith('clipboard.')) return 'host-integration'
  if (name === 'time.now' || name === 'environment.observe') return 'runtime-context'
  throw new Error(`Unclassified E2E Tool: ${name}`)
}

function toolEffect(name: string): ToolEffect {
  if (
    name === 'execute' ||
    name === 'web.fetch' ||
    name.startsWith('open.') ||
    name.startsWith('clipboard.')
  ) {
    return 'host-interaction'
  }
  if (name === 'task.progress' || name === 'memory.write') return 'runtime-state'
  if (name === 'edit_file' || isOfficeWriteTool(name)) return 'workspace-write'
  const readOnly = new Set([
    'time.now',
    'environment.observe',
    'ls',
    'glob',
    'grep',
    'task.verify',
    'memory.search',
    'office.read',
    'office.outline',
    'office.read_range',
    'office.read_slides',
  ])
  if (readOnly.has(name)) return 'read-only'
  throw new Error(`Unclassified E2E Tool effect: ${name}`)
}

function toolRisk(name: string, effect: ToolEffect): ToolRisk {
  if (name === 'web.fetch') return 'network'
  if (effect === 'workspace-write') return 'workspace'
  if (effect === 'host-interaction') return 'host'
  if (effect === 'human-interaction') return 'human'
  return 'low'
}

function toolTraits(name: string, effect: ToolEffect): string {
  if (name === 'execute') return 'permission,side-effect,cancel,timeout,process-tree'
  if (effect === 'workspace-write') return 'permission,side-effect,workspace'
  if (name === 'web.fetch') return 'read-only,network,ssrf,timeout'
  if (name.startsWith('open.')) return 'permission,external-side-effect,allowlist'
  if (name.startsWith('clipboard.')) return 'permission,external-side-effect,os-clipboard'
  if (effect === 'runtime-state') return 'stateful'
  if (
    ['ls', 'glob', 'grep', 'task.verify'].includes(name)
    || name.startsWith('office.')
  ) return 'read-only,workspace'
  return 'read-only'
}

function isOfficeWriteTool(name: string): boolean {
  return name.startsWith('office.') && ![
    'office.read',
    'office.outline',
    'office.read_range',
    'office.read_slides',
  ].includes(name)
}

function officeWriteCase(
  name: string,
  extension: 'docx' | 'xlsx' | 'pptx',
  args: (workspace: string) => Record<string, unknown>,
  expected: string,
): ToolCase {
  return {
    name,
    args,
    expected,
    verifyBeforeApproval: workspace => expect(
      existsSync(editedOfficePath(workspace, name, extension)),
    ).toBe(false),
    verify: workspace => expect(existsSync(editedOfficePath(workspace, name, extension))).toBe(true),
  }
}

function freshOfficeFile(
  workspace: string,
  name: string,
  extension: 'docx' | 'xlsx' | 'pptx',
): string {
  const path = officePath(workspace, name, extension)
  rmSync(editedOfficePath(workspace, name, extension), { force: true })
  copyFileSync(join(workspace, `base.${extension}`), path)
  return path
}

function officePath(workspace: string, name: string, extension: string): string {
  return join(workspace, `${name.replaceAll('.', '_')}.${extension}`)
}

function editedOfficePath(workspace: string, name: string, extension: string): string {
  return join(workspace, `${name.replaceAll('.', '_')}.edited.${extension}`)
}

function createOfficeFixtures(workspace: string): void {
  const script = `
import sys
from pathlib import Path
from docx import Document
from openpyxl import Workbook
from pptx import Presentation

root = Path(sys.argv[1])
doc = Document()
doc.add_heading("E2E heading", level=1)
for text in ("Replace target", "Update target", "Delete target", "Style target"):
    doc.add_paragraph(text)
doc.save(root / "base.docx")

book = Workbook()
sheet = book.active
sheet.title = "Data"
sheet.append(["Name", "Value"])
sheet.append(["Needle", 2])
book.save(root / "base.xlsx")

deck = Presentation()
cover = deck.slides.add_slide(deck.slide_layouts[0])
cover.shapes.title.text = "E2E Deck"
second = deck.slides.add_slide(deck.slide_layouts[1])
second.shapes.title.text = "Second slide"
second.placeholders[1].text = "Initial bullet"
deck.save(root / "base.pptx")
`
  execFileSync('uv', ['run', 'python', '-c', script, workspace], {
    cwd: resolve(process.cwd(), '../runtime'),
  })
  writeFileSync(
    join(workspace, 'pixel.png'),
    Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=', 'base64'),
  )
}
