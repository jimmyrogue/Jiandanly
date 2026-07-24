import { useEffect, useState } from 'react'
import { IconExternalLink, IconNotes } from '@tabler/icons-react'

import { Button } from '@/components/ui/button'
import { useI18n } from '@/shared/i18n/i18n'
import { fetchPptxOutline, fetchRunInputPptxOutline } from '@/runtime/client'
import type { PptxSlideOutline, RuntimeConnection } from '@/runtime/client'

interface Props {
  sourceKey: string
  name: string
  localPath?: string
  runId?: string
  inputId?: string
  loadBytes: () => Promise<ArrayBuffer>
  config: RuntimeConnection
  refreshKey?: number
  onStatus?: (status: 'loading' | 'ready' | 'error', error?: Error) => void
}

/**
 * Right-side preview for .pptx files. There's no mature pure-browser
 * PowerPoint renderer (docx-preview / ExcelJS have no equivalent), so
 * we render an outline view: one card per slide showing title,
 * bullets, and notes. The user clicks "Open in PowerPoint" to get the
 * real visual deck via the OS default app.
 */
export function PptxPreview({
  sourceKey,
  name,
  localPath,
  runId,
  inputId,
  loadBytes,
  config,
  refreshKey = 0,
  onStatus,
}: Props) {
  const { t } = useI18n()
  const [slides, setSlides] = useState<PptxSlideOutline[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [openError, setOpenError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    setError(null)
    setSlides(null)
    onStatus?.('loading')
    const outline = runId && inputId
      ? fetchRunInputPptxOutline(runId, inputId, config)
      : localPath
        ? fetchPptxOutline(localPath, config)
        : Promise.reject(new Error('PowerPoint source is unavailable'))
    outline
      .then((data) => {
        if (cancelled) return
        setSlides(data.slides)
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
    }
  }, [sourceKey, refreshKey, localPath, runId, inputId, config, onStatus])

  async function openNatively() {
    setOpenError(null)
    const bridge = window.shejaneClient
    if (runId && inputId && bridge?.openFileSnapshot) {
      try {
        const result = await bridge.openFileSnapshot({
          name,
          bytes: new Uint8Array(await loadBytes()),
          action: 'open',
        })
        if (result) setOpenError(result)
      } catch (err) {
        setOpenError(err instanceof Error ? err.message : String(err))
      }
    } else if (localPath && bridge?.openFileWithDefaultApp) {
      try {
        const result = await bridge.openFileWithDefaultApp(localPath)
        if (result) setOpenError(result)
      } catch (err) {
        setOpenError(err instanceof Error ? err.message : String(err))
      }
    }
  }

  if (error) {
    return (
      <div className="doc-preview-error" role="alert">
        <p>{t('pptxPreview.loadFailed', { error })}</p>
      </div>
    )
  }
  if (!slides) {
    return <div className="doc-preview-loading">…</div>
  }
  return (
    <div className="doc-preview-pptx" data-testid="pptx-preview">
      <div className="pptx-toolbar">
        <Button type="button" variant="outline" size="sm" onClick={() => void openNatively()}>
          <IconExternalLink size={14} />
          {t('pptxPreview.openNatively')}
        </Button>
      </div>
      {openError ? (
        <p className="doc-preview-open-error" role="alert">
          {t('pptxPreview.openFailed', { error: openError })}
        </p>
      ) : null}
      {slides.length === 0 ? (
        <div className="doc-preview-empty">{t('pptxPreview.empty')}</div>
      ) : (
        <ul className="pptx-slide-list" role="list">
          {slides.map((slide) => (
            <li key={slide.index} className="pptx-slide-card">
              <div className="pptx-slide-header">
                <span className="pptx-slide-number">
                  {t('pptxPreview.slideNumber', { n: slide.index + 1 })}
                </span>
                {slide.layout ? <span className="pptx-slide-layout">{slide.layout}</span> : null}
              </div>
              {slide.title ? <h3 className="pptx-slide-title">{slide.title}</h3> : null}
              {slide.bullets.length > 0 ? (
                <ul className="pptx-slide-bullets">
                  {keyTextOccurrences(slide.bullets).map(({ key, value }) => (
                    <li key={key}>{value}</li>
                  ))}
                </ul>
              ) : null}
              {slide.notes ? (
                <details className="pptx-slide-notes">
                  <summary>
                    <IconNotes size={12} />
                    {t('pptxPreview.notesLabel')}
                  </summary>
                  <p>{slide.notes}</p>
                </details>
              ) : null}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

function keyTextOccurrences(values: string[]): Array<{ key: string, value: string }> {
  const occurrences = new Map<string, number>()
  return values.map((value) => {
    const occurrence = occurrences.get(value) ?? 0
    occurrences.set(value, occurrence + 1)
    return { key: `${value}:${occurrence}`, value }
  })
}
