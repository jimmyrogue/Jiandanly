import { Children, cloneElement, isValidElement, useEffect, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkBreaks from 'remark-breaks'
import remarkGfm from 'remark-gfm'
import remarkNormalizeHeadings from 'remark-normalize-headings'
import { filePreviewKind } from '@/shared/files/filePreview'
import { pathBasename } from '@/shared/files/path'
import type { AgentTimelineItem, LocalFileRef } from '@/shared/local-data/types'
import { ChatImage } from './ChatImage'
import { CodeBlock } from './CodeBlock'

export function MarkdownContent({
  content,
  generatedArtifactImageRefs,
  normalizeHeadings = false,
  workspaceRoot,
  onPreviewLocalFile,
  onLocalFileContextMenu,
}: {
  content: string
  generatedArtifactImageRefs?: ReadonlySet<string>
  normalizeHeadings?: boolean
  workspaceRoot?: string
  onPreviewLocalFile?: (ref: LocalFileRef) => void
  onLocalFileContextMenu?: (ref: LocalFileRef) => void
}) {
  if (!content) {
    return null
  }
  // remark-breaks: a single newline becomes a line break (LLMs emit single
  // newlines as paragraph separators; CommonMark would otherwise merge them).
  // remark-normalize-headings: rebalance ad-hoc heading levels — finished
  // content only, so streaming headings don't jump as more arrive.
  const remarkPlugins = normalizeHeadings
    ? [remarkGfm, remarkBreaks, remarkNormalizeHeadings]
    : [remarkGfm, remarkBreaks]
  // Click-to-preview is only enabled when (a) the parent gave us a
  // callback AND (b) we know which workspace to resolve relative paths
  // against. Without `workspaceRoot` we'd be guessing — the absolute
  // path case still works because the regex captures it whole.
  const previewEnabled = Boolean(onPreviewLocalFile)
  const previewClick = onPreviewLocalFile
  // react-markdown doesn't expose a `text`-node component override (the
  // `components` map only takes HTML element names), so we walk the
  // rendered children of common text containers and replace recognized
  // office filenames inside any string descendant. This catches refs in
  // paragraphs, list items, table cells, headings, blockquotes — without
  // needing a custom remark/rehype plugin.
  const renderChildren = (children: React.ReactNode): React.ReactNode => {
    if (!previewEnabled || !previewClick) return children
    return processChildren(children, workspaceRoot, previewClick, onLocalFileContextMenu)
  }
  return (
    <ReactMarkdown
      remarkPlugins={remarkPlugins}
      components={{
        a: ({ node: _node, ...props }) => <a {...props} target="_blank" rel="noreferrer" />,
        img: ({ node: _node, src, alt }) => {
          const source = typeof src === 'string' ? src : undefined
          const filename = source ? pathBasename(source.split(/[?#]/, 1)[0]) : ''
          return filename && generatedArtifactImageRefs?.has(filename)
            ? null
            : <ChatImage src={source} alt={alt} />
        },
        p: ({ children }) => <p>{renderChildren(children)}</p>,
        li: ({ children }) => <li>{renderChildren(children)}</li>,
        td: ({ children }) => <td>{renderChildren(children)}</td>,
        th: ({ children }) => <th>{renderChildren(children)}</th>,
        h1: ({ children }) => <h1>{renderChildren(children)}</h1>,
        h2: ({ children }) => <h2>{renderChildren(children)}</h2>,
        h3: ({ children }) => <h3>{renderChildren(children)}</h3>,
        h4: ({ children }) => <h4>{renderChildren(children)}</h4>,
        h5: ({ children }) => <h5>{renderChildren(children)}</h5>,
        h6: ({ children }) => <h6>{renderChildren(children)}</h6>,
        blockquote: ({ children }) => <blockquote>{renderChildren(children)}</blockquote>,
        // Fenced code blocks get syntax highlighting + a per-block copy
        // button (CodeBlock). Inline code keeps the plain chip styling.
        // react-markdown v9 dropped the `inline` prop, so detect a block by
        // a `language-*` class or a multi-line body.
        code: ({ node: _node, className, children, ...rest }) => {
          const text = childrenToText(children)
          const match = /language-(\w+)/.exec(className || '')
          if (match || text.includes('\n')) {
            return <CodeBlock language={match?.[1]} code={text.replace(/\n$/, '')} />
          }
          const display = text.trim()
          const kind = filePreviewKind(display)
          const path = resolveLocalPath(display, workspaceRoot)
          if (previewClick && kind && path) {
            return (
              <code className={className} {...rest}>
                <LocalFileLink
                  path={path}
                  kind={kind}
                  name={pathBasename(display) || display}
                  display={display}
                  onClick={previewClick}
                  onContextMenu={onLocalFileContextMenu}
                />
              </code>
            )
          }
          return (
            <code className={className} {...rest}>
              {children}
            </code>
          )
        },
        // CodeBlock supplies its own <pre>; unwrap react-markdown's so we
        // don't nest <pre><div><pre>.
        pre: ({ children }) => <>{children}</>,
      }}
    >
      {content}
    </ReactMarkdown>
  )
}
/** Flatten a react-markdown children tree to its raw text — used to recover
 *  the verbatim source of a fenced code block for highlighting + copy. */
function childrenToText(node: React.ReactNode): string {
  if (node == null || node === false || node === true) {
    return ''
  }
  if (typeof node === 'string' || typeof node === 'number') {
    return String(node)
  }
  if (Array.isArray(node)) {
    return node.map(childrenToText).join('')
  }
  if (typeof node === 'object' && 'props' in node) {
    return childrenToText((node as { props?: { children?: React.ReactNode } }).props?.children)
  }
  return ''
}

/** Element types we deliberately don't crack open. `a` and `button`
 *  would produce invalid nested-interactive markup if we put an
 *  LocalFileLink (button) inside them; LocalFileLink itself never
 *  contains file refs to find. */
const NON_RECURSIVE_INLINE_TYPES = new Set<unknown>(['a', 'button', 'code'])

/** Walk a ReactNode tree, replacing any string descendants with a
 *  fragment that has recognized local filenames wrapped in clickable
 *  buttons. Non-string nodes recurse into their `children` prop so
 *  refs nested inside `<code>` / `<strong>` / `<em>` / etc. get
 *  picked up too (LLMs commonly format filenames as bold inline code,
 *  which we'd otherwise miss). */
function processChildren(
  children: React.ReactNode,
  workspaceRoot: string | undefined,
  onPreviewLocalFile: (ref: LocalFileRef) => void,
  onLocalFileContextMenu?: (ref: LocalFileRef) => void,
): React.ReactNode {
  if (typeof children === 'string') {
    return renderTextWithLocalFileLinks(children, workspaceRoot, onPreviewLocalFile, onLocalFileContextMenu)
  }
  if (Array.isArray(children)) {
    return Children.map(children, (child) =>
      processChildren(child, workspaceRoot, onPreviewLocalFile, onLocalFileContextMenu))
  }
  if (isValidElement(children)) {
    const props = (children.props ?? {}) as {
      children?: React.ReactNode
      node?: { tagName?: string }
    }
    if (
      NON_RECURSIVE_INLINE_TYPES.has(children.type) ||
      props.node?.tagName === 'code' ||
      children.type === LocalFileLink
    ) {
      return children
    }
    if (props.children === undefined) {
      return children
    }
    return cloneElement(
      children,
      undefined,
      processChildren(props.children, workspaceRoot, onPreviewLocalFile, onLocalFileContextMenu),
    )
  }
  // null / boolean / number — leave alone.
  return children
}

/** Regex that captures a chunk of "looks like a path" text ending in
 *  a short extension. The deterministic extension map decides whether
 *  the result is actually previewable. Matches:
 *    foo.docx
 *    sub/foo.docx
 *    /Users/me/project/foo.docx
 *    "my report.pdf"
 *  Doesn't try to be exhaustive — we stop at whitespace, quotes, or
 *  common markdown delimiters (backticks, parens) so we don't swallow
 *  surrounding punctuation. Case-insensitive on the extension only.
 */
const LOCAL_FILE_RE = /(["'])([^"'`\n]+?\.[A-Za-z0-9]{1,12})\1|([^\s"'`(){}\[\]<>]+\.[A-Za-z0-9]{1,12})/g

/** Returns true if `path` looks absolute (POSIX or Windows). Used to
 *  decide whether we need to prepend `workspaceRoot`. */
function isAbsolutePath(path: string): boolean {
  return path.startsWith('/') || /^[A-Za-z]:[\\/]/.test(path)
}

/** Join two path segments with the system's preferred separator. We
 *  default to "/" since the runtime runs on the user's machine and
 *  Windows tolerates forward slashes everywhere. */
function joinPath(root: string, rel: string): string {
  const cleanedRoot = root.replace(/[/\\]+$/, '')
  const cleanedRel = rel.replace(/^[/\\]+/, '')
  return `${cleanedRoot}/${cleanedRel}`
}

function resolveLocalPath(path: string, workspaceRoot: string | undefined): string | null {
  // The renderer cannot deterministically expand HOME. Keeping a tilde path
  // clickable only guarantees a Runtime 404, so leave it as honest prose.
  if (path.startsWith('~')) return null
  if (isAbsolutePath(path)) return path
  return workspaceRoot ? joinPath(workspaceRoot, path) : null
}

/** Scan a text node for office file references, returning a React
 *  fragment with the recognized refs replaced by clickable buttons.
 *  Non-match characters pass through verbatim, so this is safe to use
 *  on arbitrary agent prose.
 */
function renderTextWithLocalFileLinks(
  text: string,
  workspaceRoot: string | undefined,
  onPreviewLocalFile: (ref: LocalFileRef) => void,
  onLocalFileContextMenu?: (ref: LocalFileRef) => void,
): React.ReactNode {
  const parts: React.ReactNode[] = []
  let lastIndex = 0
  // The regex has `g` flag; we reset `lastIndex` to 0 on entry by using
  // `matchAll` (which constructs a fresh internal cursor each call).
  const matches = Array.from(text.matchAll(LOCAL_FILE_RE))
  if (matches.length === 0) {
    // Return the bare string — fragment-wrapping is invisible to React
    // but causes Testing Library's `findByText` to occasionally fail
    // when comparing normalized text content.
    return text
  }
  for (const match of matches) {
    const matchedText = match[2] ?? match[3]
    const fullMatch = match[0]
    const quoted = Boolean(match[2])
    const fullStart = match.index ?? 0
    const start = fullStart + (quoted ? 1 : 0)
    if (start > lastIndex) {
      parts.push(text.slice(lastIndex, start))
    }
    const kind = filePreviewKind(matchedText)
    const absolutePath = resolveLocalPath(matchedText, workspaceRoot)
    const prefix = text.slice(0, start)
    const truncatedAbsolutePath = !quoted && /(?:^|\s)[^\s]*[/\\][^\s]*\s$/.test(prefix)
    const ambiguousRelativePath = !quoted
      && !isAbsolutePath(matchedText)
      && /[/\\]/.test(matchedText)
      // One leading prose token ("Open docs/report.pdf") is unambiguous;
      // two or more may be a truncated space-containing path, so require
      // quotes/inline code rather than inventing a boundary.
      && prefix.trim().split(/\s+/).filter(Boolean).length >= 2
    if (!kind || !absolutePath || truncatedAbsolutePath || ambiguousRelativePath) {
      // Either the extension isn't one we preview, or we have no
      // workspace root to resolve against — render as plain text.
      parts.push(matchedText)
    } else {
      parts.push(
        <LocalFileLink
          key={`${start}-${matchedText}`}
          path={absolutePath}
          kind={kind}
          name={pathBasename(matchedText) || matchedText}
          display={matchedText}
          onClick={onPreviewLocalFile}
          onContextMenu={onLocalFileContextMenu}
        />,
      )
    }
    lastIndex = fullStart + fullMatch.length - (quoted ? 1 : 0)
  }
  if (lastIndex < text.length) {
    parts.push(text.slice(lastIndex))
  }
  return <>{parts}</>
}

function LocalFileLink({
  path,
  kind,
  name,
  display,
  onClick,
  onContextMenu,
}: {
  path: string
  kind: NonNullable<LocalFileRef['kind']>
  name: string
  display: string
  onClick: (ref: LocalFileRef) => void
  onContextMenu?: (ref: LocalFileRef) => void
}) {
  const ref = { path, kind, name }
  return (
    <button
      type="button"
      className="message-office-link"
      title={path}
      onClick={(event) => {
        event.preventDefault()
        onClick(ref)
      }}
      onContextMenu={(event) => {
        if (!onContextMenu) return
        event.preventDefault()
        onContextMenu(ref)
      }}
    >
      {display}
    </button>
  )
}

/**
 * Pulls all base64-encoded image/png payloads out of an assistant
 * message's `tool.completed` events for `code.execute` and renders
 * them as inline `<img>` elements. Used to surface matplotlib/PIL
 * figures so the user actually sees what the agent generated, rather
 * than relying on the LLM's text description (which often hallucinates
 * markdown image links pointing at fake URLs).
 *
 * Returns `null` when there are no images — the surrounding bubble
 * keeps its layout clean for text-only replies.
 */
export function CodeExecutionImages({ events }: { events?: AgentTimelineItem[] }) {
  if (!events || events.length === 0) return null
  const images: string[] = []
  const seen = new Set<string>()
  for (const item of events) {
    if (item.type !== 'tool.completed' || item.tool !== 'code.execute') continue
    for (const img of item.codeExecImages ?? []) {
      if (!img || seen.has(img)) continue
      seen.add(img)
      images.push(img)
    }
  }
  if (images.length === 0) return null
  return (
    <div className="message-code-images">
      {images.map((b64) => (
        <img
          key={b64}
          src={`data:image/png;base64,${b64}`}
          alt=""
          className="message-code-image"
          loading="lazy"
        />
      ))}
    </div>
  )
}

export function GeneratedArtifactImages({
  events,
  onLoadArtifactContent,
}: {
  events?: AgentTimelineItem[]
  onLoadArtifactContent?: (artifactID: string) => Promise<Blob>
}) {
  const [images, setImages] = useState<Array<{ id: string; title: string; url: string }>>([])
  const loaderRef = useRef(onLoadArtifactContent)
  loaderRef.current = onLoadArtifactContent

  useEffect(() => {
    const loader = loaderRef.current
    const artifacts = (events ?? []).filter((item) => (
      item.type === 'artifact.created'
      && (item.artifactTool === 'image.generate' || item.artifactTool === 'image.edit')
      && item.artifactMediaType?.startsWith('image/')
      && item.artifactId
    ))
    if (!loader || artifacts.length === 0) {
      setImages([])
      return
    }
    let disposed = false
    const createdURLs: string[] = []
    void Promise.all(artifacts.map(async (artifact) => {
      try {
        const blob = await loader(artifact.artifactId!)
        const url = URL.createObjectURL(blob)
        createdURLs.push(url)
        return {
          id: artifact.artifactId!,
          title: artifact.artifactTitle || artifact.artifactId!,
          url,
        }
      } catch {
        return undefined
      }
    })).then((loaded) => {
      if (disposed) {
        createdURLs.forEach((url) => URL.revokeObjectURL(url))
        return
      }
      setImages(loaded.filter((item): item is NonNullable<typeof item> => Boolean(item)))
    })
    return () => {
      disposed = true
      createdURLs.forEach((url) => URL.revokeObjectURL(url))
    }
  }, [events, Boolean(onLoadArtifactContent)])

  if (images.length === 0) return null
  return (
    <div className="message-code-images">
      {images.map((image) => (
        <ChatImage key={image.id} src={image.url} alt={image.title} />
      ))}
    </div>
  )
}
