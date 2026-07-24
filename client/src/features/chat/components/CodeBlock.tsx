import { useEffect, useRef, useState } from 'react'
import { IconCheck, IconCopy } from '@tabler/icons-react'
import { useI18n } from '@/shared/i18n/i18n'

function escapeHtml(value: string): string {
  return value.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
}

/** One fenced code block: a header bar (language label + per-block copy
 *  button) over a highlighted <pre>. The copy button copies the RAW code
 *  (not the whole message) and has its own accessible name so it never
 *  collides with the message-level copy button. */
export function CodeBlock({ language, code }: { language?: string; code: string }) {
  const { t } = useI18n()
  const [copied, setCopied] = useState(false)
  const [highlighted, setHighlighted] = useState<{
    code: string
    language?: string
    html: string
    resolvedLang: string
  }>()
  const resetRef = useRef<number | undefined>(undefined)
  useEffect(() => () => window.clearTimeout(resetRef.current), [])

  useEffect(() => {
    let cancelled = false
    async function loadSyntaxHighlighter() {
      try {
        const { highlightCode } = await import('./syntaxHighlighter')
        if (!cancelled) {
          setHighlighted({ code, language, ...highlightCode(code, language) })
        }
      } catch {
        // The escaped plain-text fallback remains available.
      }
    }
    void loadSyntaxHighlighter()
    return () => {
      cancelled = true
    }
  }, [code, language])

  const current = highlighted?.code === code && highlighted.language === language
    ? highlighted
    : { html: escapeHtml(code), resolvedLang: '' }

  const handleCopy = () => {
    void navigator.clipboard?.writeText(code).then(() => {
      setCopied(true)
      window.clearTimeout(resetRef.current)
      resetRef.current = window.setTimeout(() => setCopied(false), 1500)
    })
  }

  return (
    <div className="code-block">
      <div className="code-block-header">
        <span className="code-block-lang">{language || current.resolvedLang || 'text'}</span>
        <button
          type="button"
          className="code-block-copy"
          onClick={handleCopy}
          title={copied ? t('code.copied') : t('code.copy')}
          aria-label={copied ? t('code.copied') : t('code.copy')}
        >
          {copied ? <IconCheck size={13} aria-hidden="true" /> : <IconCopy size={13} aria-hidden="true" />}
        </button>
      </div>
      <pre className="code-block-pre">
        <code className="hljs" dangerouslySetInnerHTML={{ __html: current.html }} />
      </pre>
    </div>
  )
}
