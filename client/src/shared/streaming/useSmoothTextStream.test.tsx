import { act, renderHook } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { segmentStreamText, useSmoothTextStream } from './useSmoothTextStream'

describe('useSmoothTextStream', () => {
  afterEach(() => {
    vi.useRealTimers()
  })

  it('segments Chinese text more naturally than raw token chunks', () => {
    const segments = segmentStreamText('今天AI新闻更新。OpenAI released updates.', 'zh')

    expect(segments.join('')).toBe('今天AI新闻更新。OpenAI released updates.')
    expect(segments.length).toBeGreaterThan(4)
  })

  it('buffers dense chunks and releases them on a UI cadence', () => {
    vi.useFakeTimers()
    const commits: string[] = []
    const { result } = renderHook(() =>
      useSmoothTextStream({
        locale: 'zh',
        segmentsPerTick: 2,
        tickMs: 24,
        onCommit: (text) => commits.push(text),
      }),
    )

    act(() => {
      result.current.start()
      for (const chunk of ['今天', 'AI', '新闻', '很', '多。']) {
        result.current.pushChunk(chunk)
      }
    })

    expect(result.current.text).toBe('')
    expect(commits).toHaveLength(0)

    act(() => {
      vi.advanceTimersByTime(24)
    })

    expect(result.current.text.length).toBeGreaterThan(0)
    expect(commits).toHaveLength(1)
    expect(result.current.text).not.toBe('今天AI新闻很多。')

    act(() => {
      result.current.finish()
    })

    expect(result.current.text).toBe('今天AI新闻很多。')
    expect(result.current.isStreaming).toBe(false)
    expect(commits.length).toBeLessThan(5)
    expect(commits.at(-1)).toBe('今天AI新闻很多。')
  })

  it('flushes residual English word fragments on finish', () => {
    vi.useFakeTimers()
    const { result } = renderHook(() => useSmoothTextStream({ locale: 'en', segmentsPerTick: 1, tickMs: 16 }))

    act(() => {
      result.current.start()
      result.current.pushChunk('hel')
      vi.advanceTimersByTime(16)
    })

    expect(result.current.text).toBe('')

    act(() => {
      result.current.pushChunk('lo world')
      result.current.finish()
    })

    expect(result.current.text).toBe('hello world')
  })

  it('can start from already displayed text without replaying it', () => {
    vi.useFakeTimers()
    const { result } = renderHook(() => useSmoothTextStream({ locale: 'zh', segmentsPerTick: 3, tickMs: 24 }))

    act(() => {
      result.current.start('第一段。')
    })

    expect(result.current.text).toBe('第一段。')

    act(() => {
      result.current.pushChunk('第二段。')
      vi.advanceTimersByTime(24)
    })

    expect(result.current.text).toBe('第一段。第二段。')
  })
})
