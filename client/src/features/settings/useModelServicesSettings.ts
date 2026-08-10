import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react'
import {
  connectModelService,
  deleteModelService,
  getSheJaneAuthorization,
  reconnectModelService,
  refreshModelService,
  RuntimeHTTPError,
  startSheJaneAuthorization,
  updateCentralDiagnostics,
  verifyModelServiceModel,
  type ModelServiceConnection,
  type ModelServicePreset,
  type RuntimeConnection,
} from '@/runtime/client'
import { useI18n } from '@/shared/i18n/i18n'
import { defaultModelProtocol } from './modelServiceModels'
import { useModelServiceCatalog } from './useModelServiceCatalog'
import { useModelServiceOperation } from './useModelServiceOperation'

type ConnectionFieldErrors = Partial<Record<'apiKey' | 'baseURL' | 'name', string>>
type ModelTestState = 'testing' | 'verified' | 'failed'

export interface ModelServicesSettingsProps {
  config?: RuntimeConnection | null
  openAdd?: boolean
  onOpenAdd?: () => void
  onChanged?: () => void
}

function isValidServiceURL(value: string) {
  try {
    const protocol = new URL(value).protocol
    return protocol === 'http:' || protocol === 'https:'
  } catch {
    return false
  }
}

export function useModelServicesSettings({
  config,
  openAdd,
  onOpenAdd,
  onChanged,
}: ModelServicesSettingsProps) {
  const { t } = useI18n()
  const [error, setError] = useState('')
  const {
    bindings,
    diagnostics,
    load,
    presets,
    services,
    setDiagnostics,
  } = useModelServiceCatalog(config)
  const reloadAfterExternalCompletion = useCallback(() => {
    void load().catch((reason) => setError(reason instanceof Error ? reason.message : String(reason)))
  }, [load])
  const {
    active: operationActive,
    begin: beginOperation,
    busy,
    finish: finishOperation,
    update: updateOperation,
  } = useModelServiceOperation(config, reloadAfterExternalCompletion)
  const [adding, setAdding] = useState(false)
  const [selected, setSelected] = useState<ModelServicePreset>()
  const [reconnecting, setReconnecting] = useState<ModelServiceConnection>()
  const [apiKey, setAPIKey] = useState('')
  const [region, setRegion] = useState('cn')
  const [name, setName] = useState('')
  const [baseURL, setBaseURL] = useState('')
  const [adapterID, setAdapterID] = useState<ModelServiceConnection['adapter_id']>()
  const [showAdvanced, setShowAdvanced] = useState(false)
  const [viewingService, setViewingService] = useState<ModelServiceConnection>()
  const [managingService, setManagingService] = useState<ModelServiceConnection>()
  const [connectionTestStates, setConnectionTestStates] = useState<Record<string, ModelTestState>>({})
  const [fieldErrors, setFieldErrors] = useState<ConnectionFieldErrors>({})
  const authorizationRun = useRef(0)
  const defaultBaseURL = reconnecting?.base_url
    ?? selected?.regions.find((item) => item.id === region)?.base_url
    ?? selected?.regions.find((item) => item.default)?.base_url
    ?? selected?.regions[0]?.base_url
    ?? ''

  const clearConnectionTestState = (connectionID: string) => {
    setConnectionTestStates((current) => {
      const next = { ...current }
      delete next[connectionID]
      return next
    })
  }

  useEffect(() => {
    void load().catch((reason) => setError(reason instanceof Error ? reason.message : String(reason)))
  }, [load])

  const openAddDialog = () => {
    if (operationActive) return
    setAdding(true)
    setSelected(undefined)
    setReconnecting(undefined)
    setError('')
    setFieldErrors({})
  }

  useEffect(() => {
    if (!config || !openAdd) return
    setAdding(true)
    setSelected(undefined)
    setReconnecting(undefined)
    setError('')
    setFieldErrors({})
    onOpenAdd?.()
  }, [config, onOpenAdd, openAdd])

  const authorizeOfficial = async () => {
    if (!config) return
    const operation = beginOperation('authorize')
    if (operation === undefined) return
    const run = ++authorizationRun.current
    setError('')
    try {
      const started = await startSheJaneAuthorization(config)
      if (run !== authorizationRun.current) return
      if (!window.shejaneClient?.openExternal) {
        throw new Error(t('settings.modelServices.authorization.browserUnavailable'))
      }
      updateOperation(operation, 'authorize:pending')
      await window.shejaneClient.openExternal(started.authorization_url)
      while (run === authorizationRun.current) {
        const status = await getSheJaneAuthorization(started.authorization_id, config)
        if (run !== authorizationRun.current) return
        if (status.status === 'pending') {
          await new Promise((resolve) => globalThis.setTimeout(resolve, 750))
          continue
        }
        if (status.status !== 'succeeded' || !status.connection) {
          const key = status.status === 'denied'
            ? 'settings.modelServices.authorization.denied'
            : status.status === 'expired'
              ? 'settings.modelServices.authorization.expired'
              : 'settings.modelServices.authorization.failed'
          throw new Error(t(key))
        }
        onChanged?.()
        updateOperation(operation, 'authorize:syncing')
        let diagnosticsError = false
        let reloadError: unknown
        try {
          setDiagnostics(await updateCentralDiagnostics({
            enabled: true,
            connection_id: status.connection.id,
            success_sample_rate: 0,
          }, config))
        } catch {
          diagnosticsError = true
        }
        try {
          await load()
        } catch (reason) {
          reloadError = reason
        }
        if (run !== authorizationRun.current) return
        setAdding(false)
        setSelected(undefined)
        if (reloadError) {
          setError(reloadError instanceof Error ? reloadError.message : String(reloadError))
        } else if (diagnosticsError) {
          setError(t('settings.modelServices.authorization.diagnosticsFailed'))
        } else if (!status.connection.models.some((model) => (
          model.capabilities?.some((capability) => capability.capability === 'agent_chat')
          || model.capabilities?.length === 0
        ))) {
          setError(t('settings.modelServices.authorization.modelsUnavailable'))
        }
        return
      }
    } catch (reason) {
      if (run === authorizationRun.current) {
        setError(reason instanceof Error ? reason.message : String(reason))
      }
    } finally {
      finishOperation(operation)
    }
  }

  const openPreset = (preset: ModelServicePreset) => {
    if (operationActive) return
    const defaultRegion = preset.regions.find((item) => item.default) ?? preset.regions[0]
    setReconnecting(undefined)
    setSelected(preset)
    setAPIKey('')
    setRegion(defaultRegion?.id ?? 'custom')
    setName('')
    setBaseURL('')
    setAdapterID(undefined)
    setShowAdvanced(false)
    setError('')
    setFieldErrors({})
    if (preset.connection_method === 'browser_authorization') {
      void authorizeOfficial()
    }
  }

  const openReconnect = (service: ModelServiceConnection) => {
    if (operationActive) return
    setAdding(false)
    setSelected(undefined)
    setReconnecting(service)
    setAPIKey('')
    setBaseURL(service.base_url)
    setShowAdvanced(false)
    setError('')
    setFieldErrors({})
  }

  const toggleDiagnostics = async (service: ModelServiceConnection, enabled: boolean) => {
    if (!config) return
    const operation = beginOperation(`diagnostics:${service.id}`)
    if (operation === undefined) return
    setError('')
    try {
      setDiagnostics(await updateCentralDiagnostics({
        enabled,
        connection_id: enabled ? service.id : null,
        success_sample_rate: enabled ? diagnostics?.success_sample_rate ?? 0 : 0,
      }, config))
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      finishOperation(operation)
    }
  }

  const connect = async (event: FormEvent) => {
    event.preventDefault()
    if (!config || (!selected && !reconnecting)) return
    const nextErrors: ConnectionFieldErrors = {}
    const normalizedAPIKey = apiKey.trim()
    const normalizedBaseURL = baseURL.trim() || defaultBaseURL
    const normalizedName = name.trim()
    if (!normalizedAPIKey) nextErrors.apiKey = t('settings.modelServices.apiKeyRequired')
    if (!normalizedBaseURL) {
      nextErrors.baseURL = t('settings.modelServices.addressRequired')
    } else if (!isValidServiceURL(normalizedBaseURL)) {
      nextErrors.baseURL = t('settings.modelServices.addressInvalid')
    }
    if (selected?.id === 'custom' && !normalizedName) {
      nextErrors.name = t('settings.modelServices.serviceNameRequired')
    }
    setFieldErrors(nextErrors)
    if (Object.keys(nextErrors).length > 0) return

    const operation = beginOperation('connect')
    if (operation === undefined) return
    setError('')
    try {
      if (reconnecting) {
        await reconnectModelService(reconnecting.id, {
          api_key: normalizedAPIKey,
          base_url: normalizedBaseURL,
        }, config)
      } else if (selected) {
        await connectModelService({
          preset_id: selected.id,
          api_key: normalizedAPIKey,
          base_url: normalizedBaseURL,
          ...(selected.id === 'custom'
            ? {
                name: normalizedName,
                region: 'custom' as const,
                ...(adapterID ? { adapter_id: adapterID } : {}),
              }
            : { region: region as 'cn' | 'intl' }),
        }, config)
      }
      setAdding(false)
      setSelected(undefined)
      setReconnecting(undefined)
      setFieldErrors({})
      if (reconnecting) clearConnectionTestState(reconnecting.id)
      await load()
      onChanged?.()
    } catch (reason) {
      if (reason instanceof RuntimeHTTPError && reason.code === 'adapter_detection_failed') {
        setShowAdvanced(true)
      }
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      finishOperation(operation)
    }
  }

  const refresh = async (service: ModelServiceConnection) => {
    if (!config) return
    const operation = beginOperation(`refresh:${service.id}`)
    if (operation === undefined) return
    clearConnectionTestState(service.id)
    setError('')
    try {
      const refreshed = await refreshModelService(service.id, config)
      const nextServices = await load()
      if (managingService?.id === service.id) {
        setManagingService(nextServices.find((item) => item.id === service.id) ?? refreshed)
      }
      onChanged?.()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      finishOperation(operation)
    }
  }

  const remove = async (service: ModelServiceConnection) => {
    if (!config || !window.confirm(t('settings.modelServices.deleteConfirm', { name: service.name }))) return
    const operation = beginOperation(service.id)
    if (operation === undefined) return
    setError('')
    try {
      await deleteModelService(service.id, config)
      await load()
      onChanged?.()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      finishOperation(operation)
    }
  }

  const dialogPreset = selected ?? presets.find((preset) => preset.id === reconnecting?.preset_id)
  const officialPresets = presets.filter((preset) => preset.id !== 'custom')
  const customPreset = presets.find((preset) => preset.id === 'custom')

  const backToPicker = () => {
    if (operationActive && !busy.startsWith('authorize')) return
    setSelected(undefined)
    setAPIKey('')
    setBaseURL('')
    setName('')
    setError('')
    setFieldErrors({})
  }

  const openModelPicker = (service: ModelServiceConnection) => {
    setManagingService(service)
    setError('')
  }

  const testConnection = async (service: ModelServiceConnection) => {
    if (!config) return
    const operation = beginOperation(`test:${service.id}`)
    if (operation === undefined) return
    const candidates = service.models.filter((model) => (
      model.capabilities?.some((capability) => capability.capability === 'agent_chat')
      || model.capabilities?.length === 0
    ))
    const model = candidates.find((candidate) => (
      candidate.recommended_for?.includes('agent_chat')
    )) ?? candidates[0]
    if (!model) {
      setError(t('settings.modelServices.noChatModel'))
      setConnectionTestStates((current) => ({ ...current, [service.id]: 'failed' }))
      finishOperation(operation)
      return
    }
    const capability = model.capabilities?.find((item) => item.capability === 'agent_chat')
    setError('')
    setConnectionTestStates((current) => ({ ...current, [service.id]: 'testing' }))
    try {
      try {
        await verifyModelServiceModel(
          service.id,
          model.model_id,
          {
            capability: 'agent_chat',
            protocol: capability?.protocol ?? defaultModelProtocol(service, 'agent_chat'),
          },
          config,
        )
      } catch (reason) {
        setConnectionTestStates((current) => ({ ...current, [service.id]: 'failed' }))
        setError(reason instanceof Error ? reason.message : String(reason))
        return
      }
      setConnectionTestStates((current) => ({ ...current, [service.id]: 'verified' }))
      onChanged?.()
      try {
        await load()
      } catch (reason) {
        setError(reason instanceof Error ? reason.message : String(reason))
      }
    } finally {
      finishOperation(operation)
    }
  }

  return {
    adapterID, adding, apiKey, authorizeOfficial, backToPicker, baseURL, beginOperation,
    bindings, busy, config, connect, connectionTestStates, customPreset, defaultBaseURL,
    diagnostics, dialogPreset, error, fieldErrors, finishOperation, load, managingService,
    name, officialPresets, onChanged, openAddDialog, openModelPicker, openPreset,
    openReconnect, presets, reconnecting, refresh, region, remove, selected, services,
    setAPIKey, setAdapterID, setAdding, setBaseURL, setError, setFieldErrors,
    setManagingService, setName, setReconnecting, setRegion, setSelected,
    setViewingService, showAdvanced, t, testConnection, toggleDiagnostics,
    viewingModels: viewingService?.models ?? [], viewingService,
  }
}
