import { useCallback, useEffect, useMemo, useState, type FormEvent } from 'react'
import {
  IconDeviceFloppy,
  IconExternalLink,
  IconFileDescription,
  IconPencil,
  IconPlus,
  IconRefresh,
  IconSearch,
  IconServer,
  IconTrash,
  IconX,
} from '@tabler/icons-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Switch } from '@/components/ui/switch'
import { useI18n } from '@/shared/i18n/i18n'
import type { McpServerCatalog, McpServerInfo, McpServerWriteRequest } from '@/runtime/client'

export interface MCPViewProps {
  listCatalog: () => Promise<McpServerCatalog>
  embedded?: boolean
  /** Names the user explicitly disabled. The switch is OFF for names
   *  in this set; flipping it removes (or adds) the name. We track
   *  *disabled* rather than *enabled* so newly discovered servers
   *  default to ON without the renderer having to touch state. */
  disabledServers: readonly string[]
  onDisabledChange: (next: string[]) => void
  onCreateServer?: (input: McpServerWriteRequest) => Promise<void>
  onUpdateServer?: (name: string, input: McpServerWriteRequest) => Promise<void>
  onDeleteServer?: (name: string) => Promise<void>
  /** Open the config file's parent directory in the OS file manager.
   *  Hidden when the Electron bridge isn't wired (browser-only). */
  onOpenFolder?: (path: string) => void
}

// Section ordering + display labels mirror the SkillsView convention.
// We keep our own source key on the runtime side (see tools/mcp.py
// `SOURCE_*` constants) so the renderer can group consistently.
const SECTION_ORDER: readonly string[] = [
  'shejane',
  'shejane-legacy',
  'env',
]
const SECTION_LABEL: Record<string, { zh: string; en: string }> = {
  shejane: { zh: '个人', en: 'Personal' },
  'shejane-legacy': { zh: '历史配置', en: 'Legacy config' },
  env: { zh: '环境变量', en: 'Environment override' },
}

const STATUS_KEY = {
  idle: 'mcp.status.idle',
  ready: 'mcp.status.ready',
  error: 'mcp.status.error',
} as const

function sectionLabel(source: string, locale: string): string {
  const known = SECTION_LABEL[source]
  if (!known) return source
  return locale.startsWith('zh') ? known.zh : known.en
}

function matchesQuery(server: McpServerInfo, needle: string): boolean {
  if (!needle) return true
  const haystack = `${server.name} ${server.command ?? ''} ${server.url ?? ''}`.toLowerCase()
  return haystack.includes(needle)
}

const EMPTY_CATALOG: McpServerCatalog = { servers: [], sources_scanned: [] }

interface McpFormState {
  name: string
  transport: string
  command: string
  argsText: string
  url: string
}

type McpEditorState =
  | { mode: 'create' }
  | { mode: 'edit'; originalName: string }
  | null

const EMPTY_FORM: McpFormState = {
  name: '',
  transport: 'stdio',
  command: '',
  argsText: '',
  url: '',
}

function formFromServer(server: McpServerInfo): McpFormState {
  return {
    name: server.name,
    transport: server.transport || 'stdio',
    command: server.command ?? '',
    argsText: server.args.join(' '),
    url: server.url ?? '',
  }
}

function argsFromText(value: string): string[] {
  return value.trim().split(/\s+/).filter(Boolean)
}

function requestFromForm(form: McpFormState): McpServerWriteRequest {
  const input: McpServerWriteRequest = {
    name: form.name.trim(),
    transport: form.transport,
    args: [],
    env: {},
  }
  if (form.command.trim()) input.command = form.command.trim()
  const args = argsFromText(form.argsText)
  input.args = args
  if (form.url.trim()) input.url = form.url.trim()
  return input
}

function useMCPViewModel({
  listCatalog,
  embedded = false,
  disabledServers,
  onDisabledChange,
  onCreateServer,
  onUpdateServer,
  onDeleteServer,
  onOpenFolder,
}: MCPViewProps) {
  const { t, locale } = useI18n()
  const [catalog, setCatalog] = useState<McpServerCatalog>(EMPTY_CATALOG)
  const [loading, setLoading] = useState(true)
  const [query, setQuery] = useState('')
  const [editor, setEditor] = useState<McpEditorState>(null)
  const [form, setForm] = useState<McpFormState>(EMPTY_FORM)
  const [saving, setSaving] = useState(false)

  const refresh = useCallback(async () => {
    setLoading(true)
    try {
      setCatalog(await listCatalog())
    } finally {
      setLoading(false)
    }
  }, [listCatalog])

  useEffect(() => {
    void refresh()
  }, [refresh])

  const disabledSet = useMemo(() => new Set(disabledServers), [disabledServers])

  const filteredServers = useMemo(() => {
    const needle = query.trim().toLowerCase()
    if (!needle) return catalog.servers
    return catalog.servers.filter((s) => matchesQuery(s, needle))
  }, [catalog.servers, query])

  // Group filtered servers by their `source` label. Unlike skills (which
  // also surfaces empty source directories so the user knows where to
  // drop a SKILL.md), MCP sources are config files — we only show a
  // section header when the source actually exists. The exception is
  // `shejane`, which we always show with a "drop a config here" hint
  // so the user knows their canonical override path.
  const sections = useMemo(() => {
    const buckets = new Map<string, McpServerInfo[]>()
    for (const server of filteredServers) {
      const list = buckets.get(server.source) ?? []
      list.push(server)
      buckets.set(server.source, list)
    }
    const allSources = new Set<string>(catalog.servers.map((s) => s.source))
    allSources.add('shejane') // always render the personal section header
    return Array.from(allSources)
      .sort((a, b) => {
        const ai = SECTION_ORDER.indexOf(a)
        const bi = SECTION_ORDER.indexOf(b)
        if (ai !== -1 && bi !== -1) return ai - bi
        if (ai !== -1) return -1
        if (bi !== -1) return 1
        return a.localeCompare(b)
      })
      .map((source) => ({
        source,
        servers: buckets.get(source) ?? [],
        // Pick any representative server's source_path for the
        // "open folder" button. All servers from one source share
        // the same config file.
        anyPath:
          catalog.servers.find((s) => s.source === source)?.source_path ?? '',
      }))
  }, [catalog.servers, filteredServers])

  const toggle = useCallback(
    (name: string) => {
      const next = new Set(disabledServers)
      if (next.has(name)) {
        next.delete(name)
      } else {
        next.add(name)
      }
      onDisabledChange(Array.from(next))
    },
    [disabledServers, onDisabledChange],
  )

  const openCreate = useCallback(() => {
    setForm(EMPTY_FORM)
    setEditor({ mode: 'create' })
  }, [])

  const openEdit = useCallback((server: McpServerInfo) => {
    setForm(formFromServer(server))
    setEditor({ mode: 'edit', originalName: server.name })
  }, [])

  const submitEditor = useCallback(async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (saving) return
    const input = requestFromForm(form)
    if (!input.name) return
    setSaving(true)
    try {
      if (editor?.mode === 'edit') {
        await onUpdateServer?.(editor.originalName, input)
      } else {
        await onCreateServer?.(input)
      }
      setEditor(null)
      setForm(EMPTY_FORM)
      await refresh()
    } finally {
      setSaving(false)
    }
  }, [editor, form, onCreateServer, onUpdateServer, refresh, saving])

  const deleteServer = useCallback(async (name: string) => {
    await onDeleteServer?.(name)
    await refresh()
  }, [onDeleteServer, refresh])

  const totalCount = catalog.servers.length
  const filteredEmpty = totalCount > 0 && filteredServers.length === 0

  return { deleteServer, disabledSet, editor, embedded, filteredEmpty, form, loading, locale, onCreateServer, onDeleteServer, onOpenFolder, onUpdateServer, openCreate, openEdit, query, refresh, saving, sections, setEditor, setForm, setQuery, submitEditor, t, toggle, totalCount }
}

export function MCPView(props: Parameters<typeof useMCPViewModel>[0]) {
  return <MCPViewContent view={useMCPViewModel(props)} />
}

function MCPViewContent({ view }: { view: ReturnType<typeof useMCPViewModel> }) {
  const { deleteServer, disabledSet, editor, embedded, filteredEmpty, form, loading, locale, onCreateServer, onDeleteServer, onOpenFolder, onUpdateServer, openCreate, openEdit, query, refresh, saving, sections, setEditor, setForm, setQuery, submitEditor, t, toggle, totalCount } = view
  return (
    <section className="workspace skills-view">
      {!embedded ? (
        <header className="topbar topbar-page">
          <div className="chat-toolbar-title">
            <span>{t('mcp.title')}</span>
          </div>
        </header>
      ) : null}

      <div className="skills-scroll">
        <div className="skills-content">
          <div className="skills-toolbar mcp-toolbar">
            <div className="skills-search">
              <IconSearch className="skills-search-icon" size={15} aria-hidden="true" />
              <Input
                type="search"
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder={t('mcp.searchPlaceholder')}
                aria-label={t('mcp.searchPlaceholder')}
              />
            </div>
            <div className="skills-toolbar-actions">
              {onCreateServer ? (
                <Button
                  type="button"
                  size="sm"
                  variant="ghost"
                  className="skills-new-button"
                  onClick={openCreate}
                >
                  <IconPlus size={14} aria-hidden="true" />
                  {t('mcp.addServer')}
                </Button>
              ) : null}
              <Button
                type="button"
                size="sm"
                variant="ghost"
                className="skills-refresh-button"
                onClick={() => void refresh()}
                disabled={loading}
                aria-label={t('mcp.refresh')}
                title={t('mcp.refresh')}
              >
                <IconRefresh size={14} aria-hidden="true" />
              </Button>
            </div>
          </div>
          {editor ? (
            <form className="resource-editor-form" onSubmit={(event) => void submitEditor(event)}>
              <div className="resource-editor-grid">
                <label>
                  <span>{t('mcp.form.name')}</span>
                  <Input
                    value={form.name}
                    onChange={(event) => setForm((current) => ({ ...current, name: event.target.value }))}
                    aria-label={t('mcp.form.name')}
                    required
                  />
                </label>
                <label>
                  <span>{t('mcp.form.transport')}</span>
                  <select
                    value={form.transport}
                    onChange={(event) => setForm((current) => ({ ...current, transport: event.target.value }))}
                    aria-label={t('mcp.form.transport')}
                  >
                    <option value="stdio">stdio</option>
                    <option value="streamable_http">streamable_http</option>
                    <option value="sse">sse</option>
                    <option value="websocket">websocket</option>
                  </select>
                </label>
                <label>
                  <span>{t('mcp.form.command')}</span>
                  <Input
                    value={form.command}
                    onChange={(event) => setForm((current) => ({ ...current, command: event.target.value }))}
                    aria-label={t('mcp.form.command')}
                  />
                </label>
                <label>
                  <span>{t('mcp.form.args')}</span>
                  <Input
                    value={form.argsText}
                    onChange={(event) => setForm((current) => ({ ...current, argsText: event.target.value }))}
                    aria-label={t('mcp.form.args')}
                  />
                </label>
                <label className="resource-editor-wide">
                  <span>{t('mcp.form.url')}</span>
                  <Input
                    value={form.url}
                    onChange={(event) => setForm((current) => ({ ...current, url: event.target.value }))}
                    aria-label={t('mcp.form.url')}
                  />
                </label>
              </div>
              <div className="resource-editor-actions">
                <Button type="submit" size="sm" disabled={saving}>
                  <IconDeviceFloppy size={14} aria-hidden="true" />
                  {t('mcp.saveServer')}
                </Button>
                <Button type="button" size="sm" variant="ghost" onClick={() => setEditor(null)}>
                  <IconX size={14} aria-hidden="true" />
                  {t('common.cancel')}
                </Button>
              </div>
            </form>
          ) : null}

          {filteredEmpty ? <p className="skills-not-found">{t('mcp.notFound')}</p> : null}

          {totalCount === 0 && !loading ? (
            <p className="mcp-overall-empty">{t('mcp.empty')}</p>
          ) : null}

          {sections.map(({ source, servers, anyPath }) => (
            <section className="skills-section" key={source}>
              <header className="skills-section-header">
                <h3 className="skills-section-title">{sectionLabel(source, locale)}</h3>
                {anyPath && onOpenFolder ? (
                  <Button
                    type="button"
                    size="sm"
                    variant="ghost"
                    className="skills-section-open"
                    onClick={() => onOpenFolder(anyPath)}
                    title={anyPath}
                    aria-label={t('mcp.openConfig')}
                  >
                    <IconFileDescription size={14} aria-hidden="true" />
                  </Button>
                ) : null}
              </header>
              {servers.length === 0 ? (
                <p className="skills-section-empty">
                  {source === 'shejane'
                    ? t('mcp.section.personalEmptyHint')
                    : t('mcp.section.emptyHint')}
                </p>
              ) : (
                <div className="mcp-list">
                  {servers.map((server) => {
                    const isOff = disabledSet.has(server.name)
                    return (
                      <div className="mcp-row" key={server.name} data-disabled={isOff || undefined}>
                        <div className="mcp-row-icon" aria-hidden="true">
                          <IconServer size={18} />
                        </div>
                        <div className="mcp-row-body">
                          <div className="mcp-row-title">
                            <span className="mcp-row-name">{server.name}</span>
                            <span className="mcp-row-transport">{server.transport}</span>
                            <span
                              className="mcp-row-status"
                              data-status={server.status}
                              title={server.error_type ?? undefined}
                            >
                              {t(STATUS_KEY[server.status ?? 'idle'])}
                              {server.status === 'ready' ? ` · ${server.tool_count}` : ''}
                            </span>
                          </div>
                          <div className="mcp-row-meta">
                            {server.command ? (
                              <span className="mcp-row-cmd" title={server.command}>
                                {server.command}
                                {server.args.length > 0 ? ` ${server.args.join(' ')}` : ''}
                              </span>
                            ) : null}
                            {server.url ? (
                              <span className="mcp-row-url">
                                <IconExternalLink size={12} aria-hidden="true" />
                                {server.url}
                              </span>
                            ) : null}
                            {server.env_keys.length > 0 ? (
                              <span className="mcp-row-envs">
                                env: {server.env_keys.join(', ')}
                              </span>
                            ) : null}
                          </div>
                        </div>
                        {server.source === 'shejane' && (onUpdateServer || onDeleteServer) ? (
                          <div className="resource-row-actions">
                            {onUpdateServer ? (
                              <Button
                                type="button"
                                size="icon-xs"
                                variant="ghost"
                                aria-label={t('mcp.editServerAria', { name: server.name })}
                                onClick={() => openEdit(server)}
                              >
                                <IconPencil size={13} aria-hidden="true" />
                              </Button>
                            ) : null}
                            {onDeleteServer ? (
                              <Button
                                type="button"
                                size="icon-xs"
                                variant="ghost"
                                aria-label={t('mcp.deleteServerAria', { name: server.name })}
                                onClick={() => void deleteServer(server.name)}
                              >
                                <IconTrash size={13} aria-hidden="true" />
                              </Button>
                            ) : null}
                          </div>
                        ) : null}
                        <Switch
                          checked={!isOff}
                          aria-label={server.name}
                          onCheckedChange={() => toggle(server.name)}
                        />
                      </div>
                    )
                  })}
                </div>
              )}
            </section>
          ))}
        </div>
      </div>
    </section>
  )
}
