import type { JSX } from 'react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { toast } from 'sonner'
import { useLexicalComposerContext } from '@lexical/react/LexicalComposerContext'
import {
  LexicalTypeaheadMenuPlugin,
  MenuOption,
  type MenuTextMatch,
} from '@lexical/react/LexicalTypeaheadMenuPlugin'
import {
  $createTextNode,
  $getRoot,
  $getSelection,
  $isElementNode,
  $isRangeSelection,
  type TextNode,
} from 'lexical'

import type { InstalledSkill, McpServerInfo, PluginDetail } from '@/runtime/client'
import { isAvailablePlugin } from '@/features/plugins/pluginAvailability'
import { useI18n } from '@/shared/i18n/i18n'
import {
  $createFunctionNode,
  $createMCPNode,
  $createPluginCommandNode,
  $createSkillNode,
  $isPluginCommandNode,
} from './SkillNode'

type MenuKind = 'function' | 'skill' | 'mcp' | 'plugin-command'

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

export function SkillCommandTypeaheadPlugin(props: Parameters<typeof useSkillTypeaheadViewModel>[0]) {
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


