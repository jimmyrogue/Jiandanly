import { createContext, useContext } from 'react'
import { en } from './locales/en'
import { zh } from './locales/zh'
import type { TranslationKey } from './locales/zh'

export type Locale = 'zh' | 'en'

type TranslationValues = Record<string, number | string | undefined>

export const localeStorageKey = 'shejane.locale'
const zhShortDateFormatter = new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit' })
const enShortDateFormatter = new Intl.DateTimeFormat('en', { month: '2-digit', day: '2-digit' })

export type { TranslationKey } from './locales/zh'
export type Translator = (key: TranslationKey, values?: TranslationValues) => string

const dictionaries: Record<Locale, Record<TranslationKey, string>> = { zh, en }

export interface I18nContextValue {
  locale: Locale
  setLocale: (locale: Locale) => void
  t: Translator
}

export const I18nContext = createContext<I18nContextValue | null>(null)

export function normalizeLocale(value?: string | null): Locale {
  const normalized = (value ?? '').toLowerCase()
  if (normalized.startsWith('en')) {
    return 'en'
  }
  if (normalized.startsWith('zh')) {
    return 'zh'
  }
  return 'zh'
}

export function createTranslator(locale: Locale): Translator {
  const dictionary = dictionaries[locale] ?? dictionaries.zh
  return (key, values) => {
    const template = dictionary[key] ?? dictionaries.zh[key] ?? key
    return formatTranslation(template, values)
  }
}

export function formatTranslation(template: string, values: TranslationValues = {}): string {
  return template.replace(/\{([A-Za-z0-9_]+)\}/g, (_match, key: string) => {
    const value = values[key]
    return value === undefined ? '' : String(value)
  })
}

export function formatRelativeTime(value: string, locale: Locale, t: Translator): string {
  const time = new Date(value).getTime()
  if (!Number.isFinite(time)) {
    return t('relative.invalid')
  }
  const minutes = Math.max(0, Math.round((Date.now() - time) / 60000))
  if (minutes < 1) {
    return t('relative.now')
  }
  if (minutes < 60) {
    return t('relative.minutesAgo', { count: minutes })
  }
  const hours = Math.round(minutes / 60)
  if (hours < 24) {
    return t('relative.hoursAgo', { count: hours })
  }
  const days = Math.round(hours / 24)
  if (days < 7) {
    return t('relative.daysAgo', { count: days })
  }
  const weeks = Math.round(days / 7)
  if (weeks < 5) {
    return t('relative.weeksAgo', { count: weeks })
  }
  return (locale === 'zh' ? zhShortDateFormatter : enShortDateFormatter).format(new Date(value))
}

/** Relative time for a chat message — like formatRelativeTime but the
 *  under-a-minute case reads "刚刚 / just now" (not "刚刚更新"). */
export function formatMessageTime(value: string, locale: Locale, t: Translator): string {
  const time = new Date(value).getTime()
  if (!Number.isFinite(time)) {
    return ''
  }
  const minutes = Math.max(0, Math.round((Date.now() - time) / 60000))
  if (minutes < 1) {
    return t('relative.justNow')
  }
  return formatRelativeTime(value, locale, t)
}

export function useI18n(): I18nContextValue {
  const value = useContext(I18nContext)
  if (!value) {
    throw new Error('useI18n must be used inside I18nProvider')
  }
  return value
}

export function readStoredLocale(): Locale {
  try {
    const stored = window.localStorage.getItem(localeStorageKey)
    return stored === 'en' || stored === 'zh' ? stored : 'zh'
  } catch {
    return 'zh'
  }
}
