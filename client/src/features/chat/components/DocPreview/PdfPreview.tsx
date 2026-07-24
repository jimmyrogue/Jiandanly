import { useEffect, useState } from 'react'

import { useI18n } from '@/shared/i18n/i18n'

interface Props {
  sourceKey: string
  loadBytes: () => Promise<ArrayBuffer>
  refreshKey?: number
  onStatus?: (status: 'loading' | 'ready' | 'error', error?: Error) => void
}

/**
 * Right-side preview for .pdf files.
 *
 * Implementation: Chromium has a built-in PDF viewer (the same one
 * the browser uses for `<a href="x.pdf">` clicks) — we just hand
 * it a blob URL via `<embed type="application/pdf">` and let the
 * native chrome handle pagination, zoom, and search. Zero extra
 * dependencies, no react-pdf bundle bloat (~700 KB), and the user
 * gets a familiar PDF UI.
 *
 * Lifecycle:
 *   - On mount / refreshKey bump / sourceKey change: fetch bytes,
 *     wrap in a Blob, `URL.createObjectURL`, plug into <embed>.
 *   - On unmount or before next fetch: `URL.revokeObjectURL` to
 *     release memory. Long-lived sessions opening many PDFs would
 *     otherwise leak megabytes per file.
 *   - Concurrent loads are guarded by a `cancelled` flag in the
 *     effect; if the user closes/reopens fast, only the last
 *     fetch's bytes win.
 */
export function PdfPreview({ sourceKey, loadBytes, refreshKey = 0, onStatus }: Props) {
  const { t } = useI18n()
  const [blobURL, setBlobURL] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    let cancelled = false
    let createdURL: string | null = null
    setError(null)
    setBlobURL(null)
    onStatus?.('loading')

    loadBytes()
      .then((bytes) => {
        if (cancelled) return
        const blob = new Blob([bytes], { type: 'application/pdf' })
        const url = URL.createObjectURL(blob)
        createdURL = url
        setBlobURL(url)
        onStatus?.('ready')
      })
      .catch((err: unknown) => {
        if (cancelled) return
        const e = err instanceof Error ? err : new Error(String(err))
        setError(e.message)
        onStatus?.('error', e)
      })

    return () => {
      cancelled = true
      if (createdURL) {
        URL.revokeObjectURL(createdURL)
      }
    }
  }, [sourceKey, refreshKey, loadBytes, onStatus])

  if (error) {
    return (
      <div className="doc-preview-error" role="alert">
        <p>{t('pdfPreview.loadFailed', { error })}</p>
      </div>
    )
  }
  if (!blobURL) {
    return <div className="doc-preview-loading">…</div>
  }
  return (
    <div className="doc-preview-pdf" data-testid="pdf-preview">
      {/* `<embed>` and `<iframe>` are interchangeable here — embed
       *  is slightly smaller in DOM weight and Chromium treats both
       *  paths identically for PDFs. Width/height are 100% so the
       *  zoom transform on the parent stage actually re-flows. */}
      <embed
        src={blobURL}
        type="application/pdf"
        title="PDF preview"
        className="doc-preview-pdf-embed"
      />
    </div>
  )
}
