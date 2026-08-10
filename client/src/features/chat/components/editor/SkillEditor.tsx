import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { LexicalComposer } from '@lexical/react/LexicalComposer'
import { PlainTextPlugin } from '@lexical/react/LexicalPlainTextPlugin'
import { ContentEditable } from '@lexical/react/LexicalContentEditable'
import { HistoryPlugin } from '@lexical/react/LexicalHistoryPlugin'
import { OnChangePlugin } from '@lexical/react/LexicalOnChangePlugin'
import { LexicalErrorBoundary } from '@lexical/react/LexicalErrorBoundary'
import { useLexicalComposerContext } from '@lexical/react/LexicalComposerContext'
import {
  $createLineBreakNode,
  $createParagraphNode,
  $createTextNode,
  $getRoot,
  $getSelection,
  $isRangeSelection,
  COMMAND_PRIORITY_LOW,
  type EditorState,
  KEY_BACKSPACE_COMMAND,
  KEY_DELETE_COMMAND,
  KEY_ENTER_COMMAND,
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
import { tokenizeDraft } from '../../skillDraft'
import { useI18n } from '@/shared/i18n/i18n'
import type { InstalledSkill, McpServerInfo, PluginDetail } from '@/runtime/client'
import { PluginMentionTypeaheadPlugin } from './PluginMentionTypeaheadPlugin'
import { SkillCommandTypeaheadPlugin } from './SkillCommandTypeaheadPlugin'

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
          <SkillCommandTypeaheadPlugin
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
