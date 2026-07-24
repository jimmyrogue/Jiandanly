import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { I18nProvider } from '@/shared/i18n/I18nProvider'
import { SkillsView } from './SkillsView'
import type { InstalledSkill, SkillCatalog } from '@/runtime/client'

afterEach(cleanup)

const shejaneRoot = { source: 'shejane', path: '/u/.shejane/skills' }
const claudeRoot = { source: 'claude', path: '/u/.claude/skills' }

const shejaneSkill: InstalledSkill = {
  name: 'my-skill',
  description: 'mine here',
  path: '/u/.shejane/skills/my-skill/SKILL.md',
  source: 'shejane',
  root_path: '/u/.shejane/skills',
}

const claudeSkill: InstalledSkill = {
  name: 'claude-skill',
  description: 'from claude',
  path: '/u/.claude/skills/claude-skill/SKILL.md',
  source: 'claude',
  root_path: '/u/.claude/skills',
}

function catalog(skills: InstalledSkill[], roots = [claudeRoot, shejaneRoot]): SkillCatalog {
  return { skills, roots }
}

function renderView(overrides: Partial<Parameters<typeof SkillsView>[0]> = {}) {
  const props = {
    listInstalled: vi.fn().mockResolvedValue(catalog([])),
    ...overrides,
  }
  render(
    <I18nProvider>
      <SkillsView {...props} />
    </I18nProvider>,
  )
  return props
}

describe('SkillsView — grouped catalog', () => {
  it('always renders every known root, even when its skills list is empty', async () => {
    renderView({
      listInstalled: vi.fn().mockResolvedValue(catalog([])),
    })
    // Both sections appear with empty-state hints — that's the whole
    // point of returning `roots` separately. Previously the Personal
    // section silently vanished when ~/.shejane/skills was empty and
    // the user had no idea where to drop files.
    expect(await screen.findByText('系统')).toBeInTheDocument()
    expect(screen.getByText('个人')).toBeInTheDocument()
    expect(
      screen.getByText(/\.claude\/skills.+刷新/),
    ).toBeInTheDocument()
    expect(
      screen.getByText(/\.shejane\/skills.+刷新/),
    ).toBeInTheDocument()
  })

  it('renders 系统 before 个人 even when input order is reversed', async () => {
    renderView({
      listInstalled: vi
        .fn()
        .mockResolvedValue(catalog([shejaneSkill, claudeSkill], [shejaneRoot, claudeRoot])),
    })
    const system = await screen.findByText('系统')
    const personal = screen.getByText('个人')
    expect(system.compareDocumentPosition(personal)).toBe(
      Node.DOCUMENT_POSITION_FOLLOWING,
    )
    expect(screen.getByText('my-skill')).toBeInTheDocument()
    expect(screen.getByText('claude-skill')).toBeInTheDocument()
  })

  it('exposes one Open-folder icon button per section when the bridge is wired', async () => {
    const onOpenFolder = vi.fn()
    renderView({
      onOpenFolder,
      listInstalled: vi.fn().mockResolvedValue(catalog([shejaneSkill, claudeSkill])),
    })
    const buttons = await screen.findAllByRole('button', { name: '打开文件夹' })
    expect(buttons).toHaveLength(2)
    fireEvent.click(buttons[0])
    expect(onOpenFolder).toHaveBeenCalledWith('/u/.claude/skills')
  })

  it('omits Open-folder buttons when no bridge is provided', async () => {
    renderView({
      listInstalled: vi.fn().mockResolvedValue(catalog([shejaneSkill])),
    })
    await screen.findByText('my-skill')
    expect(screen.queryByRole('button', { name: '打开文件夹' })).not.toBeInTheDocument()
  })

  it('keeps the search box visible even when no skills exist', async () => {
    renderView({ listInstalled: vi.fn().mockResolvedValue(catalog([])) })
    await screen.findByText('系统')
    expect(screen.getByLabelText('搜索 Skill')).toBeInTheDocument()
  })

  it('opens the personal skills folder from the new-skill action', async () => {
    const onOpenFolder = vi.fn()
    renderView({
      onOpenFolder,
      listInstalled: vi.fn().mockResolvedValue(catalog([shejaneSkill, claudeSkill])),
    })
    fireEvent.click(await screen.findByRole('button', { name: '新建 Skill' }))
    expect(onOpenFolder).toHaveBeenCalledWith('/u/.shejane/skills')
  })

  it('filters Skills by search query and shows the empty state', async () => {
    renderView({
      listInstalled: vi.fn().mockResolvedValue(catalog([shejaneSkill, claudeSkill])),
    })
    await screen.findByText('my-skill')
    fireEvent.change(screen.getByLabelText('搜索 Skill'), { target: { value: 'nonexistent' } })
    expect(await screen.findByText('未找到 Skill')).toBeInTheDocument()
    expect(screen.queryByText('my-skill')).not.toBeInTheDocument()
    expect(screen.queryByText('claude-skill')).not.toBeInTheDocument()
  })

  it('refresh button re-invokes listInstalled', async () => {
    const listInstalled = vi.fn().mockResolvedValue(catalog([]))
    renderView({ listInstalled })
    await waitFor(() => expect(listInstalled).toHaveBeenCalledTimes(1))
    fireEvent.click(screen.getByRole('button', { name: /刷新/ }))
    await waitFor(() => expect(listInstalled).toHaveBeenCalledTimes(2))
  })

  it('creates a personal skill from the inline editor', async () => {
    const listInstalled = vi.fn().mockResolvedValue(catalog([]))
    const onCreateSkill = vi.fn().mockResolvedValue(undefined)
    renderView({ listInstalled, onCreateSkill })

    fireEvent.click(await screen.findByRole('button', { name: '新建 Skill' }))
    fireEvent.change(screen.getByLabelText('Skill 名称'), { target: { value: 'daily-digest' } })
    fireEvent.change(screen.getByLabelText('描述'), { target: { value: '整理每日摘要' } })
    fireEvent.change(screen.getByLabelText('SKILL.md'), { target: { value: '# Daily\n' } })
    fireEvent.click(screen.getByRole('button', { name: '保存 Skill' }))

    await waitFor(() => {
      expect(onCreateSkill).toHaveBeenCalledWith({
        name: 'daily-digest',
        description: '整理每日摘要',
        content: '# Daily\n',
      })
    })
    await waitFor(() => expect(listInstalled).toHaveBeenCalledTimes(2))
  })

  it('edits and deletes personal skills only', async () => {
    const onLoadSkill = vi.fn().mockResolvedValue({
      name: 'my-skill',
      description: 'mine here',
      path: shejaneSkill.path,
      root_path: shejaneSkill.root_path,
      content: '# Mine\n',
    })
    const onUpdateSkill = vi.fn().mockResolvedValue(undefined)
    const onDeleteSkill = vi.fn().mockResolvedValue(undefined)
    renderView({
      listInstalled: vi.fn().mockResolvedValue(catalog([shejaneSkill, claudeSkill])),
      onLoadSkill,
      onUpdateSkill,
      onDeleteSkill,
    })

    await screen.findByText('my-skill')
    expect(screen.queryByRole('button', { name: '编辑 Skill：claude-skill' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '编辑 Skill：my-skill' }))
    await waitFor(() => expect(onLoadSkill).toHaveBeenCalledWith('my-skill'))
    fireEvent.change(await screen.findByLabelText('SKILL.md'), { target: { value: '# Updated\n' } })
    fireEvent.click(screen.getByRole('button', { name: '保存 Skill' }))
    await waitFor(() => {
      expect(onUpdateSkill).toHaveBeenCalledWith('my-skill', {
        name: 'my-skill',
        description: 'mine here',
        content: '# Updated\n',
      })
    })

    fireEvent.click(screen.getByRole('button', { name: '删除 Skill：my-skill' }))
    await waitFor(() => expect(onDeleteSkill).toHaveBeenCalledWith('my-skill'))
  })
})
