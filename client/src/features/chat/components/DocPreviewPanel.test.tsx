import { cleanup, render, screen, fireEvent } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { I18nProvider } from '@/shared/i18n/I18nProvider'
import { createTranslator } from '@/shared/i18n/i18n'
import type { OpenDocument } from '@/shared/local-data/types'

import { DocPreviewPanel } from './DocPreviewPanel'
import { buildPdfMetaSummary } from './pdfMetaSummary'

// docx-preview and exceljs both touch DOM APIs (canvas / blob) that jsdom
// only partially implements. We stub the subcomponents so this test focuses
// on the panel's shell behavior — wiring is verified by the integration
// smoke step, not unit tests.
vi.mock('./DocPreview/DocxPreview', () => ({
  DocxPreview: ({ sourceKey }: { sourceKey: string }) => (
    <div data-testid="docx-preview-stub">docx:{sourceKey}</div>
  ),
}))
vi.mock('./DocPreview/XlsxPreview', () => ({
  XlsxPreview: ({ sourceKey }: { sourceKey: string }) => (
    <div data-testid="xlsx-preview-stub">xlsx:{sourceKey}</div>
  ),
}))
vi.mock('./DocPreview/PdfPreview', () => ({
  PdfPreview: ({ sourceKey }: { sourceKey: string }) => (
    <div data-testid="pdf-preview-stub">pdf:{sourceKey}</div>
  ),
}))
vi.mock('./DocPreview/TextPreview', () => ({
  TextPreview: ({ sourceKey }: { sourceKey: string }) => (
    <div data-testid="text-preview-stub">text:{sourceKey}</div>
  ),
}))
vi.mock('./DocPreview/ImagePreview', () => ({
  ImagePreview: ({ sourceKey }: { sourceKey: string }) => (
    <div data-testid="image-preview-stub">image:{sourceKey}</div>
  ),
}))

function makeDoc(overrides: Partial<OpenDocument> = {}): OpenDocument {
  return {
    sourceKey: 'local:/tmp/report.docx',
    kind: 'word',
    name: 'report.docx',
    tooltip: '/tmp/report.docx',
    loadBytes: () => Promise.resolve(new ArrayBuffer(0)),
    ...overrides,
  }
}

afterEach(() => cleanup())

describe('DocPreviewPanel', () => {
  it('renders nothing visible when no document is open', () => {
    render(
      <I18nProvider>
        <DocPreviewPanel doc={null} onClose={vi.fn()} />
      </I18nProvider>,
    )
    expect(screen.queryByTestId('docx-preview-stub')).not.toBeInTheDocument()
    expect(screen.queryByTestId('xlsx-preview-stub')).not.toBeInTheDocument()
  })

  it('mounts the Word renderer when kind=word and shows the title', () => {
    const doc = makeDoc()
    render(
      <I18nProvider>
        <DocPreviewPanel doc={doc} onClose={vi.fn()} />
      </I18nProvider>,
    )
    expect(screen.getByTestId('docx-preview-stub')).toHaveTextContent(`docx:${doc.sourceKey}`)
    expect(screen.getByText('report.docx')).toBeInTheDocument()
    expect(screen.getByText('Word 文档')).toBeInTheDocument()
  })

  it('mounts the Excel renderer when kind=excel', () => {
    const doc = makeDoc({
      sourceKey: 'local:/tmp/q4.xlsx',
      kind: 'excel',
      name: 'q4.xlsx',
    })
    render(
      <I18nProvider>
        <DocPreviewPanel doc={doc} onClose={vi.fn()} />
      </I18nProvider>,
    )
    expect(screen.getByTestId('xlsx-preview-stub')).toHaveTextContent('xlsx:local:/tmp/q4.xlsx')
    expect(screen.queryByTestId('docx-preview-stub')).not.toBeInTheDocument()
    expect(screen.getByText('Excel 表格')).toBeInTheDocument()
  })

  it('calls onClose when the Sheet primitive close button is clicked', () => {
    const doc = makeDoc()
    const onClose = vi.fn()
    render(
      <I18nProvider>
        <DocPreviewPanel doc={doc} onClose={onClose} />
      </I18nProvider>,
    )
    // shadcn Sheet ships its own absolute-positioned close button with
    // sr-only "Close" text — we no longer add a duplicate inside the
    // header, so the test targets that built-in affordance.
    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('zoom buttons update the percentage label and the inline style on the stage', () => {
    const doc = makeDoc()
    render(
      <I18nProvider>
        <DocPreviewPanel doc={doc} onClose={vi.fn()} />
      </I18nProvider>,
    )

    // Sheet renders into a portal under document.body, so we can't use
    // the render() container — query the live document instead.
    const stage = document.body.querySelector('.doc-preview-zoom-stage') as HTMLElement | null
    expect(stage).not.toBeNull()
    expect(screen.getByText('100%')).toBeInTheDocument()
    expect(stage!.style.zoom).toBe('1')

    fireEvent.click(screen.getByTitle('放大'))
    expect(screen.getByText('110%')).toBeInTheDocument()
    expect(stage!.style.zoom).toBe('1.1')

    fireEvent.click(screen.getByTitle('缩小'))
    fireEvent.click(screen.getByTitle('缩小'))
    expect(screen.getByText('90%')).toBeInTheDocument()

    // The percentage label itself is a reset trigger.
    fireEvent.click(screen.getByText('90%'))
    expect(screen.getByText('100%')).toBeInTheDocument()
  })

  it('mounts the PDF renderer and shows the metadata badge in the subtitle', () => {
    const doc = makeDoc({
      sourceKey: 'local:/tmp/paper.pdf',
      kind: 'pdf',
      name: 'paper.pdf',
      metadata: { pages: 15, author: 'Vaswani et al.' },
    })
    render(
      <I18nProvider>
        <DocPreviewPanel doc={doc} onClose={vi.fn()} />
      </I18nProvider>,
    )
    expect(screen.getByTestId('pdf-preview-stub')).toHaveTextContent('pdf:local:/tmp/paper.pdf')
    // Subtitle = kind label · pages · author (proves Layer A
    // metadata threaded all the way to the header).
    expect(screen.getByText('PDF 文档 · 15 页 · Vaswani et al.')).toBeInTheDocument()
  })

  it.each([
    ['code', 'local:/tmp/server.ts', '代码文件', 'text-preview-stub'],
    ['text', 'local:/tmp/notes.txt', '文本文件', 'text-preview-stub'],
    ['image', 'local:/tmp/photo.png', '图片', 'image-preview-stub'],
  ] as const)('mounts the %s renderer', (kind, sourceKey, label, testId) => {
    render(
      <I18nProvider>
        <DocPreviewPanel
          doc={makeDoc({ kind, sourceKey, name: sourceKey.split('/').at(-1) ?? sourceKey })}
          onClose={vi.fn()}
        />
      </I18nProvider>,
    )
    expect(screen.getByTestId(testId)).toHaveTextContent(sourceKey)
    expect(screen.getByText(label)).toBeInTheDocument()
  })

  it('omits the metadata badge for a PDF with no metadata', () => {
    const doc = makeDoc({
      sourceKey: 'local:/tmp/bare.pdf',
      kind: 'pdf',
      name: 'bare.pdf',
      // no metadata field
    })
    render(
      <I18nProvider>
        <DocPreviewPanel doc={doc} onClose={vi.fn()} />
      </I18nProvider>,
    )
    // Subtitle is just the kind label — no trailing " · ".
    expect(screen.getByText('PDF 文档')).toBeInTheDocument()
    expect(screen.queryByText(/· /)).not.toBeInTheDocument()
  })

  it('clamps stored width to the viewport so an oversized saved value does not break layout', () => {
    // Pre-seed a width way beyond the viewport. The panel reads from
    // localStorage on mount and should clamp on first render.
    window.localStorage.setItem('shejane.docPreview.width', '99999')
    const doc = makeDoc()
    render(
      <I18nProvider>
        <DocPreviewPanel doc={doc} onClose={vi.fn()} />
      </I18nProvider>,
    )
    const sheet = document.body.querySelector('.doc-preview-panel') as HTMLElement | null
    expect(sheet).not.toBeNull()
    const widthPx = Number((sheet!.style.width || '0').replace('px', ''))
    expect(widthPx).toBeGreaterThan(0)
    expect(widthPx).toBeLessThanOrEqual(Math.floor(window.innerWidth * 0.95))
    window.localStorage.removeItem('shejane.docPreview.width')
  })
})

describe('buildPdfMetaSummary', () => {
  const t = createTranslator('zh')

  it('returns empty string for undefined metadata', () => {
    expect(buildPdfMetaSummary(undefined, t)).toBe('')
  })

  it('returns empty string when metadata has no surfaced fields', () => {
    // creator/producer aren't surfaced — only pages/author/encrypted.
    expect(buildPdfMetaSummary({ creator: 'LaTeX', producer: 'pdfTeX' }, t)).toBe('')
  })

  it('joins pages + author with a middot', () => {
    expect(buildPdfMetaSummary({ pages: 15, author: 'Vaswani et al.' }, t)).toBe('15 页 · Vaswani et al.')
  })

  it('shows pages alone when author is missing', () => {
    expect(buildPdfMetaSummary({ pages: 7 }, t)).toBe('7 页')
  })

  it('ignores a zero/negative page count', () => {
    expect(buildPdfMetaSummary({ pages: 0, author: 'X' }, t)).toBe('X')
  })

  it('appends the encrypted marker', () => {
    expect(buildPdfMetaSummary({ pages: 3, encrypted: true }, t)).toBe('3 页 · 已加密')
  })

  it('trims whitespace-only author to nothing', () => {
    expect(buildPdfMetaSummary({ pages: 2, author: '   ' }, t)).toBe('2 页')
  })
})
