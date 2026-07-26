import { useCallback, useEffect, useState, type FormEvent } from 'react'
import {
  IconArrowLeft,
  IconChevronRight,
  IconExternalLink,
  IconKey,
  IconPlus,
  IconRefresh,
  IconTrash,
} from '@tabler/icons-react'
import {
  addModelServiceModel,
  connectModelService,
  deleteModelService,
  listModelServicePresets,
  listModelServices,
  reconnectModelService,
  refreshModelService,
  RuntimeHTTPError,
  verifyModelServiceModel,
  type ModelServiceConnection,
  type ModelServicePreset,
  type RuntimeConnection,
} from '@/runtime/client'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import {
  Field,
  FieldDescription,
  FieldError,
  FieldGroup,
  FieldLabel,
} from '@/components/ui/field'
import { Input } from '@/components/ui/input'
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

function isValidServiceURL(value: string) {
  try {
    const protocol = new URL(value).protocol
    return protocol === 'http:' || protocol === 'https:'
  } catch {
    return false
  }
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
  const [adding, setAdding] = useState(false)
  const [selected, setSelected] = useState<ModelServicePreset>()
  const [reconnecting, setReconnecting] = useState<ModelServiceConnection>()
  const [apiKey, setAPIKey] = useState('')
  const [region, setRegion] = useState('cn')
  const [name, setName] = useState('')
  const [baseURL, setBaseURL] = useState('')
  const [adapterID, setAdapterID] = useState<'openai_chat' | 'anthropic_messages'>()
  const [showAdvanced, setShowAdvanced] = useState(false)
  const [manualModels, setManualModels] = useState<Record<string, string>>({})
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [fieldErrors, setFieldErrors] = useState<ConnectionFieldErrors>({})
  const defaultBaseURL = reconnecting?.base_url
    ?? selected?.regions.find((item) => item.id === region)?.base_url
    ?? selected?.regions.find((item) => item.default)?.base_url
    ?? selected?.regions[0]?.base_url
    ?? ''

  const load = useCallback(async () => {
    if (!config) {
      setPresets([])
      setServices([])
      return
    }
    const [nextPresets, nextServices] = await Promise.all([
      listModelServicePresets(config),
      listModelServices(config),
    ])
    setPresets(nextPresets)
    setServices(nextServices)
  }, [config])

  useEffect(() => {
    void load().catch((reason) => setError(reason instanceof Error ? reason.message : String(reason)))
  }, [load])

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
      await load()
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
    setBusy(service.id)
    setError('')
    try {
      await refreshModelService(service.id, config)
      await load()
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
      await addModelServiceModel(service.id, { model_id: modelID }, config)
      setManualModels((current) => ({ ...current, [service.id]: '' }))
      await load()
      onChanged?.()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      setBusy('')
    }
  }

  const verifyModel = async (service: ModelServiceConnection, modelID: string) => {
    if (!config) return
    setBusy(service.id)
    setError('')
    try {
      await verifyModelServiceModel(service.id, modelID, config)
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
    setSelected(undefined)
    setAPIKey('')
    setBaseURL('')
    setName('')
    setError('')
    setFieldErrors({})
  }

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
            const regionLabel = service.region === 'intl'
              ? t('settings.modelServices.international')
              : service.region === 'cn'
                ? t('settings.modelServices.china')
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
                  <span>
                    {service.models
                      .filter((model) => model.verification === 'verified')
                      .map((model) => model.display_name)
                      .join('、') || t('settings.modelServices.noModels')}
                  </span>
                </div>
                <span className={`settings-model-service-state${service.credential_configured ? '' : ' missing'}`}>
                  {!service.credential_configured
                    ? t('settings.modelServices.needsApiKey')
                    : `${regionLabel} · ${statusLabel}`}
                </span>
                <div className="settings-model-service-actions">
                  {preset?.billing_url && (
                    <button
                      type="button"
                      className="settings-model-service-action"
                      aria-label={t('settings.modelServices.openConsole', { name: service.name })}
                      onClick={() => void window.shejaneClient?.openExternal?.(preset.billing_url!)}
                    >
                      <IconExternalLink size={15} aria-hidden="true" />
                    </button>
                  )}
                  <button
                    type="button"
                    className="settings-model-service-action"
                    aria-label={t('settings.modelServices.updateApiKeyAria', { name: service.name })}
                    disabled={busy === service.id}
                    onClick={() => openReconnect(service)}
                  >
                    <IconKey size={15} aria-hidden="true" />
                  </button>
                  <button
                    type="button"
                    className="settings-model-service-action"
                    aria-label={t('settings.modelServices.refresh')}
                    disabled={busy === service.id}
                    onClick={() => void refresh(service)}
                  >
                    <IconRefresh size={15} aria-hidden="true" />
                  </button>
                  <button
                    type="button"
                    className="settings-model-service-action danger"
                    aria-label={t('settings.modelServices.delete')}
                    disabled={busy === service.id}
                    onClick={() => void remove(service)}
                  >
                    <IconTrash size={15} aria-hidden="true" />
                  </button>
                </div>
                {service.models.length === 0 && (
                  <div className="settings-model-service-manual">
                    <Input
                      value={manualModels[service.id] ?? ''}
                      placeholder={t('settings.modelServices.modelId')}
                      onChange={(event) => setManualModels((current) => ({
                        ...current,
                        [service.id]: event.target.value,
                      }))}
                    />
                    <button type="button" onClick={() => void addManualModel(service)}>
                      {t('settings.modelServices.addModel')}
                    </button>
                  </div>
                )}
                {service.models.some((model) => model.verification === 'unverified') && (
                  <div className="settings-model-service-unverified">
                    {service.models.filter((model) => model.verification === 'unverified').map((model) => (
                      <span key={model.model_id}>
                        <span>{model.display_name}</span>
                        <button
                          type="button"
                          disabled={busy === service.id}
                          onClick={() => void verifyModel(service, model.model_id)}
                        >
                          {t('settings.modelServices.verify')}
                        </button>
                      </span>
                    ))}
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
          {(selected || reconnecting) && (
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
              <Button type="submit" size="lg" className="h-11 w-full" disabled={busy === 'connect'}>
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
    </section>
  )
}
