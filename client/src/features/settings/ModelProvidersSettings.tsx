import { useCallback, useEffect, useReducer, useState, type FormEvent } from 'react'
import { IconPhoto, IconPlus, IconTool, IconTrash } from '@tabler/icons-react'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { useI18n } from '@/shared/i18n/i18n'
import {
  deleteLocalModelProvider,
  discoverLocalModels,
  listLocalModelProviders,
  upsertLocalModelProvider,
  type DiscoveredLocalModel,
  type RuntimeConnection,
  type LocalModelProfile,
  type LocalModelProvider,
} from '@/runtime/client'

const PROVIDER_TEMPLATES = [
  { id: 'openai', name: 'OpenAI', kind: 'openai_compatible', baseURL: 'https://api.openai.com/v1' },
  { id: 'openrouter', name: 'OpenRouter', kind: 'openai_compatible', baseURL: 'https://openrouter.ai/api/v1' },
  { id: 'deepseek', name: 'DeepSeek', kind: 'openai_compatible', baseURL: 'https://api.deepseek.com/v1' },
  { id: 'anthropic', name: 'Anthropic', kind: 'anthropic', baseURL: 'https://api.anthropic.com' },
  { id: 'custom-openai', name: '', kind: 'openai_compatible', baseURL: '' },
  { id: 'custom-anthropic', name: '', kind: 'anthropic', baseURL: '' },
] as const

type ProviderTemplateID = typeof PROVIDER_TEMPLATES[number]['id']
type ProviderKind = LocalModelProvider['kind']
type ManualModelDraft = { id: string, value: string }
const compactTokens = new Intl.NumberFormat('en', { notation: 'compact', maximumFractionDigits: 1 })

function customProviderID(kind: ProviderKind) {
  return `custom-${kind === 'anthropic' ? 'anthropic' : 'openai'}-${Date.now().toString(36)}`.slice(0, 32)
}

function createManualModelDraft(value = ''): ManualModelDraft {
  return { id: crypto.randomUUID(), value }
}

type ProviderEditorState = {
  dialogOpen: boolean
  templateID: ProviderTemplateID
  providerID: string
  name: string
  providerKind: ProviderKind
  baseURL: string
  apiKey: string
  selectedModels: LocalModelProfile[]
  manualModels: ManualModelDraft[]
  modelQuery: string
  maxInputTokens: string
  maxOutputTokens: string
  requiresAPIKey: boolean
  discoveredModels: DiscoveredLocalModel[]
  manualModelID: boolean
  discovering: boolean
  savedCredentialConfigured: boolean
  editing: boolean
  saving: boolean
}

type ProviderEditorAction = { type: 'patch'; patch: Partial<ProviderEditorState> }

const initialProviderEditorState: ProviderEditorState = {
  dialogOpen: false,
  templateID: 'openai',
  providerID: 'openai',
  name: 'OpenAI',
  providerKind: 'openai_compatible',
  baseURL: 'https://api.openai.com/v1',
  apiKey: '',
  selectedModels: [],
  manualModels: [{ id: 'initial-model', value: '' }],
  modelQuery: '',
  maxInputTokens: '',
  maxOutputTokens: '',
  requiresAPIKey: true,
  discoveredModels: [],
  manualModelID: true,
  discovering: false,
  savedCredentialConfigured: false,
  editing: false,
  saving: false,
}

function providerEditorReducer(
  state: ProviderEditorState,
  action: ProviderEditorAction,
): ProviderEditorState {
  return { ...state, ...action.patch }
}

function useModelProvidersViewModel({
  config,
  onChanged,
}: {
  config?: RuntimeConnection | null
  onChanged?: () => void
}) {
  const { t } = useI18n()
  const [providers, setProviders] = useState<LocalModelProvider[]>([])
  const [editor, dispatchEditor] = useReducer(providerEditorReducer, initialProviderEditorState)
  const {
    apiKey,
    baseURL,
    dialogOpen,
    discoveredModels,
    discovering,
    editing,
    manualModelID,
    manualModels,
    maxInputTokens,
    maxOutputTokens,
    modelQuery,
    name,
    providerID,
    providerKind,
    requiresAPIKey,
    savedCredentialConfigured,
    saving,
    selectedModels,
    templateID,
  } = editor
  const patchEditor = (patch: Partial<ProviderEditorState>) => {
    dispatchEditor({ type: 'patch', patch })
  }
  const setAPIKey = (apiKey: string) => patchEditor({ apiKey })
  const setBaseURL = (baseURL: string) => patchEditor({ baseURL })
  const setDialogOpen = (dialogOpen: boolean) => patchEditor({ dialogOpen })
  const setMaxInputTokens = (maxInputTokens: string) => patchEditor({ maxInputTokens })
  const setMaxOutputTokens = (maxOutputTokens: string) => patchEditor({ maxOutputTokens })
  const setModelQuery = (modelQuery: string) => patchEditor({ modelQuery })
  const setName = (name: string) => patchEditor({ name })
  const [error, setError] = useState('')

  const refresh = useCallback(async () => {
    if (!config) {
      setProviders([])
      return
    }
    setProviders(await listLocalModelProviders(config))
  }, [config])

  useEffect(() => {
    void refresh().catch((reason) => setError(reason instanceof Error ? reason.message : String(reason)))
  }, [refresh])

  const selectTemplate = (nextID: ProviderTemplateID) => {
    const template = PROVIDER_TEMPLATES.find((candidate) => candidate.id === nextID)!
    patchEditor({
      templateID: nextID,
      providerID: nextID.startsWith('custom-') ? customProviderID(template.kind) : template.id,
      name: template.name,
      providerKind: template.kind,
      baseURL: template.baseURL,
      requiresAPIKey: true,
      apiKey: '',
      selectedModels: [],
      manualModels: [createManualModelDraft()],
      modelQuery: '',
      discoveredModels: [],
      manualModelID: true,
      savedCredentialConfigured: false,
    })
  }

  const startAdding = () => {
    patchEditor({
      ...initialProviderEditorState,
      dialogOpen: true,
    })
    setError('')
  }

  const editProvider = (provider: LocalModelProvider) => {
    const model = provider.models[0]
    const sharedMaxInputTokens = model?.max_input_tokens !== undefined
      && provider.models.every((candidate) => candidate.max_input_tokens === model.max_input_tokens)
      ? model.max_input_tokens
      : undefined
    const sharedMaxOutputTokens = model?.max_output_tokens !== undefined
      && provider.models.every((candidate) => candidate.max_output_tokens === model.max_output_tokens)
      ? model.max_output_tokens
      : undefined
    const knownTemplate = PROVIDER_TEMPLATES.find((template) => (
      template.id === provider.id && template.kind === provider.kind
    ))
    patchEditor({
      templateID: knownTemplate?.id ?? (
        provider.kind === 'anthropic' ? 'custom-anthropic' : 'custom-openai'
      ),
      providerID: provider.id,
      name: provider.name,
      providerKind: provider.kind,
      baseURL: provider.base_url,
      apiKey: '',
      selectedModels: provider.models,
      manualModels: provider.models.length > 0
        ? provider.models.map((candidate) => createManualModelDraft(candidate.model_id))
        : [createManualModelDraft()],
      modelQuery: '',
      maxInputTokens: sharedMaxInputTokens?.toString() ?? '',
      maxOutputTokens: sharedMaxOutputTokens?.toString() ?? '',
      requiresAPIKey: provider.requires_api_key,
      discoveredModels: provider.models.map((candidate) => ({ ...candidate })),
      manualModelID: false,
      savedCredentialConfigured: provider.credential_configured,
      editing: true,
      dialogOpen: true,
    })
    setError('')
  }

  const discoverModels = async () => {
    if (!config) return
    patchEditor({ discovering: true })
    setError('')
    try {
      const models = await discoverLocalModels(
        {
          provider_id: providerID,
          kind: providerKind,
          base_url: baseURL,
          api_key: apiKey || undefined,
        },
        config,
      )
      const discovered = [...models]
      for (const selected of selectedModels) {
        if (!discovered.some((candidate) => candidate.model_id === selected.model_id)) {
          discovered.push({
            ...selected,
          })
        }
      }
      patchEditor({ discoveredModels: discovered, modelQuery: '' })
      if (models.length === 0) {
        patchEditor({ manualModelID: true })
        setError(t('settings.models.noModelsFound'))
        return
      }
      patchEditor({ manualModelID: false })
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      patchEditor({ discovering: false })
    }
  }

  const toggleModel = (model: DiscoveredLocalModel) => {
    patchEditor({
      selectedModels: selectedModels.some((candidate) => candidate.model_id === model.model_id)
        ? selectedModels.filter((candidate) => candidate.model_id !== model.model_id)
        : [...selectedModels, {
          ...model,
        }],
    })
  }

  const updateManualModel = (id: string, value: string) => {
    const manualModelsNext = manualModels.map((candidate) => (
      candidate.id === id ? { ...candidate, value } : candidate
    ))
    const ids = [...new Set(manualModelsNext.flatMap((candidate) => {
      const value = candidate.value.trim()
      return value ? [value] : []
    }))]
    patchEditor({
      manualModels: manualModelsNext,
      selectedModels: ids.map((modelID) => selectedModels.find(
        (candidate) => candidate.model_id === modelID,
      ) ?? {
        model_id: modelID,
        display_name: modelID,
        tool_calling: true,
        streaming: true,
        image_inputs: false,
      }),
    })
  }

  const addManualModel = () => {
    patchEditor({ manualModels: [...manualModels, createManualModelDraft()] })
  }

  const useSelectedModelsManually = () => {
    patchEditor({
      manualModels: selectedModels.length > 0
        ? selectedModels.map((model) => createManualModelDraft(model.model_id))
        : [createManualModelDraft()],
      manualModelID: true,
    })
  }

  const showModelConfiguration = !requiresAPIKey || Boolean(apiKey.trim()) || savedCredentialConfigured
  const normalizedModelQuery = modelQuery.trim().toLocaleLowerCase()
  const visibleModels = normalizedModelQuery
    ? discoveredModels.filter((model) => `${model.display_name} ${model.model_id}`.toLocaleLowerCase().includes(normalizedModelQuery))
    : discoveredModels

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (!config) return
    patchEditor({ saving: true })
    setError('')
    try {
      await upsertLocalModelProvider(
        providerID,
        {
          name,
          kind: providerKind,
          base_url: baseURL,
          requires_api_key: requiresAPIKey,
          api_key: apiKey || undefined,
          models: selectedModels.map((model) => ({
            ...model,
            max_input_tokens: maxInputTokens
              ? Number(maxInputTokens)
              : model.max_input_tokens,
            max_output_tokens: maxOutputTokens
              ? Number(maxOutputTokens)
              : model.max_output_tokens,
          })),
          enabled: true,
        },
        config,
      )
      patchEditor({ apiKey: '', dialogOpen: false })
      await refresh()
      onChanged?.()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      patchEditor({ saving: false })
    }
  }

  const remove = async (provider: LocalModelProvider) => {
    if (!config || !window.confirm(t('settings.models.deleteConfirm', { name: provider.name }))) return
    setError('')
    try {
      await deleteLocalModelProvider(provider.id, config)
      await refresh()
      onChanged?.()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    }
  }

  return { addManualModel, apiKey, baseURL, config, dialogOpen, discoverModels, discovering, editProvider, editing, error, manualModelID, manualModels, maxInputTokens, maxOutputTokens, modelQuery, name, providers, remove, requiresAPIKey, saving, selectTemplate, selectedModels, setAPIKey, setBaseURL, setDialogOpen, setMaxInputTokens, setMaxOutputTokens, setModelQuery, setName, showModelConfiguration, startAdding, submit, t, templateID, toggleModel, updateManualModel, useSelectedModelsManually, visibleModels }
}

export function ModelProvidersSettings(props: Parameters<typeof useModelProvidersViewModel>[0]) {
  return <ModelProvidersSettingsView view={useModelProvidersViewModel(props)} />
}

function ModelProvidersSettingsView({ view }: { view: ReturnType<typeof useModelProvidersViewModel> }) {
  const { addManualModel, apiKey, baseURL, config, dialogOpen, discoverModels, discovering, editProvider, editing, error, manualModelID, manualModels, maxInputTokens, maxOutputTokens, modelQuery, name, providers, remove, requiresAPIKey, saving, selectTemplate, selectedModels, setAPIKey, setBaseURL, setDialogOpen, setMaxInputTokens, setMaxOutputTokens, setModelQuery, setName, showModelConfiguration, startAdding, submit, t, templateID, toggleModel, updateManualModel, useSelectedModelsManually, visibleModels } = view
  if (!config) {
    return <div className="settings-provider-empty">{t('settings.models.runtimeOffline')}</div>
  }
  return (
    <div className="settings-model-providers">
      {providers.length === 0 ? (
        <div className="settings-provider-empty">
          <strong>{t('settings.models.empty')}</strong>
          <span>{t('settings.models.emptyHint')}</span>
        </div>
      ) : providers.map((provider) => (
        <div className="settings-provider-row" key={provider.id}>
          <button type="button" className="settings-provider-summary" onClick={() => editProvider(provider)}>
            <span className="settings-row-label">{provider.name}</span>
            <span className="settings-row-hint">
              {provider.base_url} · {t('settings.models.modelCount', { count: provider.models.length })}
            </span>
          </button>
          <span className={`settings-provider-state${provider.credential_configured ? '' : ' missing'}`}>
            {provider.requires_api_key
              ? (provider.credential_configured ? t('settings.models.configured') : t('settings.models.missingCredential'))
              : t('settings.models.noCredentialNeeded')}
          </span>
          <button
            type="button"
            className="settings-provider-delete"
            aria-label={t('settings.models.delete')}
            onClick={() => void remove(provider)}
          >
            <IconTrash size={15} aria-hidden="true" />
          </button>
        </div>
      ))}

      <button type="button" className="settings-provider-add" onClick={startAdding}>
        <IconPlus size={16} aria-hidden="true" />
        {t('settings.models.add')}
      </button>

      <Dialog open={dialogOpen} onOpenChange={(open) => !saving && setDialogOpen(open)}>
        <DialogContent className="settings-provider-dialog sm:max-w-[480px]">
          <DialogHeader>
            <DialogTitle>{editing ? t('settings.models.editTitle') : t('settings.models.addTitle')}</DialogTitle>
            <DialogDescription>{t('settings.models.dialogDescription')}</DialogDescription>
          </DialogHeader>

          <form className="settings-provider-form" onSubmit={(event) => void submit(event)}>
            <label className="settings-provider-field">
              <span>{t('settings.models.providerType')}</span>
              <Select
                value={templateID}
                disabled={editing}
                onValueChange={(value) => selectTemplate(value as ProviderTemplateID)}
              >
                <SelectTrigger aria-label={t('settings.models.providerType')}>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {PROVIDER_TEMPLATES.map((template) => (
                    <SelectItem key={template.id} value={template.id}>
                      {template.id === 'custom-openai'
                        ? t('settings.models.customOpenAI')
                        : template.id === 'custom-anthropic'
                          ? t('settings.models.customAnthropic')
                          : template.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </label>

            {templateID.startsWith('custom-') ? (
              <label className="settings-provider-field">
                <span>{t('settings.models.providerName')}</span>
                <Input required value={name} onChange={(event) => setName(event.target.value)} />
              </label>
            ) : null}

            <label className="settings-provider-field">
              <span>{t('settings.models.baseURL')}</span>
              <Input required type="url" value={baseURL} onChange={(event) => setBaseURL(event.target.value)} />
            </label>

            {requiresAPIKey ? (
              <label className="settings-provider-field">
                <span>{t('settings.models.apiKey')}</span>
                <Input
                  required={!editing}
                  type="password"
                  autoComplete="off"
                  value={apiKey}
                  placeholder={editing ? t('settings.models.keepCredential') : undefined}
                  onChange={(event) => setAPIKey(event.target.value)}
                />
              </label>
            ) : null}

            {showModelConfiguration ? (
              <>
                <div className="settings-provider-field settings-provider-model-picker">
                  <div className="settings-provider-model-heading">
                    <span>{t('settings.models.model')}</span>
                    <button
                      type="button"
                      className="settings-row-button settings-provider-discover"
                      disabled={discovering || !baseURL || (requiresAPIKey && !editing && !apiKey)}
                      onClick={() => void discoverModels()}
                    >
                      {discovering ? t('settings.models.fetchingModels') : t('settings.models.fetchModels')}
                    </button>
                  </div>
                  {manualModelID ? (
                    <div className="settings-provider-manual-models">
                      {manualModels.map((model, index) => (
                        <div className="settings-provider-manual-row" key={model.id}>
                          <Input
                            aria-label={`${t('settings.models.modelId')} ${index + 1}`}
                            value={model.value}
                            placeholder={t('settings.models.modelIdHint')}
                            onChange={(event) => updateManualModel(model.id, event.target.value)}
                          />
                          {index === manualModels.length - 1 ? (
                            <button
                              type="button"
                              className="settings-provider-add-model"
                              aria-label={t('settings.models.addModel')}
                              disabled={!model.value.trim()}
                              onClick={addManualModel}
                            >
                              <IconPlus size={15} aria-hidden="true" />
                            </button>
                          ) : null}
                        </div>
                      ))}
                    </div>
                  ) : (
                    <div className="settings-provider-model-controls">
                      <Input
                        aria-label={t('settings.models.searchModels')}
                        value={modelQuery}
                        placeholder={t('settings.models.searchModels')}
                        onChange={(event) => setModelQuery(event.target.value)}
                      />
                      <div
                        className="settings-provider-model-list"
                        role="group"
                        aria-label={t('settings.models.model')}
                      >
                        {visibleModels.map((model) => {
                          const context = model.max_input_tokens
                            ? compactTokens.format(model.max_input_tokens)
                            : null
                          return <label className="settings-provider-model-choice" key={model.model_id}>
                            <input
                              type="checkbox"
                              checked={selectedModels.some((candidate) => candidate.model_id === model.model_id)}
                              aria-label={`${model.display_name} (${model.model_id})`}
                              onChange={() => toggleModel(model)}
                            />
                            <span className="settings-provider-model-option">
                              <span>{model.display_name}</span>
                              <span className="settings-provider-model-details">
                                {model.display_name !== model.model_id ? (
                                  <span className="settings-provider-model-id">{model.model_id}</span>
                                ) : null}
                                <span className="settings-provider-model-metadata">
                                  {context ? <span>{t('settings.models.contextWindow', { size: context })}</span> : null}
                                  {model.image_inputs ? (
                                    <span><IconPhoto size={12} aria-hidden="true" />{t('settings.models.imageCapability')}</span>
                                  ) : null}
                                  {model.tool_calling ? (
                                    <span><IconTool size={12} aria-hidden="true" />{t('settings.models.toolCapability')}</span>
                                  ) : null}
                                </span>
                              </span>
                            </span>
                          </label>
                        })}
                      </div>
                      <div className="settings-provider-model-footer">
                        <span>{t('settings.models.selectedCount', { count: selectedModels.length })}</span>
                        <button
                          type="button"
                          className="settings-provider-manual-model"
                          onClick={useSelectedModelsManually}
                        >
                          {t('settings.models.enterModelId')}
                        </button>
                      </div>
                    </div>
                  )}
                </div>

                <details className="settings-provider-advanced">
                  <summary>{t('settings.models.advanced')}</summary>
                  <div className="settings-provider-advanced-fields">
                    <div className="settings-provider-limits-row">
                      <label className="settings-provider-field">
                        <span>{t('settings.models.maxInputTokens')}</span>
                        <Input type="number" min={1} value={maxInputTokens} onChange={(event) => setMaxInputTokens(event.target.value)} />
                      </label>
                      <label className="settings-provider-field">
                        <span>{t('settings.models.maxOutputTokens')}</span>
                        <Input type="number" min={128} value={maxOutputTokens} onChange={(event) => setMaxOutputTokens(event.target.value)} />
                      </label>
                    </div>
                  </div>
                </details>
              </>
            ) : null}

            {error ? <p className="settings-provider-error">{error}</p> : null}
            <DialogFooter className="settings-provider-actions">
              <button type="button" className="settings-row-button" disabled={saving} onClick={() => setDialogOpen(false)}>
                {t('common.cancel')}
              </button>
              <button type="submit" className="settings-primary-button" disabled={saving || selectedModels.length === 0}>
                {saving ? t('settings.models.saving') : t('settings.models.save')}
              </button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </div>
  )
}
