import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useI18n } from '@/shared/i18n/i18n'
import type {
  AdvancedAgentSettings,
  AgentSettings,
  FixedRuntimeAssetPluginID,
  FixedRuntimeAssetStatus,
  RuntimeAssetCleanupResult,
  RuntimeAssetCleanupScope,
  RuntimeAssetStorage,
  RuntimeConnection,
} from '@/runtime/client'

export type SettingsSectionID = 'models' | 'agent' | 'general' | 'data'

const SETTINGS_SECTION_TOP_OFFSET = 72
export const FIXED_RUNTIME_ASSETS: ReadonlyArray<{
  pluginID: FixedRuntimeAssetPluginID
  name: string
}> = [
  { pluginID: 'org.shejane.browser-qa', name: 'Browser QA' },
  { pluginID: 'org.shejane.ocr', name: 'RapidOCR' },
]

export function useSettingsViewModel({
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
  getRuntimeAssetStorage,
  onCleanupRuntimeAssets,
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
  getRuntimeAssetStorage?: () => Promise<RuntimeAssetStorage>
  onCleanupRuntimeAssets?: (scope: RuntimeAssetCleanupScope) => Promise<RuntimeAssetCleanupResult>
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
    Record<FixedRuntimeAssetPluginID, 'unavailable' | 'downloading' | 'downloaded' | 'deleting' | 'error' | 'delete_error'>
  >>({})
  const [runtimeAssetProgress, setRuntimeAssetProgress] = useState<Partial<
    Record<FixedRuntimeAssetPluginID, number | null>
  >>({})
  const [runtimeAssetStorage, setRuntimeAssetStorage] = useState<RuntimeAssetStorage | null>(null)
  const [runtimeAssetStorageError, setRuntimeAssetStorageError] = useState<'load' | 'cleanup' | null>(null)
  const [runtimeAssetCleanupConfirm, setRuntimeAssetCleanupConfirm] = useState<RuntimeAssetCleanupScope | null>(null)
  const [cleaningRuntimeAssets, setCleaningRuntimeAssets] = useState<RuntimeAssetCleanupScope | null>(null)

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
            if (status.available === false) next[pluginID] = 'unavailable'
            else if (status.downloaded) next[pluginID] = 'downloaded'
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
    if (!isDesktop || !getRuntimeAssetStorage) return
    let active = true
    setRuntimeAssetStorageError(null)
    void getRuntimeAssetStorage()
      .then((storage) => {
        if (active) setRuntimeAssetStorage(storage)
      })
      .catch(() => {
        if (active) setRuntimeAssetStorageError('load')
      })
    return () => {
      active = false
    }
  }, [getRuntimeAssetStorage, isDesktop])

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

  const cleanupRuntimeAssets = useCallback(async (scope: RuntimeAssetCleanupScope) => {
    if (!onCleanupRuntimeAssets) return
    setCleaningRuntimeAssets(scope)
    setRuntimeAssetStorageError(null)
    try {
      const result = await onCleanupRuntimeAssets(scope)
      setRuntimeAssetStorage(result)
      if (scope === 'all') {
        runtimeAssetActiveDownloads.current.clear()
        setRuntimeAssetDownloads({})
        setRuntimeAssetProgress({})
      }
    } catch {
      setRuntimeAssetStorageError('cleanup')
    } finally {
      setCleaningRuntimeAssets(null)
      setRuntimeAssetCleanupConfirm(null)
    }
  }, [onCleanupRuntimeAssets])

  return { activeSection, adv, advancedSettingsReady, agentSettings, cleanupRuntimeAssets, cleaningRuntimeAssets, clearMemoryConfirmOpen, clearingMemory, downloadRuntimeAsset, getRuntimeAssetStorage, importInputRef, isDesktop, locale, memoryEnabled, navItems, onAgentSettingsChange, onCleanupRuntimeAssets, onClearMemory, onDownloadRuntimeAsset, onExportLocalData, onImportLocalData, onModelServiceAddOpened, onModelServicesChange, onRemoveRuntimeAsset, openModelServiceAdd, removeRuntimeAsset, runtimeAssetCleanupConfirm, runtimeAssetDeleteConfirm, runtimeAssetDownloads, runtimeAssetProgress, runtimeAssetStorage, runtimeAssetStorageError, runtimeConnection, selectSection, setAdv, setClearMemoryConfirmOpen, setClearingMemory, setClientUpdate, setLocale, setRuntimeAssetCleanupConfirm, setRuntimeAssetDeleteConfirm, settingsScrollRef, t, updateAction, updateActiveSectionFromScroll, updateHint, updateStatus }
}
