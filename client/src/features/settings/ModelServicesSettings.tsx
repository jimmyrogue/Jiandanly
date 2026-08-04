import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react'
import {
  IconArrowLeft,
  IconCheck,
  IconChevronRight,
  IconDots,
  IconExternalLink,
  IconInfoCircle,
  IconKey,
  IconLoader2,
  IconPlus,
  IconRefresh,
  IconSearch,
  IconTrash,
} from '@tabler/icons-react'
import {
  addModelServiceModel,
  connectModelService,
  deleteModelService,
  getCentralDiagnostics,
  getSheJaneAuthorization,
  listModelCapabilityBindings,
  listModelServicePresets,
  listModelServices,
  reconnectModelService,
  refreshModelService,
  RuntimeHTTPError,
  setModelCapabilityBinding,
  startSheJaneAuthorization,
  updateCentralDiagnostics,
  verifyModelServiceModel,
  type CentralDiagnosticsStatus,
  type ModelCapabilityBinding,
  type ModelServiceConnection,
  type ModelServicePreset,
  type RuntimeConnection,
  type VerifyModelServiceModelRequest,
} from '@/runtime/client'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import {
  Field,
  FieldDescription,
  FieldError,
  FieldGroup,
  FieldLabel,
} from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { Switch } from '@/components/ui/switch'
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { useI18n } from '@/shared/i18n/i18n'

type ConnectionFieldErrors = Partial<Record<'apiKey' | 'baseURL' | 'name', string>>
type ModelCapabilityName = VerifyModelServiceModelRequest['capability']
type ModelProtocol = VerifyModelServiceModelRequest['protocol']
type ModelTestState = 'testing' | 'verified' | 'failed'

function defaultModelProtocol(
  service: ModelServiceConnection,
  capability: ModelCapabilityName,
): ModelProtocol {
  if (capability === 'image_generation') return 'openai_images_generations'
  if (capability === 'image_editing') return 'openai_images_edits'
  if (service.adapter_id === 'google_genai') return 'google_generate_content'
  if (service.preset_id === 'openai') return 'openai_responses'
  return service.adapter_id === 'anthropic_messages'
    ? 'anthropic_messages'
    : 'openai_chat_completions'
}

function isValidServiceURL(value: string) {
  try {
    const protocol = new URL(value).protocol
    return protocol === 'http:' || protocol === 'https:'
  } catch {
    return false
  }
}

function capabilityTranslationKey(capability: ModelCapabilityName) {
  if (capability === 'agent_chat') return 'settings.modelServices.purpose.agentChat'
  if (capability === 'image_understanding') return 'settings.modelServices.purpose.imageUnderstanding'
  if (capability === 'image_generation') return 'settings.modelServices.purpose.imageGeneration'
  return 'settings.modelServices.purpose.imageEditing'
}

export function ModelServicesSettings({
  config,
  openAdd,
  onOpenAdd,
  onChanged,
}: {
  config?: RuntimeConnection | null
  openAdd?: boolean
  onOpenAdd?: () => void
  onChanged?: () => void
}) {
  const { t } = useI18n()
  const [presets, setPresets] = useState<ModelServicePreset[]>([])
  const [services, setServices] = useState<ModelServiceConnection[]>([])
  const [bindings, setBindings] = useState<ModelCapabilityBinding[]>([])
  const [diagnostics, setDiagnostics] = useState<CentralDiagnosticsStatus>()
  const [adding, setAdding] = useState(false)
  const [selected, setSelected] = useState<ModelServicePreset>()
  const [reconnecting, setReconnecting] = useState<ModelServiceConnection>()
  const [apiKey, setAPIKey] = useState('')
  const [region, setRegion] = useState('cn')
  const [name, setName] = useState('')
  const [baseURL, setBaseURL] = useState('')
  const [adapterID, setAdapterID] = useState<ModelServiceConnection['adapter_id']>()
  const [showAdvanced, setShowAdvanced] = useState(false)
  const [manualModels, setManualModels] = useState<Record<string, string>>({})
  const [modelCapabilities, setModelCapabilities] = useState<Record<string, ModelCapabilityName>>({})
  const [modelProtocols, setModelProtocols] = useState<Record<string, ModelProtocol>>({})
  const [viewingService, setViewingService] = useState<ModelServiceConnection>()
  const [managingService, setManagingService] = useState<ModelServiceConnection>()
  const [modelSearch, setModelSearch] = useState('')
  const [selectedModels, setSelectedModels] = useState<Record<string, boolean>>({})
  const [modelTestStates, setModelTestStates] = useState<Record<string, ModelTestState>>({})
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [fieldErrors, setFieldErrors] = useState<ConnectionFieldErrors>({})
  const authorizationRun = useRef(0)
  const defaultBaseURL = reconnecting?.base_url
    ?? selected?.regions.find((item) => item.id === region)?.base_url
    ?? selected?.regions.find((item) => item.default)?.base_url
    ?? selected?.regions[0]?.base_url
    ?? ''

  const load = useCallback(async () => {
    if (!config) {
      setPresets([])
      setServices([])
      setBindings([])
      setDiagnostics(undefined)
      return []
    }
    const [nextPresets, nextServices, nextBindings] = await Promise.all([
      listModelServicePresets(config),
      listModelServices(config),
      listModelCapabilityBindings(config),
    ])
    setPresets(nextPresets)
    setServices(nextServices)
    setBindings(nextBindings)
    try {
      setDiagnostics(await getCentralDiagnostics(config))
    } catch {
      setDiagnostics(undefined)
    }
    return nextServices
  }, [config])

  useEffect(() => {
    void load().catch((reason) => setError(reason instanceof Error ? reason.message : String(reason)))
  }, [load])

  useEffect(() => () => {
    authorizationRun.current += 1
  }, [])

  const openAddDialog = () => {
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
    const run = ++authorizationRun.current
    setBusy('authorize')
    setError('')
    try {
      const started = await startSheJaneAuthorization(config)
      if (run !== authorizationRun.current) return
      if (!window.shejaneClient?.openExternal) {
        throw new Error(t('settings.modelServices.authorization.browserUnavailable'))
      }
      setBusy('authorize:pending')
      await window.shejaneClient.openExternal(started.authorization_url)
      while (run === authorizationRun.current) {
        const status = await getSheJaneAuthorization(started.authorization_id, config)
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
        setBusy('authorize:syncing')
        let diagnosticsError = false
        try {
          setDiagnostics(await updateCentralDiagnostics({
            enabled: true,
            connection_id: status.connection.id,
            success_sample_rate: 0,
          }, config))
        } catch {
          diagnosticsError = true
        }
        await load()
        if (run !== authorizationRun.current) return
        setAdding(false)
        setSelected(undefined)
        if (diagnosticsError) {
          setError(t('settings.modelServices.authorization.diagnosticsFailed'))
        }
        onChanged?.()
        return
      }
    } catch (reason) {
      if (run === authorizationRun.current) {
        setError(reason instanceof Error ? reason.message : String(reason))
      }
    } finally {
      if (run === authorizationRun.current) setBusy('')
    }
  }

  const openPreset = (preset: ModelServicePreset) => {
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
    setBusy(`diagnostics:${service.id}`)
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
      setBusy('')
    }
  }

  const connect = async (event: FormEvent) => {
    event.preventDefault()
    if (!config || (!selected && !reconnecting)) return
    const nextErrors: ConnectionFieldErrors = {}
    const normalizedAPIKey = apiKey.trim()
    const normalizedBaseURL = baseURL.trim() || defaultBaseURL
    const normalizedName = name.trim()
    if (!normalizedAPIKey) {
      nextErrors.apiKey = t('settings.modelServices.apiKeyRequired')
    }
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

    setBusy('connect')
    setError('')
    try {
      let connectedService: ModelServiceConnection | undefined
      if (reconnecting) {
        await reconnectModelService(reconnecting.id, {
          api_key: normalizedAPIKey,
          base_url: normalizedBaseURL,
        }, config)
      } else if (selected) {
        connectedService = await connectModelService({
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
      await load()
      if (connectedService?.id) {
        setManagingService(connectedService)
        setModelSearch('')
        setSelectedModels({})
        setModelTestStates({})
      }
      onChanged?.()
    } catch (reason) {
      if (reason instanceof RuntimeHTTPError && reason.code === 'adapter_detection_failed') {
        setShowAdvanced(true)
      }
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      setBusy('')
    }
  }

  const refresh = async (service: ModelServiceConnection) => {
    if (!config) return
    setBusy(`refresh:${service.id}`)
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
      setBusy('')
    }
  }

  const remove = async (service: ModelServiceConnection) => {
    if (!config || !window.confirm(t('settings.modelServices.deleteConfirm', { name: service.name }))) return
    setBusy(service.id)
    setError('')
    try {
      await deleteModelService(service.id, config)
      await load()
      onChanged?.()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      setBusy('')
    }
  }

  const addManualModel = async (service: ModelServiceConnection) => {
    const modelID = manualModels[service.id]?.trim()
    if (!config || !modelID) return
    setBusy(service.id)
    setError('')
    try {
      const addedModel = await addModelServiceModel(service.id, { model_id: modelID }, config)
      setManualModels((current) => ({ ...current, [service.id]: '' }))
      const nextServices = await load()
      setManagingService(
        nextServices.find((item) => item.id === service.id)
        ?? (addedModel ? { ...service, models: [...service.models, addedModel] } : service),
      )
      onChanged?.()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      setBusy('')
    }
  }

  const verifySelectedModels = async () => {
    if (!config || !managingService) return
    const models = managingService.models.filter((model) => selectedModels[model.model_id])
    if (models.length === 0) return
    const readyBindings = new Set(
      bindings.filter((binding) => binding.status === 'ready').map((binding) => binding.capability),
    )
    const failures: string[] = []
    setBusy(`verify:${managingService.id}`)
    setError('')
    for (const model of models) {
      const key = `${managingService.id}:${model.model_id}`
      const capability = modelCapabilities[key] ?? model.capabilities?.[0]?.capability ?? 'agent_chat'
      const selectedCapability = (model.capabilities ?? []).find(
        (item) => item.capability === capability,
      )
      const protocol = modelProtocols[key]
        ?? selectedCapability?.protocol
        ?? defaultModelProtocol(managingService, capability)
      setModelTestStates((current) => ({ ...current, [model.model_id]: 'testing' }))
      try {
        await verifyModelServiceModel(
          managingService.id,
          model.model_id,
          { capability, protocol },
          config,
        )
        if (
          (capability === 'image_generation' || capability === 'image_editing')
          && !readyBindings.has(capability)
        ) {
          await setModelCapabilityBinding(
            capability,
            { model_spec: `local:${managingService.id}:${model.model_id}` },
            config,
          )
          readyBindings.add(capability)
        }
        setModelTestStates((current) => ({ ...current, [model.model_id]: 'verified' }))
      } catch (reason) {
        failures.push(reason instanceof Error ? reason.message : String(reason))
        setModelTestStates((current) => ({ ...current, [model.model_id]: 'failed' }))
      }
    }
    try {
      const nextServices = await load()
      setManagingService(nextServices.find((item) => item.id === managingService.id) ?? managingService)
      if (failures.length === 0) setSelectedModels({})
      else setError(failures[0])
      onChanged?.()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      setBusy('')
    }
  }

  const makeDefault = async (
    service: ModelServiceConnection,
    modelID: string,
    capability: ModelCapabilityBinding['capability'],
  ) => {
    if (!config) return
    setBusy(service.id)
    setError('')
    try {
      await setModelCapabilityBinding(
        capability,
        { model_spec: `local:${service.id}:${modelID}` },
        config,
      )
      await load()
      onChanged?.()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      setBusy('')
    }
  }

  const dialogPreset = selected
    ?? presets.find((preset) => preset.id === reconnecting?.preset_id)
  const officialPresets = presets.filter((preset) => preset.id !== 'custom')
  const customPreset = presets.find((preset) => preset.id === 'custom')

  const backToPicker = () => {
    authorizationRun.current += 1
    setBusy('')
    setSelected(undefined)
    setAPIKey('')
    setBaseURL('')
    setName('')
    setError('')
    setFieldErrors({})
  }

  const openModelPicker = (service: ModelServiceConnection) => {
    setManagingService(service)
    setModelSearch('')
    setSelectedModels({})
    setModelTestStates({})
    setError('')
  }

  const filteredModels = managingService?.models.filter((model) => {
    const query = modelSearch.trim().toLocaleLowerCase()
    return !query
      || model.model_id.toLocaleLowerCase().includes(query)
      || model.display_name.toLocaleLowerCase().includes(query)
  }) ?? []
  const selectedModelCount = managingService?.models.filter(
    (model) => selectedModels[model.model_id],
  ).length ?? 0
  const selectedImageCapability = managingService?.models.some((model) => {
    if (!selectedModels[model.model_id]) return false
    const capability = modelCapabilities[`${managingService.id}:${model.model_id}`]
      ?? model.capabilities?.[0]?.capability
      ?? 'agent_chat'
    return capability === 'image_generation' || capability === 'image_editing'
  }) ?? false
  const modelPickerBusy = Boolean(managingService && busy === `verify:${managingService.id}`)
  const viewingModels = viewingService?.models ?? []

  return (
    <section id="settings-models" className="settings-section">
      <div className="settings-section-head settings-model-services-head">
        <div>
          <h2>{t('settings.group.models')}</h2>
          <p>{t('settings.modelServices.note')}</p>
        </div>
        {config && (
          <button
            type="button"
            className="settings-model-service-add"
            onClick={openAddDialog}
          >
            <IconPlus size={16} aria-hidden="true" />
            <span>{t('settings.modelServices.addService')}</span>
          </button>
        )}
      </div>

      {!config && (
        <div className="settings-model-service-empty">
          {t('settings.modelServices.runtimeOffline')}
        </div>
      )}

      {config && services.length > 0 && (
        <div className="settings-card settings-model-services">
          {services.map((service) => {
            const preset = presets.find((item) => item.id === service.preset_id)
            const refreshing = busy === `refresh:${service.id}`
            const regionLabel = service.region === 'intl'
              ? t('settings.modelServices.international')
              : service.region === 'cn'
                ? t('settings.modelServices.china')
                : service.region === 'official'
                  ? t('settings.modelServices.official')
                  : t('settings.modelServices.custom')
            const statusLabel = service.catalog_status === 'ready'
              ? t('settings.modelServices.status.ready')
              : service.catalog_status === 'stale'
                ? t('settings.modelServices.status.stale')
                : t('settings.modelServices.status.unavailable')
            return (
              <div className="settings-model-service" key={service.id}>
                <div className="settings-model-service-main">
                  <strong>{service.name}</strong>
                  <button
                    type="button"
                    className="settings-model-service-info"
                    aria-label={t('settings.modelServices.viewModels', { name: service.name })}
                    title={t('settings.modelServices.viewModels', { name: service.name })}
                    onClick={() => setViewingService(service)}
                  >
                    <IconInfoCircle size={15} aria-hidden="true" />
                  </button>
                </div>
                <span
                  className={`settings-model-service-state${service.credential_configured ? '' : ' missing'}${refreshing ? ' refreshing' : ''}`}
                  role={refreshing ? 'status' : undefined}
                  aria-busy={refreshing || undefined}
                >
                  {refreshing ? (
                    <>
                      <IconLoader2 className="animate-spin" size={14} aria-hidden="true" />
                      {t('settings.modelServices.refreshing')}
                    </>
                  ) : !service.credential_configured
                    ? t('settings.modelServices.needsApiKey')
                    : `${regionLabel} · ${statusLabel}`}
                </span>
                <div className="settings-model-service-actions">
                  <button
                    type="button"
                    className="settings-model-service-models"
                    disabled={busy === service.id || refreshing}
                    onClick={() => openModelPicker(service)}
                  >
                    {t('settings.modelServices.manageModels')}
                  </button>
                  <DropdownMenu>
                    <DropdownMenuTrigger asChild>
                      <button
                        type="button"
                        className="settings-model-service-action"
                        aria-label={t('settings.modelServices.serviceActions', { name: service.name })}
                        disabled={busy === service.id || refreshing}
                        aria-busy={refreshing}
                      >
                        <IconDots size={16} aria-hidden="true" />
                      </button>
                    </DropdownMenuTrigger>
                    <DropdownMenuContent align="end" className="settings-model-service-menu">
                      {preset?.billing_url && (
                        <DropdownMenuItem
                          onSelect={() => void window.shejaneClient?.openExternal?.(preset.billing_url!)}
                        >
                          <IconExternalLink size={15} aria-hidden="true" />
                          {t('settings.modelServices.openConsole', { name: service.name })}
                        </DropdownMenuItem>
                      )}
                      {preset?.connection_method !== 'browser_authorization' && (
                        <DropdownMenuItem onSelect={() => openReconnect(service)}>
                          <IconKey size={15} aria-hidden="true" />
                          {t('settings.modelServices.updateApiKeyAria', { name: service.name })}
                        </DropdownMenuItem>
                      )}
                      <DropdownMenuItem onSelect={() => void refresh(service)}>
                        <IconRefresh size={15} aria-hidden="true" />
                        {t('settings.modelServices.refresh')}
                      </DropdownMenuItem>
                      <DropdownMenuSeparator />
                      <DropdownMenuItem
                        className="settings-model-service-menu-danger"
                        onSelect={() => void remove(service)}
                      >
                        <IconTrash size={15} aria-hidden="true" />
                        {t('settings.modelServices.delete')}
                      </DropdownMenuItem>
                    </DropdownMenuContent>
                  </DropdownMenu>
                </div>
                {service.preset_id === 'shejane-official' && (
                  <div className="settings-model-service-diagnostics">
                    <div className="settings-model-service-diagnostics-copy">
                      <strong>{t('settings.modelServices.diagnostics.label')}</strong>
                      <small id={`model-service-diagnostics-${service.id}`}>
                        {t('settings.modelServices.diagnostics.hint')}
                      </small>
                    </div>
                    <Switch
                      aria-label={t('settings.modelServices.diagnostics.label')}
                      aria-describedby={`model-service-diagnostics-${service.id}`}
                      checked={diagnostics?.enabled === true
                        && diagnostics.credential_configured
                        && diagnostics.connection_id === service.id}
                      disabled={busy === `diagnostics:${service.id}`}
                      onCheckedChange={(checked) => void toggleDiagnostics(service, checked)}
                    />
                  </div>
                )}
              </div>
            )
          })}
        </div>
      )}

      {error && <div className="settings-model-service-error" role="alert">{error}</div>}

      <Dialog
        open={adding || Boolean(reconnecting)}
        onOpenChange={(open) => {
          if (!open && !busy) {
            setAdding(false)
            setSelected(undefined)
            setReconnecting(undefined)
          }
        }}
      >
        <DialogContent className="settings-model-service-dialog sm:max-w-[400px]">
          <DialogHeader>
            <div className="flex items-start gap-2 pr-8">
              {adding && selected && (
                <Button
                  type="button"
                  variant="ghost"
                  size="icon-sm"
                  className="-ml-2"
                  aria-label={t('settings.modelServices.backToServices')}
                  onClick={backToPicker}
                >
                  <IconArrowLeft data-icon="inline-start" />
                </Button>
              )}
              <div className="flex min-w-0 flex-col gap-1">
                <DialogTitle>
                  {adding && !selected
                    ? t('settings.modelServices.addService')
                    : selected?.name ?? reconnecting?.name}
                </DialogTitle>
                {(selected || reconnecting) && (
                  <DialogDescription>
                    {reconnecting
                      ? t('settings.modelServices.updateApiKeyHint')
                      : selected?.description}
                  </DialogDescription>
                )}
              </div>
            </div>
          </DialogHeader>
          {adding && !selected && (
            <div className="settings-model-service-picker">
              <div className="settings-model-service-picker-list">
                {officialPresets.map((preset) => (
                  <button type="button" key={preset.id} onClick={() => openPreset(preset)}>
                    <span>
                      <strong>{preset.name}</strong>
                      <small>{preset.description}</small>
                    </span>
                    <IconChevronRight size={16} aria-hidden="true" />
                  </button>
                ))}
              </div>
              {customPreset && (
                <button
                  type="button"
                  className="settings-model-service-picker-custom"
                  onClick={() => openPreset(customPreset)}
                >
                  <span>
                    <strong>{customPreset.name}</strong>
                    <small>{customPreset.description}</small>
                  </span>
                  <IconChevronRight size={16} aria-hidden="true" />
                </button>
              )}
            </div>
          )}
          {selected?.connection_method === 'browser_authorization' && (
            <div className="settings-model-service-form">
              {!error && (
                <p
                  className="flex items-center justify-center gap-2 text-center text-sm text-muted-foreground"
                  role="status"
                  aria-busy={busy.startsWith('authorize')}
                >
                  {busy.startsWith('authorize') && (
                    <IconLoader2 className="animate-spin" size={16} aria-hidden="true" />
                  )}
                  {busy === 'authorize:syncing'
                    ? t('settings.modelServices.authorization.syncing')
                    : busy.startsWith('authorize')
                      ? t('settings.modelServices.authorization.pending')
                      : t('settings.modelServices.authorization.ready')}
                </p>
              )}
              {error && <div className="settings-model-service-error" role="alert">{error}</div>}
              {!busy && (
                <Button
                  type="button"
                  size="lg"
                  className="h-11 w-full"
                  onClick={() => void authorizeOfficial()}
                >
                  {t('settings.modelServices.authorization.retry')}
                </Button>
              )}
            </div>
          )}
          {(reconnecting || (selected && selected.connection_method !== 'browser_authorization')) && (
            <form
              className="settings-model-service-form"
              noValidate
              onSubmit={(event) => void connect(event)}
            >
              <FieldGroup>
                {selected?.id === 'custom' && (
                  <Field data-invalid={Boolean(fieldErrors.name)}>
                    <FieldLabel htmlFor="model-service-name">
                      {t('settings.modelServices.serviceName')}
                    </FieldLabel>
                    <Input
                      id="model-service-name"
                      value={name}
                      aria-invalid={Boolean(fieldErrors.name)}
                      onChange={(event) => {
                        setName(event.target.value)
                        setFieldErrors((current) => ({ ...current, name: undefined }))
                      }}
                    />
                    <FieldError>{fieldErrors.name}</FieldError>
                  </Field>
                )}
                {selected && selected.id !== 'custom' && selected.regions.length > 1 && (
                  <Field>
                    <FieldLabel>{t('settings.modelServices.region')}</FieldLabel>
                    <Select
                      value={region}
                      onValueChange={(value) => {
                        setRegion(value)
                        setBaseURL('')
                        setFieldErrors((current) => ({ ...current, baseURL: undefined }))
                      }}
                    >
                      <SelectTrigger><SelectValue /></SelectTrigger>
                      <SelectContent>
                        <SelectGroup>
                          {selected.regions.map((item) => (
                            <SelectItem key={item.id} value={item.id}>{item.name}</SelectItem>
                          ))}
                        </SelectGroup>
                      </SelectContent>
                    </Select>
                  </Field>
                )}
                <Field data-invalid={Boolean(fieldErrors.baseURL)}>
                  <FieldLabel htmlFor="model-service-base-url">
                    {t('settings.modelServices.address')}
                  </FieldLabel>
                  <Input
                    id="model-service-base-url"
                    type="url"
                    value={baseURL}
                    placeholder={defaultBaseURL || undefined}
                    aria-invalid={Boolean(fieldErrors.baseURL)}
                    onChange={(event) => {
                      setBaseURL(event.target.value)
                      setFieldErrors((current) => ({ ...current, baseURL: undefined }))
                    }}
                  />
                  <FieldDescription>
                    {t('settings.modelServices.addressHint')}
                  </FieldDescription>
                  <FieldError>{fieldErrors.baseURL}</FieldError>
                </Field>
                <Field data-invalid={Boolean(fieldErrors.apiKey)}>
                  <FieldLabel htmlFor="model-service-api-key">
                    {t('settings.modelServices.apiKey')}
                  </FieldLabel>
                  <Input
                    id="model-service-api-key"
                    type="password"
                    value={apiKey}
                    aria-invalid={Boolean(fieldErrors.apiKey)}
                    autoComplete="off"
                    onChange={(event) => {
                      setAPIKey(event.target.value)
                      setFieldErrors((current) => ({ ...current, apiKey: undefined }))
                    }}
                  />
                  <FieldDescription>
                    {t('settings.modelServices.apiKeyHint')}
                  </FieldDescription>
                  <FieldError>{fieldErrors.apiKey}</FieldError>
                </Field>
                {showAdvanced && selected && (
                  <Field>
                    <FieldLabel>{t('settings.modelServices.protocol')}</FieldLabel>
                    <Select value={adapterID} onValueChange={(value) => setAdapterID(value as typeof adapterID)}>
                      <SelectTrigger><SelectValue /></SelectTrigger>
                      <SelectContent>
                        <SelectGroup>
                          <SelectItem value="openai_chat">OpenAI Chat</SelectItem>
                          <SelectItem value="anthropic_messages">Anthropic Messages</SelectItem>
                          <SelectItem value="google_genai">Google GenerateContent</SelectItem>
                        </SelectGroup>
                      </SelectContent>
                    </Select>
                  </Field>
                )}
              </FieldGroup>
              {dialogPreset?.api_key_url && (
                <Button
                  type="button"
                  variant="outline"
                  size="lg"
                  className="w-full"
                  onClick={() => void window.shejaneClient?.openExternal?.(dialogPreset.api_key_url!)}
                >
                  {t('settings.modelServices.getApiKey')}
                  <IconExternalLink data-icon="inline-end" aria-hidden="true" />
                </Button>
              )}
              {error && <div className="settings-model-service-error" role="alert">{error}</div>}
              {busy === 'connect' && (
                <p className="text-center text-xs text-muted-foreground" role="status">
                  {t('settings.modelServices.configuringHint')}
                </p>
              )}
              <Button
                type="submit"
                size="lg"
                className="h-11 w-full"
                disabled={busy === 'connect'}
                aria-busy={busy === 'connect'}
              >
                {busy === 'connect' && <IconLoader2 className="animate-spin" aria-hidden="true" />}
                {busy === 'connect'
                  ? t(reconnecting
                    ? 'settings.modelServices.updating'
                    : 'settings.modelServices.connecting')
                  : t(reconnecting
                    ? 'settings.modelServices.updateConnection'
                    : 'settings.modelServices.connect')}
              </Button>
            </form>
          )}
        </DialogContent>
      </Dialog>

      <Dialog
        open={Boolean(viewingService)}
        onOpenChange={(open) => {
          if (!open) setViewingService(undefined)
        }}
      >
        <DialogContent className="settings-model-list-dialog sm:max-w-[420px]">
          {viewingService && (
            <>
              <DialogHeader>
                <DialogTitle>{viewingService.name}</DialogTitle>
                <DialogDescription>
                  {t('settings.modelServices.modelCount', { count: viewingModels.length })}
                </DialogDescription>
              </DialogHeader>
              <div className="settings-model-list" role="list">
                {viewingModels.length === 0 ? (
                  <div className="settings-model-list-empty">
                    {t('settings.modelServices.noModels')}
                  </div>
                ) : viewingModels.map((model) => (
                  <div className="settings-model-list-row" role="listitem" key={model.model_id}>
                    <strong>{model.display_name}</strong>
                    {model.model_id !== model.display_name && <small>{model.model_id}</small>}
                  </div>
                ))}
              </div>
            </>
          )}
        </DialogContent>
      </Dialog>

      <Dialog
        open={Boolean(managingService)}
        onOpenChange={(open) => {
          if (!open && !modelPickerBusy) {
            setManagingService(undefined)
            setModelSearch('')
            setSelectedModels({})
            setModelTestStates({})
            setError('')
          }
        }}
      >
        <DialogContent className="settings-model-picker-dialog sm:max-w-[680px]">
          {managingService && (
            <>
              <DialogHeader className="settings-model-picker-header">
                <div>
                  <DialogTitle>{t('settings.modelServices.modelPickerTitle')}</DialogTitle>
                  <DialogDescription>
                    {t('settings.modelServices.modelPickerDescription', { name: managingService.name })}
                  </DialogDescription>
                </div>
                <Button
                  type="button"
                  variant="ghost"
                  size="icon-sm"
                  aria-label={t('settings.modelServices.refresh')}
                  disabled={busy === `refresh:${managingService.id}` || modelPickerBusy}
                  onClick={() => void refresh(managingService)}
                >
                  <IconRefresh className={busy === `refresh:${managingService.id}` ? 'animate-spin' : undefined} />
                </Button>
              </DialogHeader>

              <div className="settings-model-picker-search">
                <IconSearch size={16} aria-hidden="true" />
                <Input
                  type="search"
                  role="searchbox"
                  value={modelSearch}
                  aria-label={t('settings.modelServices.filterModels')}
                  placeholder={t('settings.modelServices.filterModels')}
                  onChange={(event) => setModelSearch(event.target.value)}
                />
              </div>

              <div className="settings-model-picker-meta">
                <span>{t('settings.modelServices.modelCount', { count: managingService.models.length })}</span>
                <span>{t('settings.modelServices.selectedCount', { count: selectedModelCount })}</span>
              </div>

              <div className="settings-model-picker-list">
                {filteredModels.length === 0 ? (
                  <div className="settings-model-picker-empty">
                    {t('settings.modelServices.noMatchingModels')}
                  </div>
                ) : filteredModels.map((model) => {
                  const key = `${managingService.id}:${model.model_id}`
                  const selectedForTest = Boolean(selectedModels[model.model_id])
                  const capability = modelCapabilities[key]
                    ?? model.capabilities?.[0]?.capability
                    ?? 'agent_chat'
                  const selectedCapability = (model.capabilities ?? []).find(
                    (item) => item.capability === capability,
                  )
                  const protocol = modelProtocols[key]
                    ?? selectedCapability?.protocol
                    ?? defaultModelProtocol(managingService, capability)
                  const verifiedCapabilities = (model.capabilities ?? []).filter(
                    (item) => item.verification === 'verified',
                  )
                  const testState = modelTestStates[model.model_id]
                  return (
                    <div
                      className={`settings-model-picker-row${selectedForTest ? ' selected' : ''}`}
                      key={model.model_id}
                    >
                      <label className="settings-model-picker-row-head">
                        <input
                          type="checkbox"
                          checked={selectedForTest}
                          aria-label={t('settings.modelServices.selectModel', { name: model.display_name })}
                          disabled={modelPickerBusy}
                          onChange={(event) => setSelectedModels((current) => ({
                            ...current,
                            [model.model_id]: event.target.checked,
                          }))}
                        />
                        <span className="settings-model-picker-name">
                          <strong>{model.display_name}</strong>
                          {model.model_id !== model.display_name && <small>{model.model_id}</small>}
                        </span>
                        <span className={`settings-model-picker-result${testState ? ` ${testState}` : ''}`}>
                          {testState === 'testing' ? (
                            <><IconLoader2 className="animate-spin" size={14} aria-hidden="true" />{t('settings.modelServices.testing')}</>
                          ) : testState === 'verified' ? (
                            <><IconCheck size={14} aria-hidden="true" />{t('settings.modelServices.modelVerified')}</>
                          ) : testState === 'failed' ? t('settings.modelServices.modelFailed')
                            : verifiedCapabilities.length > 0 ? t('settings.modelServices.modelVerified') : null}
                        </span>
                      </label>

                      {verifiedCapabilities.length > 0 && (
                        <div className="settings-model-picker-enabled">
                          <span>
                            {t('settings.modelServices.enabledCapabilities', {
                              capabilities: verifiedCapabilities
                                .map((item) => t(capabilityTranslationKey(item.capability)))
                                .join('、'),
                            })}
                          </span>
                          {verifiedCapabilities.map((item) => {
                            const bindable = item.capability === 'image_generation'
                              || item.capability === 'image_editing'
                            if (!bindable) return null
                            const binding = bindings.find((candidate) => candidate.capability === item.capability)
                            const selectedByDefault = binding?.status === 'ready'
                              && binding.connection_id === managingService.id
                              && binding.model_id === model.model_id
                            return selectedByDefault ? (
                              <span key={item.capability}>{t('settings.modelServices.defaultCapability')}</span>
                            ) : (
                              <button
                                type="button"
                                key={item.capability}
                                disabled={modelPickerBusy}
                                onClick={() => void makeDefault(
                                  managingService,
                                  model.model_id,
                                  item.capability as ModelCapabilityBinding['capability'],
                                )}
                              >
                                {t('settings.modelServices.setDefaultCapability')}
                              </button>
                            )
                          })}
                        </div>
                      )}

                      {selectedForTest && (
                        <div className="settings-model-picker-config">
                          <div className="settings-model-picker-field">
                            <span>{t('settings.modelServices.use')}</span>
                            <Select
                              value={capability}
                              onValueChange={(value) => {
                                const nextCapability = value as ModelCapabilityName
                                setModelCapabilities((current) => ({ ...current, [key]: nextCapability }))
                                setModelProtocols((current) => ({
                                  ...current,
                                  [key]: defaultModelProtocol(managingService, nextCapability),
                                }))
                              }}
                            >
                              <SelectTrigger aria-label={t('settings.modelServices.purposeAria', { name: model.display_name })}>
                                <SelectValue />
                              </SelectTrigger>
                              <SelectContent>
                                <SelectGroup>
                                  <SelectItem value="agent_chat">{t('settings.modelServices.purpose.agentChat')}</SelectItem>
                                  <SelectItem value="image_understanding">{t('settings.modelServices.purpose.imageUnderstanding')}</SelectItem>
                                  <SelectItem value="image_generation">{t('settings.modelServices.purpose.imageGeneration')}</SelectItem>
                                  <SelectItem value="image_editing">{t('settings.modelServices.purpose.imageEditing')}</SelectItem>
                                </SelectGroup>
                              </SelectContent>
                            </Select>
                          </div>
                          <details className="settings-model-picker-advanced">
                            <summary>{t('settings.modelServices.advanced')}</summary>
                            <div className="settings-model-picker-field">
                              <span>{t('settings.modelServices.protocol')}</span>
                              <Select
                                value={protocol}
                                onValueChange={(value) => setModelProtocols((current) => ({
                                  ...current,
                                  [key]: value as ModelProtocol,
                                }))}
                              >
                                <SelectTrigger aria-label={t('settings.modelServices.protocolAria', { name: model.display_name })}>
                                  <SelectValue />
                                </SelectTrigger>
                                <SelectContent>
                                  <SelectGroup>
                                    {capability === 'image_generation' ? (
                                      <SelectItem value="openai_images_generations">OpenAI Images</SelectItem>
                                    ) : capability === 'image_editing' ? (
                                      <SelectItem value="openai_images_edits">OpenAI Image Edits</SelectItem>
                                    ) : (
                                      <>
                                        <SelectItem value="openai_chat_completions">OpenAI Chat</SelectItem>
                                        <SelectItem value="openai_responses">OpenAI Responses</SelectItem>
                                        <SelectItem value="anthropic_messages">Anthropic Messages</SelectItem>
                                        <SelectItem value="google_generate_content">Google GenerateContent</SelectItem>
                                      </>
                                    )}
                                  </SelectGroup>
                                </SelectContent>
                              </Select>
                            </div>
                          </details>
                        </div>
                      )}
                    </div>
                  )
                })}
              </div>

              {managingService.preset_id === 'custom' && (
                <details className="settings-model-picker-manual">
                  <summary>{t('settings.modelServices.addManualModel')}</summary>
                  <div>
                    <Input
                      value={manualModels[managingService.id] ?? ''}
                      aria-label={t('settings.modelServices.modelId')}
                      placeholder={t('settings.modelServices.modelId')}
                      onChange={(event) => setManualModels((current) => ({
                        ...current,
                        [managingService.id]: event.target.value,
                      }))}
                    />
                    <Button
                      type="button"
                      variant="outline"
                      disabled={!manualModels[managingService.id]?.trim() || busy === managingService.id}
                      onClick={() => void addManualModel(managingService)}
                    >
                      {t('settings.modelServices.addModel')}
                    </Button>
                  </div>
                </details>
              )}

              {error && <div className="settings-model-service-error" role="alert">{error}</div>}

              <div className="settings-model-picker-footer">
                <div>
                  <strong>{t('settings.modelServices.selectedCount', { count: selectedModelCount })}</strong>
                  {selectedImageCapability && <small>{t('settings.modelServices.imageCostHint')}</small>}
                </div>
                <div>
                  <Button
                    type="button"
                    variant="outline"
                    disabled={modelPickerBusy}
                    onClick={() => setManagingService(undefined)}
                  >
                    {t('common.cancel')}
                  </Button>
                  <Button
                    type="button"
                    disabled={selectedModelCount === 0 || modelPickerBusy}
                    aria-busy={modelPickerBusy}
                    onClick={() => void verifySelectedModels()}
                  >
                    {modelPickerBusy && <IconLoader2 className="animate-spin" aria-hidden="true" />}
                    {t(modelPickerBusy
                      ? 'settings.modelServices.testingSelected'
                      : 'settings.modelServices.testSelected', { count: selectedModelCount })}
                  </Button>
                </div>
              </div>
            </>
          )}
        </DialogContent>
      </Dialog>
    </section>
  )
}
