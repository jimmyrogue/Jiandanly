import { useCallback, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
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
import { useI18n } from '@/shared/i18n/i18n'
import type { PluginDetail } from '@/runtime/client'
import { isAvailablePlugin } from '@/features/plugins/pluginAvailability'
import { $createPluginNode, $isPluginNode } from './SkillNode'

class PluginMentionOption extends MenuOption {
  plugin: PluginDetail

  constructor(plugin: PluginDetail) {
    super(`plugin:${plugin.id}`)
    this.plugin = plugin
  }
}

export function PluginMentionTypeaheadPlugin({
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
