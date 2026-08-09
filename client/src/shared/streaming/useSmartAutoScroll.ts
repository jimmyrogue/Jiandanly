import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'

export interface SmartAutoScrollOptions {
  bottomThreshold?: number
  behavior?: ScrollBehavior
  resetKey?: unknown
  /** True while a run is in flight. When it transitions true → false the
   *  hook force-scrolls the view to the bottom (the run just finished) and
   *  re-enables stickiness so any final presentation/process data that
   *  renders afterwards keeps the view pinned to the latest content. */
  runActive?: boolean
}

export function useSmartAutoScroll<T extends HTMLElement>(
  deps: unknown[],
  options: SmartAutoScrollOptions = {},
) {
  const bottomThreshold = options.bottomThreshold ?? 96
  const behavior = options.behavior ?? 'smooth'
  const runActive = options.runActive ?? false
  const resetKey = options.resetKey
  // A callback ref (not a plain ref): the chat surface only mounts its
  // scroll container once the first message renders. The element state makes
  // the scroll-listener effect re-run when that container appears — with a
  // plain ref the listener would silently never attach when the component
  // starts on the empty state, breaking scroll-away detection.
  const containerElementRef = useRef<T | null>(null)
  const [containerElement, setContainerElement] = useState<T | null>(null)
  const containerRef = useCallback((node: T | null) => {
    containerElementRef.current = node
    setContainerElement(node)
  }, [])
  const shouldStickToBottomRef = useRef(true)
  const isPinnedToBottomRef = useRef(true)
  const [isPinnedToBottom, setIsPinnedToBottom] = useState(true)
  const prevRunActiveRef = useRef(runActive)
  const previousResetKeyRef = useRef(resetKey)

  useEffect(() => {
    if (!containerElement) {
      return
    }
    const handleScroll = () => {
      const distanceToBottom = containerElement.scrollHeight - containerElement.scrollTop - containerElement.clientHeight
      const pinned = distanceToBottom <= bottomThreshold
      shouldStickToBottomRef.current = pinned
      // Only re-render when the pinned state flips, not on every scroll
      // pixel — otherwise the FAB toggling would churn the whole chat.
      if (pinned !== isPinnedToBottomRef.current) {
        isPinnedToBottomRef.current = pinned
        setIsPinnedToBottom(pinned)
      }
    }
    containerElement.addEventListener('scroll', handleScroll, { passive: true })
    return () => containerElement.removeEventListener('scroll', handleScroll)
  }, [bottomThreshold, containerElement])

  useEffect(() => {
    if (!containerElement || typeof MutationObserver === 'undefined') {
      return
    }
    // MessageBubble can keep flushing its internal smooth-text buffer after
    // the Runtime status and ChatThread deps have settled. Observe the real
    // DOM growth so a pinned view follows that final expansion; a user who
    // scrolled away remains untouched because the scroll handler cleared
    // shouldStickToBottomRef.
    let frame: number | undefined
    const followGrowth = () => {
      if (!shouldStickToBottomRef.current || frame !== undefined) {
        return
      }
      frame = window.requestAnimationFrame(() => {
        frame = undefined
        if (shouldStickToBottomRef.current) {
          scrollElementToBottom(containerElement, 'auto')
        }
      })
    }
    const observer = new MutationObserver(followGrowth)
    observer.observe(containerElement, {
      childList: true,
      characterData: true,
      subtree: true,
    })
    // Image load changes layout without mutating the DOM after the <img>
    // was inserted. Capture the non-bubbling load event for the same pinned
    // correction path.
    containerElement.addEventListener('load', followGrowth, true)
    return () => {
      observer.disconnect()
      containerElement.removeEventListener('load', followGrowth, true)
      if (frame !== undefined) {
        window.cancelAnimationFrame(frame)
      }
    }
  }, [containerElement])

  useLayoutEffect(() => {
    if (Object.is(previousResetKeyRef.current, resetKey)) {
      return
    }
    previousResetKeyRef.current = resetKey
    shouldStickToBottomRef.current = true
    isPinnedToBottomRef.current = true
    setIsPinnedToBottom(true)
    const element = containerElementRef.current
    if (element) {
      scrollElementToBottom(element, 'auto')
    }
  }, [resetKey])

  // When a run completes, bring the user back to the latest content. The
  // reset re-enables follow-on auto-scroll if any final presentation/process
  // data renders afterwards. The immediate scroll is intentionally INSTANT
  // (`auto`, not `smooth`): a smooth scroll toward a stale `scrollHeight`
  // would land short of the final content (which commits right after the
  // status flip) and its trailing scroll event would flip stickiness off,
  // suppressing the corrective dep-driven scroll. Landing exactly at the
  // current bottom keeps stickiness on, so the next content commit
  // smooth-scrolls to the true final bottom. A layout effect guarantees this
  // runs before the passive scroll effect when both fire in the same commit.
  useLayoutEffect(() => {
    if (prevRunActiveRef.current && !runActive) {
      shouldStickToBottomRef.current = true
      isPinnedToBottomRef.current = true
      setIsPinnedToBottom(true)
      const element = containerElementRef.current
      if (element) {
        scrollElementToBottom(element, 'auto')
      }
    }
    prevRunActiveRef.current = runActive
  }, [runActive])

  useEffect(() => {
    const element = containerElementRef.current
    if (!element || !shouldStickToBottomRef.current) {
      return
    }
    scrollElementToBottom(element, behavior)
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)

  /** Explicit user action (scroll-to-bottom button): force the view to the
   *  bottom and re-pin it regardless of the user's current scroll offset. */
  const scrollToBottom = useCallback(() => {
    const element = containerElementRef.current
    if (!element) {
      return
    }
    shouldStickToBottomRef.current = true
    isPinnedToBottomRef.current = true
    setIsPinnedToBottom(true)
    scrollElementToBottom(element, 'auto')
  }, [])

  return { containerRef, isPinnedToBottom, scrollToBottom }
}

function scrollElementToBottom(element: HTMLElement, behavior: ScrollBehavior): void {
  const resolvedBehavior = behavior === 'smooth'
    && typeof window.matchMedia === 'function'
    && window.matchMedia('(prefers-reduced-motion: reduce)').matches
    ? 'auto'
    : behavior
  if (typeof element.scrollTo === 'function') {
    element.scrollTo({ top: element.scrollHeight, behavior: resolvedBehavior })
  } else {
    element.scrollTop = element.scrollHeight
  }
}
