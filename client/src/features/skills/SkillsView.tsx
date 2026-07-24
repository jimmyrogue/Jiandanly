import { useCallback, useEffect, useMemo, useState, type FormEvent } from 'react'
import {
  IconCheck,
  IconDeviceFloppy,
  IconFolderOpen,
  IconPencil,
  IconPlus,
  IconRefresh,
  IconSearch,
  IconSparkles,
  IconTrash,
  IconX,
} from '@tabler/icons-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { useI18n } from '@/shared/i18n/i18n'
import type { InstalledSkill, SkillCatalog, SkillFile, SkillRoot, SkillWriteRequest } from '@/runtime/client'

export interface SkillsViewProps {
  listInstalled: () => Promise<SkillCatalog>
  embedded?: boolean
  onCreateSkill?: (input: SkillWriteRequest) => Promise<void>
  onLoadSkill?: (name: string) => Promise<SkillFile>
  onUpdateSkill?: (name: string, input: SkillWriteRequest) => Promise<void>
  onDeleteSkill?: (name: string) => Promise<void>
  /** Open a folder in the OS file manager. When the Electron bridge
   *  isn't wired (browser-only build) the Open-folder buttons hide. */
  onOpenFolder?: (path: string) => void
}

// Section ordering + display labels. Anything not in the map keeps its raw
// source string as the header — covers custom SHEJANE_RUNTIME_SKILLS_PATH
// overrides without code changes.
const SECTION_ORDER: readonly string[] = ['claude', 'shejane']
const SECTION_LABEL: Record<string, { zh: string; en: string }> = {
  claude: { zh: '系统', en: 'System' },
  shejane: { zh: '个人', en: 'Personal' },
}

function sectionLabel(source: string, locale: string): string {
  const known = SECTION_LABEL[source]
  if (!known) return source
  return locale.startsWith('zh') ? known.zh : known.en
}

function matchesQuery(skill: InstalledSkill, needle: string): boolean {
  if (!needle) return true
  const hay = `${skill.name} ${skill.description ?? ''}`.toLowerCase()
  return hay.includes(needle)
}

const EMPTY_CATALOG: SkillCatalog = { skills: [], roots: [] }

function skillKind(source: string | undefined): 'builtin' | 'custom' {
  return source === 'shejane' ? 'custom' : 'builtin'
}

interface SkillFormState {
  name: string
  description: string
  content: string
}

type SkillEditorState =
  | { mode: 'create' }
  | { mode: 'edit'; originalName: string }
  | null

const EMPTY_SKILL_FORM: SkillFormState = {
  name: '',
  description: '',
  content: '',
}

function defaultSkillContent(name: string, description: string): string {
  const safeName = name.trim() || 'new-skill'
  const lines = ['---', `name: ${safeName}`]
  if (description.trim()) {
    lines.push(`description: ${description.trim()}`)
  }
  lines.push('---', '', `# ${safeName}`, '')
  return lines.join('\n')
}

function useSkillsViewModel({
  listInstalled,
  embedded = false,
  onCreateSkill,
  onLoadSkill,
  onUpdateSkill,
  onDeleteSkill,
  onOpenFolder,
}: SkillsViewProps) {
  const { t, locale } = useI18n()
  const [catalog, setCatalog] = useState<SkillCatalog>(EMPTY_CATALOG)
  const [loading, setLoading] = useState(true)
  const [query, setQuery] = useState('')
  const [editor, setEditor] = useState<SkillEditorState>(null)
  const [form, setForm] = useState<SkillFormState>(EMPTY_SKILL_FORM)
  const [saving, setSaving] = useState(false)

  const refresh = useCallback(async () => {
    setLoading(true)
    try {
      setCatalog(await listInstalled())
    } finally {
      setLoading(false)
    }
  }, [listInstalled])

  useEffect(() => {
    void refresh()
  }, [refresh])

  const filteredSkills = useMemo(() => {
    const needle = query.trim().toLowerCase()
    if (!needle) return catalog.skills
    return catalog.skills.filter((s) => matchesQuery(s, needle))
  }, [catalog.skills, query])

  // Section list comes from `roots` so an empty Personal dir still
  // shows a header. Skills get bucketed under their `source`; anything
  // whose source isn't represented in roots (shouldn't happen — the
  // runtime owns both) falls back to an "Unknown" bucket.
  const sections = useMemo(() => {
    const buckets = new Map<string, InstalledSkill[]>()
    for (const skill of filteredSkills) {
      const key = skill.source ?? 'unknown'
      const list = buckets.get(key) ?? []
      list.push(skill)
      buckets.set(key, list)
    }
    const orderedRoots: SkillRoot[] = [...catalog.roots].sort((a, b) => {
      const ai = SECTION_ORDER.indexOf(a.source)
      const bi = SECTION_ORDER.indexOf(b.source)
      if (ai !== -1 && bi !== -1) return ai - bi
      if (ai !== -1) return -1
      if (bi !== -1) return 1
      return a.source.localeCompare(b.source)
    })
    // Roots first (in canonical order), then any orphan source from
    // skills (defensive — shouldn't trigger in practice).
    const seen = new Set(orderedRoots.map((r) => r.source))
    const orphans = Array.from(buckets.keys())
      .filter((src) => !seen.has(src))
      .sort()
      .map<SkillRoot>((src) => ({ source: src, path: '' }))
    return [...orderedRoots, ...orphans].map((root) => ({
      root,
      skills: buckets.get(root.source) ?? [],
    }))
  }, [catalog.roots, filteredSkills])

  const totalCount = catalog.skills.length
  const filteredEmpty = totalCount > 0 && filteredSkills.length === 0
  const personalRootPath = useMemo(() => {
    return (
      catalog.roots.find((root) => root.source === 'shejane')?.path
      ?? catalog.skills.find((skill) => skill.source === 'shejane')?.root_path
      ?? ''
    )
  }, [catalog.roots, catalog.skills])
  const canCreateSkill = Boolean(onOpenFolder && personalRootPath)
  const canEditSkills = Boolean(onCreateSkill)

  const openCreate = useCallback(() => {
    setForm({ ...EMPTY_SKILL_FORM, content: defaultSkillContent('', '') })
    setEditor({ mode: 'create' })
  }, [])

  const openEdit = useCallback(async (skill: InstalledSkill) => {
    if (!onLoadSkill) return
    const file = await onLoadSkill(skill.name)
    setForm({
      name: file.name,
      description: file.description ?? skill.description ?? '',
      content: file.content,
    })
    setEditor({ mode: 'edit', originalName: skill.name })
  }, [onLoadSkill])

  const submitEditor = useCallback(async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (saving) return
    const input: SkillWriteRequest = {
      name: form.name.trim(),
      description: form.description.trim(),
      content: form.content,
    }
    if (!input.name) return
    setSaving(true)
    try {
      if (editor?.mode === 'edit') {
        await onUpdateSkill?.(editor.originalName, input)
      } else {
        await onCreateSkill?.(input)
      }
      setEditor(null)
      setForm(EMPTY_SKILL_FORM)
      await refresh()
    } finally {
      setSaving(false)
    }
  }, [editor, form, onCreateSkill, onUpdateSkill, refresh, saving])

  const deleteSkill = useCallback(async (name: string) => {
    await onDeleteSkill?.(name)
    await refresh()
  }, [onDeleteSkill, refresh])

  return { canCreateSkill, canEditSkills, deleteSkill, editor, embedded, filteredEmpty, form, loading, locale, onDeleteSkill, onLoadSkill, onOpenFolder, onUpdateSkill, openCreate, openEdit, personalRootPath, query, refresh, saving, sections, setEditor, setForm, setQuery, submitEditor, t }
}

export function SkillsView(props: Parameters<typeof useSkillsViewModel>[0]) {
  return <SkillsViewContent view={useSkillsViewModel(props)} />
}

function SkillsViewContent({ view }: { view: ReturnType<typeof useSkillsViewModel> }) {
  const { canCreateSkill, canEditSkills, deleteSkill, editor, embedded, filteredEmpty, form, loading, locale, onDeleteSkill, onLoadSkill, onOpenFolder, onUpdateSkill, openCreate, openEdit, personalRootPath, query, refresh, saving, sections, setEditor, setForm, setQuery, submitEditor, t } = view
  return (
    <section className="workspace skills-view">
      {!embedded ? (
        <header className="topbar topbar-page">
          <div className="chat-toolbar-title">
            <span>{t('skills.title')}</span>
          </div>
        </header>
      ) : null}

      <div className="skills-scroll">
        <div className="skills-content">
          <div className="skills-toolbar">
            <div className="skills-search">
              <IconSearch className="skills-search-icon" size={15} aria-hidden="true" />
              <Input
                type="search"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder={t('skills.searchPlaceholder')}
                aria-label={t('skills.searchPlaceholder')}
              />
            </div>
            <div className="skills-toolbar-actions">
              {canEditSkills ? (
                <Button
                  type="button"
                  size="sm"
                  variant="ghost"
                  className="skills-new-button"
                  onClick={openCreate}
                >
                  <IconPlus size={14} aria-hidden="true" />
                  {t('skills.newSkill')}
                </Button>
              ) : canCreateSkill ? (
                <Button
                  type="button"
                  size="sm"
                  variant="ghost"
                  className="skills-new-button"
                  onClick={() => onOpenFolder?.(personalRootPath)}
                >
                  <IconPlus size={14} aria-hidden="true" />
                  {t('skills.newSkill')}
                </Button>
              ) : null}
              <Button
                type="button"
                size="sm"
                variant="ghost"
                className="skills-refresh-button"
                onClick={() => void refresh()}
                disabled={loading}
                aria-label={t('skills.refresh')}
                title={t('skills.refresh')}
              >
                <IconRefresh size={14} aria-hidden="true" />
              </Button>
            </div>
          </div>

          {editor ? (
            <form className="resource-editor-form" onSubmit={(event) => void submitEditor(event)}>
              <div className="resource-editor-grid">
                <label>
                  <span>{t('skills.form.name')}</span>
                  <Input
                    value={form.name}
                    onChange={(event) => {
                      const name = event.target.value
                      setForm((current) => ({
                        ...current,
                        name,
                        content: editor.mode === 'create' && !current.content.trim()
                          ? defaultSkillContent(name, current.description)
                          : current.content,
                      }))
                    }}
                    aria-label={t('skills.form.name')}
                    disabled={editor.mode === 'edit'}
                    required
                  />
                </label>
                <label>
                  <span>{t('skills.form.description')}</span>
                  <Input
                    value={form.description}
                    onChange={(event) => setForm((current) => ({ ...current, description: event.target.value }))}
                    aria-label={t('skills.form.description')}
                  />
                </label>
                <label className="resource-editor-wide">
                  <span>{t('skills.form.content')}</span>
                  <textarea
                    value={form.content}
                    onChange={(event) => setForm((current) => ({ ...current, content: event.target.value }))}
                    aria-label={t('skills.form.content')}
                    rows={10}
                  />
                </label>
              </div>
              <div className="resource-editor-actions">
                <Button type="submit" size="sm" disabled={saving}>
                  <IconDeviceFloppy size={14} aria-hidden="true" />
                  {t('skills.saveSkill')}
                </Button>
                <Button type="button" size="sm" variant="ghost" onClick={() => setEditor(null)}>
                  <IconX size={14} aria-hidden="true" />
                  {t('common.cancel')}
                </Button>
              </div>
            </form>
          ) : null}

          {filteredEmpty ? (
            <p className="skills-not-found">{t('skills.notFound')}</p>
          ) : null}

          {sections.map(({ root, skills }) => (
            <section className="skills-section" key={root.source}>
              <header className="skills-section-header">
                <h3 className="skills-section-title">{sectionLabel(root.source, locale)}</h3>
                {root.path && onOpenFolder ? (
                  <Button
                    type="button"
                    size="sm"
                    variant="ghost"
                    className="skills-section-open"
                    onClick={() => onOpenFolder(root.path)}
                    title={root.path}
                    aria-label={t('skills.openFolder')}
                  >
                    <IconFolderOpen size={14} aria-hidden="true" />
                  </Button>
                ) : null}
              </header>
              {skills.length === 0 ? (
                <p className="skills-section-empty">
                  {t('skills.section.emptyHint', { path: root.path || '—' })}
                </p>
              ) : (
                <div className="skills-grid">
                  {skills.map((skill) => (
                    <div className="skill-card" key={skill.path} data-source={root.source}>
                      <div className="skill-card-head">
                        <div className="skill-card-title">
                          <div className="skill-card-icon" aria-hidden="true">
                            <IconSparkles size={16} />
                          </div>
                          <div className="skill-card-name">{skill.name}</div>
                        </div>
                        <IconCheck className="skill-card-check" size={14} aria-label={t('skills.statusReady')} />
                      </div>
                      <div className="skill-card-text">
                        {skill.description ? (
                          <div className="skill-card-desc">{skill.description}</div>
                        ) : null}
                      </div>
                      <div className="skill-card-footer">
                        <span className="skill-card-kind">
                          {t(skillKind(skill.source) === 'builtin' ? 'skills.kindBuiltin' : 'skills.kindCustom')}
                        </span>
                        {skill.source === 'shejane' && (onUpdateSkill || onDeleteSkill) ? (
                          <div className="resource-row-actions">
                            {onUpdateSkill && onLoadSkill ? (
                              <Button
                                type="button"
                                size="icon-xs"
                                variant="ghost"
                                aria-label={t('skills.editSkillAria', { name: skill.name })}
                                onClick={() => void openEdit(skill)}
                              >
                                <IconPencil size={13} aria-hidden="true" />
                              </Button>
                            ) : null}
                            {onDeleteSkill ? (
                              <Button
                                type="button"
                                size="icon-xs"
                                variant="ghost"
                                aria-label={t('skills.deleteSkillAria', { name: skill.name })}
                                onClick={() => void deleteSkill(skill.name)}
                              >
                                <IconTrash size={13} aria-hidden="true" />
                              </Button>
                            ) : null}
                          </div>
                        ) : null}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </section>
          ))}
        </div>
      </div>
    </section>
  )
}
