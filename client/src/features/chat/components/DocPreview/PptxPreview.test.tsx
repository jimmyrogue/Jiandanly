import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { I18nProvider } from '@/shared/i18n/I18nProvider'
import { fetchPptxOutline, fetchRunInputPptxOutline } from '@/runtime/client'

import { PptxPreview } from './PptxPreview'

vi.mock('@/runtime/client', () => ({
  fetchPptxOutline: vi.fn(),
  fetchRunInputPptxOutline: vi.fn(),
}))

const config = { baseURL: 'http://127.0.0.1:17371', session: 'client' as const }

beforeEach(() => {
  vi.mocked(fetchPptxOutline).mockReset()
  vi.mocked(fetchRunInputPptxOutline).mockReset()
})

afterEach(() => {
  cleanup()
  delete window.shejaneClient
})

describe('PptxPreview', () => {
  it('loads an immutable Runtime attachment outline by exact run/input IDs', async () => {
    vi.mocked(fetchRunInputPptxOutline).mockResolvedValue({
      slide_count: 1,
      slides: [{
        index: 0,
        layout: 'Title and Content',
        title: 'Quarterly result',
        bullets: ['Revenue increased'],
        notes: 'Internal note',
        shape_count: 2,
        image_count: 0,
      }],
    })

    render(
      <I18nProvider>
        <PptxPreview
          sourceKey="run:run-1:source"
          name="results.pptx"
          runId="run-1"
          inputId="source"
          loadBytes={() => Promise.resolve(new ArrayBuffer(0))}
          config={config}
        />
      </I18nProvider>,
    )

    expect(await screen.findByText('Quarterly result')).toBeInTheDocument()
    expect(screen.getByText('Revenue increased')).toBeInTheDocument()
    expect(fetchRunInputPptxOutline).toHaveBeenCalledWith('run-1', 'source', config)
    expect(fetchPptxOutline).not.toHaveBeenCalled()
  })

  it('opens the exact Runtime bytes with the original attachment name', async () => {
    vi.mocked(fetchRunInputPptxOutline).mockResolvedValue({ slide_count: 0, slides: [] })
    const openFileSnapshot = vi.fn().mockResolvedValue('')
    window.shejaneClient = { platform: 'darwin', openFileSnapshot }
    const bytes = new Uint8Array([80, 75, 3, 4]).buffer

    render(
      <I18nProvider>
        <PptxPreview
          sourceKey="run:run-1:source"
          name="真实结果.pptx"
          runId="run-1"
          inputId="source"
          loadBytes={() => Promise.resolve(bytes)}
          config={config}
        />
      </I18nProvider>,
    )

    fireEvent.click(await screen.findByRole('button', { name: '在 PowerPoint 中打开' }))
    await waitFor(() => expect(openFileSnapshot).toHaveBeenCalledTimes(1))
    expect(openFileSnapshot.mock.calls[0]?.[0]).toMatchObject({
      name: '真实结果.pptx',
      action: 'open',
    })
    expect(Array.from(openFileSnapshot.mock.calls[0]?.[0].bytes as Uint8Array)).toEqual([80, 75, 3, 4])
  })
})
