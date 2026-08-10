import { useEffect, useRef, useState, type CSSProperties, type KeyboardEvent, type PointerEvent } from 'react'

const sidebarWidthStorageKey = 'shejane.sidebar.width.v2'
const sidebarCollapsedStorageKey = 'shejane.sidebar.collapsed.v1'
const defaultSidebarWidth = 252
export const minSidebarWidth = 190
export const maxSidebarWidth = 340
const sidebarKeyboardStep = 12
const sidebarMotionMs = 220

function clampSidebarWidth(width: number): number {
  return Math.min(maxSidebarWidth, Math.max(minSidebarWidth, Math.round(width)))
}

function readSidebarWidth(): number {
  if (typeof window === 'undefined') return defaultSidebarWidth
  try {
    const rawWidth = window.localStorage.getItem(sidebarWidthStorageKey)
    if (!rawWidth) return defaultSidebarWidth
    const parsedWidth = Number(rawWidth)
    return Number.isFinite(parsedWidth) ? clampSidebarWidth(parsedWidth) : defaultSidebarWidth
  } catch {
    return defaultSidebarWidth
  }
}

function readSidebarCollapsed(): boolean {
  if (typeof window === 'undefined') return false
  try {
    return window.localStorage.getItem(sidebarCollapsedStorageKey) === '1'
  } catch {
    return false
  }
}

function persistSidebarWidth(width: number) {
  try {
    window.localStorage.setItem(sidebarWidthStorageKey, String(clampSidebarWidth(width)))
  } catch {
    // Local storage can be unavailable in restricted browser contexts.
  }
}

function persistSidebarCollapsed(collapsed: boolean) {
  try {
    window.localStorage.setItem(sidebarCollapsedStorageKey, collapsed ? '1' : '0')
  } catch {
    // Local storage can be unavailable in restricted browser contexts.
  }
}

export function useSidebarLayout() {
  const resizeStateRef = useRef<{ startX: number, startWidth: number } | null>(null)
  const motionTimerRef = useRef<number>()
  const [sidebarWidth, setSidebarWidth] = useState(readSidebarWidth)
  const [sidebarCollapsed, setSidebarCollapsed] = useState(readSidebarCollapsed)
  const [sidebarMotion, setSidebarMotion] = useState<'idle' | 'closing' | 'opening'>('idle')
  const [isResizingSidebar, setIsResizingSidebar] = useState(false)

  useEffect(() => persistSidebarWidth(sidebarWidth), [sidebarWidth])
  useEffect(() => persistSidebarCollapsed(sidebarCollapsed), [sidebarCollapsed])

  useEffect(() => () => {
    if (motionTimerRef.current) window.clearTimeout(motionTimerRef.current)
  }, [])

  useEffect(() => {
    const visibleWidth = sidebarCollapsed ? 0 : clampSidebarWidth(sidebarWidth)
    document.documentElement.style.setProperty('--toast-center-offset', `${visibleWidth / 2}px`)
  }, [sidebarWidth, sidebarCollapsed])

  useEffect(() => {
    if (!isResizingSidebar) return undefined
    document.body.classList.add('sidebar-resizing')

    function handlePointerMove(event: globalThis.PointerEvent) {
      const resizeState = resizeStateRef.current
      if (!resizeState || !Number.isFinite(event.clientX)) return
      setSidebarWidth(clampSidebarWidth(resizeState.startWidth + event.clientX - resizeState.startX))
    }

    function finishResize() {
      resizeStateRef.current = null
      setIsResizingSidebar(false)
    }

    window.addEventListener('pointermove', handlePointerMove)
    window.addEventListener('pointerup', finishResize)
    window.addEventListener('pointercancel', finishResize)
    return () => {
      document.body.classList.remove('sidebar-resizing')
      window.removeEventListener('pointermove', handlePointerMove)
      window.removeEventListener('pointerup', finishResize)
      window.removeEventListener('pointercancel', finishResize)
    }
  }, [isResizingSidebar])

  function beginSidebarResize(event: PointerEvent<HTMLDivElement>) {
    if (event.pointerType === 'mouse' && event.button !== 0) return
    if (!Number.isFinite(event.clientX)) return
    event.preventDefault()
    resizeStateRef.current = { startX: event.clientX, startWidth: sidebarWidth }
    setIsResizingSidebar(true)
  }

  function handleSidebarResizeKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key === 'ArrowLeft') {
      event.preventDefault()
      setSidebarWidth((current) => clampSidebarWidth(current - sidebarKeyboardStep))
      return
    }
    if (event.key === 'ArrowRight') {
      event.preventDefault()
      setSidebarWidth((current) => clampSidebarWidth(current + sidebarKeyboardStep))
      return
    }
    if (event.key === 'Home') {
      event.preventDefault()
      setSidebarWidth(minSidebarWidth)
      return
    }
    if (event.key === 'End') {
      event.preventDefault()
      setSidebarWidth(maxSidebarWidth)
    }
  }

  function setCollapsed(collapsed: boolean, motion: 'closing' | 'opening') {
    if (motionTimerRef.current) window.clearTimeout(motionTimerRef.current)
    setSidebarMotion(motion)
    setSidebarCollapsed(collapsed)
    motionTimerRef.current = window.setTimeout(() => setSidebarMotion('idle'), sidebarMotionMs)
  }

  return {
    appShellStyle: { '--sidebar-width': `${sidebarWidth}px` } as CSSProperties,
    beginSidebarResize,
    collapseSidebar: () => setCollapsed(true, 'closing'),
    expandSidebar: () => setCollapsed(false, 'opening'),
    handleSidebarResizeKeyDown,
    isResizingSidebar,
    sidebarCollapsed,
    sidebarMotion,
    sidebarWidth,
  }
}
