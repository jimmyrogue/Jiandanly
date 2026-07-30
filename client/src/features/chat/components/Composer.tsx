import { useRef, useState, type DragEvent } from 'react'
import {
  IconArrowUp,
  IconFolder,
  IconFolderPlus,
  IconPaperclip,
  IconPlayerStopFilled,
  IconShield,
  IconX,
} from '@tabler/icons-react'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { ModeSelector, type ModelOption } from './ModeSelector'
import { SkillEditor } from './SkillEditor'
import { useI18n } from '@/shared/i18n/i18n'
import { FileTypeIcon } from '@/shared/files/FileTypeIcon'
import type {
  InstalledSkill,
  McpServerInfo,
  PermissionMode,
  PluginDetail,
} from '@/runtime/client'
import type { ChatMode, LocalAttachmentRef } from '@/shared/local-data/types'

const EMPTY_MODELS: ModelOption[] = []
const EMPTY_ATTACHMENTS: LocalAttachmentRef[] = []
const PERMISSION_MODES: PermissionMode[] = ['ask', 'auto', 'full_access']

export function Composer({
  draft,
  onDraftChange,
  isSending,
  hasActiveRun = false,
  onSend,
  onAppendInstruction,
  onStop,
  listSkills,
  listMcpServers,
  listPlugins,
  mode,
  models = EMPTY_MODELS,
  onModeChange,
  imageMode,
  imageModels = EMPTY_MODELS,
  onImageModeChange,
  permissionMode = 'auto',
  onPermissionModeChange,
  onModelRequired,
  onConfigureModels,
  onRefreshCurrentModel,
  projectName,
  onSelectProject,
  onRemoveProject,
  attachments = EMPTY_ATTACHMENTS,
  onSelectAttachments,
  onDropAttachments,
  onRemoveAttachment,
  isDesktop = true,
  slashCommandsEnabled = true,
}: {
  draft: string
  onDraftChange: (value: string) => void
  isSending: boolean
  /** Runtime runs remain cancelable while paused for permission or input. */
  hasActiveRun?: boolean
  onSend: () => void
  /** Append a user instruction into the currently active Runtime run. */
  onAppendInstruction?: () => void
  onStop?: () => void
  listSkills: () => Promise<InstalledSkill[]>
  listMcpServers?: () => Promise<McpServerInfo[]>
  listPlugins?: () => Promise<PluginDetail[]>
  mode: ChatMode
  models?: ModelOption[]
  onModeChange: (mode: ChatMode) => void
  imageMode?: ChatMode
  imageModels?: ModelOption[]
  onImageModeChange?: (mode: ChatMode) => void
  permissionMode?: PermissionMode
  onPermissionModeChange?: (mode: PermissionMode) => void
  onModelRequired?: () => void
  onConfigureModels?: () => void
  onRefreshCurrentModel?: () => void
  projectName?: string
  onSelectProject?: () => void
  onRemoveProject?: () => void
  attachments?: LocalAttachmentRef[]
  onSelectAttachments?: () => void
  onDropAttachments?: (files: File[]) => void
  onRemoveAttachment?: (path: string) => void
  isDesktop?: boolean
  slashCommandsEnabled?: boolean
}) {
  const { t } = useI18n()
  const canStop = (isSending || hasActiveRun) && Boolean(onStop)
  const steeringMode = hasActiveRun && Boolean(onAppendInstruction)
  const modelAvailable = models.some((model) => model.id === mode)
  const sendLabel = steeringMode ? t('composer.appendInstruction') : t('composer.send')
  const sendTitle = steeringMode ? t('composer.appendInstruction') : t('composer.kbdHint')
  const handleSend = steeringMode ? onAppendInstruction : onSend
  const requestModel = onModelRequired ?? onConfigureModels
  const dragDepthRef = useRef(0)
  const [isDragging, setIsDragging] = useState(false)
  const canDropAttachments = isDesktop && !isSending && !hasActiveRun && Boolean(onDropAttachments)
  const submit = () => {
    if (!steeringMode && !modelAvailable) {
      requestModel?.()
      return
    }
    handleSend?.()
  }

  function handleDragEnter(event: DragEvent<HTMLElement>) {
    if (!event.dataTransfer.types.includes('Files')) return
    event.preventDefault()
    if (!canDropAttachments) return
    dragDepthRef.current += 1
    setIsDragging(true)
  }

  function handleDragLeave() {
    dragDepthRef.current = Math.max(0, dragDepthRef.current - 1)
    if (dragDepthRef.current === 0) setIsDragging(false)
  }

  function handleDragOver(event: DragEvent<HTMLElement>) {
    if (!event.dataTransfer.types.includes('Files')) return
    event.preventDefault()
    event.dataTransfer.dropEffect = canDropAttachments ? 'copy' : 'none'
  }

  function handleDrop(event: DragEvent<HTMLElement>) {
    if (!event.dataTransfer.types.includes('Files')) return
    event.preventDefault()
    dragDepthRef.current = 0
    setIsDragging(false)
    if (canDropAttachments) onDropAttachments?.(Array.from(event.dataTransfer.files))
  }

  return (
    <footer
      className={`composer${isDragging ? ' composer-dragging' : ''}`}
      onDragEnter={handleDragEnter}
      onDragLeave={handleDragLeave}
      onDragOver={handleDragOver}
      onDrop={handleDrop}
    >
      <div className="composer-input">
        {attachments.length ? (
          <div className="composer-chips">
            {attachments.map((attachment) => (
              <span className="composer-attachment-chip" key={attachment.path} title={attachment.path}>
                <FileTypeIcon name={attachment.name} size={14} />
                <span>{attachment.name}</span>
                <button
                  type="button"
                  aria-label={t('composer.attachment.remove', { name: attachment.name })}
                  disabled={!onRemoveAttachment || isSending || hasActiveRun}
                  onClick={() => onRemoveAttachment?.(attachment.path)}
                >
                  <IconX size={12} aria-hidden="true" />
                </button>
              </span>
            ))}
          </div>
        ) : null}
        <SkillEditor
          draft={draft}
          onDraftChange={onDraftChange}
          onSend={submit}
          listSkills={listSkills}
          listMcpServers={listMcpServers}
          listPlugins={listPlugins}
          commandsEnabled={slashCommandsEnabled}
          pluginReferencesEnabled={!steeringMode}
          placeholder={steeringMode ? t('composer.steeringPlaceholder') : t('composer.placeholder')}
        />
      </div>

      <div className="composer-toolbar">
        {isDesktop ? (
          <button
            type="button"
            className="composer-tool"
            aria-label={t('composer.attachment.add')}
            title={t('composer.attachment.tooltip')}
            disabled={!onSelectAttachments || isSending || hasActiveRun}
            onClick={() => onSelectAttachments?.()}
          >
            <IconPaperclip size={16} aria-hidden="true" />
          </button>
        ) : null}
        {!isDesktop ? null : projectName ? (
          <span
            className="composer-project-chip"
            title={t('composer.projectPicker.selected', { name: projectName })}
          >
            <button
              type="button"
              className="composer-project-select"
              aria-label={t('composer.projectPicker.replace', { name: projectName })}
              title={t('composer.projectPicker.replace', { name: projectName })}
              disabled={!onSelectProject || isSending || hasActiveRun}
              onClick={() => onSelectProject?.()}
            >
              <IconFolder size={14} aria-hidden="true" />
              <span className="composer-project-chip-name">{projectName}</span>
            </button>
            <button
              type="button"
              className="composer-project-remove"
              aria-label={t('composer.projectPicker.remove', { name: projectName })}
              title={t('composer.projectPicker.remove', { name: projectName })}
              disabled={!onRemoveProject || isSending || hasActiveRun}
              onClick={() => onRemoveProject?.()}
            >
              <IconX size={12} aria-hidden="true" />
            </button>
          </span>
        ) : (
          <button
            type="button"
            className="composer-tool composer-project-button"
            aria-label={t('composer.projectPicker.add')}
            title={t('composer.projectPicker.tooltip')}
            disabled={!onSelectProject || isSending || steeringMode}
            onClick={() => onSelectProject?.()}
          >
            <IconFolderPlus size={16} aria-hidden="true" />
          </button>
        )}

        <PermissionModeSelector
          mode={permissionMode}
          onChange={onPermissionModeChange}
          disabled={isSending || hasActiveRun}
        />

        <ModeSelector
          mode={mode}
          models={models}
          onChange={onModeChange}
          imageMode={imageMode}
          imageModels={imageModels}
          onImageModeChange={onImageModeChange}
          onConfigureModels={onConfigureModels}
          onRefreshCurrent={onRefreshCurrentModel}
          disabled={isSending || steeringMode}
        />

        {canStop ? (
          <button
            type="button"
            className="composer-send composer-send-stop"
            aria-label={t('composer.stop')}
            title={t('composer.stop')}
            onClick={onStop}
          >
            <IconPlayerStopFilled size={14} aria-hidden="true" />
            <span className="sr-only">{t('composer.stop')}</span>
          </button>
        ) : null}

        {!canStop || steeringMode ? (
          <button
            type="button"
            className="composer-send"
            aria-label={sendLabel}
            disabled={
              steeringMode
                ? !draft.trim()
                : isSending || !draft.trim()
            }
            title={sendTitle}
            onClick={submit}
          >
            <IconArrowUp size={16} aria-hidden="true" />
            <span className="sr-only">{sendLabel}</span>
          </button>
        ) : null}
      </div>
    </footer>
  )
}

function PermissionModeSelector({
  mode,
  onChange,
  disabled,
}: {
  mode: PermissionMode
  onChange?: (mode: PermissionMode) => void
  disabled: boolean
}) {
  const { t } = useI18n()
  const label = t(`composer.permission.${mode}.label`)

  const select = (next: PermissionMode) => {
    if (next === 'full_access' && !window.confirm(t('composer.permission.fullAccessConfirm'))) {
      return
    }
    onChange?.(next)
  }

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild disabled={disabled || !onChange}>
        <button
          type="button"
          className={`composer-permission-trigger${mode === 'full_access' ? ' is-full-access' : ''}`}
          aria-label={t('composer.permission.menuLabel', { mode: label })}
          title={label}
          disabled={disabled || !onChange}
        >
          <IconShield size={15} aria-hidden="true" />
          <span>{label}</span>
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" sideOffset={8} className="composer-permission-menu">
        {PERMISSION_MODES.map((item) => (
          <DropdownMenuItem
            key={item}
            className="composer-permission-item"
            onSelect={() => select(item)}
          >
            <span>
              <strong>{t(`composer.permission.${item}.label`)}</strong>
              <small>{t(`composer.permission.${item}.description`)}</small>
            </span>
            {item === mode ? <span aria-hidden="true">✓</span> : null}
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
