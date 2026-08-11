import { useEffect, useMemo, useRef, useState } from 'react'
import {
  IconBrain,
  IconCheck,
  IconChevronDown,
  IconInfoCircle,
  IconSparkles,
} from '@tabler/icons-react'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { useI18n } from '@/shared/i18n/i18n'
import type { ChatMode } from '@/shared/local-data/types'
import { readReasoningMode, writeReasoningMode } from '@/features/app/appStorage'
import type { ReasoningMode, RuntimeModelSpec } from '@shejane/runtime-sdk'

const OFF_REASONING_MODES: ReasoningMode[] = ['off']
const REASONING_MODES: ReasoningMode[] = ['off', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max']

/** One selectable Runtime model. */
export interface ModelOption {
  id: RuntimeModelSpec
  label: string
  description?: string
  vendor?: string
  vendor_info?: string
  imageInputs: boolean
  recommended?: boolean
  reasoningModes?: ReasoningMode[]
  defaultReasoningMode?: ReasoningMode
}

/**
 * Composer-attached picker for concrete Runtime BYOK models.
 */
export function ModeSelector({
  mode,
  models,
  onChange,
  imageMode,
  imageModels = [],
  onImageModeChange,
  onConfigureModels,
  onRefreshCurrent,
  disabled = false,
}: {
  mode: ChatMode
  models: ModelOption[]
  onChange: (next: ChatMode) => void
  imageMode?: ChatMode
  imageModels?: ModelOption[]
  onImageModeChange?: (next: ChatMode) => void
  onConfigureModels?: () => void
  onRefreshCurrent?: () => void
  disabled?: boolean
}) {
  const { t } = useI18n()
  const [open, setOpen] = useState(false)
  const confirmedServiceChange = useRef(false)
  const selectedModel = models.find((model) => model.id === mode)
  const supportedReasoningModes = selectedModel?.reasoningModes ?? OFF_REASONING_MODES
  const defaultReasoningMode = selectedModel?.defaultReasoningMode ?? 'off'
  const storedReasoningMode = readReasoningMode(mode)
  const storedReasoningModeIsSupported = storedReasoningMode !== undefined
    && supportedReasoningModes.includes(storedReasoningMode)
  const preferredReasoningMode = storedReasoningModeIsSupported
    ? storedReasoningMode
    : supportedReasoningModes.includes(defaultReasoningMode) ? defaultReasoningMode : 'off'
  const [reasoningMode, setReasoningMode] = useState<ReasoningMode>(preferredReasoningMode)
  const groupedModels = useMemo(() => groupModelsByVendor(models), [models])
  const groupedImageModels = useMemo(() => groupModelsByVendor(imageModels), [imageModels])
  const recommendedGroups = groupedModels
    .filter((group) => group.models.some((model) => model.recommended))
    .map((group) => ({
      ...group,
      models: [
        ...group.models.filter((model) => model.recommended),
        ...group.models.filter((model) => !model.recommended),
      ],
    }))
  const moreGroups = groupedModels
    .filter((group) => group.models.every((model) => !model.recommended))
  const selectedLabel = selectedModel?.label ?? t('composer.mode.chooseModel')
  const reasoningLabel = t(`composer.reasoning.${reasoningMode}`)

  useEffect(() => {
    setReasoningMode(preferredReasoningMode)
    if (storedReasoningMode !== preferredReasoningMode) {
      writeReasoningMode(preferredReasoningMode, mode)
    }
  }, [mode, preferredReasoningMode, storedReasoningMode, storedReasoningModeIsSupported])

  const selectReasoningMode = (next: ReasoningMode) => {
    if (!supportedReasoningModes.includes(next)) return
    setReasoningMode(next)
    writeReasoningMode(next, mode)
  }

  const handleOpenChange = (nextOpen: boolean) => {
    setOpen(nextOpen)
    if (nextOpen) onRefreshCurrent?.()
  }

  const selectModel = (model: ModelOption) => {
    const currentConnection = connectionID(mode)
    const nextConnection = connectionID(model.id)
    if (
      currentConnection
      && nextConnection
      && currentConnection !== nextConnection
      && !confirmedServiceChange.current
    ) {
      if (!window.confirm(t('composer.mode.serviceChangeConfirm'))) return
      confirmedServiceChange.current = true
    }
    const nextModes = model.reasoningModes ?? OFF_REASONING_MODES
    const storedNextMode = readReasoningMode(model.id)
    const fallback = nextModes.includes(model.defaultReasoningMode ?? 'off')
      ? model.defaultReasoningMode ?? 'off'
      : 'off'
    const nextMode = storedNextMode && nextModes.includes(storedNextMode)
      ? storedNextMode
      : fallback
    setReasoningMode(nextMode)
    if (storedNextMode !== undefined && storedNextMode !== nextMode) {
      writeReasoningMode(nextMode, model.id)
    }
    onChange(model.id)
  }

  if (models.length === 0) {
    const configureLabel = t('composer.mode.configureModels')
    return (
      <button
        type="button"
        className="composer-mode-trigger"
        aria-label={configureLabel}
        title={configureLabel}
        disabled={disabled || !onConfigureModels}
        onClick={onConfigureModels}
      >
        <IconSparkles size={14} aria-hidden="true" />
        <span className="composer-mode-trigger-label">{configureLabel}</span>
      </button>
    )
  }

  const renderModel = (
    model: ModelOption,
    active: boolean,
    select: (model: ModelOption) => void,
    hint: string,
  ) => {
    return (
      <DropdownMenuItem
        key={model.id}
        className={`composer-mode-item composer-mode-model-item${active ? ' is-active' : ''}`}
        onSelect={() => select(model)}
      >
        <span className="composer-mode-item-text">
          <span className="composer-mode-item-label">{model.label}</span>
          <span className="composer-mode-item-hint">
            {hint}
          </span>
        </span>
        <span className="composer-mode-item-side">
          {active ? (
            <IconCheck size={14} aria-hidden="true" className="composer-mode-item-check" />
          ) : (
            <span aria-hidden="true" className="composer-mode-item-spacer" />
          )}
        </span>
      </DropdownMenuItem>
    )
  }

  const renderGroup = (
    group: (typeof groupedModels)[number],
    kind: 'chat' | 'image',
  ) => (
    <div key={`${kind}:${group.vendor}`}>
      <div className="composer-mode-group-heading">
        <span className="composer-mode-group-line" />
        <span className="composer-mode-group-label">
          {group.vendor}
          <Tooltip>
            <TooltipTrigger asChild>
              <span
                className="composer-mode-vendor-info-trigger"
                aria-label={group.vendorInfo}
                title={group.vendorInfo}
                tabIndex={0}
              >
                <IconInfoCircle size={12} strokeWidth={1.8} aria-hidden="true" />
              </span>
            </TooltipTrigger>
            <TooltipContent side="top" sideOffset={6}>
              {group.vendorInfo}
            </TooltipContent>
          </Tooltip>
        </span>
        <span className="composer-mode-group-line" />
      </div>
      {group.models.map((model) => renderModel(
        model,
        kind === 'chat' ? model.id === mode : model.id === imageMode,
        kind === 'chat' ? selectModel : (item) => onImageModeChange?.(item.id),
        kind === 'chat'
          ? model.imageInputs ? t('composer.mode.supportsImages') : t('composer.mode.textOnly')
          : t('composer.mode.imageGeneration'),
      ))}
    </div>
  )

  return (
    <>
      <DropdownMenu open={open} onOpenChange={handleOpenChange}>
        <DropdownMenuTrigger asChild disabled={disabled}>
          <button
            type="button"
            className="composer-mode-trigger"
            aria-label={t('composer.mode.menuLabel')}
            title={selectedLabel}
            disabled={disabled}
          >
            <IconSparkles size={14} aria-hidden="true" />
            <span className="composer-mode-trigger-label">{selectedLabel}</span>
            <IconChevronDown size={12} aria-hidden="true" className="composer-mode-trigger-chevron" />
          </button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" alignOffset={4} sideOffset={8} className="composer-mode-menu">
          <div className="composer-mode-model-list">
            <div className="composer-mode-section-label">{t('composer.mode.chatModels')}</div>
            {recommendedGroups.map((group) => renderGroup(group, 'chat'))}
            {moreGroups.map((group) => renderGroup(group, 'chat'))}
            {groupedImageModels.length > 0 ? (
              <>
                <div className="composer-mode-separator" />
                <div className="composer-mode-section-label">{t('composer.mode.imageModels')}</div>
                {groupedImageModels.map((group) => renderGroup(group, 'image'))}
              </>
            ) : null}
          </div>
        </DropdownMenuContent>
      </DropdownMenu>

      <DropdownMenu>
        <DropdownMenuTrigger asChild disabled={disabled}>
          <button
            type="button"
            className="composer-mode-trigger composer-reasoning-trigger"
            aria-label={t('composer.reasoning.menuLabel', { mode: reasoningLabel })}
            title={t('composer.reasoning.menuLabel', { mode: reasoningLabel })}
            disabled={disabled}
          >
            <IconBrain size={14} aria-hidden="true" />
            <span className="composer-mode-trigger-label">
              {t('composer.reasoning.button', { mode: reasoningLabel })}
            </span>
            <IconChevronDown size={12} aria-hidden="true" className="composer-mode-trigger-chevron" />
          </button>
        </DropdownMenuTrigger>
        <DropdownMenuContent
          align="end"
          sideOffset={8}
          className="composer-mode-menu composer-reasoning-menu"
        >
          {REASONING_MODES.filter((item) => supportedReasoningModes.includes(item)).map((item) => (
            <DropdownMenuItem
              key={item}
              className={`composer-mode-item composer-mode-model-item composer-reasoning-item${reasoningMode === item ? ' is-active' : ''}`}
              onSelect={() => selectReasoningMode(item)}
            >
              <span>{t(`composer.reasoning.${item}`)}</span>
              {reasoningMode === item ? (
                <IconCheck size={14} aria-hidden="true" className="composer-mode-item-check" />
              ) : null}
            </DropdownMenuItem>
          ))}
        </DropdownMenuContent>
      </DropdownMenu>
    </>
  )
}

function connectionID(mode: ChatMode): string | undefined {
  return mode ? mode.split(':', 3)[1] : undefined
}

function groupModelsByVendor(models: ModelOption[]): Array<{ vendor: string; vendorInfo: string; models: ModelOption[] }> {
  const groups: Array<{ vendor: string; vendorInfo: string; models: ModelOption[] }> = []
  const byVendor = new Map<string, { vendor: string; vendorInfo: string; models: ModelOption[] }>()
  for (const model of models) {
    const vendor = canonicalVendorName(model.vendor)
    let group = byVendor.get(vendor)
    if (!group) {
      group = { vendor, vendorInfo: model.vendor_info?.trim() || '', models: [] }
      byVendor.set(vendor, group)
      groups.push(group)
    } else if (!group.vendorInfo && model.vendor_info?.trim()) {
      group.vendorInfo = model.vendor_info.trim()
    }
    group.models.push(model)
  }
  for (const group of groups) {
    if (!group.vendorInfo) group.vendorInfo = vendorInfo(group.vendor)
  }
  return groups
}

function canonicalVendorName(vendor?: string): string {
  const trimmed = vendor?.trim()
  switch (trimmed?.toLowerCase()) {
    case 'deepseek':
      return 'DeepSeek'
    case 'xiaomi':
      return 'Xiaomi'
    case 'chatgpt':
      return 'ChatGPT'
    case 'openai':
      return 'OpenAI'
    case 'claude':
      return 'Claude'
    case 'anthropic':
      return 'Anthropic'
    case 'minimax':
      return 'MiniMax'
    case 'kimi':
      return 'Kimi'
    case 'qwen':
      return 'Qwen'
    case 'gemini':
      return 'Gemini'
    default:
      return trimmed || '其他'
  }
}

function vendorInfo(vendor: string): string {
  switch (vendor.toLowerCase()) {
    case 'deepseek':
      return '深度求索，推理能力与性价比突出。'
    case 'claude':
      return 'Anthropic 出品，擅长写作、代码与长文理解。'
    case 'chatgpt':
    case 'openai':
      return 'OpenAI 出品，通用能力全面。'
    case 'qwen':
      return '阿里通义千问，中文与多语言表现出色。'
    case 'kimi':
      return '月之暗面，擅长长上下文与长文档。'
    case 'gemini':
      return 'Google 出品，原生多模态能力突出。'
    case 'minimax':
      return 'MiniMax 出品，适合长上下文和 Agent 任务。'
    case 'xiaomi':
      return '小米模型，适合快速问答与编码辅助。'
    default:
      return `${vendor} 模型`
  }
}
