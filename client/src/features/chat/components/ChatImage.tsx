import { useCallback, useEffect, useState } from 'react'
import { createPortal } from 'react-dom'
import { IconCopy, IconDownload, IconExternalLink } from '@tabler/icons-react'
import { downloadFile } from '@/shared/files/downloadFile'
import { useI18n } from '@/shared/i18n/i18n'

interface MenuPos {
  x: number
  y: number
}

async function fetchBlob(src: string): Promise<Blob> {
  const response = await fetch(src)
  if (!response.ok) {
    throw new Error(`fetch ${response.status}`)
  }
  return response.blob()
}

function filenameFor(src: string): string {
  try {
    if (src.startsWith('data:')) {
      const ext = src.slice(5, src.indexOf(';')).split('/')[1] || 'png'
      return `image-${Date.now()}.${ext}`
    }
    const path = new URL(src).pathname
    const last = path.split('/').filter(Boolean).pop()
    return last && last.includes('.') ? last : `image-${Date.now()}.png`
  } catch {
    return `image-${Date.now()}.png`
  }
}

/** A chat image with a right-click menu to copy / download / open. Used as the
 *  markdown `img` renderer so generated images in the conversation are
 *  actionable; degrades gracefully when fetch/clipboard are blocked. */
export function ChatImage({ src, alt }: { src?: string; alt?: string }) {
  const { t } = useI18n()
  const [menu, setMenu] = useState<MenuPos | null>(null)
  const [note, setNote] = useState('')

  const closeMenu = useCallback(() => setMenu(null), [])

  useEffect(() => {
    if (!menu) {
      return
    }
    const onAway = () => closeMenu()
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        closeMenu()
      }
    }
    window.addEventListener('mousedown', onAway)
    window.addEventListener('resize', onAway)
    window.addEventListener('keydown', onKey)
    return () => {
      window.removeEventListener('mousedown', onAway)
      window.removeEventListener('resize', onAway)
      window.removeEventListener('keydown', onKey)
    }
  }, [menu, closeMenu])

  useEffect(() => {
    if (!note) {
      return
    }
    const timer = window.setTimeout(() => setNote(''), 2200)
    return () => window.clearTimeout(timer)
  }, [note])

  if (!src) {
    return null
  }

  async function copyImage() {
    try {
      let blob = await fetchBlob(src!)
      if (blob.type !== 'image/png' && typeof ClipboardItem !== 'undefined') {
        const bitmap = await createImageBitmap(blob)
        const canvas = document.createElement('canvas')
        canvas.width = bitmap.width
        canvas.height = bitmap.height
        const ctx = canvas.getContext('2d')
        if (ctx) {
          ctx.drawImage(bitmap, 0, 0)
          blob = await new Promise<Blob>((resolve, reject) =>
            canvas.toBlob((out) => (out ? resolve(out) : reject(new Error('toBlob failed'))), 'image/png'),
          )
        }
      }
      await navigator.clipboard.write([new ClipboardItem({ [blob.type || 'image/png']: blob })])
      setNote(t('chat.image.copied'))
    } catch {
      try {
        await navigator.clipboard.writeText(src!)
        setNote(t('chat.image.copied'))
      } catch {
        setNote(t('chat.image.copyFailed'))
      }
    }
  }

  async function downloadImage() {
    try {
      const blob = await fetchBlob(src!)
      await downloadFile(filenameFor(src!), () => blob.arrayBuffer())
    } catch {
      window.open(src!, '_blank', 'noopener,noreferrer')
    }
  }

  return (
    <span className="chat-image-wrap">
      <img
        src={src}
        alt={alt ?? t('chat.image.alt')}
        className="chat-image"
        loading="lazy"
        onContextMenu={(event) => {
          event.preventDefault()
          setMenu({ x: event.clientX, y: event.clientY })
        }}
      />
      {note ? <span className="agent-image-note">{note}</span> : null}
      {menu
        ? createPortal(
            <div
              className="agent-image-menu"
              style={{ left: menu.x, top: menu.y }}
              role="menu"
              onMouseDown={(event) => event.stopPropagation()}
            >
              <button
                type="button"
                role="menuitem"
                onClick={() => {
                  void copyImage()
                  closeMenu()
                }}
              >
                <IconCopy size={14} aria-hidden="true" />
                {t('chat.image.copy')}
              </button>
              <button
                type="button"
                role="menuitem"
                onClick={() => {
                  void downloadImage()
                  closeMenu()
                }}
              >
                <IconDownload size={14} aria-hidden="true" />
                {t('chat.image.download')}
              </button>
              <button
                type="button"
                role="menuitem"
                onClick={() => {
                  window.open(src, '_blank', 'noopener,noreferrer')
                  closeMenu()
                }}
              >
                <IconExternalLink size={14} aria-hidden="true" />
                {t('chat.image.open')}
              </button>
            </div>,
            document.body,
          )
        : null}
    </span>
  )
}
