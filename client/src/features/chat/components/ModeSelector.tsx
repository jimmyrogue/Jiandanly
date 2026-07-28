import { useMemo, useRef, useState } from 'react'
import {
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
import type { RuntimeModelSpec } from '@shejane/runtime-sdk'

/** One selectable Runtime model. */
export interface ModelOption {
  id: RuntimeModelSpec
  label: string
  description?: string
  vendor?: string
  vendor_info?: string
  imageInputs: boolean
  recommended?: boolean
}

/**
 * Composer-attached picker for concrete Runtime BYOK models.
 */
export function ModeSelector({
  mode,
  models,
  onChange,
  onConfigureModels,
  onRefreshCurrent,
  disabled = false,
}: {
  mode: ChatMode
  models: ModelOption[]
  onChange: (next: ChatMode) => void
  onConfigureModels?: () => void
  onRefreshCurrent?: () => void
  disabled?: boolean
}) {
  const { t } = useI18n()
  const [open, setOpen] = useState(false)
  const confirmedServiceChange = useRef(false)
  const selectedModel = models.find((model) => model.id === mode)
  const groupedModels = useMemo(() => groupModelsByVendor(models), [models])
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

  const renderModel = (model: ModelOption) => {
    const active = model.id === mode
    return (
      <DropdownMenuItem
        key={model.id}
        className={`composer-mode-item composer-mode-model-item${active ? ' is-active' : ''}`}
        onSelect={() => selectModel(model)}
      >
        <span className="composer-mode-item-text">
          <span className="composer-mode-model-label-row">
            <span className="composer-mode-item-label">{model.label}</span>
          </span>
          <span className="composer-mode-item-hint">
            {model.imageInputs ? t('composer.mode.supportsImages') : t('composer.mode.textOnly')}
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

  const renderGroup = (group: (typeof groupedModels)[number]) => (
    <div key={`${group.models[0]?.recommended ? 'recommended' : 'more'}:${group.vendor}`}>
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
      {group.models.map(renderModel)}
    </div>
  )

  return (
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
          {recommendedGroups.map(renderGroup)}
          {moreGroups.map(renderGroup)}
        </div>
      </DropdownMenuContent>
    </DropdownMenu>
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
