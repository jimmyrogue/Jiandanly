import { lazy, Suspense } from 'react'
import { IconDownload, IconSparkles, IconTrash } from '@tabler/icons-react'
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogMedia,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import { ChatThread } from '@/features/chat/components/ChatThread'
import { Composer } from '@/features/chat/components/Composer'
import { PendingApprovalBar } from '@/features/chat/components/PendingApprovalBar'
import { PendingPlanApprovalBar } from '@/features/chat/components/PendingPlanApprovalBar'
import { PendingQuestionBar } from '@/features/chat/components/PendingQuestionBar'
import { isAvailablePlugin } from '@/features/plugins/pluginAvailability'
import type { ChatMode } from '@/shared/local-data/types'
import type { Translator } from '@/shared/i18n/i18n'
import type { RuntimeConnection, RuntimeProbe } from '@/runtime/client'
import {
  hasRuntimeAuthorization,
  getLocalArtifactContent,
  listInstalledSkills,
  listLocalPlugins,
  getLocalPlugin,
  listMcpServers,
} from '@/runtime/client'

const ArtifactPanel = lazy(() => import('@/features/chat/components/ArtifactPanel').then((module) => ({ default: module.ArtifactPanel })))
const DocPreviewPanel = lazy(() => import('@/features/chat/components/DocPreviewPanel').then((module) => ({ default: module.DocPreviewPanel })))

function runtimeStatusLabel(
  runtime: RuntimeProbe | null,
  config: RuntimeConnection | null,
  t: Translator,
): string {
  if (!runtime?.online) return t('app.localStatus.runtimeOffline')
  if (!hasRuntimeAuthorization(config)) return t('app.localStatus.unpaired')
  return t('app.localStatus.connected')
}

export function AppChatWorkspace({ chat, common }: {
  chat: Record<string, unknown>
  common: Record<string, unknown>
}) {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const {
    activeConversation, activeDocument, activeWorkspace, appendInstructionToActiveRun,
    artifactPreview, cancelActiveRun, changeMode, changeImageMode,
    docPreviewRefreshKey, draft, dropAttachments, exportLocalRunDiagnostics,
    handleAgentFailureAction, handleDeleteMessage, handleEditResendMessage,
    handlePermissionDecision, handlePlanApprovalDecision, handleQuestionAnswer,
    handleRegenerateMessage, handleToolReconciliation, hasActiveRun, isSending,
    imageMode, imageModels, mode, modelRequiredOpen, models,
    openLocalArtifact, openLocalDocument, pendingApproval, pendingAttachments,
    pendingDeleteMessageID, pendingDiagnosticsRunID, pendingPlanApproval,
    pendingProject, pendingQuestion, permissionMode, removeAttachment,
    removeProjectFromActiveConversation, refreshCurrentModel, selectAttachments,
    selectProjectForActiveConversation, sendMessage, setActiveDocument,
    setArtifactPreview, setDraft, setModelRequiredOpen, setPendingDeleteMessageID,
    setPendingDiagnosticsRunID, setPermissionMode, showLocalFileContextMenu,
  } = chat as unknown as Record<string, any>
  const {
    isDesktop, runtime, runtimeConnection, t, setMainView,
    setModelServiceAddRequested, modelServiceAddRequested,
  } = common as unknown as Record<string, any>

  function openModelServiceSettings() {
    setModelServiceAddRequested(true)
    setMainView('settings')
  }

  return (
    <section className="workspace">
      <header className="topbar">
        <div className="chat-toolbar-title">
          <span>{(activeConversation as any)?.title ?? (t as any)('app.newChat')}</span>
        </div>
        {(isDesktop as boolean) ? (
          <div className="topbar-status">
            <span
              className={`topbar-runtime-dot${(runtime as any)?.online ? ' is-online' : ' is-offline'}`}
              title={runtimeStatusLabel(runtime as any, runtimeConnection as any, t as any)}
              aria-label={runtimeStatusLabel(runtime as any, runtimeConnection as any, t as any)}
            />
          </div>
        ) : null}
      </header>
      {(isDesktop as boolean) && !(runtime as any)?.online ? (
        <div className="status-banner status-banner-warning" role="status">
          <span className="status-banner-text">{(t as any)('topbar.bannerRuntimeOffline')}</span>
        </div>
      ) : null}

      <ChatThread
        conversation={activeConversation as any}
        workspaceRoot={(activeWorkspace as any)?.path}
        onOpenArtifact={(artifactID: string) => void (openLocalArtifact as any)(artifactID)}
        onLoadArtifactContent={runtimeConnection
          ? (artifactID: string) => getLocalArtifactContent(artifactID, runtimeConnection as any)
          : undefined}
        onOpenDiagnostics={setPendingDiagnosticsRunID as any}
        onPreviewLocalFile={openLocalDocument as any}
        onLocalFileContextMenu={(ref: any) => void (showLocalFileContextMenu as any)(ref)}
        onPickSuggestion={setDraft as any}
        onRegenerateMessage={handleRegenerateMessage as any}
        onEditResendMessage={handleEditResendMessage as any}
        onDeleteMessage={setPendingDeleteMessageID as any}
        onFailureAction={handleAgentFailureAction as any}
      />

      {artifactPreview && runtimeConnection ? (
        <Suspense fallback={null}>
          <ArtifactPanel
            artifact={artifactPreview as any}
            onClose={() => (setArtifactPreview as any)(null)}
            onLoadContent={(artifactID: string) => getLocalArtifactContent(artifactID, runtimeConnection as any)}
          />
        </Suspense>
      ) : null}
      {activeDocument ? (
        <Suspense fallback={null}>
          <DocPreviewPanel
            doc={activeDocument as any}
            refreshKey={docPreviewRefreshKey as number}
            onClose={() => (setActiveDocument as any)(null)}
          />
        </Suspense>
      ) : null}
      <AlertDialog open={modelRequiredOpen as boolean} onOpenChange={setModelRequiredOpen as any}>
        <AlertDialogContent className="conversation-delete-dialog">
          <AlertDialogHeader className="conversation-delete-header">
            <AlertDialogMedia className="conversation-delete-media">
              <IconSparkles aria-hidden="true" />
            </AlertDialogMedia>
            <AlertDialogTitle>{(t as any)('composer.modelRequired.title')}</AlertDialogTitle>
            <AlertDialogDescription>{(t as any)('composer.modelRequired.description')}</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter className="conversation-delete-footer">
            <AlertDialogCancel variant="outline" autoFocus>
              <span className="conversation-delete-button-label">{(t as any)('composer.modelRequired.later')}</span>
            </AlertDialogCancel>
            <AlertDialogAction onClick={openModelServiceSettings}>
              <span className="conversation-delete-button-label">{(t as any)('composer.modelRequired.openSettings')}</span>
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <AlertDialog
        open={Boolean(pendingDiagnosticsRunID)}
        onOpenChange={(open: boolean) => !open && (setPendingDiagnosticsRunID as any)(undefined)}
      >
        <AlertDialogContent className="conversation-delete-dialog">
          <AlertDialogHeader className="conversation-delete-header">
            <AlertDialogMedia className="conversation-delete-media">
              <IconDownload aria-hidden="true" />
            </AlertDialogMedia>
            <AlertDialogTitle>{(t as any)('diagnostics.downloadConfirmTitle')}</AlertDialogTitle>
            <AlertDialogDescription>{(t as any)('diagnostics.downloadConfirmBody')}</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter className="conversation-delete-footer">
            <AlertDialogCancel variant="outline" autoFocus>
              <span className="conversation-delete-button-label">{(t as any)('sidebar.dialog.cancel')}</span>
            </AlertDialogCancel>
            <AlertDialogAction
              onClick={() => {
                if (pendingDiagnosticsRunID) {
                  void (exportLocalRunDiagnostics as any)(pendingDiagnosticsRunID)
                  ;(setPendingDiagnosticsRunID as any)(undefined)
                }
              }}
            >
              <span className="conversation-delete-button-label">{(t as any)('diagnostics.downloadConfirm')}</span>
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <AlertDialog
        open={Boolean(pendingDeleteMessageID)}
        onOpenChange={(open: boolean) => !open && (setPendingDeleteMessageID as any)(undefined)}
      >
        <AlertDialogContent className="conversation-delete-dialog">
          <AlertDialogHeader className="conversation-delete-header">
            <AlertDialogMedia className="conversation-delete-media">
              <IconTrash aria-hidden="true" />
            </AlertDialogMedia>
            <AlertDialogTitle>{(t as any)('message.deleteConfirmTitle')}</AlertDialogTitle>
            <AlertDialogDescription>{(t as any)('message.deleteConfirmBody')}</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter className="conversation-delete-footer">
            <AlertDialogCancel variant="outline" autoFocus onClick={() => (setPendingDeleteMessageID as any)(undefined)}>
              <span className="conversation-delete-button-label">{(t as any)('sidebar.dialog.cancel')}</span>
            </AlertDialogCancel>
            <AlertDialogAction
              variant="destructive"
              onClick={() => {
                if (pendingDeleteMessageID) {
                  void (handleDeleteMessage as any)(pendingDeleteMessageID)
                  ;(setPendingDeleteMessageID as any)(undefined)
                }
              }}
            >
              <span className="conversation-delete-button-label">{(t as any)('message.delete')}</span>
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <div className="composer-dock">
        <PendingApprovalBar
          approval={pendingApproval as any}
          onDecision={handlePermissionDecision as any}
          onReconcile={(messageID: string, requestID: string, decision: any) => void (handleToolReconciliation as any)(messageID, requestID, decision)}
        />
        <PendingPlanApprovalBar
          key={(pendingPlanApproval as any)?.requestID ?? 'no-plan-approval'}
          plan={pendingPlanApproval as any}
          onDecision={(messageID: string, requestID: string, decision: any, instructions?: string) => void (handlePlanApprovalDecision as any)(messageID, requestID, decision, instructions)}
        />
        <PendingQuestionBar
          key={(pendingQuestion as any)?.requestID ?? 'no-question'}
          question={pendingQuestion as any}
          onAnswer={(messageID: string, requestID: string, answers: any) => void (handleQuestionAnswer as any)(messageID, requestID, answers)}
          onSkip={(messageID: string, requestID: string) => {
            if (!pendingQuestion) return
            const q = pendingQuestion as any
            const skipAnswers: Record<string, string[]> = {}
            for (const item of (q.questions as Array<{ question: string }>) ?? []) {
              skipAnswers[item.question] = []
            }
            void (handleQuestionAnswer as any)(messageID, requestID, skipAnswers)
          }}
          onCancel={() => void (cancelActiveRun as any)()}
        />

        <Composer
          draft={draft as string}
          onDraftChange={setDraft as any}
          isSending={isSending as boolean}
          hasActiveRun={hasActiveRun as boolean}
          onSend={() => void (sendMessage as any)()}
          onAppendInstruction={(hasActiveRun as boolean) ? () => void (appendInstructionToActiveRun as any)() : undefined}
          onStop={() => void (cancelActiveRun as any)()}
          listSkills={async () => {
            if (!runtimeConnection) return []
            const catalog = await listInstalledSkills(runtimeConnection as any)
            return catalog.skills
          }}
          listMcpServers={runtimeConnection ? async () => { const catalog = await listMcpServers(runtimeConnection as any); return catalog.servers } : undefined}
          listPlugins={runtimeConnection ? async () => { const plugins = await listLocalPlugins(runtimeConnection as any); return Promise.all(plugins.flatMap((plugin: any) => isAvailablePlugin(plugin) ? [getLocalPlugin(plugin.id, runtimeConnection as any)] : [])) } : undefined}
          mode={mode as ChatMode}
          models={models as any}
          onModeChange={changeMode as any}
          imageMode={imageMode as ChatMode}
          imageModels={imageModels as any}
          onImageModeChange={(next: ChatMode) => void (changeImageMode as any)(next)}
          permissionMode={permissionMode as any}
          onPermissionModeChange={setPermissionMode as any}
          onModelRequired={() => (setModelRequiredOpen as any)(true)}
          onConfigureModels={openModelServiceSettings}
          onRefreshCurrentModel={() => void (refreshCurrentModel as any)()}
          projectName={(activeConversation as any)?.project?.name ?? (pendingProject as any)?.name}
          onSelectProject={() => void (selectProjectForActiveConversation as any)()}
          onRemoveProject={() => void (removeProjectFromActiveConversation as any)()}
          attachments={pendingAttachments as any}
          onSelectAttachments={() => void (selectAttachments as any)()}
          onDropAttachments={dropAttachments as any}
          onRemoveAttachment={removeAttachment as any}
          isDesktop={isDesktop as boolean}
          slashCommandsEnabled={isDesktop as boolean}
        />
        <p className="composer-disclaimer">{(t as any)('composer.disclaimer')}</p>
      </div>
    </section>
  )
}
