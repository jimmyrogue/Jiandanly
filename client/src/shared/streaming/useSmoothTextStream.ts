import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

const zhWordSegmenter = typeof Intl !== 'undefined' && 'Segmenter' in Intl
  ? new Intl.Segmenter('zh', { granularity: 'word' })
  : null

export interface StreamSegment {
  id: number
  text: string
}

export interface SmoothTextStreamOptions {
  locale?: 'zh' | 'en' | 'auto'
  segmentsPerTick?: number
  tickMs?: number
  onCommit?: (text: string) => void
}

export interface SmoothTextStreamState {
  segments: StreamSegment[]
  text: string
  isStreaming: boolean
  start: (initialText?: string) => void
  pushChunk: (chunk: string) => void
  finish: () => void
  end: () => void
  cancel: () => void
}

export function segmentStreamText(text: string, locale: SmoothTextStreamOptions['locale'] = 'auto'): string[] {
  if (!text) {
    return []
  }
  const resolvedLocale = locale === 'auto' ? (containsCJK(text) ? 'zh' : 'en') : locale
  if (resolvedLocale === 'zh' && zhWordSegmenter) {
    return Array.from(zhWordSegmenter.segment(text), (part) => part.segment).filter(Boolean)
  }
  return text.match(/\S+\s*|\s+/g) ?? []
}

export function useSmoothTextStream(options: SmoothTextStreamOptions = {}): SmoothTextStreamState {
  const locale = options.locale ?? 'auto'
  const segmentsPerTick = options.segmentsPerTick ?? 2
  const tickMs = options.tickMs ?? 24
  const onCommitRef = useRef(options.onCommit)
  const [segments, setSegments] = useState<StreamSegment[]>([])
  const [isStreaming, setIsStreaming] = useState(false)
  const bufferRef = useRef('')
  const queueRef = useRef<string[]>([])
  const timerRef = useRef<number | undefined>()
  const idRef = useRef(0)
  const endedRef = useRef(false)
  const committedTextRef = useRef('')

  useEffect(() => {
    onCommitRef.current = options.onCommit
  }, [options.onCommit])

  const appendSegments = useCallback((items: string[]) => {
    if (!items.length) {
      return
    }
    const next = items.map((text) => ({ id: idRef.current++, text }))
    setSegments((previous) => [...previous, ...next])
    committedTextRef.current += items.join('')
    onCommitRef.current?.(committedTextRef.current)
  }, [])

  const drainBufferToQueue = useCallback((flushAll: boolean) => {
    const raw = bufferRef.current
    if (!raw) {
      return
    }
    const parts = segmentStreamText(raw, locale)
    if (!parts.length) {
      return
    }
    if (flushAll) {
      queueRef.current.push(...parts)
      bufferRef.current = ''
      return
    }
    const last = parts.at(-1) ?? ''
    const lastLooksComplete = /[\s。！？.!?，,；;：:、）)\]}》」』”"']$/.test(last)
    if (lastLooksComplete || parts.length > 1) {
      const ready = lastLooksComplete ? parts : parts.slice(0, -1)
      queueRef.current.push(...ready)
      bufferRef.current = lastLooksComplete ? '' : last
    }
  }, [locale])

  const stopTimer = useCallback(() => {
    if (timerRef.current !== undefined) {
      window.clearTimeout(timerRef.current)
      timerRef.current = undefined
    }
  }, [])

  const tick = useCallback(() => {
    const ended = endedRef.current
    drainBufferToQueue(ended)
    const next = queueRef.current.splice(0, segmentsPerTick)
    appendSegments(next)
    if (ended && queueRef.current.length === 0 && !bufferRef.current) {
      setIsStreaming(false)
      stopTimer()
      return
    }
    timerRef.current = window.setTimeout(tick, tickMs)
  }, [appendSegments, drainBufferToQueue, segmentsPerTick, tickMs, stopTimer])

  const start = useCallback((initialText = '') => {
    stopTimer()
    bufferRef.current = ''
    queueRef.current = []
    idRef.current = initialText ? 1 : 0
    endedRef.current = false
    committedTextRef.current = initialText
    setSegments(initialText ? [{ id: 0, text: initialText }] : [])
    if (initialText) {
      onCommitRef.current?.(initialText)
    }
    setIsStreaming(true)
    timerRef.current = window.setTimeout(tick, tickMs)
  }, [stopTimer, tick, tickMs])

  const pushChunk = useCallback((chunk: string) => {
    if (!chunk) {
      return
    }
    bufferRef.current += chunk
  }, [])

  const finish = useCallback(() => {
    drainBufferToQueue(true)
    appendSegments(queueRef.current.splice(0))
    setIsStreaming(false)
    stopTimer()
  }, [appendSegments, drainBufferToQueue, stopTimer])

  // Graceful end: no more input is coming, but keep revealing the already
  // buffered text at the normal animation cadence instead of dumping it all
  // at once (that is what `finish` does). The tick loop stops itself once the
  // buffer and queue are fully drained.
  const end = useCallback(() => {
    endedRef.current = true
    if (timerRef.current === undefined) {
      timerRef.current = window.setTimeout(tick, tickMs)
    }
  }, [tick, tickMs])

  const cancel = useCallback(() => {
    bufferRef.current = ''
    queueRef.current = []
    endedRef.current = false
    committedTextRef.current = ''
    setSegments([])
    setIsStreaming(false)
    stopTimer()
  }, [stopTimer])

  useEffect(() => stopTimer, [stopTimer])

  const text = useMemo(() => segments.map((segment) => segment.text).join(''), [segments])

  return {
    segments,
    text,
    isStreaming,
    start,
    pushChunk,
    finish,
    end,
    cancel,
  }
}

function containsCJK(text: string): boolean {
  return /[\u3400-\u9fff]/.test(text)
}
