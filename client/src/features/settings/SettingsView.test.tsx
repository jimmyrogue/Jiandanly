import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { I18nProvider } from '@/shared/i18n/I18nProvider'
import type { AgentSettings } from '@/runtime/client'
import { SettingsView } from './SettingsView'

const settings: Required<AgentSettings> = {
  memory: 'on',
  skills: 'on',
  mcp: 'on',
  mcpDisabled: [],
  advanced: {},
}

describe('SettingsView', () => {
  afterEach(() => {
    cleanup()
    delete window.shejaneClient
  })

  it('keeps settings focused on models, agent, general, and local data', () => {
    render(
      <I18nProvider>
        <SettingsView
          isDesktop
          agentSettings={settings}
          onAgentSettingsChange={vi.fn()}
          onImportLocalData={vi.fn()}
        />
      </I18nProvider>,
    )

    expect(screen.getAllByText('模型供应商')).not.toHaveLength(0)
    expect(screen.getByRole('switch', { name: '记忆' })).toBeInTheDocument()
    expect(screen.getByRole('switch', { name: '子代理' })).toBeInTheDocument()
    expect(screen.getByRole('switch', { name: '浏览器无头' })).toBeInTheDocument()
    expect(screen.getByRole('combobox', { name: '输入防护' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Runtime' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '运行' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '推理质量' })).not.toBeInTheDocument()
    expect(screen.queryByRole('switch', { name: 'Skill' })).not.toBeInTheDocument()
    expect(screen.queryByRole('switch', { name: 'MCP 服务' })).not.toBeInTheDocument()
    expect(screen.queryByText('账户')).not.toBeInTheDocument()
    expect(screen.queryByText('退出登录')).not.toBeInTheDocument()
  })

  it('keeps local data controls available', () => {
    render(
      <I18nProvider>
        <SettingsView
          isDesktop
          agentSettings={settings}
          onAgentSettingsChange={vi.fn()}
          onImportLocalData={vi.fn()}
          onExportLocalData={vi.fn()}
        />
      </I18nProvider>,
    )

    expect(screen.getAllByText('数据与安全')).not.toHaveLength(0)
  })

  it('lets desktop users see their version and check for Client updates', async () => {
    const check = vi.fn().mockResolvedValue({ currentVersion: '0.1.11', status: 'current' })
    const install = vi.fn().mockResolvedValue(true)
    const openExternal = vi.fn().mockResolvedValue('https://github.com/jimmyrogue/SheJane/releases')
    let publishState: ((state: ClientUpdateState) => void) | undefined
    Object.defineProperty(window, 'shejaneClient', {
      configurable: true,
      value: {
        openExternal,
        updates: {
          getState: vi.fn().mockResolvedValue({ currentVersion: '0.1.11', status: 'idle' }),
          check,
          install,
          onStateChange: vi.fn((handler) => {
            publishState = handler
            return () => undefined
          }),
        },
      },
    })
    render(
      <I18nProvider>
        <SettingsView
          isDesktop
          agentSettings={settings}
          onAgentSettingsChange={vi.fn()}
          onImportLocalData={vi.fn()}
        />
      </I18nProvider>,
    )

    expect(await screen.findByText('当前版本 v0.1.11')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '检查更新' }))
    expect(check).toHaveBeenCalledOnce()

    act(() => publishState?.({
      currentVersion: '0.1.11',
      status: 'ready',
      availableVersion: '0.1.12',
      progress: 100,
    }))
    fireEvent.click(screen.getByRole('button', { name: '重启并更新' }))
    expect(install).toHaveBeenCalledOnce()

    act(() => publishState?.({ currentVersion: '0.1.11', status: 'error' }))
    fireEvent.click(screen.getByRole('button', { name: '前往下载' }))
    expect(openExternal).toHaveBeenCalledWith('https://github.com/jimmyrogue/SheJane/releases')
  })
})
