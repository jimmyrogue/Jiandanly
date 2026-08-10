import { useState } from 'react'
import type { Translator } from '@/shared/i18n/i18n'
import type { LocalFileRef, OpenDocument } from '@/shared/local-data/types'
import {
  fetchRunInput,
  fetchWorkspaceFile,
  getLocalArtifact,
  type LocalArtifact,
  type RuntimeConnection,
} from '@/runtime/client'
import { downloadFile } from '@/shared/files/downloadFile'
import { filePreviewKind } from '@/shared/files/filePreview'

interface UseLocalDocumentsOptions {
  runtimeConnection: RuntimeConnection | null
  t: Translator
  setNotice: (message: string) => void
}

export function useLocalDocuments({
  runtimeConnection,
  t,
  setNotice,
}: UseLocalDocumentsOptions) {
  const [artifactPreview, setArtifactPreview] = useState<LocalArtifact | null>(null)
  const [activeDocument, setActiveDocument] = useState<OpenDocument | null>(null)
  // Bumped on doc.changed to force the renderer to re-fetch file bytes.
  const [docPreviewRefreshKey, setDocPreviewRefreshKey] = useState(0)

  function loadLocalFileBytes(ref: LocalFileRef): Promise<ArrayBuffer> {
    if (!runtimeConnection) {
      return Promise.reject(new Error(t('app.notice.runtimeDisconnected')))
    }
    if (ref.runId && ref.inputId) {
      return fetchRunInput(ref.runId, ref.inputId, runtimeConnection)
    }
    return fetchWorkspaceFile(ref.path, runtimeConnection)
  }

  /** Open supported files in the right panel; external-only files use their OS app. */
  function openLocalDocument(ref: LocalFileRef) {
    const kind = ref.kind ?? filePreviewKind(ref.name)
    if (!kind) {
      void openLocalFileNatively(ref)
      return
    }
    setActiveDocument({
      sourceKey: ref.runId && ref.inputId
        ? `run-input:${ref.runId}:${ref.inputId}`
        : `local:${ref.path}`,
      kind,
      name: ref.name,
      tooltip: ref.path,
      loadBytes: () => loadLocalFileBytes(ref),
      localPath: ref.path,
      runId: ref.runId,
      inputId: ref.inputId,
    })
    setDocPreviewRefreshKey((k) => k + 1)
  }

  async function openLocalFileNatively(ref: LocalFileRef) {
    try {
      const error = ref.runId && ref.inputId
        ? await window.shejaneClient?.openFileSnapshot?.({
          name: ref.name,
          bytes: new Uint8Array(await loadLocalFileBytes(ref)),
          action: 'open',
        })
        : await window.shejaneClient?.openFileWithDefaultApp?.(ref.path)
      if (error) setNotice(error)
    } catch (error) {
      setNotice(error instanceof Error ? error.message : String(error))
    }
  }

  async function revealLocalFile(ref: LocalFileRef) {
    try {
      if (ref.runId && ref.inputId) {
        const error = await window.shejaneClient?.openFileSnapshot?.({
          name: ref.name,
          bytes: new Uint8Array(await loadLocalFileBytes(ref)),
          action: 'reveal',
        })
        if (error) setNotice(error)
        return
      }
      await window.shejaneClient?.revealFileInFolder?.(ref.path)
    } catch (error) {
      setNotice(error instanceof Error ? error.message : String(error))
    }
  }

  async function downloadLocalFile(ref: LocalFileRef) {
    try {
      await downloadFile(ref.name, () => loadLocalFileBytes(ref))
    } catch (error) {
      setNotice(error instanceof Error ? error.message : String(error))
    }
  }

  async function showLocalFileContextMenu(ref: LocalFileRef) {
    const action = await window.shejaneClient?.showFileContextMenu?.({
      canPreview: Boolean(ref.kind ?? filePreviewKind(ref.name)),
    })
    if (action === 'preview') openLocalDocument(ref)
    if (action === 'open') await openLocalFileNatively(ref)
    if (action === 'save') await downloadLocalFile(ref)
    if (action === 'reveal') await revealLocalFile(ref)
  }

  async function openLocalArtifact(artifactID: string) {
    if (!runtimeConnection) {
      setNotice(t('app.notice.runtimeDisconnected'))
      return
    }
    setNotice('')
    try {
      setArtifactPreview(await getLocalArtifact(artifactID, runtimeConnection))
    } catch (error) {
      setNotice(error instanceof Error ? error.message : t('app.notice.artifactReadFailed'))
    }
  }

  return {
    activeDocument,
    artifactPreview,
    docPreviewRefreshKey,
    openLocalArtifact,
    openLocalDocument,
    setActiveDocument,
    setArtifactPreview,
    showLocalFileContextMenu,
  }
}
