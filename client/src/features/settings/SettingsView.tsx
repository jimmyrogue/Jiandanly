import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { IconTrash } from '@tabler/icons-react'
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogMedia,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Switch } from '@/components/ui/switch'
import { useI18n, type Locale } from '@/shared/i18n/i18n'
import type {
  AdvancedAgentSettings,
  AgentSettings,
  FixedRuntimeAssetPluginID,
  FixedRuntimeAssetStatus,
  RuntimeConnection,
} from '@/runtime/client'
import { ModelServicesSettings } from './ModelServicesSettings'

type SettingsSectionID = 'models' | 'agent' | 'general' | 'data'

const SETTINGS_SECTION_TOP_OFFSET = 72
const FIXED_RUNTIME_ASSETS: ReadonlyArray<{
  pluginID: FixedRuntimeAssetPluginID
  name: string
}> = [
  { pluginID: 'org.shejane.browser-qa', name: 'Browser QA' },
  { pluginID: 'org.shejane.ocr', name: 'RapidOCR' },
]

function SettingRow({
  label,
  hint,
  children,
  danger = false,
}: {
  label: string
  hint?: string
  children?: ReactNode
  danger?: boolean
}) {
  return (
    <div className={`settings-row${danger ? ' settings-danger' : ''}`}>
      <div className="settings-row-copy">
        <div className="settings-row-label">{label}</div>
        {hint ? <div className="settings-row-hint">{hint}</div> : null}
      </div>
      {children ? <div className="settings-row-control">{children}</div> : null}
    </div>
  )
}

function SettingsRowButton({
  ariaLabel,
  children,
  danger,
  disabled,
  onClick,
}: {
  ariaLabel?: string
  children: ReactNode
  danger?: boolean
  disabled?: boolean
  onClick: () => void
}) {
  return (
    <button
      type="button"
      aria-label={ariaLabel}
      className={`settings-row-button${danger ? ' settings-row-button-danger' : ''}`}
      disabled={disabled}
      onClick={onClick}
    >
      {children}
    </button>
  )
}

function SettingsSection({
  id,
  title,
  note,
  children,
}: {
  id: SettingsSectionID
  title: string
  note?: string
  children: ReactNode
}) {
  return (
    <section id={`settings-${id}`} className="settings-section">
      <div className="settings-section-head">
        <h2>{title}</h2>
        {note ? <p>{note}</p> : null}
      </div>
      <div className="settings-card">{children}</div>
    </section>
  )
}

function useSettingsViewModel({
  isDesktop = true,
  agentSettings,
  advancedSettingsReady = true,
  onAgentSettingsChange,
  onClearMemory,
  onImportLocalData,
  onExportLocalData,
  runtimeConnection,
  openModelServiceAdd,
  onModelServiceAddOpened,
  onModelServicesChange,
  getRuntimeAssetStatus,
  onDownloadRuntimeAsset,
  onRemoveRuntimeAsset,
}: {
  isDesktop?: boolean
  agentSettings: Required<AgentSettings>
  advancedSettingsReady?: boolean
  onAgentSettingsChange: (next: Required<AgentSettings>) => void
  onClearMemory?: () => Promise<number>
  onImportLocalData: (file?: File) => void
  onExportLocalData?: () => void
  runtimeConnection?: RuntimeConnection | null
  openModelServiceAdd?: boolean
  onModelServiceAddOpened?: () => void
  onModelServicesChange?: () => void
  getRuntimeAssetStatus?: (pluginID: FixedRuntimeAssetPluginID) => Promise<FixedRuntimeAssetStatus>
  onDownloadRuntimeAsset?: (pluginID: FixedRuntimeAssetPluginID) => Promise<unknown>
  onRemoveRuntimeAsset?: (pluginID: FixedRuntimeAssetPluginID) => Promise<unknown>
}) {
  const { t, locale, setLocale } = useI18n()
  const importInputRef = useRef<HTMLInputElement>(null)
  const settingsScrollRef = useRef<HTMLDivElement>(null)
  const runtimeAssetActiveDownloads = useRef(new Set<FixedRuntimeAssetPluginID>())
  const [activeSection, setActiveSection] = useState<SettingsSectionID>(
    isDesktop ? 'models' : 'general',
  )
  const [clearMemoryConfirmOpen, setClearMemoryConfirmOpen] = useState(false)
  const [runtimeAssetDeleteConfirm, setRuntimeAssetDeleteConfirm] = useState<FixedRuntimeAssetPluginID | null>(null)
  const [clearingMemory, setClearingMemory] = useState(false)
  const [clientUpdate, setClientUpdate] = useState<ClientUpdateState | null>(null)
  const [runtimeAssetDownloads, setRuntimeAssetDownloads] = useState<Partial<
    Record<FixedRuntimeAssetPluginID, 'downloading' | 'downloaded' | 'deleting' | 'error' | 'delete_error'>
  >>({})
  const [runtimeAssetProgress, setRuntimeAssetProgress] = useState<Partial<
    Record<FixedRuntimeAssetPluginID, number | null>
  >>({})

  const memoryEnabled = (agentSettings.memory ?? 'on') === 'on'
  const adv: AdvancedAgentSettings = agentSettings.advanced ?? {}
  const setAdv = (patch: Partial<AdvancedAgentSettings>) =>
    onAgentSettingsChange({ ...agentSettings, advanced: { ...adv, ...patch } })

  const navItems = useMemo<Array<{ id: SettingsSectionID, label: string }>>(
    () => [
      ...(isDesktop
        ? [
            { id: 'models' as const, label: t('settings.group.models') },
          ]
        : []),
      { id: 'agent', label: t('settings.group.agent') },
      { id: 'general', label: t('settings.group.general') },
      { id: 'data', label: t('settings.group.dataSecurity') },
    ],
    [isDesktop, t],
  )

  const updateActiveSectionFromScroll = useCallback(() => {
    const scrollRoot = settingsScrollRef.current
    if (!scrollRoot) return

    const rootTop = scrollRoot.getBoundingClientRect().top
    const sectionPositions = navItems
      .map((item) => {
        const section = document.getElementById(`settings-${item.id}`)
        if (!section) return null
        return {
          id: item.id,
          top: section.getBoundingClientRect().top - rootTop,
        }
      })
      .filter((item): item is { id: SettingsSectionID, top: number } => item !== null)

    if (sectionPositions.length === 0) return

    const hasScrollableLayout = scrollRoot.scrollHeight > scrollRoot.clientHeight
    const hasMeasuredSections = sectionPositions.some((position, index) =>
      index === 0 ? position.top !== 0 : position.top !== sectionPositions[0].top,
    )
    if (!hasScrollableLayout && !hasMeasuredSections) return

    const atBottom = hasScrollableLayout
      && scrollRoot.scrollTop + scrollRoot.clientHeight >= scrollRoot.scrollHeight - 8
    const nextActive = atBottom
      ? sectionPositions[sectionPositions.length - 1].id
      : sectionPositions.reduce<SettingsSectionID>((current, position) => (
          position.top <= SETTINGS_SECTION_TOP_OFFSET ? position.id : current
        ), sectionPositions[0].id)

    setActiveSection((current) => (current === nextActive ? current : nextActive))
  }, [navItems])

  useEffect(() => {
    updateActiveSectionFromScroll()
  }, [updateActiveSectionFromScroll])

  useEffect(() => {
    if (!isDesktop) return
    const updates = window.shejaneClient?.updates
    if (!updates) return
    const unsubscribe = updates.onStateChange(setClientUpdate)
    void updates.getState().then(setClientUpdate).catch(() => undefined)
    return unsubscribe
  }, [isDesktop])

  useEffect(() => {
    if (!isDesktop || !getRuntimeAssetStatus) return
    let active = true
    for (const { pluginID } of FIXED_RUNTIME_ASSETS) {
      void getRuntimeAssetStatus(pluginID)
        .then((status) => {
          if (!active) return
          setRuntimeAssetDownloads((current) => {
            if (current[pluginID] === 'downloading' || current[pluginID] === 'deleting') return current
            const next = { ...current }
            if (status.downloaded) next[pluginID] = 'downloaded'
            else if (status.downloading) next[pluginID] = 'downloading'
            else delete next[pluginID]
            return next
          })
          if (status.downloading) {
            runtimeAssetActiveDownloads.current.add(pluginID)
            setRuntimeAssetProgress((current) => ({
              ...current,
              [pluginID]: status.download_progress ?? null,
            }))
          }
        })
        .catch(() => {
          if (!active) return
          setRuntimeAssetDownloads((current) => (
            current[pluginID] === 'downloading' || current[pluginID] === 'deleting'
          )
            ? current
            : { ...current, [pluginID]: 'error' })
        })
    }
    return () => {
      active = false
    }
  }, [getRuntimeAssetStatus, isDesktop])

  useEffect(() => {
    if (!getRuntimeAssetStatus) return
    const pluginIDs = FIXED_RUNTIME_ASSETS
      .map(({ pluginID }) => pluginID)
      .filter((pluginID) => runtimeAssetDownloads[pluginID] === 'downloading')
    if (pluginIDs.length === 0) return
    let active = true
    let timer: ReturnType<typeof setTimeout> | undefined
    const poll = async () => {
      await Promise.all(pluginIDs.map(async (pluginID) => {
        try {
          const status = await getRuntimeAssetStatus(pluginID)
          if (!active) return
          if (status.downloaded) {
            runtimeAssetActiveDownloads.current.delete(pluginID)
            setRuntimeAssetDownloads((current) => ({ ...current, [pluginID]: 'downloaded' }))
            setRuntimeAssetProgress((current) => {
              const next = { ...current }
              delete next[pluginID]
              return next
            })
            return
          }
          if (status.downloading) {
            runtimeAssetActiveDownloads.current.add(pluginID)
            setRuntimeAssetProgress((current) => ({
              ...current,
              [pluginID]: status.download_progress ?? null,
            }))
            return
          }
          if (!runtimeAssetActiveDownloads.current.delete(pluginID)) return
          setRuntimeAssetDownloads((current) => ({ ...current, [pluginID]: 'error' }))
          setRuntimeAssetProgress((current) => {
            const next = { ...current }
            delete next[pluginID]
            return next
          })
        } catch {
          // The PUT request owns download failure; polling is best-effort UI state.
        }
      }))
      if (active) timer = setTimeout(() => void poll(), 250)
    }
    void poll()
    return () => {
      active = false
      if (timer) clearTimeout(timer)
    }
  }, [getRuntimeAssetStatus, runtimeAssetDownloads])

  const updateStatus = clientUpdate?.status ?? 'unavailable'
  const updateVersion = clientUpdate?.availableVersion ?? clientUpdate?.currentVersion ?? '—'
  const updateProgress = typeof clientUpdate?.progress === 'number'
    ? ` · ${Math.round(clientUpdate.progress)}%`
    : ''
  const updateHint = updateStatus === 'checking'
    ? t('settings.updateChecking')
    : updateStatus === 'downloading'
      ? t('settings.updateDownloading', { version: updateVersion, progress: updateProgress })
      : updateStatus === 'ready'
        ? t('settings.updateReady', { version: updateVersion })
        : updateStatus === 'current'
          ? t('settings.updateLatest', { version: updateVersion })
          : updateStatus === 'error'
            ? t('settings.updateError')
            : updateStatus === 'unavailable'
              ? t('settings.updateUnavailable')
              : t('settings.updateCurrent', { version: updateVersion })
  const updateAction = updateStatus === 'ready'
    ? t('settings.updateRestartAction')
    : updateStatus === 'error'
      ? t('settings.updateDownloadAction')
      : updateStatus === 'checking'
        ? t('settings.updateChecking')
        : updateStatus === 'downloading'
          ? `${Math.round(clientUpdate?.progress ?? 0)}%`
          : t('settings.updateCheckAction')

  const selectSection = (id: SettingsSectionID) => {
    setActiveSection(id)
    const scrollRoot = settingsScrollRef.current
    const section = document.getElementById(`settings-${id}`)
    if (!section) return
    if (!scrollRoot) {
      section.scrollIntoView?.({ block: 'start' })
      return
    }

    const rootTop = scrollRoot.getBoundingClientRect().top
    const sectionTop = section.getBoundingClientRect().top - rootTop + scrollRoot.scrollTop
    const nextTop = Math.max(0, sectionTop - 12)
    if (typeof scrollRoot.scrollTo === 'function') {
      scrollRoot.scrollTo({
        top: nextTop,
        behavior: 'smooth',
      })
    } else {
      scrollRoot.scrollTop = nextTop
    }
  }

  const downloadRuntimeAsset = useCallback(async (pluginID: FixedRuntimeAssetPluginID) => {
    if (!onDownloadRuntimeAsset) return
    runtimeAssetActiveDownloads.current.delete(pluginID)
    setRuntimeAssetDownloads((current) => ({ ...current, [pluginID]: 'downloading' }))
    setRuntimeAssetProgress((current) => ({ ...current, [pluginID]: null }))
    try {
      await onDownloadRuntimeAsset(pluginID)
      setRuntimeAssetDownloads((current) => ({ ...current, [pluginID]: 'downloaded' }))
    } catch {
      setRuntimeAssetDownloads((current) => ({ ...current, [pluginID]: 'error' }))
    } finally {
      runtimeAssetActiveDownloads.current.delete(pluginID)
      setRuntimeAssetProgress((current) => {
        const next = { ...current }
        delete next[pluginID]
        return next
      })
    }
  }, [onDownloadRuntimeAsset])

  const removeRuntimeAsset = useCallback(async (pluginID: FixedRuntimeAssetPluginID) => {
    if (!onRemoveRuntimeAsset) return
    setRuntimeAssetDownloads((current) => ({ ...current, [pluginID]: 'deleting' }))
    try {
      await onRemoveRuntimeAsset(pluginID)
      setRuntimeAssetDownloads((current) => {
        const next = { ...current }
        delete next[pluginID]
        return next
      })
    } catch {
      setRuntimeAssetDownloads((current) => ({ ...current, [pluginID]: 'delete_error' }))
    }
  }, [onRemoveRuntimeAsset])

  return { activeSection, adv, advancedSettingsReady, agentSettings, clearMemoryConfirmOpen, clearingMemory, downloadRuntimeAsset, importInputRef, isDesktop, locale, memoryEnabled, navItems, onAgentSettingsChange, onClearMemory, onDownloadRuntimeAsset, onExportLocalData, onImportLocalData, onModelServiceAddOpened, onModelServicesChange, onRemoveRuntimeAsset, openModelServiceAdd, removeRuntimeAsset, runtimeAssetDeleteConfirm, runtimeAssetDownloads, runtimeAssetProgress, runtimeConnection, selectSection, setAdv, setClearMemoryConfirmOpen, setClearingMemory, setClientUpdate, setLocale, setRuntimeAssetDeleteConfirm, settingsScrollRef, t, updateAction, updateActiveSectionFromScroll, updateHint, updateStatus }
}

export function SettingsView(props: Parameters<typeof useSettingsViewModel>[0]) {
  return <SettingsViewContent view={useSettingsViewModel(props)} />
}

function SettingsViewContent({ view }: { view: ReturnType<typeof useSettingsViewModel> }) {
  const { activeSection, adv, advancedSettingsReady, agentSettings, clearMemoryConfirmOpen, clearingMemory, downloadRuntimeAsset, importInputRef, isDesktop, locale, memoryEnabled, navItems, onAgentSettingsChange, onClearMemory, onDownloadRuntimeAsset, onExportLocalData, onImportLocalData, onModelServiceAddOpened, onModelServicesChange, onRemoveRuntimeAsset, openModelServiceAdd, removeRuntimeAsset, runtimeAssetDeleteConfirm, runtimeAssetDownloads, runtimeAssetProgress, runtimeConnection, selectSection, setAdv, setClearMemoryConfirmOpen, setClearingMemory, setClientUpdate, setLocale, setRuntimeAssetDeleteConfirm, settingsScrollRef, t, updateAction, updateActiveSectionFromScroll, updateHint, updateStatus } = view
  const runtimeAssetDeleteName = FIXED_RUNTIME_ASSETS.find(
    ({ pluginID }) => pluginID === runtimeAssetDeleteConfirm,
  )?.name
  return (
    <section className="workspace">
      <header className="topbar topbar-page">
        <div className="chat-toolbar-title">
          <span>{t('sidebar.settings')}</span>
        </div>
      </header>

      <div ref={settingsScrollRef} className="skills-scroll settings-scroll" onScroll={updateActiveSectionFromScroll}>
        <div className="settings-layout">
          <nav className="settings-nav" aria-label={t('settings.navAria')}>
            {navItems.map((item) => (
              <button
                key={item.id}
                type="button"
                className={`settings-nav-item${activeSection === item.id ? ' active' : ''}`}
                aria-current={activeSection === item.id ? 'page' : undefined}
                onClick={() => selectSection(item.id)}
              >
                {item.label}
              </button>
            ))}
          </nav>

          <div className="settings-main-scroll">
            <div className="settings-main">
              {isDesktop ? (
                <ModelServicesSettings
                  config={runtimeConnection}
                  openAdd={openModelServiceAdd}
                  onOpenAdd={onModelServiceAddOpened}
                  onChanged={onModelServicesChange}
                />
              ) : null}

              <SettingsSection id="agent" title={t('settings.group.agent')}>
                <SettingRow label={t('sidebar.agentSettings.memory.label')} hint={t('sidebar.agentSettings.memory.hint')}>
                  <Switch
                    checked={memoryEnabled}
                    aria-label={t('sidebar.agentSettings.memory.label')}
                    onCheckedChange={(checked) =>
                      onAgentSettingsChange({ ...agentSettings, memory: checked ? 'on' : 'off' })
                    }
                  />
                </SettingRow>
                {isDesktop ? (
                  <>
                    <SettingRow label={t('sidebar.agentSettings.advanced.subagents.label')} hint={t('sidebar.agentSettings.advanced.subagents.hint')}>
                      <Switch
                        disabled={!advancedSettingsReady}
                        checked={adv.subagents ?? true}
                        aria-label={t('sidebar.agentSettings.advanced.subagents.label')}
                        onCheckedChange={(checked) => setAdv({ subagents: checked })}
                      />
                    </SettingRow>
                    <SettingRow label={t('sidebar.agentSettings.advanced.browserHeadless.label')} hint={t('sidebar.agentSettings.advanced.browserHeadless.hint')}>
                      <Switch
                        disabled={!advancedSettingsReady}
                        checked={adv.browserHeadless ?? true}
                        aria-label={t('sidebar.agentSettings.advanced.browserHeadless.label')}
                        onCheckedChange={(checked) => setAdv({ browserHeadless: checked })}
                      />
                    </SettingRow>
                    <SettingRow label={t('sidebar.agentSettings.advanced.inputGuard.label')} hint={t('sidebar.agentSettings.advanced.inputGuard.hint')}>
                      <Select
                        disabled={!advancedSettingsReady}
                        value={adv.inputGuard ?? '__default__'}
                        onValueChange={(value) =>
                          setAdv({ inputGuard: value === '__default__' ? undefined : (value as 'off' | 'observe' | 'block') })
                        }
                      >
                        <SelectTrigger className="settings-select-trigger" aria-label={t('sidebar.agentSettings.advanced.inputGuard.label')}>
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectItem value="__default__">{t('sidebar.agentSettings.advanced.default')}</SelectItem>
                          <SelectItem value="off">Off</SelectItem>
                          <SelectItem value="observe">Observe</SelectItem>
                          <SelectItem value="block">Block</SelectItem>
                        </SelectContent>
                      </Select>
                    </SettingRow>
                  </>
                ) : null}
              </SettingsSection>

              <SettingsSection id="general" title={t('settings.group.general')}>
                {isDesktop ? (
                  <SettingRow label={t('settings.update')} hint={updateHint}>
                    <SettingsRowButton
                      disabled={!window.shejaneClient?.updates || ['checking', 'downloading', 'unavailable'].includes(updateStatus)}
                      onClick={() => {
                        const updates = window.shejaneClient?.updates
                        if (!updates) return
                        const action = updateStatus === 'ready'
                          ? updates.install()
                          : updateStatus === 'error'
                            ? window.shejaneClient?.openExternal?.('https://github.com/jimmyrogue/SheJane/releases')
                            : updates.check()
                        if (!action) return
                        void action.catch(() => setClientUpdate((current) => current
                          ? { ...current, status: 'error' }
                          : null))
                      }}
                    >
                      {updateAction}
                    </SettingsRowButton>
                  </SettingRow>
                ) : null}
                {isDesktop ? FIXED_RUNTIME_ASSETS.map(({ pluginID, name }) => {
                  const status = runtimeAssetDownloads[pluginID]
                  const progress = runtimeAssetProgress[pluginID]
                  const downloaded = status === 'downloaded' || status === 'deleting' || status === 'delete_error'
                  const action = status === 'downloading'
                    ? t('settings.runtimeAsset.downloading')
                    : status === 'deleting'
                      ? t('settings.runtimeAsset.deleting')
                      : downloaded
                        ? t('settings.runtimeAsset.delete')
                      : t('settings.runtimeAsset.download')
                  const ariaLabel = status === 'deleting'
                    ? t('settings.runtimeAsset.deletingAria', { name })
                    : status === 'downloading'
                      ? t('settings.runtimeAsset.downloadingAria', { name })
                      : downloaded
                        ? t('settings.runtimeAsset.deleteAria', { name })
                      : t('settings.runtimeAsset.downloadAria', { name })
                  const liveStatus = status === 'delete_error'
                    ? t('settings.runtimeAsset.deleteErrorAria', { name })
                    : status === 'deleting'
                      ? ariaLabel
                      : downloaded
                      ? t('settings.runtimeAsset.downloadedAria', { name })
                      : ariaLabel
                  return (
                    <SettingRow
                      key={pluginID}
                      label={name}
                      hint={status === 'error'
                        ? t('settings.runtimeAsset.error')
                        : status === 'delete_error'
                          ? t('settings.runtimeAsset.deleteError')
                          : t('settings.runtimeAsset.hint')}
                    >
                      {downloaded && status !== 'deleting' ? (
                        <span className="settings-row-value">{t('settings.runtimeAsset.downloaded')}</span>
                      ) : null}
                      {status === 'downloading' ? (
                        <div className="settings-runtime-asset-progress">
                          <progress
                            aria-label={t('settings.runtimeAsset.progressAria', { name })}
                            max={100}
                            value={typeof progress === 'number' ? progress : undefined}
                          />
                          <span>{typeof progress === 'number' ? `${progress}%` : action}</span>
                        </div>
                      ) : (
                        <SettingsRowButton
                          ariaLabel={ariaLabel}
                          danger={downloaded}
                          disabled={status === 'deleting' || (
                            downloaded ? !onRemoveRuntimeAsset : !onDownloadRuntimeAsset
                          )}
                          onClick={() => void (downloaded
                            ? setRuntimeAssetDeleteConfirm(pluginID)
                            : downloadRuntimeAsset(pluginID))}
                        >
                          {action}
                        </SettingsRowButton>
                      )}
                      <span className="sr-only" role="status" aria-live="polite">
                        {liveStatus}
                      </span>
                    </SettingRow>
                  )
                }) : null}
                <SettingRow label={t('settings.language')}>
                  <Select value={locale} onValueChange={(value) => setLocale(value as Locale)}>
                    <SelectTrigger className="settings-language-select" aria-label={t('settings.language')}>
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="zh">中文</SelectItem>
                      <SelectItem value="en">English</SelectItem>
                    </SelectContent>
                  </Select>
                </SettingRow>
              </SettingsSection>

              <SettingsSection id="data" title={t('settings.group.dataSecurity')}>
                <SettingRow label={t('settings.import')} hint={t('settings.importHint')}>
                  <SettingsRowButton onClick={() => importInputRef.current?.click()}>
                    {t('settings.importAction')}
                  </SettingsRowButton>
                </SettingRow>
                <input
                  ref={importInputRef}
                  type="file"
                  accept="application/json"
                  hidden
                  onChange={(event) => {
                    onImportLocalData(event.currentTarget.files?.[0])
                    event.currentTarget.value = ''
                  }}
                />
                {onExportLocalData ? (
                  <SettingRow label={t('settings.export')} hint={t('settings.exportHint')}>
                    <SettingsRowButton onClick={onExportLocalData}>
                      {t('settings.exportAction')}
                    </SettingsRowButton>
                  </SettingRow>
                ) : null}
                {onClearMemory ? (
                  <SettingRow
                    label={t('sidebar.agentSettings.memory.clearAction')}
                    hint={t('sidebar.agentSettings.memory.clearHint')}
                    danger
                  >
                    <SettingsRowButton danger onClick={() => setClearMemoryConfirmOpen(true)}>
                      {t('settings.clearAction')}
                    </SettingsRowButton>
                  </SettingRow>
                ) : null}
              </SettingsSection>
            </div>
          </div>
        </div>
      </div>

      <AlertDialog open={clearMemoryConfirmOpen} onOpenChange={setClearMemoryConfirmOpen}>
        <AlertDialogContent className="conversation-delete-dialog">
          <AlertDialogHeader className="conversation-delete-header">
            <AlertDialogMedia className="conversation-delete-media">
              <IconTrash aria-hidden="true" />
            </AlertDialogMedia>
            <AlertDialogTitle>{t('sidebar.agentSettings.memory.clearConfirmTitle')}</AlertDialogTitle>
            <AlertDialogDescription>{t('sidebar.agentSettings.memory.clearConfirmBody')}</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter className="conversation-delete-footer">
            <AlertDialogCancel variant="outline" autoFocus>
              <span className="conversation-delete-button-label">{t('sidebar.dialog.cancel')}</span>
            </AlertDialogCancel>
            <AlertDialogAction
              variant="destructive"
              disabled={clearingMemory || !onClearMemory}
              onClick={async (event) => {
                event.preventDefault()
                if (!onClearMemory) return
                setClearingMemory(true)
                try {
                  await onClearMemory()
                } finally {
                  setClearingMemory(false)
                  setClearMemoryConfirmOpen(false)
                }
              }}
            >
              <span className="conversation-delete-button-label">
                {t('sidebar.agentSettings.memory.clearConfirmAction')}
              </span>
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <AlertDialog
        open={runtimeAssetDeleteConfirm !== null}
        onOpenChange={(open) => {
          if (!open) setRuntimeAssetDeleteConfirm(null)
        }}
      >
        <AlertDialogContent className="conversation-delete-dialog">
          <AlertDialogHeader className="conversation-delete-header">
            <AlertDialogMedia className="conversation-delete-media">
              <IconTrash aria-hidden="true" />
            </AlertDialogMedia>
            <AlertDialogTitle>
              {t('settings.runtimeAsset.deleteConfirmTitle', { name: runtimeAssetDeleteName ?? '' })}
            </AlertDialogTitle>
            <AlertDialogDescription>
              {t('settings.runtimeAsset.deleteConfirmBody', { name: runtimeAssetDeleteName ?? '' })}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter className="conversation-delete-footer">
            <AlertDialogCancel variant="outline" autoFocus>
              <span className="conversation-delete-button-label">{t('sidebar.dialog.cancel')}</span>
            </AlertDialogCancel>
            <AlertDialogAction
              variant="destructive"
              onClick={() => {
                if (!runtimeAssetDeleteConfirm) return
                const pluginID = runtimeAssetDeleteConfirm
                setRuntimeAssetDeleteConfirm(null)
                void removeRuntimeAsset(pluginID)
              }}
            >
              <span className="conversation-delete-button-label">
                {t('settings.runtimeAsset.deleteConfirmAction')}
              </span>
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

    </section>
  )
}
