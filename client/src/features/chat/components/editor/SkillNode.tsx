import type { JSX } from 'react'
import { IconBox, IconCommand, IconPhoto, IconServer, IconSparkles } from '@tabler/icons-react'
import {
  $applyNodeReplacement,
  DecoratorNode,
  type EditorConfig,
  type LexicalEditor,
  type LexicalNode,
  type NodeKey,
  type SerializedLexicalNode,
  type Spread,
} from 'lexical'
import {
  functionToken,
  mcpToken,
  pluginCommandToken,
  pluginToken,
  skillToken,
} from '../../skillDraft'

// Display labels for function ids. Extensible: add more capabilities here.
export const FUNCTION_LABELS: Record<string, string> = {
  image: '生图',
}

function functionLabel(id: string): string {
  return FUNCTION_LABELS[id] ?? id
}

export type SerializedSkillNode = Spread<{ name: string }, SerializedLexicalNode>

export class SkillNode extends DecoratorNode<JSX.Element> {
  __name: string

  static getType(): string {
    return 'skill'
  }

  static clone(node: SkillNode): SkillNode {
    return new SkillNode(node.__name, node.__key)
  }

  static importJSON(serialized: SerializedSkillNode): SkillNode {
    return $createSkillNode(serialized.name)
  }

  constructor(name: string, key?: NodeKey) {
    super(key)
    this.__name = name
  }

  exportJSON(): SerializedSkillNode {
    return { type: 'skill', version: 1, name: this.__name }
  }

  createDOM(): HTMLElement {
    return document.createElement('span')
  }

  updateDOM(): false {
    return false
  }

  isInline(): boolean {
    return true
  }

  isKeyboardSelectable(): boolean {
    return true
  }

  getTextContent(): string {
    return skillToken(this.__name)
  }

  decorate(_editor: LexicalEditor, _config: EditorConfig): JSX.Element {
    return (
      <span className="skill-chip--inline" data-skill={this.__name}>
        <IconSparkles className="skill-chip-icon" size={12} aria-hidden="true" />
        {this.__name}
      </span>
    )
  }
}

export function $createSkillNode(name: string): SkillNode {
  return $applyNodeReplacement(new SkillNode(name))
}

export function $isSkillNode(node: LexicalNode | null | undefined): node is SkillNode {
  return node instanceof SkillNode
}

export type SerializedFunctionNode = Spread<{ name: string }, SerializedLexicalNode>

export class FunctionNode extends DecoratorNode<JSX.Element> {
  __name: string

  static getType(): string {
    return 'function'
  }

  static clone(node: FunctionNode): FunctionNode {
    return new FunctionNode(node.__name, node.__key)
  }

  static importJSON(serialized: SerializedFunctionNode): FunctionNode {
    return $createFunctionNode(serialized.name)
  }

  constructor(name: string, key?: NodeKey) {
    super(key)
    this.__name = name
  }

  exportJSON(): SerializedFunctionNode {
    return { type: 'function', version: 1, name: this.__name }
  }

  createDOM(): HTMLElement {
    return document.createElement('span')
  }

  updateDOM(): false {
    return false
  }

  isInline(): boolean {
    return true
  }

  isKeyboardSelectable(): boolean {
    return true
  }

  getTextContent(): string {
    return functionToken(this.__name)
  }

  decorate(_editor: LexicalEditor, _config: EditorConfig): JSX.Element {
    return (
      <span className="function-chip--inline" data-function={this.__name}>
        <IconPhoto className="skill-chip-icon" size={12} aria-hidden="true" />
        {functionLabel(this.__name)}
      </span>
    )
  }
}

export function $createFunctionNode(name: string): FunctionNode {
  return $applyNodeReplacement(new FunctionNode(name))
}

export function $isFunctionNode(node: LexicalNode | null | undefined): node is FunctionNode {
  return node instanceof FunctionNode
}

export type SerializedMCPNode = Spread<{ name: string }, SerializedLexicalNode>

/** Inline MCP server reference (selected from the slash menu's MCP
 *  group). Round-trips through the draft string as
 *  `${MCP_OPEN}name${MCP_CLOSE}` so deletion/serialization is
 *  symmetric with skill + function nodes. */
export class MCPNode extends DecoratorNode<JSX.Element> {
  __name: string

  static getType(): string {
    return 'mcp'
  }

  static clone(node: MCPNode): MCPNode {
    return new MCPNode(node.__name, node.__key)
  }

  static importJSON(serialized: SerializedMCPNode): MCPNode {
    return $createMCPNode(serialized.name)
  }

  constructor(name: string, key?: NodeKey) {
    super(key)
    this.__name = name
  }

  exportJSON(): SerializedMCPNode {
    return { type: 'mcp', version: 1, name: this.__name }
  }

  createDOM(): HTMLElement {
    return document.createElement('span')
  }

  updateDOM(): false {
    return false
  }

  isInline(): boolean {
    return true
  }

  isKeyboardSelectable(): boolean {
    return true
  }

  getTextContent(): string {
    return mcpToken(this.__name)
  }

  decorate(_editor: LexicalEditor, _config: EditorConfig): JSX.Element {
    return (
      <span className="mcp-chip--inline" data-mcp={this.__name}>
        <IconServer className="skill-chip-icon" size={12} aria-hidden="true" />
        {this.__name}
      </span>
    )
  }
}

export function $createMCPNode(name: string): MCPNode {
  return $applyNodeReplacement(new MCPNode(name))
}

export function $isMCPNode(node: LexicalNode | null | undefined): node is MCPNode {
  return node instanceof MCPNode
}

export type SerializedPluginNode = Spread<
  { pluginId: string; name: string; expectedDigest: string },
  SerializedLexicalNode
>

export class PluginNode extends DecoratorNode<JSX.Element> {
  __pluginId: string
  __name: string
  __expectedDigest: string

  static getType(): string {
    return 'plugin'
  }

  static clone(node: PluginNode): PluginNode {
    return new PluginNode(node.__pluginId, node.__name, node.__expectedDigest, node.__key)
  }

  static importJSON(serialized: SerializedPluginNode): PluginNode {
    return $createPluginNode(serialized.pluginId, serialized.name, serialized.expectedDigest)
  }

  constructor(pluginId: string, name: string, expectedDigest: string, key?: NodeKey) {
    super(key)
    this.__pluginId = pluginId
    this.__name = name
    this.__expectedDigest = expectedDigest
  }

  exportJSON(): SerializedPluginNode {
    return {
      type: 'plugin',
      version: 1,
      pluginId: this.__pluginId,
      name: this.__name,
      expectedDigest: this.__expectedDigest,
    }
  }

  createDOM(): HTMLElement {
    return document.createElement('span')
  }

  updateDOM(): false {
    return false
  }

  isInline(): boolean {
    return true
  }

  isKeyboardSelectable(): boolean {
    return true
  }

  getPluginId(): string {
    return this.__pluginId
  }

  getTextContent(): string {
    return pluginToken({
      pluginId: this.__pluginId,
      name: this.__name,
      expectedDigest: this.__expectedDigest,
    })
  }

  decorate(_editor: LexicalEditor, _config: EditorConfig): JSX.Element {
    return (
      <span className="plugin-chip--inline" data-plugin={this.__pluginId} title={this.__pluginId}>
        <IconBox className="skill-chip-icon" size={12} aria-hidden="true" />
        {this.__name}
      </span>
    )
  }
}

export function $createPluginNode(
  pluginId: string,
  name: string,
  expectedDigest: string,
): PluginNode {
  return $applyNodeReplacement(new PluginNode(pluginId, name, expectedDigest))
}

export function $isPluginNode(node: LexicalNode | null | undefined): node is PluginNode {
  return node instanceof PluginNode
}

export type SerializedPluginCommandNode = Spread<
  {
    pluginId: string
    pluginName: string
    commandId: string
    title: string
    expectedDigest: string
  },
  SerializedLexicalNode
>

export class PluginCommandNode extends DecoratorNode<JSX.Element> {
  __pluginId: string
  __pluginName: string
  __commandId: string
  __title: string
  __expectedDigest: string

  static getType(): string {
    return 'plugin-command'
  }

  static clone(node: PluginCommandNode): PluginCommandNode {
    return new PluginCommandNode(
      node.__pluginId,
      node.__pluginName,
      node.__commandId,
      node.__title,
      node.__expectedDigest,
      node.__key,
    )
  }

  static importJSON(serialized: SerializedPluginCommandNode): PluginCommandNode {
    return $createPluginCommandNode(
      serialized.pluginId,
      serialized.pluginName,
      serialized.commandId,
      serialized.title,
      serialized.expectedDigest,
    )
  }

  constructor(
    pluginId: string,
    pluginName: string,
    commandId: string,
    title: string,
    expectedDigest: string,
    key?: NodeKey,
  ) {
    super(key)
    this.__pluginId = pluginId
    this.__pluginName = pluginName
    this.__commandId = commandId
    this.__title = title
    this.__expectedDigest = expectedDigest
  }

  exportJSON(): SerializedPluginCommandNode {
    return {
      type: 'plugin-command',
      version: 1,
      pluginId: this.__pluginId,
      pluginName: this.__pluginName,
      commandId: this.__commandId,
      title: this.__title,
      expectedDigest: this.__expectedDigest,
    }
  }

  createDOM(): HTMLElement {
    return document.createElement('span')
  }

  updateDOM(): false {
    return false
  }

  isInline(): boolean {
    return true
  }

  isKeyboardSelectable(): boolean {
    return true
  }

  getTextContent(): string {
    return pluginCommandToken({
      pluginId: this.__pluginId,
      pluginName: this.__pluginName,
      commandId: this.__commandId,
      title: this.__title,
      expectedDigest: this.__expectedDigest,
    })
  }

  decorate(_editor: LexicalEditor, _config: EditorConfig): JSX.Element {
    return (
      <span className="plugin-command-chip--inline" title={`${this.__pluginName}: ${this.__title}`}>
        <IconCommand className="skill-chip-icon" size={12} aria-hidden="true" />
        {this.__title}
      </span>
    )
  }
}

export function $createPluginCommandNode(
  pluginId: string,
  pluginName: string,
  commandId: string,
  title: string,
  expectedDigest: string,
): PluginCommandNode {
  return $applyNodeReplacement(
    new PluginCommandNode(pluginId, pluginName, commandId, title, expectedDigest),
  )
}

export function $isPluginCommandNode(
  node: LexicalNode | null | undefined,
): node is PluginCommandNode {
  return node instanceof PluginCommandNode
}
