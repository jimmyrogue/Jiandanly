import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { createRef, type Ref } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { I18nProvider } from '@/shared/i18n/I18nProvider'
import type { Conversation, MessageStatus } from '@/shared/local-data/types'
import { ConversationSidebar, type ConversationSidebarHandle } from './ConversationSidebar'

describe('ConversationSidebar', () => {
  afterEach(() => {
    cleanup()
    localStorage.clear()
  })

  it('marks inactive completed, paused, and failed chats while showing running chats as loading', () => {
    const { container } = renderSidebar([
      conversation('done-inactive', '完成的后台任务', 'done'),
      conversation('permission-inactive', '等待授权的后台任务', 'waiting_permission'),
      conversation('input-inactive', '等待回答的后台任务', 'waiting_input'),
      conversation('error-inactive', '失败的后台任务', 'error'),
      conversation('running-inactive', '正在执行的后台任务', 'streaming'),
      conversation('done-active', '当前打开的完成任务', 'done'),
    ], 'done-active')

    expect(screen.getAllByLabelText('需要用户操作')).toHaveLength(4)
    expect(screen.getByLabelText('对话正在执行')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '当前打开的完成任务' })).toBeInTheDocument()

    const runningRow = screen.getByRole('button', { name: '正在执行的后台任务' }).closest('.conversation-row')
    const runningMeta = runningRow?.querySelector('.conversation-row-meta')
    const runningStatus = screen.getByLabelText('对话正在执行')
    const runningTime = runningRow?.querySelector('.conversation-time')
    expect(runningMeta).toContainElement(runningStatus)
    expect(runningMeta).toContainElement(runningTime as HTMLElement)
    expect(runningStatus.compareDocumentPosition(runningTime as HTMLElement) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(container.querySelectorAll('.conversation-row-meta')).toHaveLength(6)
  })

  it('clears the attention dot after the user opens that chat once', () => {
    const conversations = [
      conversation('permission-inactive', '等待授权的后台任务', 'waiting_permission'),
      emptyConversation('current-chat', '当前对话'),
    ]
    const { rerender } = renderSidebar(conversations, 'current-chat')

    expect(screen.getByLabelText('需要用户操作')).toBeInTheDocument()

    rerender(sidebarElement(conversations, 'permission-inactive'))
    expect(screen.queryByLabelText('需要用户操作')).not.toBeInTheDocument()

    rerender(sidebarElement(conversations, 'current-chat'))
    expect(screen.queryByLabelText('需要用户操作')).not.toBeInTheDocument()
  })

  it('keeps the attention dot closed after the sidebar remounts', () => {
    const conversations = [
      conversation('permission-inactive', '等待授权的后台任务', 'waiting_permission'),
      emptyConversation('current-chat', '当前对话'),
    ]
    const { unmount } = renderSidebar(conversations, 'permission-inactive')

    unmount()
    renderSidebar(conversations, 'current-chat')

    expect(screen.queryByLabelText('需要用户操作')).not.toBeInTheDocument()
  })

  it('keeps the expanded-state window actions grouped on the right', () => {
    const { container } = renderSidebar([emptyConversation('a', '行程安排')])
    const collapseButton = screen.getByRole('button', { name: '收起侧栏' })
    const searchButton = screen.getByRole('button', { name: '搜索' })
    const actions = container.querySelector('.sidebar-window-actions')

    expect(actions).toContainElement(collapseButton)
    expect(actions).toContainElement(searchButton)
  })

  it('puts pinned conversations above the unified chats list (no project split)', () => {
    const { container } = renderSidebar([
      emptyConversation('recent-chat', '普通对话'),
      emptyConversation('pinned-chat', '固定对话', { pinned: true }),
      emptyConversation('project-chat', '我的项目', { project: { name: '我的项目' } }),
    ])

    // Section labels in document order: 已固定 (when there are pins),
    // 对话 (the unified list — projects and casual chats share that one
    // list, sorted by recency). Runtime capabilities now share one
    // Plugins entry in the footer instead of three parallel links.
    const sectionLabels = Array.from(container.querySelectorAll('.sidebar-section-label')) as HTMLElement[]
    const labelTexts = sectionLabels.map((el) => el.textContent?.trim())
    expect(labelTexts).toEqual(['已固定', '对话'])

    const pinnedConversation = screen.getByRole('button', { name: '固定对话' })
    const recentConversation = screen.getByRole('button', { name: '普通对话' })
    const projectConversation = screen.getByRole('button', { name: '我的项目' })

    // 已固定 label → pinned row → 对话 label → both unpinned rows.
    expect(sectionLabels[0].compareDocumentPosition(pinnedConversation) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(pinnedConversation.compareDocumentPosition(sectionLabels[1]) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(sectionLabels[1].compareDocumentPosition(recentConversation) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(sectionLabels[1].compareDocumentPosition(projectConversation) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()

    expect(screen.getByRole('button', { name: '插件' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Skill' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'MCP' })).not.toBeInTheDocument()

    expect(screen.getAllByTitle('更多 固定对话')).toHaveLength(1)
    expect(screen.getAllByTitle('更多 普通对话')).toHaveLength(1)
    expect(screen.getAllByTitle('更多 我的项目')).toHaveLength(1)
  })

  it('hides the plugins footer nav on the web build (no local runtime)', () => {
    // The web build (window.shejaneClient undefined → isDesktop=false) can't
    // run skills/connections, so those footer actions must not render. 设置
    // stays (it now navigates to the full settings page).
    const onOpenSettings = vi.fn()
    render(
      <I18nProvider>
        <ConversationSidebar
          conversations={[]}
          isDesktop={false}
          onNewConversation={vi.fn()}
          onSelectConversation={vi.fn()}
          onExportConversation={vi.fn()}
          onImportLocalData={vi.fn()}
          onTogglePinConversation={vi.fn()}
          onRenameConversation={vi.fn()}
          onDeleteConversation={vi.fn()}
          onCollapseSidebar={vi.fn()}
          onOpenSettings={onOpenSettings}
        />
      </I18nProvider>,
    )

    // The local-runtime Plugins workspace is gone on web.
    expect(screen.queryByText('工具')).not.toBeInTheDocument()
    expect(screen.queryByText('Skill')).not.toBeInTheDocument()
    expect(screen.queryByText('插件')).not.toBeInTheDocument()
    expect(screen.queryByText('MCP')).not.toBeInTheDocument()
    expect(screen.queryByText('连接')).not.toBeInTheDocument()

    // 设置 is now a plain nav button (no dropdown) that navigates to the page.
    fireEvent.click(screen.getByRole('button', { name: '设置' }))
    expect(onOpenSettings).toHaveBeenCalledTimes(1)
  })

  it('exposes row actions for pinning, renaming, and deleting', async () => {
    const handlers = {
      onTogglePinConversation: vi.fn(),
      onRenameConversation: vi.fn(),
      onDeleteConversation: vi.fn(),
    }
    renderSidebar([emptyConversation('target-chat', '操作目标')], undefined, handlers)

    openConversationActions('操作目标')
    fireEvent.click(await screen.findByText('固定'))
    expect(handlers.onTogglePinConversation).toHaveBeenCalledWith('target-chat')

    openConversationActions('操作目标')
    fireEvent.click(await screen.findByText('重命名'))
    fireEvent.change(await screen.findByLabelText('对话名称'), { target: { value: '新的标题' } })
    fireEvent.click(screen.getByText('保存名称'))
    expect(handlers.onRenameConversation).toHaveBeenCalledWith('target-chat', '新的标题')

    // After the redesign, project assignment lives on the composer's
    // project picker — not as a row action — so the dropdown no
    // longer carries an "添加到项目" item. The remaining items still
    // fire as expected.
    openConversationActions('操作目标')
    const deleteItem = await screen.findByText('删除')
    expect(screen.queryByText('添加到项目')).not.toBeInTheDocument()
    fireEvent.click(deleteItem)
    const deleteDialog = await screen.findByRole('alertdialog', { name: '删除这个对话？' })
    expect(within(deleteDialog).getByText('操作目标')).toBeInTheDocument()
    expect(within(deleteDialog).getByText(/全部 0 条消息将被永久删除/)).toBeInTheDocument()
    expect(within(deleteDialog).getByRole('button', { name: '取消' })).toBeInTheDocument()
    fireEvent.click(within(deleteDialog).getByRole('button', { name: '删除' }))
    expect(handlers.onDeleteConversation).toHaveBeenCalledWith('target-chat')
  })

  it('opens and marks the Plugins workspace as active', () => {
    const onOpenPlugins = vi.fn()
    render(
      <I18nProvider>
        <ConversationSidebar
          conversations={[]}
          onNewConversation={vi.fn()}
          onSelectConversation={vi.fn()}
          onExportConversation={vi.fn()}
          onImportLocalData={vi.fn()}
          onTogglePinConversation={vi.fn()}
          onRenameConversation={vi.fn()}
          onDeleteConversation={vi.fn()}
          onCollapseSidebar={vi.fn()}
          onOpenPlugins={onOpenPlugins}
          activeView="plugins"
        />
      </I18nProvider>,
    )
    const pluginsItem = screen.getByRole('button', { name: '插件' })
    expect(pluginsItem.className).toContain('active')
    fireEvent.click(pluginsItem)
    expect(onOpenPlugins).toHaveBeenCalledTimes(1)
  })

  describe('search', () => {
    function conversationWithBody(id: string, title: string, body: string): Conversation {
      return emptyConversation(id, title, {
        messages: [
          { id: `${id}-u`, role: 'user', content: '问题', createdAt: '2026-05-14T00:00:00Z', status: 'done' },
          { id: `${id}-a`, role: 'assistant', content: body, createdAt: '2026-05-14T00:00:01Z', status: 'done' },
        ],
      })
    }

    it('toggles the search input open and closed', async () => {
      renderSidebar([emptyConversation('a', '行程安排')])
      expect(screen.queryByRole('searchbox')).not.toBeInTheDocument()

      fireEvent.click(screen.getByRole('button', { name: '搜索' }))
      expect(screen.getByRole('searchbox')).toBeInTheDocument()

      fireEvent.click(screen.getByRole('button', { name: '搜索' }))
      await waitFor(() => {
        expect(screen.queryByRole('searchbox')).not.toBeInTheDocument()
      })
    })

    it('opens immediately through the app shortcut handle', () => {
      const sidebarRef = createRef<ConversationSidebarHandle>()
      renderSidebar([emptyConversation('a', '行程安排')], undefined, {}, sidebarRef)

      act(() => sidebarRef.current?.openSearch())

      expect(screen.getByRole('searchbox')).toBeInTheDocument()
    })

    it('filters rows by a title substring', () => {
      renderSidebar([emptyConversation('a', '行程安排'), emptyConversation('b', '报销流程')])
      fireEvent.click(screen.getByRole('button', { name: '搜索' }))
      fireEvent.change(screen.getByRole('searchbox'), { target: { value: '报销' } })

      expect(screen.getByRole('button', { name: '报销流程' })).toBeInTheDocument()
      expect(screen.queryByRole('button', { name: '行程安排' })).not.toBeInTheDocument()
    })

    it('surfaces a conversation that matches only in a message body and shows a snippet', () => {
      renderSidebar([
        conversationWithBody('a', '行程安排', '推荐去普吉岛玩三天,海滩很美'),
        emptyConversation('b', '报销流程'),
      ])
      fireEvent.click(screen.getByRole('button', { name: '搜索' }))
      fireEvent.change(screen.getByRole('searchbox'), { target: { value: '普吉岛' } })

      // The title does NOT contain the query, but the body does — it must still appear.
      expect(screen.getByRole('button', { name: '行程安排' })).toBeInTheDocument()
      expect(screen.queryByRole('button', { name: '报销流程' })).not.toBeInTheDocument()
      // And the matched term is surfaced in a snippet <mark>.
      const mark = screen.getByText('普吉岛', { selector: 'mark' })
      expect(mark).toBeInTheDocument()
    })

    it('clears the query with the clear button', () => {
      renderSidebar([emptyConversation('a', '行程安排'), emptyConversation('b', '报销流程')])
      fireEvent.click(screen.getByRole('button', { name: '搜索' }))
      fireEvent.change(screen.getByRole('searchbox'), { target: { value: '报销' } })
      expect(screen.queryByRole('button', { name: '行程安排' })).not.toBeInTheDocument()

      fireEvent.click(screen.getByRole('button', { name: '清除搜索' }))
      expect(screen.getByRole('button', { name: '行程安排' })).toBeInTheDocument()
      expect(screen.getByRole('button', { name: '报销流程' })).toBeInTheDocument()
    })
  })
})

function openConversationActions(title: string) {
  const trigger = screen.getByTitle(`更多 ${title}`)
  trigger.focus()
  fireEvent.keyDown(trigger, { key: 'Enter', code: 'Enter' })
}

function renderSidebar(
  conversations: Conversation[],
  activeID?: string,
  handlers: Partial<ConversationSidebarHandlers> = {},
  sidebarRef?: Ref<ConversationSidebarHandle>,
) {
  return render(sidebarElement(conversations, activeID, handlers, sidebarRef))
}

function sidebarElement(
  conversations: Conversation[],
  activeID?: string,
  handlers: Partial<ConversationSidebarHandlers> = {},
  sidebarRef?: Ref<ConversationSidebarHandle>,
) {
  return (
    <I18nProvider>
      <ConversationSidebar
        ref={sidebarRef}
        conversations={conversations}
        activeID={activeID}
        onNewConversation={vi.fn()}
        onSelectConversation={vi.fn()}
        onExportConversation={vi.fn()}
        onImportLocalData={vi.fn()}
        onTogglePinConversation={handlers.onTogglePinConversation ?? vi.fn()}
        onRenameConversation={handlers.onRenameConversation ?? vi.fn()}
        onDeleteConversation={handlers.onDeleteConversation ?? vi.fn()}
        onCollapseSidebar={vi.fn()}
        onOpenPlugins={handlers.onOpenPlugins ?? vi.fn()}
      />
    </I18nProvider>
  )
}

interface ConversationSidebarHandlers {
  onTogglePinConversation: (conversationID: string) => void
  onRenameConversation: (conversationID: string, title: string) => void
  onDeleteConversation: (conversationID: string) => void
  onOpenPlugins: () => void
}

function emptyConversation(id: string, title: string, overrides: Partial<Conversation> = {}): Conversation {
  return {
    id,
    title,
    archived: false,
    createdAt: '2026-05-14T00:00:00Z',
    updatedAt: '2026-05-14T00:01:00Z',
    messages: [],
    ...overrides,
  }
}

function conversation(id: string, title: string, status: MessageStatus): Conversation {
  return {
    id,
    title,
    archived: false,
    createdAt: '2026-05-14T00:00:00Z',
    updatedAt: '2026-05-14T00:01:00Z',
    messages: [
      {
        id: `${id}-user`,
        role: 'user',
        content: title,
        createdAt: '2026-05-14T00:00:00Z',
        status: 'done',
      },
      {
        id: `${id}-assistant`,
        role: 'assistant',
        content: 'result',
        createdAt: '2026-05-14T00:00:01Z',
        status,
      },
    ],
  }
}
