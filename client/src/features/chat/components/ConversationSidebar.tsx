import { Fragment, forwardRef, useCallback, useEffect, useImperativeHandle, useRef, useState, type ReactNode } from 'react'
import {
  IconBox,
  IconDots,
  IconDownload,
  IconLayoutSidebarLeftCollapse,
  IconLoader2,
  IconPencil,
  IconPin,
  IconPlus,
  IconSearch,
  IconSettings,
  IconTrash,
  IconUpload,
  IconX,
} from '@tabler/icons-react'
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogMedia,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { Input } from '@/components/ui/input'
import { useI18n, formatRelativeTime, type Translator } from '@/shared/i18n/i18n'
import { appLogoURL } from '@/shared/assets/logo'
import type { Conversation } from '@/shared/local-data/types'

type ConversationSidebarStatus = 'needs_attention' | 'running'

export interface ConversationSidebarHandle {
  openSearch: () => void
}

const seenConversationVersionsStorageKey = 'shejane.sidebar.seenConversationVersions.v1'

function useConversationSidebarViewModel({
  conversations,
  activeID,
  onNewConversation,
  onSelectConversation,
  onExportConversation,
  onImportLocalData,
  onTogglePinConversation,
  onRenameConversation,
  onDeleteConversation,
  onCollapseSidebar,
  isDesktop = true,
  onOpenPlugins,
  onOpenSettings,
  activeView = 'chat',
  resizeHandle,
}: {
  conversations: Conversation[]
  activeID?: string
  onNewConversation: () => void
  onSelectConversation: (conversationID: string) => void
  onExportConversation: (conversationID: string) => void
  onImportLocalData: (file?: File) => void
  onTogglePinConversation: (conversationID: string) => void
  onRenameConversation: (conversationID: string, title: string) => void
  onDeleteConversation: (conversationID: string) => void
  onCollapseSidebar: () => void
  /** Electron build flag. The web build has no local runtime, so local-agent
   *  pages are hidden when false. */
  isDesktop?: boolean
  onOpenPlugins?: () => void
  /** Navigate to the full 设置 page. Runtime, agent configuration, and data
   *  all live there now (the old account dropdown + agent-settings dialog). */
  onOpenSettings?: () => void
  activeView?: 'chat' | 'plugins' | 'settings'
  resizeHandle?: ReactNode
}) {
  const { t, locale } = useI18n()
  const importInputRef = useRef<HTMLInputElement>(null)
  const [renameConversationID, setRenameConversationID] = useState<string>()
  const [deleteConversationID, setDeleteConversationID] = useState<string>()
  const [renameTitle, setRenameTitle] = useState('')
  const [seenConversationVersions, setSeenConversationVersions] = useState<Record<string, string>>(readSeenConversationVersions)
  const [searchOpen, setSearchOpen] = useState(false)
  const [searchMounted, setSearchMounted] = useState(false)
  const [searchQuery, setSearchQuery] = useState('')
  const searchInputRef = useRef<HTMLInputElement>(null)
  const searchExitTimerRef = useRef<number>()
  const renameConversation = conversations.find((conversation) => conversation.id === renameConversationID)
  const deleteConversation = conversations.find((conversation) => conversation.id === deleteConversationID)
  const deleteConversationTitle = deleteConversation?.title ?? t('sidebar.dialog.currentConversation')
  const deleteMessageCount = deleteConversation?.messages.length ?? 0
  const activeConversation = conversations.find((conversation) => conversation.id === activeID)
  const normalizedQuery = searchQuery.trim().toLowerCase()
  const matchesQuery = (conversation: Conversation) =>
    !normalizedQuery ||
    conversation.title.toLowerCase().includes(normalizedQuery) ||
    conversation.messages.some((message) => message.content.toLowerCase().includes(normalizedQuery))
  const pinnedConversations = conversations.filter((conversation) => conversation.pinned && matchesQuery(conversation))
  // Single unified list — projects (workspace-bound conversations) and casual
  // chats live together, sorted by updatedAt desc (handled upstream by
  // LocalConversationStore.list). The composer's project chip is now how
  // users tell a workspace-bound chat apart from a free-form one.
  const recentConversations = conversations.filter(
    (conversation) => !conversation.pinned && matchesQuery(conversation),
  )
  useEffect(() => {
    if (!activeConversation) {
      return
    }
    const version = conversationSidebarVersion(activeConversation)
    if (!version) {
      return
    }
    if (seenConversationVersions[activeConversation.id] === version) {
      return
    }
    const next = { ...seenConversationVersions, [activeConversation.id]: version }
    setSeenConversationVersions(next)
    writeSeenConversationVersions(next)
  }, [activeConversation, seenConversationVersions])

  useEffect(() => {
    if (searchOpen) {
      searchInputRef.current?.focus()
    }
  }, [searchOpen])

  useEffect(() => {
    return () => {
      if (searchExitTimerRef.current) {
        window.clearTimeout(searchExitTimerRef.current)
      }
    }
  }, [])

  const openSearch = useCallback(() => {
    if (searchExitTimerRef.current) {
      window.clearTimeout(searchExitTimerRef.current)
    }
    setSearchMounted(true)
    setSearchOpen(true)
    window.setTimeout(() => searchInputRef.current?.focus(), 0)
  }, [])

  function closeSearch() {
    setSearchQuery('')
    setSearchOpen(false)
    if (searchExitTimerRef.current) {
      window.clearTimeout(searchExitTimerRef.current)
    }
    searchExitTimerRef.current = window.setTimeout(() => setSearchMounted(false), 180)
  }

  function toggleSearch() {
    if (searchOpen) {
      closeSearch()
      return
    }
    openSearch()
  }

  function renderConversationRow(conversation: Conversation) {
    const snippet = normalizedQuery ? bodyMatchSnippet(conversation, normalizedQuery) : null
    const rowClass = [
      'conversation-row',
      conversation.id === activeID ? 'active' : '',
      snippet ? 'has-snippet' : '',
    ]
      .filter(Boolean)
      .join(' ')
    return (
      <div className={rowClass} key={conversation.id}>
        <Button
          className="conversation"
          variant="ghost"
          onClick={() => onSelectConversation(conversation.id)}
        >
          <span>{conversation.title}</span>
        </Button>
        <span className="conversation-row-meta">
          <ConversationStatusIndicator
            status={conversationSidebarStatus(
              conversation,
              conversation.id === activeID,
              seenConversationVersions[conversation.id],
            )}
          />
          <span className="conversation-time" aria-hidden="true">
            {formatRelativeTime(conversation.updatedAt, locale, t)}
          </span>
        </span>
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button
              className="conversation-more"
              variant="ghost"
              size="icon-xs"
              title={t('sidebar.moreFor', { title: conversation.title })}
              aria-label={t('sidebar.moreFor', { title: conversation.title })}
            >
              <IconDots size={15} />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="conversation-row-menu">
            <DropdownMenuItem onSelect={() => onTogglePinConversation(conversation.id)}>
              <IconPin />
              <span>{conversation.pinned ? t('sidebar.actions.unpin') : t('sidebar.actions.pin')}</span>
            </DropdownMenuItem>
            <DropdownMenuItem
              onSelect={() => {
                setRenameConversationID(conversation.id)
                setRenameTitle(conversation.title)
              }}
            >
              <IconPencil />
              <span>{t('sidebar.actions.rename')}</span>
            </DropdownMenuItem>
            <DropdownMenuSeparator />
            <DropdownMenuItem onSelect={() => onExportConversation(conversation.id)}>
              <IconDownload />
              <span>{t('sidebar.dialog.export')}</span>
            </DropdownMenuItem>
            <DropdownMenuItem onSelect={() => importInputRef.current?.click()}>
              <IconUpload />
              <span>{t('sidebar.dialog.import')}</span>
            </DropdownMenuItem>
            <DropdownMenuSeparator />
            <DropdownMenuItem className="conversation-row-menu-danger" onSelect={() => setDeleteConversationID(conversation.id)}>
              <IconTrash />
              <span>{t('sidebar.actions.delete')}</span>
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
        {snippet ? (
          <div className="conversation-row-snippet" title={t('sidebar.searchSnippetMatch')}>
            {snippet.before}
            <mark>{snippet.match}</mark>
            {snippet.after}
          </div>
        ) : null}
      </div>
    )
  }

  return { activeView, deleteConversation, deleteConversationTitle, deleteMessageCount, importInputRef, isDesktop, normalizedQuery, onCollapseSidebar, onDeleteConversation, onImportLocalData, onNewConversation, onOpenPlugins, onOpenSettings, onRenameConversation, openSearch, pinnedConversations, recentConversations, renameConversation, renameTitle, renderConversationRow, resizeHandle, searchInputRef, searchMounted, searchOpen, searchQuery, setDeleteConversationID, setRenameConversationID, setRenameTitle, setSearchQuery, t, toggleSearch }
}

export const ConversationSidebar = forwardRef<
  ConversationSidebarHandle,
  Parameters<typeof useConversationSidebarViewModel>[0]
>(function ConversationSidebar(props, ref) {
  const view = useConversationSidebarViewModel(props)
  useImperativeHandle(ref, () => ({ openSearch: view.openSearch }), [view.openSearch])
  return <ConversationSidebarView view={view} />
})

function ConversationSidebarView({ view }: { view: ReturnType<typeof useConversationSidebarViewModel> }) {
  const { activeView, deleteConversation, deleteConversationTitle, deleteMessageCount, importInputRef, isDesktop, normalizedQuery, onCollapseSidebar, onDeleteConversation, onImportLocalData, onNewConversation, onOpenPlugins, onOpenSettings, onRenameConversation, pinnedConversations, recentConversations, renameConversation, renameTitle, renderConversationRow, resizeHandle, searchInputRef, searchMounted, searchOpen, searchQuery, setDeleteConversationID, setRenameConversationID, setRenameTitle, setSearchQuery, t, toggleSearch } = view
  return (
    <aside className="sidebar">
      {resizeHandle}
      <div className="sidebar-window-controls">
        <div className="sidebar-window-actions" aria-label={t('app.windowActions')}>
          <button className="sidebar-window-control-button" type="button" title={t('app.collapseSidebar')} aria-label={t('app.collapseSidebar')} onClick={onCollapseSidebar}>
            <IconLayoutSidebarLeftCollapse aria-hidden="true" />
          </button>
          <button
            className={searchOpen ? 'sidebar-window-control-button active' : 'sidebar-window-control-button'}
            type="button"
            title={t('app.search')}
            aria-label={t('app.search')}
            aria-pressed={searchOpen}
            onClick={toggleSearch}
          >
            <IconSearch aria-hidden="true" />
          </button>
        </div>
      </div>

      {searchMounted ? (
        <div className="sidebar-search" data-state={searchOpen ? 'open' : 'closed'}>
          <IconSearch className="sidebar-search-icon" size={14} aria-hidden="true" />
          <Input
            ref={searchInputRef}
            type="search"
            value={searchQuery}
            onChange={(event) => setSearchQuery(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Escape') {
                event.preventDefault()
                toggleSearch()
              }
            }}
            placeholder={t('sidebar.searchPlaceholder')}
            aria-label={t('app.search')}
          />
          {searchQuery ? (
            <button
              type="button"
              className="sidebar-search-clear"
              title={t('sidebar.searchClear')}
              aria-label={t('sidebar.searchClear')}
              onClick={() => {
                setSearchQuery('')
                searchInputRef.current?.focus()
              }}
            >
              <IconX size={13} aria-hidden="true" />
            </button>
          ) : null}
        </div>
      ) : null}

      <div className="sidebar-brand" aria-label={t('app.productName')}>
        <span className="sidebar-brand-mark" aria-hidden="true">
          <img src={appLogoURL} alt="" />
        </span>
        <span className="sidebar-brand-copy">
          <strong>{t('app.productName')}</strong>
          <small>SHEJANE</small>
        </span>
      </div>

      <div className="sidebar-section">
        <button
          className="sidebar-item"
          type="button"
          aria-label={t('app.newChat')}
          onClick={onNewConversation}
        >
          <IconPlus size={14} />
          <span>{t('app.newChat')}</span>
          <span className="sidebar-item-hint">⌘N</span>
        </button>
      </div>

      {pinnedConversations.length ? (
        <div className="sidebar-section pinned-conversation-list">
          <div className="sidebar-section-label">{t('sidebar.pinned')}</div>
          {pinnedConversations.map(renderConversationRow)}
        </div>
      ) : null}

      <div className="sidebar-section conversation-list">
        <div className="sidebar-section-label">{t('sidebar.section.chats')}</div>
        {recentConversations.length ? (
          // While searching, keep results flat (a search hit isn't really
          // "today's chat"). Otherwise bucket by day — 今天 / 昨天 / 更早 —
          // to match the v4 prototype's grouped conversation list.
          normalizedQuery ? (
            recentConversations.map(renderConversationRow)
          ) : (
            groupConversationsByDay(recentConversations, new Date(), t).map((group) => (
              <Fragment key={group.key}>
                <div className="sidebar-time-group-label">{group.label}</div>
                {group.items.map(renderConversationRow)}
              </Fragment>
            ))
          )
        ) : (
          <div className="sidebar-empty">
            {normalizedQuery ? t('sidebar.searchEmpty') : t('sidebar.empty')}
          </div>
        )}
      </div>

      <div className="sidebar-footer">
        {/* Local-agent surfaces only work in the desktop build. */}
        {isDesktop ? (
          <div className="sidebar-footer-nav" aria-label={t('sidebar.section.tools')}>
            <button
              className={`sidebar-settings-trigger sidebar-footer-link${activeView === 'plugins' ? ' active' : ''}`}
              type="button"
              onClick={() => onOpenPlugins?.()}
            >
              <IconBox size={14} />
              <span>{t('sidebar.plugins')}</span>
            </button>
          </div>
        ) : null}

        <button
          type="button"
          className={`sidebar-settings-trigger${activeView === 'settings' ? ' active' : ''}`}
          aria-label={t('sidebar.settings')}
          onClick={() => onOpenSettings?.()}
        >
          <IconSettings size={14} aria-hidden="true" />
          <span>{t('sidebar.settings')}</span>
        </button>
      </div>

      <input
        ref={importInputRef}
        type="file"
        accept="application/json"
        hidden
        onChange={(event) => {
          onImportLocalData(event.currentTarget.files?.[0])
          event.currentTarget.value = ''
        }}
      />
      <Dialog open={Boolean(renameConversation)} onOpenChange={(open) => !open && setRenameConversationID(undefined)}>
        <DialogContent className="conversation-actions-dialog sm:max-w-[420px]">
          <DialogHeader>
            <DialogTitle>{t('sidebar.rename.title')}</DialogTitle>
            <DialogDescription>
              {t('sidebar.rename.description', { title: renameConversation?.title ?? t('sidebar.dialog.currentConversation') })}
            </DialogDescription>
          </DialogHeader>
          <form
            className="conversation-action-form"
            onSubmit={(event) => {
              event.preventDefault()
              if (!renameConversation || !renameTitle.trim()) {
                return
              }
              onRenameConversation(renameConversation.id, renameTitle.trim())
              setRenameConversationID(undefined)
            }}
          >
            <label htmlFor="conversation-rename-input">{t('sidebar.rename.nameLabel')}</label>
            <Input
              id="conversation-rename-input"
              value={renameTitle}
              onChange={(event) => setRenameTitle(event.currentTarget.value)}
            />
            <DialogFooter>
              <Button type="button" variant="ghost" onClick={() => setRenameConversationID(undefined)}>
                {t('sidebar.dialog.close')}
              </Button>
              <Button type="submit" disabled={!renameTitle.trim()}>
                {t('sidebar.rename.save')}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
      <AlertDialog open={Boolean(deleteConversation)} onOpenChange={(open) => !open && setDeleteConversationID(undefined)}>
        <AlertDialogContent className="conversation-delete-dialog">
          <AlertDialogHeader className="conversation-delete-header">
            <AlertDialogMedia className="conversation-delete-media">
              <IconTrash aria-hidden="true" />
            </AlertDialogMedia>
            <AlertDialogTitle>{t('sidebar.delete.title')}</AlertDialogTitle>
            <AlertDialogDescription>
              <strong>{deleteConversationTitle}</strong>
              {t('sidebar.delete.descriptionAfterTitle', { count: deleteMessageCount })}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter className="conversation-delete-footer">
            <AlertDialogCancel variant="outline" autoFocus onClick={() => setDeleteConversationID(undefined)}>
              <span className="conversation-delete-button-label">{t('sidebar.dialog.cancel')}</span>
            </AlertDialogCancel>
            <AlertDialogAction
              variant="destructive"
              disabled={!deleteConversation}
              onClick={() => {
                if (deleteConversation) {
                  onDeleteConversation(deleteConversation.id)
                  setDeleteConversationID(undefined)
                }
              }}
            >
              <span className="conversation-delete-button-label">{t('sidebar.delete.confirm')}</span>
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </aside>
  )
}

function ConversationStatusIndicator({ status }: { status: ConversationSidebarStatus | null }) {
  const { t } = useI18n()
  if (status === 'running') {
    return (
      <span className="conversation-status-slot conversation-status-loading" role="status" aria-label={t('sidebar.status.running')}>
        <IconLoader2 size={14} aria-hidden="true" />
      </span>
    )
  }
  if (status === 'needs_attention') {
    return <span className="conversation-status-slot conversation-status-attention" role="status" aria-label={t('sidebar.status.needsAttention')} />
  }
  return <span className="conversation-status-slot" aria-hidden="true" />
}

// When a conversation matches the search only on a message body (not its
// title), surface a short snippet around the first match so the user can see
// WHY it surfaced. Returns null when the title already matches (no snippet
// needed) or nothing matches. The match part preserves the original casing.
function bodyMatchSnippet(
  conversation: Conversation,
  query: string,
): { before: string; match: string; after: string } | null {
  if (!query) {
    return null
  }
  if (conversation.title.toLowerCase().includes(query)) {
    return null
  }
  for (const message of conversation.messages) {
    const content = message.content ?? ''
    const index = content.toLowerCase().indexOf(query)
    if (index < 0) {
      continue
    }
    const start = Math.max(0, index - 24)
    const end = Math.min(content.length, index + query.length + 48)
    const before = (start > 0 ? '…' : '') + content.slice(start, index).replace(/\s+/g, ' ')
    const match = content.slice(index, index + query.length)
    const after = content.slice(index + query.length, end).replace(/\s+/g, ' ') + (end < content.length ? '…' : '')
    return { before, match, after }
  }
  return null
}

// Bucket recency-sorted conversations into 今天 / 昨天 / 更早 day groups so the
// sidebar reads as the v4 prototype's grouped list rather than a flat run of
// rows. `conversations` is assumed already sorted by updatedAt desc upstream
// (LocalConversationStore.list), so within each returned bucket order is
// preserved and the buckets themselves come out newest-first. Empty buckets
// are dropped. An unparseable updatedAt falls into "today" as a safe default.
function groupConversationsByDay(
  conversations: Conversation[],
  now: Date,
  t: Translator,
): { key: string; label: string; items: Conversation[] }[] {
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime()
  const startOfYesterday = startOfToday - 24 * 60 * 60 * 1000
  const today: Conversation[] = []
  const yesterday: Conversation[] = []
  const earlier: Conversation[] = []
  for (const conversation of conversations) {
    const ts = new Date(conversation.updatedAt).getTime()
    if (Number.isNaN(ts) || ts >= startOfToday) {
      today.push(conversation)
    } else if (ts >= startOfYesterday) {
      yesterday.push(conversation)
    } else {
      earlier.push(conversation)
    }
  }
  return [
    { key: 'today', label: t('sidebar.bucket.today'), items: today },
    { key: 'yesterday', label: t('sidebar.bucket.yesterday'), items: yesterday },
    { key: 'earlier', label: t('sidebar.bucket.earlier'), items: earlier },
  ].filter((bucket) => bucket.items.length > 0)
}

function conversationSidebarStatus(conversation: Conversation, isActive: boolean, seenVersion?: string): ConversationSidebarStatus | null {
  const latestAssistant = [...conversation.messages].reverse().find((message) => message.role === 'assistant')
  if (!latestAssistant) {
    return null
  }
  if (latestAssistant.status === 'streaming' || latestAssistant.status === 'pending') {
    return 'running'
  }
  const currentVersion = conversationSidebarVersion(conversation)
  if (!isActive && currentVersion !== seenVersion && (latestAssistant.status === 'done' || latestAssistant.status === 'waiting_permission' || latestAssistant.status === 'waiting_input' || latestAssistant.status === 'error')) {
    return 'needs_attention'
  }
  return null
}

function conversationSidebarVersion(conversation: Conversation): string {
  const latestAssistant = [...conversation.messages].reverse().find((message) => message.role === 'assistant')
  if (!latestAssistant) {
    return ''
  }
  return [
    latestAssistant.id,
    latestAssistant.status,
    latestAssistant.content.length,
    latestAssistant.agentEvents?.length ?? 0,
    conversation.updatedAt,
  ].join(':')
}

function readSeenConversationVersions(): Record<string, string> {
  try {
    const raw = window.localStorage.getItem(seenConversationVersionsStorageKey)
    if (!raw) {
      return {}
    }
    const parsed = JSON.parse(raw) as unknown
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
      return {}
    }
    return Object.fromEntries(
      Object.entries(parsed).filter((entry): entry is [string, string] => typeof entry[1] === 'string'),
    )
  } catch {
    return {}
  }
}

function writeSeenConversationVersions(value: Record<string, string>) {
  try {
    window.localStorage.setItem(seenConversationVersionsStorageKey, JSON.stringify(value))
  } catch {
    // Ignore storage failures; the in-memory state still keeps the current session correct.
  }
}
