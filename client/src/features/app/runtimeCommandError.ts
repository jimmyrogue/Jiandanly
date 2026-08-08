import type { Translator } from '@/shared/i18n/i18n'

export function runtimeCommandErrorMessage(error: unknown, t: Translator): string {
  return error instanceof Error ? error.message : t('app.notice.sendFailed')
}
