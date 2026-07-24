import { useCallback, useEffect, useRef, useState } from 'react'
import { IconBox, IconCheck, IconRefresh, IconSearch, IconTrash, IconUpload } from '@tabler/icons-react'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Switch } from '@/components/ui/switch'
import { useI18n } from '@/shared/i18n/i18n'
import {
  RuntimeHTTPError,
  type PluginReadinessSnapshot,
  type PluginSetupActionID,
  type PluginSetupAdvanceCommandReceipt,
  type PluginSummary,
} from '@/runtime/client'

type SetupActionID = PluginSetupActionID
const COMPUTER_USE_PLUGIN_ID = 'org.shejane.computer-use'
const FIXED_PLUGIN_IDS = new Set([
  COMPUTER_USE_PLUGIN_ID,
  'org.shejane.browser-qa',
  'org.shejane.ocr',
])

interface ComputerUseSetup {
  plugin: PluginSummary
  readiness: PluginReadinessSnapshot
  busy: boolean
  error?: string
}

function setupDescriptionKey(readiness: PluginReadinessSnapshot) {
  if (readiness.state === 'ready') return 'plugins.setup.description.ready.unknown' as const
  if (!readiness.step) return 'plugins.setup.description.blocked.unknown' as const
  if (readiness.step === 'install_helper') {
    return readiness.state === 'action_required'
      ? 'plugins.setup.description.action_required.install_helper' as const
      : 'plugins.setup.description.blocked.install_helper' as const
  }
  if (readiness.step === 'screen_recording') {
    if (readiness.state === 'action_required') {
      return 'plugins.setup.description.action_required.screen_recording' as const
    }
    return readiness.state === 'awaiting_user'
      ? 'plugins.setup.description.awaiting_user.screen_recording' as const
      : 'plugins.setup.description.blocked.screen_recording' as const
  }
  if (readiness.state === 'action_required') {
    return 'plugins.setup.description.action_required.accessibility' as const
  }
  return readiness.state === 'awaiting_user'
    ? 'plugins.setup.description.awaiting_user.accessibility' as const
    : 'plugins.setup.description.blocked.accessibility' as const
}

export interface PluginsViewProps {
  listPlugins: () => Promise<PluginSummary[]>
  embedded?: boolean
  refreshVersion?: number
  selectPackage?: () => Promise<string | undefined>
  installPlugin?: (sourcePath: string, allowUnsigned: boolean) => Promise<unknown>
  setEnabled?: (plugin: PluginSummary, enabled: boolean) => Promise<unknown>
  removePlugin?: (plugin: PluginSummary) => Promise<unknown>
  getReadiness?: (plugin: PluginSummary) => Promise<PluginReadinessSnapshot>
  advanceSetup?: (
    plugin: PluginSummary,
    readiness: PluginReadinessSnapshot,
    actionID: SetupActionID,
  ) => Promise<PluginSetupAdvanceCommandReceipt>
}

function usePluginsViewModel({
  listPlugins,
  embedded = false,
  refreshVersion,
  selectPackage,
  installPlugin,
  setEnabled,
  removePlugin,
  getReadiness,
  advanceSetup,
}: PluginsViewProps) {
  const { t } = useI18n()
  const [plugins, setPlugins] = useState<PluginSummary[]>([])
  const [loading, setLoading] = useState(true)
  const [failed, setFailed] = useState(false)
  const [busy, setBusy] = useState<string>()
  const [error, setError] = useState<string>()
  const [query, setQuery] = useState('')
  const [setup, setSetup] = useState<ComputerUseSetup>()
  const autoRecheckRevision = useRef<number>()

  const refresh = useCallback(async () => {
    setLoading(true)
    setFailed(false)
    try {
      setPlugins(await listPlugins())
    } catch {
      setFailed(true)
    } finally {
      setLoading(false)
    }
  }, [listPlugins])

  useEffect(() => {
    void refresh()
  }, [refresh, refreshVersion])

  const mutate = useCallback(
    async (key: string, action: () => Promise<unknown>) => {
      setBusy(key)
      setError(undefined)
      try {
        await action()
        await refresh()
      } catch (cause) {
        setError(cause instanceof Error ? cause.message : String(cause))
        throw cause
      } finally {
        setBusy(undefined)
      }
    },
    [refresh],
  )

  const withUnsignedConfirmation = useCallback(
    async (operation: (allowUnsigned: boolean) => Promise<unknown>) => {
      try {
        await operation(false)
      } catch (cause) {
        if (
          !(cause instanceof RuntimeHTTPError) ||
          cause.code !== 'unsigned_plugin_confirmation_required' ||
          !window.confirm(t('plugins.confirmUnsigned'))
        ) {
          throw cause
        }
        await operation(true)
      }
    },
    [t],
  )

  const importPlugin = async () => {
    const sourcePath = await selectPackage?.()
    if (!sourcePath || !installPlugin) return
    await mutate('install', () =>
      withUnsignedConfirmation((allowUnsigned) => installPlugin(sourcePath, allowUnsigned)),
    )
  }

  const completeSetup = useCallback(
    async (plugin: PluginSummary) => {
      if (!plugin.enabled) await setEnabled?.(plugin, true)
      setSetup(undefined)
      await refresh()
    },
    [refresh, setEnabled],
  )

  const openSetup = useCallback(
    async (plugin: PluginSummary) => {
      if (!getReadiness) return
      setBusy(`setup:${plugin.id}`)
      setError(undefined)
      try {
        const readiness = await getReadiness(plugin)
        if (readiness.state === 'ready') {
          await completeSetup(plugin)
          return
        }
        autoRecheckRevision.current = undefined
        setSetup({ plugin, readiness, busy: false })
      } catch (cause) {
        setError(cause instanceof Error ? cause.message : String(cause))
      } finally {
        setBusy(undefined)
      }
    },
    [completeSetup, getReadiness],
  )

  const advanceComputerUseSetup = useCallback(
    async (actionID: SetupActionID) => {
      if (!setup || !advanceSetup) return
      const current = setup
      setSetup({ ...current, busy: true, error: undefined })
      try {
        const receipt = await advanceSetup(current.plugin, current.readiness, actionID)
        if (receipt.readiness.state === 'ready') {
          await completeSetup(current.plugin)
          return
        }
        setSetup({ plugin: current.plugin, readiness: receipt.readiness, busy: false })
      } catch (cause) {
        setSetup({
          ...current,
          busy: false,
          error: cause instanceof Error ? cause.message : String(cause),
        })
      }
    },
    [advanceSetup, completeSetup, setup],
  )

  useEffect(() => {
    if (!setup?.readiness.can_recheck || setup.busy || !advanceSetup) return
    const revision = setup.readiness.revision
    const recheckOnFocus = () => {
      if (autoRecheckRevision.current === revision) return
      autoRecheckRevision.current = revision
      void advanceComputerUseSetup('recheck')
    }
    window.addEventListener('focus', recheckOnFocus)
    return () => window.removeEventListener('focus', recheckOnFocus)
  }, [advanceComputerUseSetup, advanceSetup, setup])

  const setupActionLabel = setup?.readiness.action_id
    ? t(`plugins.setup.action.${setup.readiness.action_id}`)
    : setup?.readiness.can_recheck
      ? t('plugins.setup.action.recheck')
      : undefined

  const normalizedQuery = query.trim().toLocaleLowerCase()
  const installedPlugins = plugins.filter((plugin) => !plugin.retired)
  const filteredPlugins = normalizedQuery
    ? installedPlugins.filter((plugin) =>
        [plugin.name, plugin.description, plugin.id, plugin.publisher.name]
          .some((value) => value.toLocaleLowerCase().includes(normalizedQuery)),
      )
    : installedPlugins

  return { advanceComputerUseSetup, busy, embedded, error, failed, filteredPlugins, getReadiness, importPlugin, installPlugin, installedPlugins, loading, mutate, openSetup, query, refresh, removePlugin, selectPackage, setEnabled, setQuery, setSetup, setup, setupActionLabel, t }
}

export function PluginsView(props: Parameters<typeof usePluginsViewModel>[0]) {
  return <PluginsViewContent view={usePluginsViewModel(props)} />
}

function PluginsViewContent({ view }: { view: ReturnType<typeof usePluginsViewModel> }) {
  const { advanceComputerUseSetup, busy, embedded, error, failed, filteredPlugins, getReadiness, importPlugin, installPlugin, installedPlugins, loading, mutate, openSetup, query, refresh, removePlugin, selectPackage, setEnabled, setQuery, setSetup, setup, setupActionLabel, t } = view
  return (
    <section className="workspace skills-view plugins-view">
      {!embedded ? (
        <header className="topbar topbar-page">
          <div className="chat-toolbar-title">{t('plugins.title')}</div>
        </header>
      ) : null}

      <div className="skills-scroll">
        <div className="skills-content">
          <div className="skills-toolbar">
            <div className="skills-search">
              <IconSearch className="skills-search-icon" size={15} aria-hidden="true" />
              <Input
                type="search"
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder={t('plugins.searchPlaceholder')}
                aria-label={t('plugins.searchPlaceholder')}
              />
            </div>
            <div className="skills-toolbar-actions">
              {selectPackage && installPlugin ? (
                <Button
                  type="button"
                  size="sm"
                  variant="ghost"
                  className="skills-new-button"
                  onClick={() => void importPlugin().catch(() => undefined)}
                  disabled={Boolean(busy)}
                >
                  <IconUpload size={14} aria-hidden="true" />
                  {t('plugins.import')}
                </Button>
              ) : null}
              <Button
                type="button"
                size="sm"
                variant="ghost"
                className="skills-refresh-button"
                onClick={() => void refresh()}
                disabled={loading || Boolean(busy)}
                aria-label={t('plugins.refresh')}
                title={t('plugins.refresh')}
              >
                <IconRefresh size={14} aria-hidden="true" />
              </Button>
            </div>
          </div>

          {error ? <div className="plugins-error" role="alert">{error}</div> : null}

          {failed ? (
            <div className="skills-section-empty">{t('plugins.loadError')}</div>
          ) : installedPlugins.length === 0 ? (
            loading ? null : <div className="skills-section-empty">{t('plugins.empty')}</div>
          ) : filteredPlugins.length === 0 ? (
            <div className="skills-section-empty">{t('plugins.notFound')}</div>
          ) : (
            <ul className="skills-grid">
              {filteredPlugins.map((plugin) => (
                <li className="skill-card plugin-card" key={plugin.id}>
                  <div className="skill-card-head">
                    <div className="skill-card-title">
                      <span className="skill-card-icon" aria-hidden="true">
                        <IconBox size={15} />
                      </span>
                      <span className="skill-card-name">{plugin.name}</span>
                    </div>
                    <span className="plugin-version">v{plugin.version}</span>
                  </div>
                  <div className="skill-card-text">
                    <div className="skill-card-desc">{plugin.description}</div>
                  </div>
                  <div className="skill-card-footer">
                    {removePlugin && !FIXED_PLUGIN_IDS.has(plugin.id) ? (
                      <Button
                        type="button"
                        size="icon-sm"
                        variant="ghost"
                        className="plugin-remove-button"
                        aria-label={t('plugins.removeAria', { name: plugin.name })}
                        title={t('plugins.remove')}
                        onClick={() => {
                          if (!window.confirm(t('plugins.confirmRemove', { name: plugin.name }))) return
                          void mutate(`remove:${plugin.id}`, () => removePlugin(plugin))
                            .catch(() => undefined)
                        }}
                        disabled={Boolean(busy)}
                      >
                        <IconTrash size={15} aria-hidden="true" />
                      </Button>
                    ) : null}
                    {setEnabled ? (
                      <Switch
                        checked={plugin.enabled}
                        onCheckedChange={(enabled) => {
                          if (enabled && plugin.id === COMPUTER_USE_PLUGIN_ID && getReadiness) {
                            void openSetup(plugin)
                            return
                          }
                          void mutate(`enabled:${plugin.id}`, () => setEnabled(plugin, enabled))
                            .catch(() => undefined)
                        }}
                        disabled={Boolean(busy) || plugin.compatibility === 'incompatible'}
                        aria-label={t('plugins.toggleAria', { name: plugin.name })}
                        title={plugin.compatibility === 'incompatible'
                          ? t('plugins.status.incompatible')
                          : undefined}
                      />
                    ) : null}
                  </div>
                </li>
              ))}
            </ul>
          )}

        </div>
      </div>

      <Dialog
        open={Boolean(setup)}
        onOpenChange={(open) => {
          if (!open && !setup?.busy) setSetup(undefined)
        }}
      >
        {setup ? (
          <DialogContent className="computer-use-setup-dialog" showCloseButton={!setup.busy}>
            <DialogHeader>
              <div className="computer-use-setup-kicker">
                {t('plugins.setup.progress', {
                  current: !setup.readiness.step || setup.readiness.step === 'install_helper'
                    ? 1
                    : setup.readiness.step === 'screen_recording'
                      ? 2
                      : 3,
                })}
              </div>
              <DialogTitle>{t('plugins.setup.title', { name: setup.plugin.name })}</DialogTitle>
              <DialogDescription>
                {t(setupDescriptionKey(setup.readiness))}
              </DialogDescription>
            </DialogHeader>

            {setup.readiness.state === 'ready' ? (
              <div className="computer-use-setup-ready">
                <IconCheck size={16} aria-hidden="true" />
                {t('plugins.setup.ready')}
              </div>
            ) : null}
            {setup.error ? <div className="computer-use-setup-error" role="alert">{setup.error}</div> : null}

            <DialogFooter className="computer-use-setup-actions">
              <Button
                type="button"
                size="sm"
                variant="ghost"
                onClick={() => setSetup(undefined)}
                disabled={setup.busy}
              >
                {t('plugins.setup.later')}
              </Button>
              {setupActionLabel ? (
                <Button
                  type="button"
                  size="sm"
                  onClick={() => void advanceComputerUseSetup(
                    setup.readiness.action_id ?? 'recheck',
                  )}
                  disabled={setup.busy}
                >
                  {setupActionLabel}
                </Button>
              ) : null}
            </DialogFooter>
          </DialogContent>
        ) : null}
      </Dialog>
    </section>
  )
}
