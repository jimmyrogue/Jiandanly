import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { IconExternalLink, IconKey, IconPlus, IconRefresh, IconTrash } from '@tabler/icons-react'
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
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { useI18n } from '@/shared/i18n/i18n'

export function ModelServicesSettings({
  config,
  onChanged,
}: {
  config?: RuntimeConnection | null
  onChanged?: () => void
}) {
  const { t } = useI18n()
  const [presets, setPresets] = useState<ModelServicePreset[]>([])
  const [services, setServices] = useState<ModelServiceConnection[]>([])
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

  const openPreset = (preset: ModelServicePreset) => {
    setReconnecting(undefined)
    setSelected(preset)
    setAPIKey('')
    setRegion(preset.regions.find((item) => item.default)?.id ?? 'custom')
    setName('')
    setBaseURL('')
    setAdapterID(undefined)
    setShowAdvanced(false)
    setError('')
  }

  const openReconnect = (service: ModelServiceConnection) => {
    setSelected(undefined)
    setReconnecting(service)
    setAPIKey('')
    setShowAdvanced(false)
    setError('')
  }

  const connect = async (event: FormEvent) => {
    event.preventDefault()
    if (!config || (!selected && !reconnecting)) return
    setBusy('connect')
    setError('')
    try {
      if (reconnecting) {
        await reconnectModelService(reconnecting.id, { api_key: apiKey }, config)
      } else if (selected) {
        await connectModelService({
          preset_id: selected.id,
          api_key: apiKey,
          ...(selected.id === 'custom'
            ? {
                name,
                base_url: baseURL,
                region: 'custom' as const,
                ...(adapterID ? { adapter_id: adapterID } : {}),
              }
            : { region: region as 'cn' | 'intl' }),
        }, config)
      }
      setSelected(undefined)
      setReconnecting(undefined)
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

  if (!config) {
    return <div className="settings-model-service-empty">{t('settings.modelServices.runtimeOffline')}</div>
  }

  const dialogPreset = selected
    ?? presets.find((preset) => preset.id === reconnecting?.preset_id)

  return (
    <div className="settings-model-services">
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
              {service.models.filter((model) => model.recommended).map((model) => model.display_name).join('、')
                || t('settings.modelServices.noModels')}
            </span>
          </div>
          <span className={`settings-model-service-state${service.credential_configured ? '' : ' missing'}`}>
            {!service.credential_configured
              ? t('settings.modelServices.needsApiKey')
              : `${regionLabel} · ${statusLabel}`}
          </span>
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

      <div className="settings-model-service-presets">
        {presets.map((preset) => (
          <button type="button" key={preset.id} onClick={() => openPreset(preset)}>
            <IconPlus size={14} aria-hidden="true" />
            <span>
              <strong>{preset.name}</strong>
              <small>{preset.description}</small>
            </span>
          </button>
        ))}
      </div>

      {error && <div className="settings-model-service-error" role="alert">{error}</div>}

      <Dialog
        open={Boolean(selected || reconnecting)}
        onOpenChange={(open) => {
          if (!open && !busy) {
            setSelected(undefined)
            setReconnecting(undefined)
          }
        }}
      >
        <DialogContent className="settings-model-service-dialog sm:max-w-[440px]">
          <DialogHeader>
            <DialogTitle>{selected?.name ?? reconnecting?.name}</DialogTitle>
            <DialogDescription>
              {reconnecting
                ? t('settings.modelServices.updateApiKeyHint')
                : selected?.description}
            </DialogDescription>
          </DialogHeader>
          {(selected || reconnecting) && (
            <form className="settings-model-service-form" onSubmit={(event) => void connect(event)}>
              {selected?.id === 'custom' ? (
                <>
                  <label className="settings-model-service-field">
                    <span>{t('settings.modelServices.serviceName')}</span>
                    <Input value={name} required onChange={(event) => setName(event.target.value)} />
                  </label>
                  <label className="settings-model-service-field">
                    <span>{t('settings.modelServices.address')}</span>
                    <Input
                      type="url"
                      value={baseURL}
                      required
                      placeholder="https://gateway.example/v1"
                      onChange={(event) => setBaseURL(event.target.value)}
                    />
                  </label>
                </>
              ) : selected && selected.regions.length > 1 && (
                <label className="settings-model-service-field">
                  <span>{t('settings.modelServices.region')}</span>
                  <Select value={region} onValueChange={setRegion}>
                    <SelectTrigger><SelectValue /></SelectTrigger>
                    <SelectContent>
                      {selected.regions.map((item) => (
                        <SelectItem key={item.id} value={item.id}>{item.name}</SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </label>
              )}
              <label className="settings-model-service-field">
                <span>{t('settings.modelServices.apiKey')}</span>
                <Input
                  type="password"
                  aria-label={t('settings.modelServices.apiKey')}
                  value={apiKey}
                  required
                  autoComplete="off"
                  onChange={(event) => setAPIKey(event.target.value)}
                />
                <small>{t('settings.modelServices.apiKeyHint')}</small>
              </label>
              {dialogPreset?.api_key_url && (
                <button
                  type="button"
                  className="settings-model-service-link"
                  onClick={() => void window.shejaneClient?.openExternal?.(dialogPreset.api_key_url!)}
                >
                  {t('settings.modelServices.getApiKey')}
                  <IconExternalLink size={14} aria-hidden="true" />
                </button>
              )}
              {showAdvanced && selected && (
                <label className="settings-model-service-field">
                  <span>{t('settings.modelServices.protocol')}</span>
                  <Select value={adapterID} onValueChange={(value) => setAdapterID(value as typeof adapterID)}>
                    <SelectTrigger><SelectValue /></SelectTrigger>
                    <SelectContent>
                      <SelectItem value="openai_chat">OpenAI Chat</SelectItem>
                      <SelectItem value="anthropic_messages">Anthropic Messages</SelectItem>
                    </SelectContent>
                  </Select>
                </label>
              )}
              {error && <div className="settings-model-service-error" role="alert">{error}</div>}
              <button type="submit" className="settings-model-service-submit" disabled={busy === 'connect'}>
                {busy === 'connect'
                  ? t(reconnecting
                    ? 'settings.modelServices.updating'
                    : 'settings.modelServices.connecting')
                  : t(reconnecting
                    ? 'settings.modelServices.updateConnection'
                    : 'settings.modelServices.connect')}
              </button>
            </form>
          )}
        </DialogContent>
      </Dialog>
    </div>
  )
}
