import { useEffect, useRef, useState } from 'react'
import type { ModelOption } from '@/features/chat/components/ModeSelector'
import { advancedSettingsFromRuntime, advancedSettingsPatchToRuntime } from '@/features/settings/runtimeSettings'
import type { Translator } from '@/shared/i18n/i18n'
import type { ChatMode } from '@/shared/local-data/types'
import {
  getRuntimeSettings,
  hasRuntimeAuthorization,
  listLocalRuntimeModels,
  listModelCapabilityBindings,
  listModelServices,
  parseRuntimeModelSpec,
  refreshModelService,
  setModelCapabilityBinding,
  updateRuntimeSettings,
  type AgentSettings,
} from '@/runtime/client'
import { readAgentSettings, readChatMode, writeAgentSettings, writeChatMode } from '../appStorage'
import { chooseAvailableMode } from '../modelSelection'
import { runtimeStore, runtimeStoreActions } from '../state/runtimeStore'
import { useStore } from '../state/store'

export function useRuntimeModelSettings({
  t,
  setNotice,
}: {
  t: Translator
  setNotice: (message: string) => void
}) {
  const {
    runtime,
    connection: runtimeConnection,
    models,
    imageMode,
    imageModels,
    catalogVersion: modelCatalogVersion,
    settingsConfig: runtimeSettingsConfig,
  } = useStore(runtimeStore)
  const [mode, setMode] = useState<ChatMode>(readChatMode)
  const [agentSettings, setAgentSettings] = useState<Required<AgentSettings>>(readAgentSettings)
  const runtimeSettingsWriteRef = useRef<Promise<void> | null>(null)

  function changeAgentSettings(next: Required<AgentSettings>) {
    const normalized = { ...next, skills: 'on' as const, mcp: 'on' as const }
    const runtimePatch = advancedSettingsPatchToRuntime(agentSettings.advanced, normalized.advanced)
    const runtimeSettingsReady = runtimeSettingsConfig === runtimeConnection && Boolean(runtime?.online)
    setAgentSettings(normalized)
    writeAgentSettings(normalized)
    if (!runtimeConnection || !runtimeSettingsReady || Object.keys(runtimePatch).length === 0) return

    const config = runtimeConnection
    runtimeSettingsWriteRef.current = (runtimeSettingsWriteRef.current ?? Promise.resolve())
      .catch(() => undefined)
      .then(async () => {
        const settings = await updateRuntimeSettings(runtimePatch, config)
        if (runtimeStore.getState().connection === config) {
          setAgentSettings((current) => ({
            ...current,
            advanced: advancedSettingsFromRuntime(settings),
          }))
        }
      })
      .catch(async (error) => {
        setNotice(error instanceof Error ? error.message : String(error))
        try {
          const settings = await getRuntimeSettings(config)
          if (runtimeStore.getState().connection === config) {
            setAgentSettings((current) => ({
              ...current,
              advanced: advancedSettingsFromRuntime(settings),
            }))
            runtimeStoreActions.setSettingsConfig(config)
          }
        } catch {
          if (runtimeStore.getState().connection === config) {
            runtimeStoreActions.setSettingsConfig(null)
          }
        }
      })
  }

  useEffect(() => {
    if (!runtimeConnection) {
      runtimeStoreActions.setModels([])
      runtimeStoreActions.setImageMode(undefined)
      runtimeStoreActions.setImageModels([])
      return
    }
    let cancelled = false
    void listLocalRuntimeModels(runtimeConnection).then(async (localCatalog) => {
      if (cancelled) return
      const catalog: ModelOption[] = localCatalog.flatMap((model) => {
        const spec = parseRuntimeModelSpec(model.spec)
        if (!model.available || !spec) return []
        return [{
          id: spec,
          label: model.display_name,
          imageInputs: Boolean(model.image_inputs),
          description: t('settings.modelServices.localDescription'),
          vendor: model.service_name,
          vendor_info: t('settings.modelServices.localVendorInfo'),
          recommended: model.recommended,
          reasoningModes: model.reasoning?.modes ?? ['off'],
          defaultReasoningMode: model.reasoning?.default_mode ?? 'off',
        }]
      })
      const savedMode = readChatMode()
      const defaultMode = chooseAvailableMode(catalog, savedMode)
      runtimeStoreActions.setModels(catalog)
      if (defaultMode && defaultMode !== savedMode) writeChatMode(defaultMode)
      setMode((current) => chooseAvailableMode(catalog, current, defaultMode))

      try {
        const [capabilityBindings, modelServices] = await Promise.all([
          listModelCapabilityBindings(runtimeConnection),
          listModelServices(runtimeConnection),
        ])
        if (cancelled) return
        const configuredConnections = new Set<string>()
        for (const service of modelServices) {
          if (service.credential_configured) configuredConnections.add(service.id)
        }
        const imageCatalog: ModelOption[] = localCatalog.flatMap((model) => {
          const spec = parseRuntimeModelSpec(model.spec)
          const imageCapability = model.capabilities.find(
            (capability) => capability.capability === 'image_generation'
              && capability.verification === 'verified',
          )
          if (!spec || !imageCapability || !configuredConnections.has(model.connection_id)) return []
          return [{
            id: spec,
            label: model.display_name,
            imageInputs: false,
            description: t('composer.mode.imageGeneration'),
            vendor: model.service_name,
            vendor_info: t('settings.modelServices.localVendorInfo'),
            recommended: model.recommended,
            reasoningModes: ['off'],
          }]
        })
        const imageBinding = capabilityBindings.find(
          (binding) => binding.capability === 'image_generation' && binding.status === 'ready',
        )
        const boundImageMode = imageBinding
          ? parseRuntimeModelSpec(imageBinding.model_spec)
          : undefined
        runtimeStoreActions.setImageModels(imageCatalog)
        runtimeStoreActions.setImageMode(
          boundImageMode && imageCatalog.some((model) => model.id === boundImageMode)
            ? boundImageMode
            : undefined,
        )
      } catch {
        if (!cancelled) {
          runtimeStoreActions.setImageModels([])
          runtimeStoreActions.setImageMode(undefined)
        }
      }
    }).catch(() => {
      runtimeStoreActions.setModels([])
      runtimeStoreActions.setImageModels([])
      runtimeStoreActions.setImageMode(undefined)
    })
    return () => {
      cancelled = true
    }
  }, [runtimeConnection, modelCatalogVersion, t])

  async function changeImageMode(next: ChatMode): Promise<void> {
    if (!runtimeConnection) return
    setNotice('')
    try {
      const binding = await setModelCapabilityBinding(
        'image_generation',
        { model_spec: next },
        runtimeConnection,
      )
      runtimeStoreActions.setImageMode(parseRuntimeModelSpec(binding.model_spec))
    } catch (error) {
      setNotice(error instanceof Error ? error.message : String(error))
    }
  }

  useEffect(() => {
    runtimeStoreActions.setSettingsConfig(null)
    if (!runtime?.online || !runtimeConnection || !hasRuntimeAuthorization(runtimeConnection)) return
    let cancelled = false
    void getRuntimeSettings(runtimeConnection)
      .then((settings) => {
        if (!cancelled) {
          setAgentSettings((current) => ({
            ...current,
            advanced: advancedSettingsFromRuntime(settings),
          }))
          runtimeStoreActions.setSettingsConfig(runtimeConnection)
        }
      })
      .catch((error) => {
        if (!cancelled) setNotice(error instanceof Error ? error.message : String(error))
      })
    return () => {
      cancelled = true
    }
  }, [runtime?.online, runtimeConnection, setNotice])

  async function refreshCurrentModel() {
    const selected = parseRuntimeModelSpec(mode)
    const connectionID = selected?.split(':', 3)[1]
    if (!runtimeConnection || !connectionID) return
    try {
      const services = await listModelServices(runtimeConnection)
      if (!services.some((service) => service.id === connectionID)) return
      await refreshModelService(connectionID, runtimeConnection)
      runtimeStoreActions.bumpCatalogVersion()
    } catch {
      // Cached models remain visible; settings exposes the actionable error.
    }
  }

  return {
    agentSettings,
    changeAgentSettings,
    changeImageMode,
    imageMode,
    imageModels,
    mode,
    models,
    refreshCurrentModel,
    runtime,
    runtimeConnection,
    runtimeSettingsConfig,
    setMode,
  }
}
