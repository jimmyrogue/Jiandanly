import { useState } from 'react'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { I18nProvider } from '@/shared/i18n/I18nProvider'
import { Composer } from './Composer'
import { pluginCommandToken, pluginToken, skillToken } from '../skillDraft'
import type { InstalledSkill, McpServerInfo, PluginDetail } from '@/runtime/client'

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

const sampleSkills: InstalledSkill[] = [
  { name: 'hunt', description: 'Diagnose before you fix', path: '/s/hunt/SKILL.md' },
  { name: 'write', description: 'Strip AI writing patterns', path: '/s/write/SKILL.md' },
]

const sampleMcpServers: McpServerInfo[] = [{
  name: 'github',
  transport: 'stdio',
  source: 'shejane',
  source_path: '/u/.shejane/mcp-servers.json',
  command: 'npx',
  args: [],
  env_keys: [],
  cwd: null,
  url: null,
  status: 'idle',
  tool_count: 0,
}]

const archiveDigest = `sha256:${'a'.repeat(64)}`
const archivePlugin: PluginDetail = {
  id: 'dev.shejane.fixture.archive',
  name: 'Archive fixture',
  version: '0.1.0',
  digest: archiveDigest,
  description: 'Create a deterministic archive',
  license: 'AGPL-3.0-only',
  publisher: { id: 'dev.shejane', name: 'SheJane' },
  execution_kind: 'wasi',
  signature_status: 'unsigned',
  compatibility: 'compatible',
  enabled: true,
  retired: false,
  actions: [],
  skills: [],
  mcp_servers: [],
  versions: [],
  commands: [
    {
      id: 'archive',
      title: 'Archive files',
      description: 'Create an archive artifact',
      required_actions: ['archive.create'],
    },
  ],
}

function prepareTypeaheadLayout() {
  Object.defineProperty(Range.prototype, 'getBoundingClientRect', {
    configurable: true,
    value: () => new DOMRect(),
  })
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as typeof ResizeObserver
}

function Harness({
  initialDraft = '',
  onSend = vi.fn(),
  onAppendInstruction,
  listSkills = vi.fn().mockResolvedValue(sampleSkills),
  listMcpServers = vi.fn().mockResolvedValue(sampleMcpServers),
  listPlugins = vi.fn().mockResolvedValue([archivePlugin]),
  onDraft = vi.fn(),
  projectName,
  onSelectProject,
  onRemoveProject,
  attachments = [],
  onSelectAttachments,
  onRemoveAttachment,
  hasActiveRun = false,
  permissionMode = 'ask',
  onPermissionModeChange = vi.fn(),
}: {
  initialDraft?: string
  onSend?: () => void
  onAppendInstruction?: () => void
  listSkills?: () => Promise<InstalledSkill[]>
  listMcpServers?: () => Promise<McpServerInfo[]>
  listPlugins?: () => Promise<PluginDetail[]>
  onDraft?: (value: string) => void
  projectName?: string
  onSelectProject?: () => void
  onRemoveProject?: () => void
  attachments?: Array<{ path: string; name: string }>
  onSelectAttachments?: () => void
  onRemoveAttachment?: (path: string) => void
  hasActiveRun?: boolean
  permissionMode?: 'ask' | 'auto' | 'full_access'
  onPermissionModeChange?: (mode: 'ask' | 'auto' | 'full_access') => void
}) {
  const [draft, setDraft] = useState(initialDraft)
  return (
    <I18nProvider>
      <Composer
        draft={draft}
        onDraftChange={(value) => {
          onDraft(value)
          setDraft(value)
        }}
        isSending={false}
        hasActiveRun={hasActiveRun}
        onSend={onSend}
        onAppendInstruction={onAppendInstruction}
        listSkills={listSkills}
        listMcpServers={listMcpServers}
        listPlugins={listPlugins}
        mode="local:test:model"
        onModeChange={vi.fn()}
        permissionMode={permissionMode}
        onPermissionModeChange={onPermissionModeChange}
        projectName={projectName}
        onSelectProject={onSelectProject}
        onRemoveProject={onRemoveProject}
        attachments={attachments}
        onSelectAttachments={onSelectAttachments}
        onRemoveAttachment={onRemoveAttachment}
      />
    </I18nProvider>
  )
}

describe('Composer (Lexical skill editor)', () => {
  it('renders the editor with a placeholder when empty', () => {
    render(<Harness />)
    expect(screen.getByRole('textbox')).toBeInTheDocument()
    expect(screen.getByText('交给石间——描述任务，可添加附件')).toBeInTheDocument()
  })

  it('renders an inline skill pill for a draft that contains a skill token', async () => {
    render(<Harness initialDraft={`fix ${skillToken('hunt')} now`} />)
    const pill = await screen.findByText('hunt')
    expect(pill.closest('.skill-chip--inline')).not.toBeNull()
    // No border / icon / remove button — just styled inline text.
    expect(screen.queryByRole('button', { name: /remove/i })).not.toBeInTheDocument()
  })

  it('renders plugin and plugin-command chips from structured draft tokens', async () => {
    const plugin = pluginToken({
      pluginId: archivePlugin.id,
      name: archivePlugin.name,
      expectedDigest: archivePlugin.digest,
    })
    const command = pluginCommandToken({
      pluginId: archivePlugin.id,
      pluginName: archivePlugin.name,
      commandId: 'archive',
      title: 'Archive files',
      expectedDigest: archivePlugin.digest,
    })
    render(<Harness initialDraft={`${plugin} ${command} 处理附件`} />)

    expect((await screen.findByText('Archive fixture')).closest('.plugin-chip--inline')).not.toBeNull()
    expect(screen.getByText('Archive files').closest('.plugin-command-chip--inline')).not.toBeNull()
  })

  it('marks a restored plugin token stale when the active digest changed', async () => {
    const token = pluginToken({
      pluginId: archivePlugin.id,
      name: archivePlugin.name,
      expectedDigest: archivePlugin.digest,
    })
    render(
      <Harness
        initialDraft={`${token} 处理附件`}
        listPlugins={vi.fn().mockResolvedValue([
          { ...archivePlugin, digest: `sha256:${'b'.repeat(64)}` },
        ])}
      />,
    )

    expect(await screen.findByText(/插件版本已变化：Archive fixture/)).toBeInTheDocument()
  })

  it('offers enabled plugins from the @ menu and inserts a structured token', async () => {
    prepareTypeaheadLayout()
    const onDraft = vi.fn()
    render(<Harness onDraft={onDraft} />)
    const editor = screen.getByRole('textbox')
    editor.textContent = '@'
    fireEvent.input(editor, { inputType: 'insertText', data: '@' })

    fireEvent.click(await screen.findByRole('option', { name: /Archive fixture/ }))
    expect(await screen.findByText('Archive fixture')).toBeInTheDocument()
    expect(onDraft).toHaveBeenLastCalledWith(
      expect.stringContaining(pluginToken({
        pluginId: archivePlugin.id,
        name: archivePlugin.name,
        expectedDigest: archivePlugin.digest,
      })),
    )
  })

  it('offers plugin commands from the slash menu and inserts a structured command token', async () => {
    prepareTypeaheadLayout()
    const onDraft = vi.fn()
    render(<Harness onDraft={onDraft} />)
    const editor = screen.getByRole('textbox')
    fireEvent.input(editor, { inputType: 'insertText', data: '/arch' })

    fireEvent.click(await screen.findByRole('option', { name: /Archive files/ }))
    expect(await screen.findByText('Archive files')).toBeInTheDocument()
    expect(onDraft).toHaveBeenLastCalledWith(
      expect.stringContaining(pluginCommandToken({
        pluginId: archivePlugin.id,
        pluginName: archivePlugin.name,
        commandId: 'archive',
        title: 'Archive files',
        expectedDigest: archivePlugin.digest,
      })),
    )
  })

  it('orders slash menu groups as functions, plugin commands, skills, then MCP', async () => {
    prepareTypeaheadLayout()
    render(<Harness />)
    const editor = screen.getByRole('textbox')
    fireEvent.input(editor, { inputType: 'insertText', data: '/' })

    await screen.findByText('插件命令')
    const menu = screen.getByRole('listbox', { name: 'Skill' })
    expect(menu.parentElement).toHaveClass('composer')
    expect(
      Array.from(menu.querySelectorAll('.composer-menu-group'))
        .map((group) => group.textContent),
    ).toEqual(['功能', '插件命令', 'Skill', 'MCP'])
  })

  it('selects an @ plugin with the keyboard', async () => {
    prepareTypeaheadLayout()
    const onDraft = vi.fn()
    render(<Harness onDraft={onDraft} />)
    const editor = screen.getByRole('textbox')
    editor.textContent = '@'
    fireEvent.input(editor, { inputType: 'insertText', data: '@' })

    await screen.findByRole('option', { name: /Archive fixture/ })
    fireEvent.keyDown(editor, { key: 'Enter' })

    expect(await screen.findByText('Archive fixture')).toBeInTheDocument()
    expect(onDraft).toHaveBeenLastCalledWith(expect.stringContaining(archivePlugin.id))
  })

  it('hides unavailable plugins from both @ references and slash commands', async () => {
    prepareTypeaheadLayout()
    const unavailable = [
      { ...archivePlugin, enabled: false },
      { ...archivePlugin, id: `${archivePlugin.id}.retired`, retired: true },
      { ...archivePlugin, id: `${archivePlugin.id}.incompatible`, compatibility: 'incompatible' as const },
    ]
    render(<Harness listPlugins={vi.fn().mockResolvedValue(unavailable)} />)
    const editor = screen.getByRole('textbox')
    editor.textContent = '@archive'
    fireEvent.input(editor, { inputType: 'insertText', data: '@archive' })
    await waitFor(() => expect(screen.queryByRole('option', { name: /Archive fixture/ })).not.toBeInTheDocument())

    editor.textContent = '/archive'
    fireEvent.input(editor, { inputType: 'insertText', data: '/archive' })
    await waitFor(() => expect(screen.queryByRole('option', { name: /Archive files/ })).not.toBeInTheDocument())
  })

  it('disambiguates same-name plugins by publisher and stable id', async () => {
    prepareTypeaheadLayout()
    const other = {
      ...archivePlugin,
      id: 'com.other.archive',
      publisher: { id: 'com.other', name: 'Other Corp' },
    }
    const onDraft = vi.fn()
    render(
      <Harness
        listPlugins={vi.fn().mockResolvedValue([archivePlugin, other])}
        onDraft={onDraft}
      />,
    )
    const editor = screen.getByRole('textbox')
    editor.textContent = '@other'
    fireEvent.input(editor, { inputType: 'insertText', data: '@other' })

    const option = await screen.findByRole('option', { name: /Other Corp.*com\.other\.archive/ })
    fireEvent.click(option)
    await waitFor(() => expect(onDraft).toHaveBeenLastCalledWith(expect.stringContaining(other.id)))
    expect(onDraft.mock.calls.at(-1)?.[0]).not.toContain(archivePlugin.id)
  })

  it('replaces the existing plugin command instead of keeping two', async () => {
    prepareTypeaheadLayout()
    const previous = pluginCommandToken({
      pluginId: archivePlugin.id,
      pluginName: archivePlugin.name,
      commandId: 'archive',
      title: 'Archive files',
      expectedDigest: archivePlugin.digest,
    })
    const pluginWithSecondCommand = {
      ...archivePlugin,
      commands: [
        ...archivePlugin.commands,
        {
          id: 'inspect',
          title: 'Inspect archive',
          description: 'Inspect without extracting',
          required_actions: ['archive.inspect'],
        },
      ],
    }
    const onDraft = vi.fn()
    render(
      <Harness
        initialDraft={`${previous} `}
        listPlugins={vi.fn().mockResolvedValue([pluginWithSecondCommand])}
        onDraft={onDraft}
      />,
    )
    const editor = screen.getByRole('textbox')
    const trailingText = editor.querySelector('[data-lexical-text]')
    expect(trailingText).not.toBeNull()
    if (trailingText) trailingText.textContent = ' /inspect'
    fireEvent.input(editor, { inputType: 'insertText', data: '/inspect' })
    fireEvent.click(await screen.findByRole('option', { name: /Inspect archive/ }))

    expect(await screen.findByText('Inspect archive')).toBeInTheDocument()
    expect(screen.queryByText('Archive files')).not.toBeInTheDocument()
    expect(onDraft).toHaveBeenLastCalledWith(expect.not.stringContaining(previous))
  })

  it('keeps plugin commands visible but disabled while steering an active run', async () => {
    prepareTypeaheadLayout()
    const onDraft = vi.fn()
    render(<Harness hasActiveRun onAppendInstruction={vi.fn()} onDraft={onDraft} />)
    const editor = screen.getByRole('textbox')
    editor.textContent = '/archive'
    fireEvent.input(editor, { inputType: 'insertText', data: '/archive' })
    const command = await screen.findByRole('option', { name: /Archive files/ })
    expect(command).toHaveAttribute('aria-disabled', 'true')
    expect(command).toHaveTextContent('请在新任务中使用')
    const draftUpdates = onDraft.mock.calls.length
    fireEvent.click(command)
    expect(onDraft).toHaveBeenCalledTimes(draftUpdates)
  })

  it('deletes a plugin chip atomically', async () => {
    const token = pluginToken({
      pluginId: archivePlugin.id,
      name: archivePlugin.name,
      expectedDigest: archivePlugin.digest,
    })
    const onDraft = vi.fn()
    render(<Harness initialDraft={token} onDraft={onDraft} />)
    const editor = screen.getByRole('textbox')
    await screen.findByText('Archive fixture')
    fireEvent.keyDown(editor, { key: 'Backspace' })

    await waitFor(() => expect(screen.queryByText('Archive fixture')).not.toBeInTheDocument())
    expect(onDraft).toHaveBeenLastCalledWith('')
  })

  it('sends with plain Enter', () => {
    const onSend = vi.fn()
    render(<Harness onSend={onSend} />)
    fireEvent.keyDown(screen.getByRole('textbox'), { key: 'Enter' })
    expect(onSend).toHaveBeenCalledTimes(1)
  })

  it('still sends with Cmd/Ctrl+Enter for muscle-memory compatibility', () => {
    const onSend = vi.fn()
    render(<Harness onSend={onSend} />)
    fireEvent.keyDown(screen.getByRole('textbox'), { key: 'Enter', metaKey: true })
    expect(onSend).toHaveBeenCalledTimes(1)
  })

  it('inserts a newline on Shift+Enter instead of sending', () => {
    const onSend = vi.fn()
    render(<Harness onSend={onSend} />)
    fireEvent.keyDown(screen.getByRole('textbox'), { key: 'Enter', shiftKey: true })
    expect(onSend).not.toHaveBeenCalled()
  })

  it('shows "添加项目" button when no project is bound, fires onSelectProject on click', () => {
    const onSelectProject = vi.fn()
    render(<Harness onSelectProject={onSelectProject} />)
    const button = screen.getByRole('button', { name: '添加项目' })
    fireEvent.click(button)
    expect(onSelectProject).toHaveBeenCalledTimes(1)
  })

  it('shows a project chip when a project is bound', () => {
    const onSelectProject = vi.fn()
    render(<Harness projectName="客户A" onSelectProject={onSelectProject} />)
    // Project name is visible on the chip…
    expect(screen.getByText('客户A').closest('.composer-project-chip')).not.toHaveClass('composer-tool')
    // …and the "添加项目" affordance is gone.
    expect(screen.queryByRole('button', { name: '添加项目' })).not.toBeInTheDocument()
  })

  it('lets the user remove a bound project', () => {
    const onRemoveProject = vi.fn()
    render(<Harness projectName="客户A" onRemoveProject={onRemoveProject} />)

    fireEvent.click(screen.getByRole('button', { name: '移除路径：客户A' }))
    expect(onRemoveProject).toHaveBeenCalledTimes(1)
  })

  it('lets the user replace a bound project', () => {
    const onSelectProject = vi.fn()
    render(<Harness projectName="客户A" onSelectProject={onSelectProject} />)

    fireEvent.click(screen.getByRole('button', { name: '更换路径：客户A' }))
    expect(onSelectProject).toHaveBeenCalledTimes(1)
  })

  it('keeps project removal disabled while a run is active', () => {
    render(<Harness projectName="客户A" onRemoveProject={vi.fn()} hasActiveRun />)
    expect(screen.getByRole('button', { name: '移除路径：客户A' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '更换路径：客户A' })).toBeDisabled()
  })

  it('selects and removes local attachments', () => {
    const onSelectAttachments = vi.fn()
    const onRemoveAttachment = vi.fn()
    render(
      <Harness
        attachments={[{ path: '/tmp/brief.pdf', name: 'brief.pdf' }]}
        onSelectAttachments={onSelectAttachments}
        onRemoveAttachment={onRemoveAttachment}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: '添加附件' }))
    fireEvent.click(screen.getByRole('button', { name: '移除附件：brief.pdf' }))

    expect(onSelectAttachments).toHaveBeenCalledTimes(1)
    expect(onRemoveAttachment).toHaveBeenCalledWith('/tmp/brief.pdf')
  })

  it('keeps the placeholder positioned inside the editor when attachments are present', () => {
    render(<Harness attachments={[{ path: '/tmp/brief.pdf', name: 'brief.pdf' }]} />)

    expect(screen.getByText('交给石间——描述任务，可添加附件').parentElement).toHaveClass(
      'composer-editor-shell',
    )
  })

  // Regression: the stop button used to disappear the moment the
  // send promise resolved, which happens early when an SSE stream
  // blocks at a HITL permission card. Users were stranded with no
  // way to cancel a run paused for approval. The fix routes the
  // stop visibility off `isSending || hasActiveRun`.
  it('shows the stop button when a run is still active even if isSending is false', () => {
    const onStop = vi.fn()
    render(
      <I18nProvider>
        <Composer
          draft=""
          onDraftChange={vi.fn()}
          isSending={false}
          hasActiveRun
          onSend={vi.fn()}
          onStop={onStop}
          listSkills={vi.fn().mockResolvedValue([])}
          mode="local:test:model"
          onModeChange={vi.fn()}
        />
      </I18nProvider>,
    )
    const stopButton = screen.getByRole('button', { name: '停止生成' })
    fireEvent.click(stopButton)
    expect(onStop).toHaveBeenCalledTimes(1)
  })

  it('keeps stop available and sends draft as an appended instruction during an active run', () => {
    const onStop = vi.fn()
    const onAppendInstruction = vi.fn()
    render(
      <I18nProvider>
        <Composer
          draft="先补失败测试"
          onDraftChange={vi.fn()}
          isSending
          hasActiveRun
          onSend={vi.fn()}
          onAppendInstruction={onAppendInstruction}
          onStop={onStop}
          listSkills={vi.fn().mockResolvedValue([])}
          mode="local:test:model"
          onModeChange={vi.fn()}
        />
      </I18nProvider>,
    )

    fireEvent.click(screen.getByRole('button', { name: '停止生成' }))
    expect(onStop).toHaveBeenCalledTimes(1)

    fireEvent.click(screen.getByRole('button', { name: '追加指示' }))
    expect(onAppendInstruction).toHaveBeenCalledTimes(1)
  })

  it('shows the appended-instruction placeholder while a local run is active', () => {
    render(
      <I18nProvider>
        <Composer
          draft=""
          onDraftChange={vi.fn()}
          isSending={false}
          hasActiveRun
          onSend={vi.fn()}
          onAppendInstruction={vi.fn()}
          onStop={vi.fn()}
          listSkills={vi.fn().mockResolvedValue([])}
          mode="local:test:model"
          onModeChange={vi.fn()}
        />
      </I18nProvider>,
    )

    expect(screen.getByText('追加指示到当前任务')).toBeInTheDocument()
    expect(screen.queryByText('当前任务运行中；新增插件需要新建一次任务。')).not.toBeInTheDocument()
  })

  it('shows the send button (not stop) when neither isSending nor hasActiveRun is set', () => {
    render(<Harness />)
    // Stop button uses aria-label "停止生成" (i18n key composer.stop).
    expect(screen.queryByRole('button', { name: '停止生成' })).not.toBeInTheDocument()
  })

  it('lets the user choose automatic approval before starting a run', async () => {
    const onPermissionModeChange = vi.fn()
    render(<Harness onPermissionModeChange={onPermissionModeChange} />)

    const trigger = screen.getByRole('button', { name: '权限模式：请求批准' })
    trigger.focus()
    fireEvent.keyDown(trigger, { key: 'Enter', code: 'Enter' })
    fireEvent.click(await screen.findByRole('menuitem', { name: /自动审批/ }))

    expect(onPermissionModeChange).toHaveBeenCalledWith('auto')
  })

  it('defaults to automatic approval when no permission mode is supplied', () => {
    render(
      <I18nProvider>
        <Composer
          draft=""
          onDraftChange={vi.fn()}
          isSending={false}
          onSend={vi.fn()}
          listSkills={vi.fn().mockResolvedValue([])}
          mode="local:test:model"
          onModeChange={vi.fn()}
        />
      </I18nProvider>,
    )

    expect(screen.getByRole('button', { name: '权限模式：自动审批' })).toBeInTheDocument()
  })

  it('requires confirmation before enabling full access', async () => {
    const onPermissionModeChange = vi.fn()
    vi.spyOn(window, 'confirm').mockReturnValue(false)
    render(<Harness onPermissionModeChange={onPermissionModeChange} />)

    const trigger = screen.getByRole('button', { name: '权限模式：请求批准' })
    trigger.focus()
    fireEvent.keyDown(trigger, { key: 'Enter', code: 'Enter' })
    fireEvent.click(await screen.findByRole('menuitem', { name: /完全访问/ }))

    expect(onPermissionModeChange).not.toHaveBeenCalled()
  })


})
