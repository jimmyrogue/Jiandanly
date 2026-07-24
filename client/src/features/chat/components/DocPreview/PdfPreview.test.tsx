import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, waitFor } from '@testing-library/react'

import { I18nProvider } from '@/shared/i18n/I18nProvider'

import { PdfPreview } from './PdfPreview'

afterEach(() => {
  vi.restoreAllMocks()
  Reflect.deleteProperty(URL, 'createObjectURL')
  Reflect.deleteProperty(URL, 'revokeObjectURL')
})

describe('PdfPreview', () => {
  it('releases its PDF URL when the preview closes', async () => {
    const createObjectURL = vi.fn().mockReturnValue('blob:pdf')
    const revokeObjectURL = vi.fn()
    Object.defineProperties(URL, {
      createObjectURL: { configurable: true, value: createObjectURL },
      revokeObjectURL: { configurable: true, value: revokeObjectURL },
    })

    const view = render(
      <I18nProvider>
        <PdfPreview sourceKey="report.pdf" loadBytes={() => Promise.resolve(new ArrayBuffer(1))} />
      </I18nProvider>,
    )
    await waitFor(() => expect(createObjectURL).toHaveBeenCalledTimes(1))

    view.unmount()

    expect(revokeObjectURL).toHaveBeenCalledWith('blob:pdf')
  })
})
