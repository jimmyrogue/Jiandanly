import { useCallback, type MutableRefObject } from 'react'
import type { Translator } from '@/shared/i18n/i18n'
import type {
  Conversation,
  ConversationProject,
  ConversationWorkspace,
} from '@/shared/local-data/types'
import type { RecoveryTarget } from '@/features/chat/recovery'
import { pathBasename } from '@/shared/files/path'
import {
  authorizeLocalWorkspace,
  diagnoseLocalWorkspace,
  getRuntimeConnection,
  hasRuntimeAuthorization,
  type LocalWorkspaceAuthorization,
  type LocalWorkspaceDiagnosis,
  type RuntimeConnection,
} from '@/runtime/client'
import { mergeAttachments } from './conversationState'
import { runtimeStoreActions } from './state/runtimeStore'
import { workspaceStoreActions } from './state/workspaceStore'

interface WorkspaceActionsContext {
  activeIDRef: MutableRefObject<string | undefined>
  hasActiveRun: boolean
  isSending: boolean
  retryRecoveryTarget: (target: RecoveryTarget) => Promise<void>
  runtimeConnection: RuntimeConnection | null
  saveActiveConversationWorkspace: (
    workspace: ConversationWorkspace | undefined,
  ) => Promise<void>
  setNotice: (message: string) => void
  t: Translator
  updateConversationMetadata: (
    conversationID: string,
    update: (conversation: Conversation) => void,
    options?: { touch?: boolean },
  ) => Promise<Conversation | undefined>
}

export function useWorkspaceActions({
  activeIDRef,
  hasActiveRun,
  isSending,
  retryRecoveryTarget,
  runtimeConnection,
  saveActiveConversationWorkspace,
  setNotice,
  t,
  updateConversationMetadata,
}: WorkspaceActionsContext) {
  const selectProjectForActiveConversation = useCallback(async (
    recoveryTarget?: RecoveryTarget,
  ) => {
    const config = runtimeConnection ?? getRuntimeConnection()
    if (!hasRuntimeAuthorization(config)) {
      setNotice(t('app.notice.runtimeNotPairedAuthorize'))
      return
    }
    if (!runtimeConnection) runtimeStoreActions.setConnection(config)
    const targetConversationID = recoveryTarget?.conversationID ?? activeIDRef.current
    const picked = await window.shejaneClient?.selectWorkspaceDirectory?.()
    if (!picked) return
    try {
      const authorized = await authorizeLocalWorkspace(picked, config)
      workspaceStoreActions.setAuthorizedWorkspaces((items) => upsertWorkspace(items, authorized))
      const name = pathBasename(authorized.path) || authorized.label || authorized.path
      const workspace: ConversationWorkspace = {
        path: authorized.path,
        label: authorized.label,
        authorized: true,
        authorizationId: authorized.id,
      }
      const project: ConversationProject = { name }
      if (targetConversationID) {
        await updateConversationMetadata(targetConversationID, (item) => {
          item.project = project
          item.workspace = workspace
        })
      } else {
        workspaceStoreActions.setPendingWorkspace(workspace)
        workspaceStoreActions.setPendingProject(project)
      }
      if (recoveryTarget) {
        setNotice(t('app.notice.workspaceBound', { label: name }))
        await retryRecoveryTarget(recoveryTarget)
        return
      }
      setNotice(t('project.notice.bound', { name }))
    } catch (error) {
      setNotice(error instanceof Error ? error.message : t('app.notice.workspaceAuthorizeFailed'))
    }
  }, [activeIDRef, retryRecoveryTarget, runtimeConnection, setNotice, t, updateConversationMetadata])

  const removeProjectFromActiveConversation = useCallback(async () => {
    if (isSending || hasActiveRun) return
    const conversationID = activeIDRef.current
    if (!conversationID) {
      workspaceStoreActions.setPendingWorkspace(undefined)
      workspaceStoreActions.setPendingProject(undefined)
      return
    }
    await updateConversationMetadata(conversationID, (conversation) => {
      delete conversation.workspace
      delete conversation.project
    })
  }, [activeIDRef, hasActiveRun, isSending, updateConversationMetadata])

  const addAttachmentPaths = useCallback((paths: string[]) => {
    if (!paths.length) return
    workspaceStoreActions.setPendingAttachments((current) => mergeAttachments(
      current,
      paths.map((path) => ({ path, name: pathBasename(path) || path })),
    ))
  }, [])

  const selectAttachments = useCallback(async () => {
    if (isSending || hasActiveRun) return
    const paths = await window.shejaneClient?.selectAttachmentFiles?.()
    addAttachmentPaths(paths ?? [])
  }, [addAttachmentPaths, hasActiveRun, isSending])

  const dropAttachments = useCallback((files: File[]) => {
    if (isSending || hasActiveRun) return
    const getPathForFile = window.shejaneClient?.getPathForFile
    if (!getPathForFile) return
    addAttachmentPaths(files.flatMap((file) => {
      try {
        const path = getPathForFile(file)
        return path ? [path] : []
      } catch {
        return []
      }
    }))
  }, [addAttachmentPaths, hasActiveRun, isSending])

  const removeAttachment = useCallback((path: string) => {
    if (isSending || hasActiveRun) return
    workspaceStoreActions.setPendingAttachments((items) => items.filter((item) => item.path !== path))
  }, [hasActiveRun, isSending])

  const authorizeWorkspace = useCallback(async (
    path: string,
  ): Promise<LocalWorkspaceAuthorization> => {
    if (!hasRuntimeAuthorization(runtimeConnection)) {
      throw new Error(t('app.notice.runtimeNotPairedAuthorize'))
    }
    const nextPath = path.trim()
    if (!nextPath) throw new Error(t('app.notice.emptyWorkspacePath'))
    const workspace = await authorizeLocalWorkspace(nextPath, runtimeConnection)
    workspaceStoreActions.setAuthorizedWorkspaces((items) => upsertWorkspace(items, workspace))
    await saveActiveConversationWorkspace({
      path: workspace.path,
      label: workspace.label,
      authorized: true,
      authorizationId: workspace.id,
    })
    setNotice(t('app.notice.workspaceBound', { label: workspace.label }))
    return workspace
  }, [runtimeConnection, saveActiveConversationWorkspace, setNotice, t])

  const diagnoseWorkspace = useCallback(async (path: string): Promise<LocalWorkspaceDiagnosis> => {
    if (!hasRuntimeAuthorization(runtimeConnection)) {
      throw new Error(t('app.notice.runtimeNotPairedDiagnose'))
    }
    const nextPath = path.trim()
    if (!nextPath) throw new Error(t('app.notice.emptyWorkspacePath'))
    return diagnoseLocalWorkspace(nextPath, runtimeConnection)
  }, [runtimeConnection, t])

  return {
    authorizeWorkspace,
    diagnoseWorkspace,
    dropAttachments,
    removeAttachment,
    removeProjectFromActiveConversation,
    selectAttachments,
    selectProjectForActiveConversation,
  }
}

export function findWorkspaceByPath(
  items: LocalWorkspaceAuthorization[],
  path: string,
): LocalWorkspaceAuthorization | undefined {
  const normalized = path.trim()
  return normalized ? items.find((item) => pathInsideWorkspace(item.path, normalized)) : undefined
}

function upsertWorkspace(
  items: LocalWorkspaceAuthorization[],
  workspace: LocalWorkspaceAuthorization,
): LocalWorkspaceAuthorization[] {
  return [workspace, ...items.filter((item) => item.id !== workspace.id && item.path !== workspace.path)]
}

function pathInsideWorkspace(root: string, target: string): boolean {
  const normalizedRoot = trimPath(root)
  const normalizedTarget = trimPath(target)
  if (!normalizedRoot || !normalizedTarget) return false
  return normalizedTarget === normalizedRoot
    || normalizedTarget.startsWith(`${normalizedRoot}/`)
    || normalizedTarget.startsWith(`${normalizedRoot}\\`)
}

function trimPath(path: string): string {
  return path.trim().replace(/[\\/]+$/u, '')
}
