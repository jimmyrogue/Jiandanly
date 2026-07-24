import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { I18nProvider } from '@/shared/i18n/I18nProvider'
import { MCPView } from './MCPView'
import type { McpServerCatalog, McpServerInfo } from '@/runtime/client'

afterEach(cleanup)

const githubServer: McpServerInfo = {
  name: 'github',
  transport: 'stdio',
  source: 'shejane',
  source_path: '/u/.shejane/mcp-servers.json',
  command: 'npx',
  args: ['-y', '@modelcontextprotocol/server-github'],
  env_keys: ['GITHUB_TOKEN'],
  cwd: null,
  url: null,
  status: 'idle',
  tool_count: 0,
}

const playwrightServer: McpServerInfo = {
  name: 'playwright',
  transport: 'stdio',
  source: 'shejane-legacy',
  source_path: '/u/.shejane/data/mcp-servers.json',
  command: 'npx',
  args: ['@playwright/mcp'],
  env_keys: [],
  cwd: null,
  url: null,
  status: 'idle',
  tool_count: 0,
}

function makeCatalog(servers: McpServerInfo[]): McpServerCatalog {
  return { servers, sources_scanned: ['env', 'shejane', 'shejane-legacy'] }
}

function renderView(overrides: Partial<Parameters<typeof MCPView>[0]> = {}) {
  const props = {
    listCatalog: vi.fn().mockResolvedValue(makeCatalog([])),
    disabledServers: [] as string[],
    onDisabledChange: vi.fn(),
    ...overrides,
  }
  render(
    <I18nProvider>
      <MCPView {...props} />
    </I18nProvider>,
  )
  return props
}

describe('MCPView', () => {
  it('renders one row per discovered server grouped by source', async () => {
    renderView({
      listCatalog: vi.fn().mockResolvedValue(makeCatalog([githubServer, playwrightServer])),
    })

    // Wait for the async catalog fetch to complete.
    await screen.findByText('github')
    await screen.findByText('playwright')

    expect(screen.getByText('个人')).toBeInTheDocument()
    expect(screen.getByText('历史配置')).toBeInTheDocument()
  })

  it('shows env-keys metadata without leaking env values', async () => {
    renderView({
      listCatalog: vi.fn().mockResolvedValue(makeCatalog([githubServer])),
    })
    await screen.findByText('github')
    // The env *keys* surface so the user knows what the server needs.
    expect(screen.getByText('env: GITHUB_TOKEN')).toBeInTheDocument()
  })

  it('shows the Runtime-owned server status', async () => {
    renderView({
      listCatalog: vi.fn().mockResolvedValue(makeCatalog([{
        ...githubServer,
        status: 'error',
        tool_count: 0,
        error_type: 'TimeoutError',
      }])),
    })
    expect(await screen.findByText('连接失败')).toHaveAttribute('title', 'TimeoutError')
  })

  it('shows the global empty-state message when zero servers are discovered', async () => {
    renderView({
      listCatalog: vi.fn().mockResolvedValue(makeCatalog([])),
    })
    await waitFor(() => {
      expect(screen.getByText(/新增/)).toBeInTheDocument()
    })
  })

  it('per-server switch reflects disabledServers state', async () => {
    renderView({
      listCatalog: vi.fn().mockResolvedValue(makeCatalog([githubServer, playwrightServer])),
      disabledServers: ['github'],
    })
    await screen.findByText('github')
    const githubSwitch = screen.getByRole('switch', { name: 'github' })
    const playwrightSwitch = screen.getByRole('switch', { name: 'playwright' })
    // github is in disabled list → switch reads OFF; playwright stays ON.
    expect(githubSwitch).toHaveAttribute('aria-checked', 'false')
    expect(playwrightSwitch).toHaveAttribute('aria-checked', 'true')
  })

  it('toggle adds the name to disabledServers when flipping a ON server off', async () => {
    const onDisabledChange = vi.fn()
    renderView({
      listCatalog: vi.fn().mockResolvedValue(makeCatalog([githubServer])),
      disabledServers: [],
      onDisabledChange,
    })
    await screen.findByText('github')
    fireEvent.click(screen.getByRole('switch', { name: 'github' }))
    expect(onDisabledChange).toHaveBeenCalledWith(['github'])
  })

  it('toggle removes the name from disabledServers when flipping an OFF server back on', async () => {
    const onDisabledChange = vi.fn()
    renderView({
      listCatalog: vi.fn().mockResolvedValue(makeCatalog([githubServer, playwrightServer])),
      disabledServers: ['github', 'playwright'],
      onDisabledChange,
    })
    await screen.findByText('github')
    fireEvent.click(screen.getByRole('switch', { name: 'github' }))
    // Only playwright remains after removing github.
    expect(onDisabledChange).toHaveBeenCalledWith(['playwright'])
  })

  it('search filters the visible server rows by name', async () => {
    renderView({
      listCatalog: vi.fn().mockResolvedValue(makeCatalog([githubServer, playwrightServer])),
    })
    await screen.findByText('github')
    fireEvent.change(screen.getByPlaceholderText('搜索 MCP 服务'), {
      target: { value: 'play' },
    })
    expect(screen.queryByText('github')).not.toBeInTheDocument()
    expect(screen.getByText('playwright')).toBeInTheDocument()
  })

  it('open-config button calls onOpenFolder with the source_path', async () => {
    const onOpenFolder = vi.fn()
    renderView({
      listCatalog: vi.fn().mockResolvedValue(makeCatalog([githubServer])),
      onOpenFolder,
    })
    await screen.findByText('github')
    fireEvent.click(screen.getByRole('button', { name: '打开配置文件' }))
    expect(onOpenFolder).toHaveBeenCalledWith(githubServer.source_path)
  })

  it('refresh re-invokes listCatalog', async () => {
    const listCatalog = vi.fn().mockResolvedValue(makeCatalog([]))
    renderView({ listCatalog })
    await waitFor(() => expect(listCatalog).toHaveBeenCalledTimes(1))
    fireEvent.click(screen.getByRole('button', { name: /刷新/ }))
    await waitFor(() => expect(listCatalog).toHaveBeenCalledTimes(2))
  })

  it('creates a personal MCP server from the inline form', async () => {
    const listCatalog = vi.fn().mockResolvedValue(makeCatalog([]))
    const onCreateServer = vi.fn().mockResolvedValue(undefined)
    renderView({ listCatalog, onCreateServer })

    fireEvent.click(await screen.findByRole('button', { name: '添加服务' }))
    fireEvent.change(screen.getByLabelText('服务名称'), { target: { value: 'context7' } })
    fireEvent.change(screen.getByLabelText('命令'), { target: { value: 'npx' } })
    fireEvent.change(screen.getByLabelText('参数'), { target: { value: '-y @upstash/context7-mcp' } })
    fireEvent.click(screen.getByRole('button', { name: '保存服务' }))

    await waitFor(() => {
      expect(onCreateServer).toHaveBeenCalledWith({
        name: 'context7',
        transport: 'stdio',
        command: 'npx',
        args: ['-y', '@upstash/context7-mcp'],
        env: {},
      })
    })
    await waitFor(() => expect(listCatalog).toHaveBeenCalledTimes(2))
  })

  it('edits and deletes only personal MCP servers', async () => {
    const personal: McpServerInfo = {
      ...githubServer,
      name: 'context7',
      source: 'shejane',
      source_path: '/u/.shejane/mcp-servers.json',
      command: 'npx',
      args: ['-y', '@upstash/context7-mcp'],
      env_keys: [],
    }
    const onUpdateServer = vi.fn().mockResolvedValue(undefined)
    const onDeleteServer = vi.fn().mockResolvedValue(undefined)
    renderView({
      listCatalog: vi.fn().mockResolvedValue(makeCatalog([personal, playwrightServer])),
      onUpdateServer,
      onDeleteServer,
    })

    await screen.findByText('context7')
    expect(screen.queryByRole('button', { name: '编辑 playwright' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '编辑 context7' }))
    fireEvent.change(screen.getByLabelText('参数'), { target: { value: '-y @upstash/context7-mcp --fresh' } })
    fireEvent.click(screen.getByRole('button', { name: '保存服务' }))
    await waitFor(() => {
      expect(onUpdateServer).toHaveBeenCalledWith('context7', {
        name: 'context7',
        transport: 'stdio',
        command: 'npx',
        args: ['-y', '@upstash/context7-mcp', '--fresh'],
        env: {},
      })
    })

    fireEvent.click(screen.getByRole('button', { name: '删除 context7' }))
    await waitFor(() => expect(onDeleteServer).toHaveBeenCalledWith('context7'))
  })
})
