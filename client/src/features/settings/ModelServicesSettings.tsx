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
  IconTrash,
} from '@tabler/icons-react'
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
import { ModelCompatibilityDialog } from './ModelCompatibilityDialog'
import {
  useModelServicesSettings,
  type ModelServicesSettingsProps,
} from './useModelServicesSettings'

export function ModelServicesSettings(props: ModelServicesSettingsProps) {
  return <ModelServicesSettingsContent view={useModelServicesSettings(props)} />
}

function ModelServicesSettingsContent({
  view,
}: {
  view: ReturnType<typeof useModelServicesSettings>
}) {
  const {
    adapterID, adding, apiKey, authorizeOfficial, backToPicker, baseURL, beginOperation,
    bindings, busy, config, connect, connectionTestStates, customPreset, defaultBaseURL,
    diagnostics, dialogPreset, error, fieldErrors, finishOperation, load, managingService,
    name, officialPresets, onChanged, openAddDialog, openModelPicker, openPreset,
    openReconnect, presets, reconnecting, refresh, region, remove, selected, services,
    setAPIKey, setAdapterID, setAdding, setBaseURL, setError, setFieldErrors,
    setManagingService, setName, setReconnecting, setRegion, setSelected,
    setViewingService, showAdvanced, t, testConnection, toggleDiagnostics,
    viewingModels, viewingService,
  } = view
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
            disabled={Boolean(busy)}
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
            const connectionTestState = connectionTestStates[service.id]
            const testingConnection = busy === `test:${service.id}`
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
                  className={`settings-model-service-state${service.credential_configured ? '' : ' missing'}${refreshing || testingConnection ? ' refreshing' : ''}${connectionTestState === 'verified' && !refreshing && !testingConnection ? ' tested' : ''}`}
                  role={refreshing || testingConnection || connectionTestState === 'verified' ? 'status' : undefined}
                  aria-busy={refreshing || testingConnection || undefined}
                >
                  {testingConnection ? (
                    <>
                      <IconLoader2 className="animate-spin" size={14} aria-hidden="true" />
                      {t('settings.modelServices.testingConnection')}
                    </>
                  ) : refreshing ? (
                    <>
                      <IconLoader2 className="animate-spin" size={14} aria-hidden="true" />
                      {t('settings.modelServices.refreshing')}
                    </>
                  ) : connectionTestState === 'verified' ? (
                    <>
                      <IconCheck size={14} aria-hidden="true" />
                      {t('settings.modelServices.connectionTestPassed')}
                    </>
                  ) : !service.credential_configured
                    ? t('settings.modelServices.needsApiKey')
                    : `${regionLabel} · ${statusLabel}`}
                </span>
                <div className="settings-model-service-actions">
                  <DropdownMenu>
                    <DropdownMenuTrigger asChild>
                      <button
                        type="button"
                        className="settings-model-service-action"
                        aria-label={t('settings.modelServices.serviceActions', { name: service.name })}
                        disabled={Boolean(busy)}
                        aria-busy={refreshing || testingConnection}
                      >
                        <IconDots size={16} aria-hidden="true" />
                      </button>
                    </DropdownMenuTrigger>
                    <DropdownMenuContent align="end" className="settings-model-service-menu">
                      <DropdownMenuItem onSelect={() => void testConnection(service)}>
                        <IconCheck size={15} aria-hidden="true" />
                        {t('settings.modelServices.testConnection')}
                      </DropdownMenuItem>
                      <DropdownMenuItem onSelect={() => openModelPicker(service)}>
                        <IconChevronRight size={15} aria-hidden="true" />
                        {t('settings.modelServices.advancedCompatibility')}
                      </DropdownMenuItem>
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
                      disabled={Boolean(busy)}
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
                  disabled={Boolean(busy) && !busy.startsWith('authorize')}
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
                  <button type="button" key={preset.id} disabled={Boolean(busy)} onClick={() => openPreset(preset)}>
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
                  disabled={Boolean(busy)}
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
                  disabled={Boolean(busy)}
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
                disabled={Boolean(busy)}
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

      {managingService && (
        <ModelCompatibilityDialog
          key={managingService.id}
          bindings={bindings}
          config={config}
          initialService={managingService}
          load={load}
          onChanged={onChanged}
          onClose={() => {
            setManagingService(undefined)
            setError('')
          }}
          operation={{ begin: beginOperation, busy, finish: finishOperation }}
        />
      )}
    </section>
  )
}
