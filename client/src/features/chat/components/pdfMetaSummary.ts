import type { Translator } from '@/shared/i18n/i18n'
import type { PdfDocumentMetadata } from '@/shared/local-data/types'

export function buildPdfMetaSummary(
  metadata: PdfDocumentMetadata | undefined,
  t: Translator,
): string {
  if (!metadata) return ''
  const parts: string[] = []
  if (typeof metadata.pages === 'number' && metadata.pages > 0) {
    parts.push(t('docPreview.metaPages', { count: String(metadata.pages) }))
  }
  if (metadata.author?.trim()) {
    parts.push(metadata.author.trim())
  }
  if (metadata.encrypted) {
    parts.push(t('docPreview.metaEncrypted'))
  }
  return parts.join(' · ')
}
