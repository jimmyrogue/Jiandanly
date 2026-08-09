import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { useSmartAutoScroll } from './useSmartAutoScroll'

function defineScrollMetrics(element: HTMLElement, metrics: { scrollHeight: number; scrollTop: number; clientHeight: number }) {
  Object.defineProperty(element, 'scrollHeight', { value: metrics.scrollHeight, configurable: true })
  Object.defineProperty(element, 'scrollTop', { value: metrics.scrollTop, configurable: true, writable: true })
  Object.defineProperty(element, 'clientHeight', { value: metrics.clientHeight, configurable: true })
}

function Harness({
  tick,
  runActive,
  renderContainer = true,
  content = '',
  resetKey,
}: {
  tick: number
  runActive?: boolean
  renderContainer?: boolean
  content?: string
  resetKey?: string
}) {
  const { containerRef, isPinnedToBottom, scrollToBottom } = useSmartAutoScroll<HTMLDivElement>([tick], {
    bottomThreshold: 80,
    runActive,
    resetKey,
  })
  return (
    <div>
      <span data-testid="pinned">{String(isPinnedToBottom)}</span>
      <button type="button" data-testid="go-bottom" onClick={scrollToBottom}>
        bottom
      </button>
      {renderContainer ? <div data-testid="scroll" ref={containerRef}>{content}</div> : null}
    </div>
  )
}

describe('useSmartAutoScroll', () => {
  afterEach(() => {
    cleanup()
  })

  it('sticks to bottom only while the user stays near the bottom', () => {
    const { getByTestId, rerender } = render(<Harness tick={1} />)
    const element = getByTestId('scroll')
    const scrollTo = vi.fn()
    Object.defineProperty(element, 'scrollTo', { value: scrollTo, configurable: true })

    defineScrollMetrics(element, { scrollHeight: 1000, scrollTop: 930, clientHeight: 80 })
    fireEvent.scroll(element)
    rerender(<Harness tick={2} />)
    expect(scrollTo).toHaveBeenCalledWith({ top: 1000, behavior: 'smooth' })

    scrollTo.mockClear()
    defineScrollMetrics(element, { scrollHeight: 1200, scrollTop: 200, clientHeight: 80 })
    fireEvent.scroll(element)
    rerender(<Harness tick={3} />)
    expect(scrollTo).not.toHaveBeenCalled()

    defineScrollMetrics(element, { scrollHeight: 1200, scrollTop: 1130, clientHeight: 80 })
    fireEvent.scroll(element)
    act(() => {
      rerender(<Harness tick={4} />)
    })
    expect(scrollTo).toHaveBeenCalledWith({ top: 1200, behavior: 'smooth' })
  })

  it('reports pinned state and re-pins on the explicit scroll-to-bottom action', () => {
    const { getByTestId } = render(<Harness tick={1} />)
    const element = getByTestId('scroll')
    const scrollTo = vi.fn()
    Object.defineProperty(element, 'scrollTo', { value: scrollTo, configurable: true })

    expect(getByTestId('pinned').textContent).toBe('true')

    defineScrollMetrics(element, { scrollHeight: 1200, scrollTop: 200, clientHeight: 80 })
    fireEvent.scroll(element)
    expect(getByTestId('pinned').textContent).toBe('false')
    expect(scrollTo).not.toHaveBeenCalled()

    fireEvent.click(getByTestId('go-bottom'))
    expect(scrollTo).toHaveBeenCalledWith({ top: 1200, behavior: 'auto' })
    expect(getByTestId('pinned').textContent).toBe('true')
  })

  it('scrolls to bottom on the next content change after a run completes, even if the user scrolled up mid-stream', () => {
    const { getByTestId, rerender } = render(<Harness tick={1} runActive />)
    const element = getByTestId('scroll')
    const scrollTo = vi.fn()
    Object.defineProperty(element, 'scrollTo', { value: scrollTo, configurable: true })
    scrollTo.mockClear()

    defineScrollMetrics(element, { scrollHeight: 1200, scrollTop: 200, clientHeight: 80 })
    fireEvent.scroll(element)
    expect(getByTestId('pinned').textContent).toBe('false')

    rerender(<Harness tick={2} runActive={false} />)
    expect(scrollTo).toHaveBeenCalledWith({ top: 1200, behavior: 'smooth' })
  })

  it('force-scrolls to the bottom when the run completes even if no content change follows', () => {
    const { getByTestId, rerender } = render(<Harness tick={1} runActive />)
    const element = getByTestId('scroll')
    const scrollTo = vi.fn()
    Object.defineProperty(element, 'scrollTo', { value: scrollTo, configurable: true })
    scrollTo.mockClear()

    defineScrollMetrics(element, { scrollHeight: 1200, scrollTop: 200, clientHeight: 80 })
    fireEvent.scroll(element)
    expect(getByTestId('pinned').textContent).toBe('false')

    rerender(<Harness tick={1} runActive={false} />)
    expect(scrollTo).toHaveBeenCalledWith({ top: 1200, behavior: 'auto' })
    expect(getByTestId('pinned').textContent).toBe('true')
  })

  it('attaches the scroll listener when the container mounts after the initial render', () => {
    const { getByTestId, rerender } = render(<Harness tick={1} renderContainer={false} />)
    expect(screen.queryByTestId('scroll')).toBeNull()
    expect(getByTestId('pinned').textContent).toBe('true')

    rerender(<Harness tick={1} renderContainer />)
    const element = getByTestId('scroll')
    const scrollTo = vi.fn()
    Object.defineProperty(element, 'scrollTo', { value: scrollTo, configurable: true })

    defineScrollMetrics(element, { scrollHeight: 1200, scrollTop: 200, clientHeight: 80 })
    fireEvent.scroll(element)
    expect(getByTestId('pinned').textContent).toBe('false')

    fireEvent.click(getByTestId('go-bottom'))
    expect(scrollTo).toHaveBeenCalledWith({ top: 1200, behavior: 'auto' })
    expect(getByTestId('pinned').textContent).toBe('true')
  })

  it('follows DOM growth that happens after the data deps settle while pinned', async () => {
    const { getByTestId, rerender } = render(<Harness tick={1} content="draft" />)
    const element = getByTestId('scroll')
    const scrollTo = vi.fn()
    Object.defineProperty(element, 'scrollTo', { value: scrollTo, configurable: true })
    defineScrollMetrics(element, { scrollHeight: 1800, scrollTop: 1720, clientHeight: 80 })
    fireEvent.scroll(element)
    scrollTo.mockClear()

    rerender(<Harness tick={1} content="final content rendered by a nested component" />)

    await waitFor(() => {
      expect(scrollTo).toHaveBeenCalledWith({ top: 1800, behavior: 'auto' })
    })
  })

  it('re-pins immediately when the FAB is clicked', () => {
    const { getByTestId } = render(<Harness tick={1} />)
    const element = getByTestId('scroll')
    const scrollTo = vi.fn()
    Object.defineProperty(element, 'scrollTo', { value: scrollTo, configurable: true })
    defineScrollMetrics(element, { scrollHeight: 1800, scrollTop: 300, clientHeight: 80 })
    fireEvent.scroll(element)
    expect(getByTestId('pinned').textContent).toBe('false')

    fireEvent.click(getByTestId('go-bottom'))
    expect(getByTestId('pinned').textContent).toBe('true')
    expect(scrollTo).toHaveBeenCalledWith({ top: 1800, behavior: 'auto' })
  })

  it('resets pinned state when switching conversations', () => {
    const { getByTestId, rerender } = render(<Harness tick={1} resetKey="conversation-a" />)
    const element = getByTestId('scroll')
    const scrollTo = vi.fn()
    Object.defineProperty(element, 'scrollTo', { value: scrollTo, configurable: true })
    defineScrollMetrics(element, { scrollHeight: 1800, scrollTop: 300, clientHeight: 80 })
    fireEvent.scroll(element)
    expect(getByTestId('pinned').textContent).toBe('false')
    scrollTo.mockClear()

    rerender(<Harness tick={1} resetKey="conversation-b" />)

    expect(getByTestId('pinned').textContent).toBe('true')
    expect(scrollTo).toHaveBeenCalledWith({ top: 1800, behavior: 'auto' })
  })
})
