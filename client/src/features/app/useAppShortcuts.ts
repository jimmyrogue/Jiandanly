import { useEffect, useRef, useState } from 'react'
import type { ConversationSidebarHandle } from '@/features/chat/components/ConversationSidebar'
import type { Translator } from '@/shared/i18n/i18n'

export function useAppShortcuts({
  cancelActiveRun,
  expandSidebar,
  hasActiveRun,
  isSending,
  setMainView,
  startNewConversation,
  t,
}: {
  cancelActiveRun: () => Promise<void>
  expandSidebar: () => void
  hasActiveRun: boolean
  isSending: boolean
  setMainView: (view: 'chat' | 'plugins' | 'settings') => void
  startNewConversation: () => void
  t: Translator
}) {
  const conversationSidebarRef = useRef<ConversationSidebarHandle>(null)
  const [keyboardHelpOpen, setKeyboardHelpOpen] = useState(false)

  /** Global app shortcuts. Bypass browser/OS defaults only for app-level
   * actions that are already visible in the shell. */
  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      const mod = event.metaKey || event.ctrlKey
      const key = event.key.toLowerCase()
      if (mod && !event.shiftKey && !event.altKey && key === 'n') {
        event.preventDefault()
        startNewConversation()
        return
      }
      if (mod && !event.shiftKey && !event.altKey && key === 'k') {
        event.preventDefault()
        expandSidebar()
        setMainView('chat')
        conversationSidebarRef.current?.openSearch()
        return
      }
      if (!mod && !event.altKey && event.key === '?' && !isEditableKeyboardTarget(event.target)) {
        event.preventDefault()
        setKeyboardHelpOpen(true)
      }
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [])

  /** Listen for the tray's "New Chat" menu item. */
  useEffect(() => window.shejaneClient?.onNewChatRequest?.(startNewConversation), [])

  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      if (event.defaultPrevented || event.key !== 'Escape') return
      if (keyboardHelpOpen) {
        event.preventDefault()
        setKeyboardHelpOpen(false)
        return
      }
      if (isSending || hasActiveRun) {
        event.preventDefault()
        void cancelActiveRun()
      }
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [cancelActiveRun, keyboardHelpOpen, isSending, hasActiveRun])

  const shortcutModifier = keyboardShortcutModifier()
  return {
    conversationSidebarRef,
    keyboardHelpOpen,
    setKeyboardHelpOpen,
    shortcutRows: [
      { label: t('shortcuts.newChat'), keys: [`${shortcutModifier}N`] },
      { label: t('shortcuts.searchChats'), keys: [`${shortcutModifier}K`] },
      { label: t('shortcuts.stopRun'), keys: ['Esc'] },
      { label: t('shortcuts.help'), keys: ['?'] },
    ],
  }
}

function isEditableKeyboardTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false
  const tagName = target.tagName.toLowerCase()
  return (
    target.isContentEditable
    || tagName === 'input'
    || tagName === 'textarea'
    || tagName === 'select'
  )
}

function keyboardShortcutModifier(): string {
  if (typeof navigator === 'undefined') return 'Ctrl+'
  return /Mac|iPhone|iPad|iPod/.test(navigator.platform) ? '⌘' : 'Ctrl+'
}
