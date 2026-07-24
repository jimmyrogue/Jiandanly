import { useEffect, useMemo, useState } from 'react'

interface Props {
  sourceKey: string
  loadBytes: () => Promise<ArrayBuffer>
  refreshKey?: number
  onStatus?: (status: 'loading' | 'ready' | 'error', error?: Error) => void
}

interface SheetView {
  name: string
  /** rows × cols of stringified cell values. Empty cells become "". */
  rows: string[][]
}

/**
 * Renders a .xlsx inline as a list of tabbed sheets.
 *
 * Source-agnostic (like DocxPreview): just consumes `loadBytes()`.
 * Sheet formatting (bold, colors) is not surfaced for v1; cell values
 * + structure cover ~90% of "let me see what's in here" cases.
 *
 * Row/column caps (1000 × 50) bound DOM size for huge sheets; if users
 * need more we'll swap in a virtual grid library.
 */
export function XlsxPreview({ sourceKey, loadBytes, refreshKey = 0, onStatus }: Props) {
  const [sheets, setSheets] = useState<SheetView[] | null>(null)
  const [error, setError] = useState<Error | null>(null)
  const [activeIndex, setActiveIndex] = useState(0)

  useEffect(() => {
    let cancelled = false
    setError(null)
    setSheets(null)
    onStatus?.('loading')
    const worker = new Worker(new URL('./xlsxPreview.worker.ts', import.meta.url), { type: 'module' })
    worker.onmessage = (event: MessageEvent<{ sheets?: SheetView[], error?: string }>) => {
      if (cancelled) return
      if (event.data.error) {
        const next = new Error(event.data.error)
        setError(next)
        onStatus?.('error', next)
        return
      }
      const out = event.data.sheets ?? []
        if (cancelled) return
        setSheets(out)
        setActiveIndex(0)
        onStatus?.('ready')
    }
    worker.onerror = (event) => {
      if (cancelled) return
      const next = new Error(event.message || 'Unable to parse workbook')
      setError(next)
      onStatus?.('error', next)
    }
    void loadBytes()
      .then((buf) => {
        if (!cancelled) worker.postMessage(buf, [buf])
      })
      .catch((err: unknown) => {
        if (cancelled) return
        const e = err instanceof Error ? err : new Error(String(err))
        setError(e)
        onStatus?.('error', e)
      })
    return () => {
      cancelled = true
      worker.terminate()
    }
  }, [sourceKey, refreshKey, loadBytes, onStatus])

  const activeSheet = useMemo(() => sheets?.[activeIndex] ?? null, [sheets, activeIndex])

  if (error) {
    return (
      <div className="doc-preview-error" role="alert">
        <p>{error.message}</p>
      </div>
    )
  }
  if (!sheets) {
    return <div className="doc-preview-loading">…</div>
  }
  if (sheets.length === 0) {
    return <div className="doc-preview-empty">(empty workbook)</div>
  }
  return (
    <div className="doc-preview-xlsx" data-testid="xlsx-preview">
      {sheets.length > 1 ? (
        <div className="xlsx-sheet-tabs" role="tablist">
          {sheets.map((sheet, i) => (
            <button
              key={sheet.name}
              role="tab"
              type="button"
              aria-selected={i === activeIndex}
              className={i === activeIndex ? 'xlsx-sheet-tab active' : 'xlsx-sheet-tab'}
              onClick={() => setActiveIndex(i)}
            >
              {sheet.name}
            </button>
          ))}
        </div>
      ) : null}
      {activeSheet ? <SheetTable rows={activeSheet.rows} /> : null}
    </div>
  )
}

function SheetTable({ rows }: { rows: string[][] }) {
  if (rows.length === 0) {
    return <div className="doc-preview-empty">(empty sheet)</div>
  }
  return (
    <div className="xlsx-sheet-scroll">
      <table className="xlsx-sheet-table">
        <tbody>
          {keyRowOccurrences(rows).map(({ key, row }) => (
            <tr key={key}>
              {row.map((cell, j) => (
                <td key={j}>{cell}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function keyRowOccurrences(rows: string[][]): Array<{ key: string, row: string[] }> {
  const occurrences = new Map<string, number>()
  return rows.map((row) => {
    const value = JSON.stringify(row)
    const occurrence = occurrences.get(value) ?? 0
    occurrences.set(value, occurrence + 1)
    return { key: `${value}:${occurrence}`, row }
  })
}
