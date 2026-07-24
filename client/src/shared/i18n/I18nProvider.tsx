import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react'

import {
  createTranslator,
  I18nContext,
  localeStorageKey,
  readStoredLocale,
  type I18nContextValue,
  type Locale,
} from './i18n'

export function I18nProvider({ children }: { children: ReactNode }) {
  const [locale, setLocaleState] = useState<Locale>(() => readStoredLocale())

  const setLocale = useCallback((nextLocale: Locale) => {
    setLocaleState(nextLocale)
    try {
      window.localStorage.setItem(localeStorageKey, nextLocale)
    } catch {
      // Ignore storage failures; the in-memory locale still changes.
    }
    void window.shejaneClient?.setLocale?.(nextLocale)
  }, [])

  useEffect(() => {
    document.documentElement.lang = locale === 'zh' ? 'zh-CN' : 'en'
    void window.shejaneClient?.setLocale?.(locale)
  }, [locale])

  const t = useMemo(() => createTranslator(locale), [locale])
  const value = useMemo<I18nContextValue>(() => ({ locale, setLocale, t }), [locale, setLocale, t])

  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>
}
