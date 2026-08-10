import { useState } from 'react'
import { IconCheck, IconLoader2, IconRefresh, IconSearch } from '@tabler/icons-react'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import {
  addModelServiceModel,
  refreshModelService,
  setModelCapabilityBinding,
  verifyModelServiceModel,
  type ModelCapabilityBinding,
  type ModelServiceConnection,
  type RuntimeConnection,
} from '@/runtime/client'
import { useI18n } from '@/shared/i18n/i18n'
import {
  defaultModelProtocol,
  type ModelCapabilityName,
  type ModelProtocol,
} from './modelServiceModels'
import type { ModelServiceOperationController } from './useModelServiceOperation'

type ModelTestState = 'testing' | 'verified' | 'failed'

function capabilityTranslationKey(capability: ModelCapabilityName) {
  if (capability === 'agent_chat') return 'settings.modelServices.purpose.agentChat'
  if (capability === 'image_understanding') return 'settings.modelServices.purpose.imageUnderstanding'
  if (capability === 'image_generation') return 'settings.modelServices.purpose.imageGeneration'
  return 'settings.modelServices.purpose.imageEditing'
}

export function ModelCompatibilityDialog({
  bindings,
  config,
  initialService,
  load,
  onChanged,
  onClose,
  operation,
}: {
  bindings: ModelCapabilityBinding[]
  config?: RuntimeConnection | null
  initialService: ModelServiceConnection
  load: () => Promise<ModelServiceConnection[]>
  onChanged?: () => void
  onClose: () => void
  operation: ModelServiceOperationController
}) {
  const { t } = useI18n()
  const [service, setService] = useState(initialService)
  const [manualModel, setManualModel] = useState('')
  const [modelCapabilities, setModelCapabilities] = useState<Record<string, ModelCapabilityName>>({})
  const [modelProtocols, setModelProtocols] = useState<Record<string, ModelProtocol>>({})
  const [modelSearch, setModelSearch] = useState('')
  const [selectedModels, setSelectedModels] = useState<Record<string, boolean>>({})
  const [modelTestStates, setModelTestStates] = useState<Record<string, ModelTestState>>({})
  const [error, setError] = useState('')

  const filteredModels = service.models.filter((model) => {
    const query = modelSearch.trim().toLocaleLowerCase()
    return !query
      || model.model_id.toLocaleLowerCase().includes(query)
      || model.display_name.toLocaleLowerCase().includes(query)
  })
  let selectedModelCount = 0
  let selectedImageCapability = false
  for (const model of service.models) {
    if (!selectedModels[model.model_id]) continue
    selectedModelCount += 1
    const capability = modelCapabilities[`${service.id}:${model.model_id}`]
      ?? model.capabilities?.[0]?.capability
      ?? 'agent_chat'
    if (capability === 'image_generation' || capability === 'image_editing') {
      selectedImageCapability = true
    }
  }
  const modelPickerBusy = operation.busy === `verify:${service.id}`
  const refreshing = operation.busy === `refresh:${service.id}`

  async function refresh() {
    if (!config) return
    const run = operation.begin(`refresh:${service.id}`)
    if (run === undefined) return
    setError('')
    try {
      const refreshed = await refreshModelService(service.id, config)
      const nextServices = await load()
      setService(nextServices.find((item) => item.id === service.id) ?? refreshed)
      onChanged?.()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      operation.finish(run)
    }
  }

  async function addManualModel() {
    const modelID = manualModel.trim()
    if (!config || !modelID) return
    const run = operation.begin(service.id)
    if (run === undefined) return
    setError('')
    try {
      const addedModel = await addModelServiceModel(service.id, { model_id: modelID }, config)
      setManualModel('')
      const nextServices = await load()
      setService(
        nextServices.find((item) => item.id === service.id)
        ?? (addedModel ? { ...service, models: [...service.models, addedModel] } : service),
      )
      onChanged?.()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      operation.finish(run)
    }
  }

  async function verifySelectedModels() {
    if (!config) return
    const models = service.models.filter((model) => selectedModels[model.model_id])
    if (models.length === 0) return
    const run = operation.begin(`verify:${service.id}`)
    if (run === undefined) return
    const readyBindings = new Set<ModelCapabilityBinding['capability']>()
    for (const binding of bindings) {
      if (binding.status === 'ready') readyBindings.add(binding.capability)
    }
    const failures: string[] = []
    setError('')
    // ponytail: keep provider verification serial; add bounded concurrency only if latency proves costly.
    for (const model of models) {
      const key = `${service.id}:${model.model_id}`
      const capability = modelCapabilities[key] ?? model.capabilities?.[0]?.capability ?? 'agent_chat'
      const selectedCapability = (model.capabilities ?? []).find(
        (item) => item.capability === capability,
      )
      const protocol = modelProtocols[key]
        ?? selectedCapability?.protocol
        ?? defaultModelProtocol(service, capability)
      setModelTestStates((current) => ({ ...current, [model.model_id]: 'testing' }))
      try {
        await verifyModelServiceModel(service.id, model.model_id, { capability, protocol }, config)
        if (
          (capability === 'image_generation' || capability === 'image_editing')
          && !readyBindings.has(capability)
        ) {
          await setModelCapabilityBinding(
            capability,
            { model_spec: `local:${service.id}:${model.model_id}` },
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
      setService(nextServices.find((item) => item.id === service.id) ?? service)
      if (failures.length === 0) setSelectedModels({})
      else setError(failures[0])
      onChanged?.()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      operation.finish(run)
    }
  }

  async function makeDefault(
    modelID: string,
    capability: ModelCapabilityBinding['capability'],
  ) {
    if (!config) return
    const run = operation.begin(service.id)
    if (run === undefined) return
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
      operation.finish(run)
    }
  }

  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open && !modelPickerBusy) onClose()
      }}
    >
      <DialogContent className="settings-model-picker-dialog sm:max-w-[680px]">
        <DialogHeader className="settings-model-picker-header">
          <div>
            <DialogTitle>{t('settings.modelServices.modelPickerTitle')}</DialogTitle>
            <DialogDescription>
              {t('settings.modelServices.modelPickerDescription', { name: service.name })}
            </DialogDescription>
          </div>
          <Button
            type="button"
            variant="ghost"
            size="icon-sm"
            aria-label={t('settings.modelServices.refresh')}
            disabled={refreshing || modelPickerBusy}
            onClick={() => void refresh()}
          >
            <IconRefresh className={refreshing ? 'animate-spin' : undefined} />
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
          <span>{t('settings.modelServices.modelCount', { count: service.models.length })}</span>
          <span>{t('settings.modelServices.selectedCount', { count: selectedModelCount })}</span>
        </div>

        <div className="settings-model-picker-list">
          {filteredModels.length === 0 ? (
            <div className="settings-model-picker-empty">
              {t('settings.modelServices.noMatchingModels')}
            </div>
          ) : filteredModels.map((model) => {
            const key = `${service.id}:${model.model_id}`
            const selectedForTest = Boolean(selectedModels[model.model_id])
            const capability = modelCapabilities[key]
              ?? model.capabilities?.[0]?.capability
              ?? 'agent_chat'
            const selectedCapability = (model.capabilities ?? []).find(
              (item) => item.capability === capability,
            )
            const protocol = modelProtocols[key]
              ?? selectedCapability?.protocol
              ?? defaultModelProtocol(service, capability)
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
                      if (
                        item.capability !== 'image_generation'
                        && item.capability !== 'image_editing'
                      ) return null
                      const capability = item.capability
                      const binding = bindings.find((candidate) => candidate.capability === capability)
                      const selectedByDefault = binding?.status === 'ready'
                        && binding.connection_id === service.id
                        && binding.model_id === model.model_id
                      return selectedByDefault ? (
                        <span key={item.capability}>{t('settings.modelServices.defaultCapability')}</span>
                      ) : (
                        <button
                          type="button"
                          key={item.capability}
                          disabled={modelPickerBusy}
                          onClick={() => void makeDefault(model.model_id, capability)}
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
                            [key]: defaultModelProtocol(service, nextCapability),
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

        {service.preset_id === 'custom' && (
          <details className="settings-model-picker-manual">
            <summary>{t('settings.modelServices.addManualModel')}</summary>
            <div>
              <Input
                value={manualModel}
                aria-label={t('settings.modelServices.modelId')}
                placeholder={t('settings.modelServices.modelId')}
                onChange={(event) => setManualModel(event.target.value)}
              />
              <Button
                type="button"
                variant="outline"
                disabled={!manualModel.trim() || operation.busy === service.id}
                onClick={() => void addManualModel()}
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
            <Button type="button" variant="outline" disabled={modelPickerBusy} onClick={onClose}>
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
      </DialogContent>
    </Dialog>
  )
}
