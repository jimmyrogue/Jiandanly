import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { I18nProvider } from '@/shared/i18n/I18nProvider'
import { PluginsView } from './PluginsView'
import {
  RuntimeHTTPError,
  type PluginReadinessSnapshot,
  type PluginSummary,
} from '@/runtime/client'

afterEach(cleanup)

const plugin: PluginSummary = {
  id: 'dev.shejane.fixture.archive',
  name: 'Archive fixture',
  description: 'Create deterministic archives.',
  version: '0.1.0',
  digest: `sha256:${'a'.repeat(64)}`,
  publisher: { id: 'dev.shejane', name: 'SheJane' },
  execution_kind: 'wasi',
  signature_status: 'unsigned',
  compatibility: 'compatible',
  enabled: true,
  retired: false,
}

const computerUsePlugin: PluginSummary = {
  ...plugin,
  id: 'org.shejane.computer-use',
  name: 'Computer Use',
  description: 'Control this Mac with screenshots, mouse, and keyboard.',
  execution_kind: 'builtin',
  enabled: false,
}

describe('PluginsView', () => {
  it('does not expose the removed plugin source management UI', async () => {
    const listSources = vi.fn().mockResolvedValue([])
    const legacySourceProps = { listSources }
    render(
      <I18nProvider>
        <PluginsView
          listPlugins={vi.fn().mockResolvedValue([])}
          {...legacySourceProps}
        />
      </I18nProvider>,
    )

    await screen.findByText('还没有安装插件。')
    expect(listSources).not.toHaveBeenCalled()
    expect(screen.queryByText('插件来源')).not.toBeInTheDocument()
  })

  it('loads Runtime-owned plugins and refreshes the list', async () => {
    const listPlugins = vi.fn().mockResolvedValue([plugin])
    render(
      <I18nProvider>
        <PluginsView listPlugins={listPlugins} />
      </I18nProvider>,
    )

    expect(await screen.findByText('Archive fixture')).toBeInTheDocument()
    expect(screen.getByText('Create deterministic archives.')).toBeInTheDocument()
    expect(screen.queryByText('dev.shejane.fixture.archive')).not.toBeInTheDocument()
    expect(screen.queryByText('unsigned')).not.toBeInTheDocument()
    expect(screen.queryByText('WASI')).not.toBeInTheDocument()
    expect(screen.queryByText('SheJane')).not.toBeInTheDocument()
    expect(screen.queryByText('已启用')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '刷新插件' }))
    await waitFor(() => expect(listPlugins).toHaveBeenCalledTimes(2))
  })

  it('keeps the current catalog visible while refreshing', async () => {
    let finishRefresh: (plugins: PluginSummary[]) => void = () => undefined
    const listPlugins = vi
      .fn()
      .mockResolvedValueOnce([plugin])
      .mockImplementationOnce(() => new Promise<PluginSummary[]>((resolve) => {
        finishRefresh = resolve
      }))
    render(
      <I18nProvider>
        <PluginsView listPlugins={listPlugins} />
      </I18nProvider>,
    )

    expect(await screen.findByText('Archive fixture')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '刷新插件' }))
    await waitFor(() => expect(listPlugins).toHaveBeenCalledTimes(2))
    expect(screen.getByText('Archive fixture')).toBeInTheDocument()

    finishRefresh([plugin])
    await waitFor(() => expect(screen.getByText('Archive fixture')).toBeInTheDocument())
  })

  it('filters plugins without exposing hidden technical metadata', async () => {
    render(
      <I18nProvider>
        <PluginsView
          listPlugins={vi.fn().mockResolvedValue([
            plugin,
            { ...plugin, id: 'org.shejane.computer-use', name: 'Computer Use' },
          ])}
        />
      </I18nProvider>,
    )

    await screen.findByText('Archive fixture')
    fireEvent.change(screen.getByLabelText('搜索插件'), { target: { value: 'computer' } })
    expect(screen.queryByText('Archive fixture')).not.toBeInTheDocument()
    expect(screen.getByText('Computer Use')).toBeInTheDocument()
  })

  it('shows the description without opening details and runs toggle and remove commands', async () => {
    const listPlugins = vi.fn().mockResolvedValue([plugin])
    const setEnabled = vi.fn().mockResolvedValue(undefined)
    const removePlugin = vi.fn().mockResolvedValue(undefined)
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    render(
      <I18nProvider>
        <PluginsView
          listPlugins={listPlugins}
          setEnabled={setEnabled}
          removePlugin={removePlugin}
        />
      </I18nProvider>,
    )

    const name = await screen.findByText('Archive fixture')
    expect(screen.getByText('Create deterministic archives.')).toBeInTheDocument()
    fireEvent.click(name)
    expect(screen.queryByRole('button', { name: '查看 Archive fixture 的详情' })).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('switch', { name: '切换插件：Archive fixture' }))
    await waitFor(() => expect(setEnabled).toHaveBeenCalledWith(plugin, false))

    const remove = screen.getByRole('button', { name: '移除插件：Archive fixture' })
    expect(remove).not.toHaveTextContent('移除')
    fireEvent.click(remove)
    await waitFor(() => expect(removePlugin).toHaveBeenCalledWith(plugin))
  })

  it('keeps the fixed Computer Use capability and starts its one-step setup before enabling', async () => {
    const readiness: PluginReadinessSnapshot = {
      state: 'action_required',
      revision: 2,
      step: 'screen_recording',
      action_id: 'request_screen_recording',
      can_recheck: false,
    }
    const awaitingUser: PluginReadinessSnapshot = {
      state: 'awaiting_user',
      revision: 3,
      step: 'screen_recording',
      action_id: 'open_screen_recording_settings',
      can_recheck: true,
    }
    const getReadiness = vi.fn().mockResolvedValue(readiness)
    const advanceSetup = vi.fn().mockResolvedValue({
      type: 'plugin.setup.advance',
      command_id: 'cmd-setup',
      plugin_id: computerUsePlugin.id,
      readiness: awaitingUser,
    })
    const removePlugin = vi.fn().mockResolvedValue(undefined)
    const setEnabled = vi.fn().mockResolvedValue(undefined)
    render(
      <I18nProvider>
        <PluginsView
          listPlugins={vi.fn().mockResolvedValue([computerUsePlugin])}
          getReadiness={getReadiness}
          advanceSetup={advanceSetup}
          setEnabled={setEnabled}
          removePlugin={removePlugin}
        />
      </I18nProvider>,
    )

    fireEvent.click(await screen.findByRole('switch', { name: '切换插件：Computer Use' }))
    expect(await screen.findByRole('dialog', { name: '设置 Computer Use' })).toBeInTheDocument()
    expect(getReadiness).toHaveBeenCalledWith(computerUsePlugin)
    expect(screen.queryByRole('button', { name: '移除插件：Computer Use' })).not.toBeInTheDocument()
    expect(setEnabled).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: '允许屏幕录制' }))
    await waitFor(() => expect(advanceSetup).toHaveBeenCalledWith(
      computerUsePlugin,
      readiness,
      'request_screen_recording',
    ))
    expect(await screen.findByRole('button', { name: '打开系统设置' })).toBeInTheDocument()
    expect(setEnabled).not.toHaveBeenCalled()
  })

  it('does not offer removal for the fixed OCR capability', async () => {
    const ocrPlugin: PluginSummary = {
      ...plugin,
      id: 'org.shejane.ocr',
      name: 'OCR',
      execution_kind: 'managed_worker',
    }
    render(
      <I18nProvider>
        <PluginsView
          listPlugins={vi.fn().mockResolvedValue([ocrPlugin])}
          removePlugin={vi.fn().mockResolvedValue(undefined)}
        />
      </I18nProvider>,
    )

    await screen.findByText('OCR')
    expect(screen.queryByRole('button', { name: '移除插件：OCR' })).not.toBeInTheDocument()
  })

  it('rechecks permission state when the user returns from System Settings', async () => {
    const awaitingUser: PluginReadinessSnapshot = {
      state: 'awaiting_user',
      revision: 6,
      step: 'accessibility',
      action_id: 'open_accessibility_settings',
      can_recheck: true,
    }
    const setEnabled = vi.fn().mockResolvedValue(undefined)
    const advanceSetup = vi.fn().mockResolvedValue({
      type: 'plugin.setup.advance',
      command_id: 'cmd-recheck',
      plugin_id: computerUsePlugin.id,
      readiness: {
        state: 'ready',
        revision: 7,
        can_recheck: false,
      },
    })
    render(
      <I18nProvider>
        <PluginsView
          listPlugins={vi.fn().mockResolvedValue([computerUsePlugin])}
          getReadiness={vi.fn().mockResolvedValue(awaitingUser)}
          advanceSetup={advanceSetup}
          setEnabled={setEnabled}
        />
      </I18nProvider>,
    )

    fireEvent.click(await screen.findByRole('switch', { name: '切换插件：Computer Use' }))
    await screen.findByRole('dialog', { name: '设置 Computer Use' })
    fireEvent.focus(window)

    await waitFor(() => expect(advanceSetup).toHaveBeenCalledWith(
      computerUsePlugin,
      awaitingUser,
      'recheck',
    ))
    await waitFor(() => expect(setEnabled).toHaveBeenCalledWith(computerUsePlugin, true))
    expect(screen.queryByRole('dialog', { name: '设置 Computer Use' })).not.toBeInTheDocument()
  })

  it('hides retired plugins from the installed catalog', async () => {
    render(
      <I18nProvider>
        <PluginsView
          listPlugins={vi.fn().mockResolvedValue([{ ...plugin, retired: true, enabled: false }])}
        />
      </I18nProvider>,
    )

    expect(await screen.findByText('还没有安装插件。')).toBeInTheDocument()
    expect(screen.queryByText('Archive fixture')).not.toBeInTheDocument()
  })

  it('retries an unsigned local install only after explicit confirmation', async () => {
    const installPlugin = vi
      .fn()
      .mockRejectedValueOnce(
        new RuntimeHTTPError(
          'installing this unsigned plugin requires explicit confirmation',
          409,
          'unsigned_plugin_confirmation_required',
        ),
      )
      .mockResolvedValue(undefined)
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    render(
      <I18nProvider>
        <PluginsView
          listPlugins={vi.fn().mockResolvedValue([])}
          selectPackage={vi.fn().mockResolvedValue('/tmp/archive.shejane-plugin')}
          installPlugin={installPlugin}
        />
      </I18nProvider>,
    )

    fireEvent.click(screen.getByRole('button', { name: '导入插件' }))
    await waitFor(() => expect(installPlugin).toHaveBeenNthCalledWith(1, '/tmp/archive.shejane-plugin', false))
    await waitFor(() => expect(installPlugin).toHaveBeenNthCalledWith(2, '/tmp/archive.shejane-plugin', true))
  })

  it('projects a pending command and surfaces its rejection', async () => {
    let rejectCommand: (cause: Error) => void = () => undefined
    const setEnabled = vi.fn().mockImplementation(
      () => new Promise((_resolve, reject) => {
        rejectCommand = reject
      }),
    )
    render(
      <I18nProvider>
        <PluginsView
          listPlugins={vi.fn().mockResolvedValue([plugin])}
          setEnabled={setEnabled}
        />
      </I18nProvider>,
    )

    const toggle = await screen.findByRole('switch', { name: '切换插件：Archive fixture' })
    fireEvent.click(toggle)
    await waitFor(() => expect(toggle).toBeDisabled())
    rejectCommand(new Error('Runtime rejected plugin command'))

    expect(await screen.findByRole('alert')).toHaveTextContent('Runtime rejected plugin command')
    expect(toggle).not.toBeDisabled()
  })
})
