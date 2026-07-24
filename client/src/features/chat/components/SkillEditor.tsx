import type { JSX } from 'react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { toast } from 'sonner'
import { LexicalComposer } from '@lexical/react/LexicalComposer'
import { PlainTextPlugin } from '@lexical/react/LexicalPlainTextPlugin'
import { ContentEditable } from '@lexical/react/LexicalContentEditable'
import { HistoryPlugin } from '@lexical/react/LexicalHistoryPlugin'
import { OnChangePlugin } from '@lexical/react/LexicalOnChangePlugin'
import { LexicalErrorBoundary } from '@lexical/react/LexicalErrorBoundary'
import { useLexicalComposerContext } from '@lexical/react/LexicalComposerContext'
import {
  LexicalTypeaheadMenuPlugin,
  MenuOption,
  type MenuTextMatch,
} from '@lexical/react/LexicalTypeaheadMenuPlugin'
import {
  $createLineBreakNode,
  $createParagraphNode,
  $createTextNode,
  $getRoot,
  $getSelection,
  $isElementNode,
  $isRangeSelection,
  COMMAND_PRIORITY_LOW,
  type EditorState,
  KEY_BACKSPACE_COMMAND,
  KEY_DELETE_COMMAND,
  KEY_ENTER_COMMAND,
  type TextNode,
} from 'lexical'
import {
  $createFunctionNode,
  $createMCPNode,
  $createPluginCommandNode,
  $createPluginNode,
  $createSkillNode,
  $isFunctionNode,
  $isMCPNode,
  $isPluginCommandNode,
  $isPluginNode,
  $isSkillNode,
  FunctionNode,
  MCPNode,
  PluginCommandNode,
  PluginNode,
  SkillNode,
} from './SkillNode'
import { tokenizeDraft } from '../skillDraft'
import { useI18n } from '@/shared/i18n/i18n'
import type { InstalledSkill, McpServerInfo, PluginDetail } from '@/runtime/client'

export interface SkillEditorProps {
  draft: string
  onDraftChange: (value: string) => void
  onSend: () => void
  listSkills: () => Promise<InstalledSkill[]>
  /** Optional — when omitted (probe not yet ready) the MCP group is
   *  hidden from the slash menu instead of crashing. */
  listMcpServers?: () => Promise<McpServerInfo[]>
  listPlugins?: () => Promise<PluginDetail[]>
  /** When false (web build, no runtime) the slash-command menu — functions,
   *  skills, MCP, all runtime-executed — is disabled entirely. The editor
   *  still works as a plain text input. Defaults to true. */
  commandsEnabled?: boolean
  pluginReferencesEnabled?: boolean
  placeholder: string
}

type MenuKind = 'function' | 'skill' | 'mcp' | 'plugin-command'

const isAvailablePlugin = (plugin: PluginDetail) =>
  plugin.enabled && !plugin.retired && plugin.compatibility === 'compatible'

class ComposerMenuOption extends MenuOption {
  kind: MenuKind
  id: string
  name: string
  description: string
  plugin?: PluginDetail
  commandId?: string
  disabled: boolean
  constructor(
    kind: MenuKind,
    id: string,
    name: string,
    description: string,
    plugin?: PluginDetail,
    commandId?: string,
    disabled = false,
  ) {
    super(`${kind}:${id}`)
    this.kind = kind
    this.id = id
    this.name = name
    this.description = description
    this.plugin = plugin
    this.commandId = commandId
    this.disabled = disabled
  }
}

class PluginMentionOption extends MenuOption {
  plugin: PluginDetail
  constructor(plugin: PluginDetail) {
    super(`plugin:${plugin.id}`)
    this.plugin = plugin
  }
}

function buildRootFromDraft(draft: string): void {
  const root = $getRoot()
  root.clear()
  const paragraph = $createParagraphNode()
  for (const node of tokenizeDraft(draft)) {
    if (node.type === 'skill') {
      paragraph.append($createSkillNode(node.name))
      continue
    }
    if (node.type === 'function') {
      paragraph.append($createFunctionNode(node.name))
      continue
    }
    if (node.type === 'mcp') {
      paragraph.append($createMCPNode(node.name))
      continue
    }
    if (node.type === 'plugin') {
      paragraph.append($createPluginNode(node.pluginId, node.name, node.expectedDigest))
      continue
    }
    if (node.type === 'plugin_command') {
      paragraph.append(
        $createPluginCommandNode(
          node.pluginId,
          node.pluginName,
          node.commandId,
          node.title,
          node.expectedDigest,
        ),
      )
      continue
    }
    const parts = node.value.split('\n')
    parts.forEach((part, index) => {
      if (index > 0) {
        paragraph.append($createLineBreakNode())
      }
      if (part) {
        paragraph.append($createTextNode(part))
      }
    })
  }
  root.append(paragraph)
  root.selectEnd()
}

function ExternalDraftPlugin({
  draft,
  lastSerializedRef,
}: {
  draft: string
  lastSerializedRef: { current: string }
}): null {
  const [editor] = useLexicalComposerContext()
  useEffect(() => {
    if (draft === lastSerializedRef.current) {
      return
    }
    lastSerializedRef.current = draft
    editor.update(() => buildRootFromDraft(draft))
    // When the draft is set externally to a non-empty value (e.g. a
    // welcome-screen suggestion tile prefills it), move focus into the
    // editor so the user can edit/send right away. buildRootFromDraft
    // already places the caret at the end.
    if (draft) {
      editor.focus()
    }
  }, [draft, editor, lastSerializedRef])
  return null
}

function SubmitPlugin({
  onSend,
  menuOpenRef,
}: {
  onSend: () => void
  menuOpenRef: { current: boolean }
}): null {
  const [editor] = useLexicalComposerContext()
  useEffect(
    () =>
      editor.registerCommand(
        KEY_ENTER_COMMAND,
        (event: KeyboardEvent | null) => {
          if (menuOpenRef.current) {
            return false
          }
          // Shift+Enter → newline. Plain Enter (or Cmd/Ctrl+Enter for
          // legacy muscle memory) → send. This is the convention users
          // expect from chat apps; the old Cmd+Enter-only behaviour was
          // documenter-oriented and surprising for newcomers.
          if (event && event.shiftKey) {
            event.preventDefault()
            editor.update(() => {
              const selection = $getSelection()
              if ($isRangeSelection(selection)) {
                selection.insertLineBreak()
              }
            })
            return true
          }
          if (event) {
            event.preventDefault()
          }
          onSend()
          return true
        },
        COMMAND_PRIORITY_LOW,
      ),
    [editor, onSend, menuOpenRef],
  )
  return null
}

function SkillDeletePlugin(): null {
  const [editor] = useLexicalComposerContext()
  useEffect(() => {
    const handle = (isBackward: boolean): boolean => {
      const selection = $getSelection()
      if (!$isRangeSelection(selection) || !selection.isCollapsed()) {
        return false
      }
      const anchor = selection.anchor
      const node = anchor.getNode()
      let target = null
      if (anchor.type === 'text') {
        if (isBackward && anchor.offset === 0) {
          target = node.getPreviousSibling()
        } else if (!isBackward && anchor.offset === node.getTextContentSize()) {
          target = node.getNextSibling()
        }
      } else {
        const index = isBackward ? anchor.offset - 1 : anchor.offset
        target = 'getChildAtIndex' in node ? node.getChildAtIndex(index) : null
      }
      if (
        target &&
        ($isSkillNode(target) ||
          $isFunctionNode(target) ||
          $isMCPNode(target) ||
          $isPluginNode(target) ||
          $isPluginCommandNode(target))
      ) {
        target.remove()
        return true
      }
      return false
    }
    const unregisterBackspace = editor.registerCommand(
      KEY_BACKSPACE_COMMAND,
      () => handle(true),
      COMMAND_PRIORITY_LOW,
    )
    const unregisterDelete = editor.registerCommand(
      KEY_DELETE_COMMAND,
      () => handle(false),
      COMMAND_PRIORITY_LOW,
    )
    return () => {
      unregisterBackspace()
      unregisterDelete()
    }
  }, [editor])
  return null
}

function useSkillTypeaheadViewModel({
  listSkills,
  listMcpServers,
  listPlugins,
  pluginCommandsEnabled,
  menuOpenRef,
}: {
  listSkills: () => Promise<InstalledSkill[]>
  listMcpServers?: () => Promise<McpServerInfo[]>
  listPlugins?: () => Promise<PluginDetail[]>
  pluginCommandsEnabled: boolean
  menuOpenRef: { current: boolean }
}) {
  const [editor] = useLexicalComposerContext()
  const { t } = useI18n()
  const [query, setQuery] = useState<string | null>(null)
  const [skills, setSkills] = useState<InstalledSkill[]>([])
  const [mcpServers, setMcpServers] = useState<McpServerInfo[]>([])
  const [plugins, setPlugins] = useState<PluginDetail[]>([])
  const [loading, setLoading] = useState(false)
  const [mcpLoading, setMcpLoading] = useState(false)
  const [pluginsLoading, setPluginsLoading] = useState(false)
  const loadedRef = useRef(false)

  useEffect(() => {
    if (query !== null && !loadedRef.current) {
      loadedRef.current = true
      // Kick off both lookups in parallel — they hit different runtime
      // endpoints and the menu shouldn't wait for the slower one to
      // render the faster one's group.
      setLoading(true)
      listSkills()
        .then(setSkills)
        .catch(() => setSkills([]))
        .finally(() => setLoading(false))
      if (listMcpServers) {
        setMcpLoading(true)
        listMcpServers()
          .then(setMcpServers)
          .catch(() => setMcpServers([]))
          .finally(() => setMcpLoading(false))
      }
      if (listPlugins) {
        setPluginsLoading(true)
        listPlugins()
          .then((items) => setPlugins(items.filter(isAvailablePlugin)))
          .catch(() => setPlugins([]))
          .finally(() => setPluginsLoading(false))
      }
    }
    if (query === null) {
      loadedRef.current = false
    }
  }, [query, listSkills, listMcpServers, listPlugins])

  const triggerFn = useCallback((text: string): MenuTextMatch | null => {
    const match = /(^|\s)\/([^\s/]*)$/.exec(text)
    if (match === null) {
      return null
    }
    const matchingString = match[2]
    const replaceableString = `/${matchingString}`
    return {
      leadOffset: text.length - replaceableString.length,
      matchingString,
      replaceableString,
    }
  }, [])

  const functionsCatalog = useMemo(
    () => [{ id: 'image', name: t('composer.fn.image.name'), description: t('composer.fn.image.desc') }],
    [t],
  )

  // Functions first, then plugin commands, skills, and MCP — fixed group order so the
  // user develops muscle memory for "/" → top options.
  const options = useMemo(() => {
    const normalized = (query ?? '').toLowerCase()
    const match = (name: string, description: string) =>
      normalized === '' ||
      name.toLowerCase().includes(normalized) ||
      description.toLowerCase().includes(normalized)
    const funcOptions = functionsCatalog.flatMap((fn) =>
      match(fn.name, fn.description)
        ? [new ComposerMenuOption('function', fn.id, fn.name, fn.description)]
        : [])
    const skillOptions = skills.flatMap((skill) =>
      match(skill.name, skill.description)
        ? [new ComposerMenuOption('skill', skill.name, skill.name, skill.description)]
        : [])
    const mcpOptions = mcpServers.flatMap((server) =>
      match(server.name, `${server.source} ${server.transport}`)
        ? [new ComposerMenuOption(
            'mcp',
            server.name,
            server.name,
            // Pack source + transport into the description slot so the
            // user can tell two same-named servers apart (rare, but
            // happens when shejane overrides a Claude Desktop one).
            `${server.source} · ${server.transport}`,
          )]
        : [])
    const pluginCommandOptions = plugins.flatMap((plugin) =>
      plugin.commands.flatMap((command) =>
        match(command.title, `${plugin.name} ${command.id} ${command.description}`)
          ? [new ComposerMenuOption(
              'plugin-command',
              `${plugin.id}:${command.id}`,
              command.title,
              `${plugin.name} · ${command.description}${
                pluginCommandsEnabled ? '' : ` · ${t('composer.pluginMenu.newTaskOnly')}`
              }`,
              plugin,
              command.id,
              !pluginCommandsEnabled,
            )]
          : []),
    )
    return [...funcOptions, ...pluginCommandOptions, ...skillOptions, ...mcpOptions]
  }, [functionsCatalog, skills, mcpServers, plugins, pluginCommandsEnabled, query, t])

  const onSelectOption = useCallback(
    (option: ComposerMenuOption, textNodeContainingQuery: TextNode | null, closeMenu: () => void) => {
      if (option.disabled) return
      let replacedPluginCommand = false
      editor.update(() => {
        let node
        if (option.kind === 'function') {
          node = $createFunctionNode(option.id)
        } else if (option.kind === 'mcp') {
          node = $createMCPNode(option.id)
        } else if (option.kind === 'plugin-command' && option.plugin && option.commandId) {
          for (const block of $getRoot().getChildren()) {
            if (!$isElementNode(block)) continue
            for (const child of block.getChildren()) {
              if ($isPluginCommandNode(child)) {
                child.remove()
                replacedPluginCommand = true
              }
            }
          }
          const command = option.plugin.commands.find((item) => item.id === option.commandId)
          if (!command) return
          node = $createPluginCommandNode(
            option.plugin.id,
            option.plugin.name,
            command.id,
            command.title,
            option.plugin.digest,
          )
        } else {
          node = $createSkillNode(option.id)
        }
        if (textNodeContainingQuery) {
          textNodeContainingQuery.replace(node)
        } else {
          const selection = $getSelection()
          if ($isRangeSelection(selection)) {
            selection.insertNodes([node])
          }
        }
        const space = $createTextNode(' ')
        node.insertAfter(space)
        space.selectEnd()
      })
      if (replacedPluginCommand) toast.message(t('composer.pluginMenu.commandReplaced'))
      closeMenu()
    },
    [editor, t],
  )

  return { editor, listMcpServers, listPlugins, loading, mcpLoading, menuOpenRef, onSelectOption, options, pluginsLoading, setQuery, t, triggerFn }
}

function SkillTypeaheadPlugin(props: Parameters<typeof useSkillTypeaheadViewModel>[0]) {
  return <SkillTypeaheadView view={useSkillTypeaheadViewModel(props)} />
}

function SkillTypeaheadView({ view }: { view: ReturnType<typeof useSkillTypeaheadViewModel> }) {
  const { editor, listMcpServers, listPlugins, loading, mcpLoading, menuOpenRef, onSelectOption, options, pluginsLoading, setQuery, t, triggerFn } = view
  return (
    <LexicalTypeaheadMenuPlugin<ComposerMenuOption>
      options={options}
      triggerFn={triggerFn}
      onQueryChange={setQuery}
      onSelectOption={onSelectOption}
      onOpen={() => {
        menuOpenRef.current = true
      }}
      onClose={() => {
        menuOpenRef.current = false
      }}
      menuRenderFn={(anchorElementRef, { selectedIndex, selectOptionAndCleanUp, setHighlightedIndex }) => {
        if (!anchorElementRef.current) {
          return null
        }
        const menuRoot = editor.getRootElement()?.closest('.composer') ?? anchorElementRef.current
        const funcOptions = options.filter((option) => option.kind === 'function')
        const skillOptions = options.filter((option) => option.kind === 'skill')
        const mcpOptions = options.filter((option) => option.kind === 'mcp')
        const pluginCommandOptions = options.filter((option) => option.kind === 'plugin-command')
        const showSkillsGroup = skillOptions.length > 0 || loading
        // The MCP group only renders when there's something to show AND
        // the App actually wired the listMcpServers prop — when the
        // runtime isn't online yet the prop is undefined and the
        // section silently disappears (avoids "empty group" noise).
        const showMcpGroup = listMcpServers !== undefined && (mcpOptions.length > 0 || mcpLoading)
        const showPluginCommandGroup =
          listPlugins !== undefined && (pluginCommandOptions.length > 0 || pluginsLoading)
        const renderItem = (option: ComposerMenuOption) => {
          const index = options.indexOf(option)
          return (
            <li
              key={option.key}
              role="option"
              aria-selected={index === selectedIndex}
              aria-disabled={option.disabled || undefined}
              ref={(element) => option.setRefElement(element)}
              className={`composer-skill-menu-item${index === selectedIndex ? ' active' : ''}${
                option.disabled ? ' disabled' : ''
              }`}
              onMouseEnter={() => {
                if (!option.disabled) setHighlightedIndex(index)
              }}
              onMouseDown={(event) => event.preventDefault()}
              onClick={() => {
                if (option.disabled) return
                setHighlightedIndex(index)
                selectOptionAndCleanUp(option)
              }}
            >
              <span className="composer-skill-menu-name">{option.name}</span>
              <span className="composer-skill-menu-desc">{option.description}</span>
            </li>
          )
        }
        const rows: JSX.Element[] = []
        if (funcOptions.length > 0) {
          rows.push(
            <li key="grp-fn" className="composer-menu-group" aria-hidden="true">
              {t('composer.menu.functionsGroup')}
            </li>,
          )
          funcOptions.forEach((option) => rows.push(renderItem(option)))
        }
        if (showPluginCommandGroup) {
          if (funcOptions.length > 0) {
            rows.push(<li key="divider-fn-plugin" className="composer-menu-divider" aria-hidden="true" />)
          }
          rows.push(
            <li key="grp-plugin-command" className="composer-menu-group" aria-hidden="true">
              {t('composer.menu.pluginCommandsGroup')}
            </li>,
          )
          if (pluginsLoading && pluginCommandOptions.length === 0) {
            rows.push(
              <li key="plugin-command-loading" className="composer-skill-menu-empty">
                {t('composer.pluginMenu.loading')}
              </li>,
            )
          } else {
            pluginCommandOptions.forEach((option) => rows.push(renderItem(option)))
          }
        }
        if (showSkillsGroup) {
          if (funcOptions.length > 0 || showPluginCommandGroup) {
            rows.push(<li key="divider-plugin-skill" className="composer-menu-divider" aria-hidden="true" />)
          }
          rows.push(
            <li key="grp-skill" className="composer-menu-group" aria-hidden="true">
              {t('composer.menu.skillsGroup')}
            </li>,
          )
          if (loading && skillOptions.length === 0) {
            rows.push(
              <li key="skill-loading" className="composer-skill-menu-empty">
                {t('composer.skillMenu.loading')}
              </li>,
            )
          } else {
            skillOptions.forEach((option) => rows.push(renderItem(option)))
          }
        }
        if (showMcpGroup) {
          if (funcOptions.length > 0 || showPluginCommandGroup || showSkillsGroup) {
            rows.push(<li key="divider-skill-mcp" className="composer-menu-divider" aria-hidden="true" />)
          }
          rows.push(
            <li key="grp-mcp" className="composer-menu-group" aria-hidden="true">
              {t('composer.menu.mcpGroup')}
            </li>,
          )
          if (mcpLoading && mcpOptions.length === 0) {
            rows.push(
              <li key="mcp-loading" className="composer-skill-menu-empty">
                {t('composer.mcpMenu.loading')}
              </li>,
            )
          } else if (mcpOptions.length === 0) {
            rows.push(
              <li key="mcp-empty" className="composer-skill-menu-empty">
                {t('composer.mcpMenu.empty')}
              </li>,
            )
          } else {
            mcpOptions.forEach((option) => rows.push(renderItem(option)))
          }
        }
        if (rows.length === 0) {
          rows.push(
            <li key="empty" className="composer-skill-menu-empty">
              {t('composer.skillMenu.empty')}
            </li>,
          )
        }
        return createPortal(
          <ul className="composer-skill-menu" role="listbox" aria-label={t('sidebar.skills')}>
            {rows}
          </ul>,
          menuRoot,
        )
      }}
    />
  )
}

function PluginMentionTypeaheadPlugin({
  listPlugins,
  menuOpenRef,
}: {
  listPlugins: () => Promise<PluginDetail[]>
  menuOpenRef: { current: boolean }
}) {
  const [editor] = useLexicalComposerContext()
  const { t } = useI18n()
  const [query, setQuery] = useState<string | null>(null)
  const [plugins, setPlugins] = useState<PluginDetail[]>([])
  const [loading, setLoading] = useState(false)
  const loadedRef = useRef(false)

  const handleQueryChange = (nextQuery: string | null) => {
    setQuery(nextQuery)
    if (nextQuery === null) {
      loadedRef.current = false
      return
    }
    if (loadedRef.current) return
    loadedRef.current = true
    setLoading(true)
    listPlugins()
      .then((items) => setPlugins(items.filter(isAvailablePlugin)))
      .catch(() => setPlugins([]))
      .finally(() => setLoading(false))
  }

  const options = useMemo(() => {
    const normalized = (query ?? '').toLowerCase()
    return plugins.flatMap((plugin) =>
      normalized === '' ||
      plugin.name.toLowerCase().includes(normalized) ||
      plugin.id.toLowerCase().includes(normalized) ||
      plugin.publisher.name.toLowerCase().includes(normalized)
        ? [new PluginMentionOption(plugin)]
        : [])
  }, [plugins, query])

  const triggerFn = useCallback((text: string): MenuTextMatch | null => {
    const match = /(^|\s)@([^\s@]*)$/.exec(text)
    if (!match) return null
    const matchingString = match[2]
    const replaceableString = `@${matchingString}`
    return {
      leadOffset: text.length - replaceableString.length,
      matchingString,
      replaceableString,
    }
  }, [])

  const onSelectOption = useCallback(
    (option: PluginMentionOption, textNodeContainingQuery: TextNode | null, closeMenu: () => void) => {
      editor.update(() => {
        for (const block of $getRoot().getChildren()) {
          if (!$isElementNode(block)) continue
          for (const child of block.getChildren()) {
            if ($isPluginNode(child) && child.getPluginId() === option.plugin.id) child.remove()
          }
        }
        const node = $createPluginNode(
          option.plugin.id,
          option.plugin.name,
          option.plugin.digest,
        )
        if (textNodeContainingQuery) {
          textNodeContainingQuery.replace(node)
        } else {
          const selection = $getSelection()
          if ($isRangeSelection(selection)) selection.insertNodes([node])
        }
        const space = $createTextNode(' ')
        node.insertAfter(space)
        space.selectEnd()
      })
      closeMenu()
    },
    [editor],
  )

  return (
    <LexicalTypeaheadMenuPlugin<PluginMentionOption>
      options={options}
      triggerFn={triggerFn}
      onQueryChange={handleQueryChange}
      onSelectOption={onSelectOption}
      onOpen={() => {
        menuOpenRef.current = true
      }}
      onClose={() => {
        menuOpenRef.current = false
      }}
      menuRenderFn={(anchorElementRef, { selectedIndex, selectOptionAndCleanUp, setHighlightedIndex }) => {
        if (!anchorElementRef.current) return null
        const menuRoot = editor.getRootElement()?.closest('.composer') ?? anchorElementRef.current
        return createPortal(
          <ul className="composer-skill-menu" role="listbox" aria-label={t('composer.menu.pluginsGroup')}>
            <li className="composer-menu-group" aria-hidden="true">
              {t('composer.menu.pluginsGroup')}
            </li>
            {loading && options.length === 0 ? (
              <li className="composer-skill-menu-empty">{t('composer.pluginMenu.loading')}</li>
            ) : options.length === 0 ? (
              <li className="composer-skill-menu-empty">{t('composer.pluginMenu.empty')}</li>
            ) : (
              options.map((option, index) => (
                <li
                  key={option.key}
                  role="option"
                  aria-selected={index === selectedIndex}
                  ref={(element) => option.setRefElement(element)}
                  className={`composer-skill-menu-item${index === selectedIndex ? ' active' : ''}`}
                  onMouseEnter={() => setHighlightedIndex(index)}
                  onMouseDown={(event) => event.preventDefault()}
                  onClick={() => {
                    setHighlightedIndex(index)
                    selectOptionAndCleanUp(option)
                  }}
                >
                  <span className="composer-skill-menu-name">{option.plugin.name}</span>
                  <span className="composer-skill-menu-desc">
                    {option.plugin.publisher.name} · {option.plugin.id}
                  </span>
                </li>
              ))
            )}
          </ul>,
          menuRoot,
        )
      }}
    />
  )
}

export function SkillEditor({
  draft,
  onDraftChange,
  onSend,
  listSkills,
  listMcpServers,
  listPlugins,
  commandsEnabled = true,
  pluginReferencesEnabled = true,
  placeholder,
}: SkillEditorProps) {
  const { t } = useI18n()
  const draftRef = useRef(draft)
  const lastSerializedRef = useRef(draft)
  const menuOpenRef = useRef(false)
  const pluginBindings = useMemo(() => {
    const bindings = new Map<string, { digest: string; label: string }>()
    for (const node of tokenizeDraft(draft)) {
      if (node.type === 'plugin') {
        bindings.set(node.pluginId, { digest: node.expectedDigest, label: node.name })
      } else if (node.type === 'plugin_command') {
        bindings.set(node.pluginId, { digest: node.expectedDigest, label: node.pluginName })
      }
    }
    return bindings
  }, [draft])
  const [stalePlugins, setStalePlugins] = useState<string[]>([])

  useEffect(() => {
    if (!listPlugins || pluginBindings.size === 0) {
      setStalePlugins([])
      return
    }
    let active = true
    void listPlugins()
      .then((plugins) => {
        if (!active) return
        const current = new Map(plugins.map((plugin) => [plugin.id, plugin.digest]))
        setStalePlugins(
          [...pluginBindings].flatMap(([id, binding]) =>
            current.get(id) === binding.digest ? [] : [binding.label],
          ),
        )
      })
      .catch(() => {
        if (active) setStalePlugins([])
      })
    return () => {
      active = false
    }
  }, [listPlugins, pluginBindings])

  const initialConfig = useMemo(
    () => ({
      namespace: 'composer-skill-editor',
      nodes: [SkillNode, FunctionNode, MCPNode, PluginNode, PluginCommandNode],
      onError: (error: Error) => {
        // Surface in dev; never crash the composer.
        console.error('[skill-editor]', error)
      },
      editorState: () => buildRootFromDraft(draftRef.current),
    }),
    [],
  )

  const handleChange = useCallback(
    (editorState: EditorState) => {
      editorState.read(() => {
        const serialized = $getRoot().getTextContent()
        if (serialized === lastSerializedRef.current) {
          return
        }
        lastSerializedRef.current = serialized
        onDraftChange(serialized)
      })
    },
    [onDraftChange],
  )

  return (
    <div className="composer-editor-shell">
      <LexicalComposer initialConfig={initialConfig}>
        <PlainTextPlugin
          contentEditable={<ContentEditable className="composer-editor" aria-label={placeholder} />}
          placeholder={<div className="composer-editor-ph">{placeholder}</div>}
          ErrorBoundary={LexicalErrorBoundary}
        />
        <HistoryPlugin />
        <OnChangePlugin onChange={handleChange} ignoreSelectionChange />
        {/* Slash menu (functions/skills/MCP) is runtime-only — omit on web. */}
        {commandsEnabled ? (
          <SkillTypeaheadPlugin
            listSkills={listSkills}
            listMcpServers={listMcpServers}
            listPlugins={listPlugins}
            pluginCommandsEnabled={pluginReferencesEnabled}
            menuOpenRef={menuOpenRef}
          />
        ) : null}
        {commandsEnabled && pluginReferencesEnabled && listPlugins ? (
          <PluginMentionTypeaheadPlugin listPlugins={listPlugins} menuOpenRef={menuOpenRef} />
        ) : null}
        <SubmitPlugin onSend={onSend} menuOpenRef={menuOpenRef} />
        <SkillDeletePlugin />
        <ExternalDraftPlugin draft={draft} lastSerializedRef={lastSerializedRef} />
      </LexicalComposer>
      {stalePlugins.length > 0 ? (
        <div className="composer-plugin-stale" role="status">
          {t('composer.pluginMenu.stale', { names: stalePlugins.join('、') })}
        </div>
      ) : null}
    </div>
  )
}
