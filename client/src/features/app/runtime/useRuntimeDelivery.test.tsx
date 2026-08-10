import { act, cleanup, render, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { LocalConversationStore } from '@/shared/local-data/localConversations'
import type { PendingRuntimeCommand } from '@/runtime/client'
import { runtimeStoreActions } from '../state/runtimeStore'
import { workspaceStoreActions } from '../state/workspaceStore'
import { useRuntimeDelivery } from './useRuntimeDelivery'

const deliverPendingRuntimeCommands = vi.hoisted(() => vi.fn())

vi.mock('@/runtime/client', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/runtime/client')>(),
  deliverPendingRuntimeCommands,
}))

const pendingCommand = {
  type: 'run.cancel',
  commandId: 'command-1',
  createdAt: '2026-08-09T00:00:00.000Z',
  input: { threadId: 'thread-1', runId: 'run-1' },
} satisfies PendingRuntimeCommand

function Harness({ revision = 0, retryDelayMs = 10, localData }: {
  revision?: number
  retryDelayMs?: number
  localData: LocalConversationStore
}) {
  useRuntimeDelivery({
    localData,
    isDesktop: true,
    settleDeliveredLocalRunCommand: async () => Boolean(revision),
    settleRejectedPendingRuntimeCommand: async () => undefined,
    setNotice: () => undefined,
    consumeRuntimeCommandFailureNotice: () => false,
    t: ((key: string) => key) as never,
    retryDelayMs,
  })
  return null
}

describe('useRuntimeDelivery', () => {
  beforeEach(() => {
    runtimeStoreActions.setConnection({ baseURL: 'http://127.0.0.1:17371', session: 'client' })
    runtimeStoreActions.setRuntime({ online: true })
  })

  afterEach(() => {
    cleanup()
    vi.useRealTimers()
    deliverPendingRuntimeCommands.mockReset()
    runtimeStoreActions.resetForTests()
    workspaceStoreActions.resetForTests()
  })

  it('does not submit the same pending command concurrently when its effect restarts', async () => {
    let finishDelivery: (report: { delivered: number, failures: [] }) => void = () => undefined
    deliverPendingRuntimeCommands.mockImplementation(() => new Promise((resolve) => {
      finishDelivery = resolve
    }))
    const localData = {
      listPendingRuntimeCommands: vi.fn().mockResolvedValue([pendingCommand]),
    } as unknown as LocalConversationStore

    const view = render(<Harness localData={localData} revision={1} />)
    await waitFor(() => expect(deliverPendingRuntimeCommands).toHaveBeenCalledTimes(1))

    view.rerender(<Harness localData={localData} revision={2} />)
    await act(async () => {
      await new Promise((resolve) => window.setTimeout(resolve, 0))
    })

    expect(deliverPendingRuntimeCommands).toHaveBeenCalledTimes(1)
    finishDelivery({ delivered: 1, failures: [] })
  })

  it('retries a pending command after a retryable delivery failure', async () => {
    deliverPendingRuntimeCommands
      .mockResolvedValueOnce({
        delivered: 0,
        failures: [{ command: pendingCommand, error: new Error('offline'), retryable: true }],
      })
      .mockResolvedValueOnce({ delivered: 1, failures: [] })
    const localData = {
      listPendingRuntimeCommands: vi.fn().mockResolvedValue([pendingCommand]),
    } as unknown as LocalConversationStore

    render(<Harness localData={localData} />)

    await waitFor(() => expect(deliverPendingRuntimeCommands).toHaveBeenCalledTimes(2))
  })

  it('does not submit commands when the effect is disposed while listing the outbox', async () => {
    let finishListing: (commands: PendingRuntimeCommand[]) => void = () => undefined
    const localData = {
      listPendingRuntimeCommands: vi.fn().mockImplementation(() => new Promise((resolve) => {
        finishListing = resolve
      })),
    } as unknown as LocalConversationStore

    const view = render(<Harness localData={localData} />)
    expect(localData.listPendingRuntimeCommands).toHaveBeenCalledTimes(1)
    view.unmount()

    await act(async () => {
      finishListing([pendingCommand])
      await Promise.resolve()
    })

    expect(deliverPendingRuntimeCommands).not.toHaveBeenCalled()
  })

  it('keeps the retry delay after an effect restart joins a failed flight', async () => {
    vi.useFakeTimers()
    let finishDelivery: (report: {
      delivered: number
      failures: Array<{ command: PendingRuntimeCommand, error: Error, retryable: boolean }>
    }) => void = () => undefined
    deliverPendingRuntimeCommands
      .mockImplementationOnce(() => new Promise((resolve) => {
        finishDelivery = resolve
      }))
      .mockResolvedValueOnce({ delivered: 1, failures: [] })
    const localData = {
      listPendingRuntimeCommands: vi.fn().mockResolvedValue([pendingCommand]),
    } as unknown as LocalConversationStore

    const view = render(<Harness localData={localData} revision={1} retryDelayMs={50} />)
    await act(async () => {
      await Promise.resolve()
    })
    expect(deliverPendingRuntimeCommands).toHaveBeenCalledTimes(1)

    view.rerender(<Harness localData={localData} revision={2} retryDelayMs={50} />)
    await act(async () => {
      finishDelivery({
        delivered: 0,
        failures: [{ command: pendingCommand, error: new Error('offline'), retryable: true }],
      })
      await Promise.resolve()
    })
    expect(deliverPendingRuntimeCommands).toHaveBeenCalledTimes(1)

    await vi.advanceTimersByTimeAsync(49)
    expect(deliverPendingRuntimeCommands).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(1)
    expect(deliverPendingRuntimeCommands).toHaveBeenCalledTimes(2)
  })
})
