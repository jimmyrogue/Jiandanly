import { describe, expect, it, vi } from 'vitest'

import macosNotarize from './macos-notarize.cjs'

const { notarizeSubmission } = macosNotarize

describe('macOS notarization', () => {
  it('retries a transient status-query outage without submitting again', async () => {
    const calls = []
    const runNotarytool = vi.fn(async (args) => {
      calls.push(args)
      if (args[0] === 'submit') {
        return { id: '23cbb984-4471-4fde-b32e-30f22b8d51be' }
      }
      if (runNotarytool.mock.calls.length === 2) {
        throw new Error('NSURLErrorDomain Code=-1009 The Internet connection appears to be offline')
      }
      return { id: '23cbb984-4471-4fde-b32e-30f22b8d51be', status: 'Accepted' }
    })
    const sleep = vi.fn(async () => {})

    const submissionId = await notarizeSubmission('/tmp/石间.zip', {
      authorizationArgs: ['--key', '/tmp/AuthKey.p8'],
      runNotarytool,
      sleep,
      timeoutMs: 60_000,
    })

    expect(submissionId).toBe('23cbb984-4471-4fde-b32e-30f22b8d51be')
    expect(calls.filter(([command]) => command === 'submit')).toHaveLength(1)
    expect(calls.filter(([command]) => command === 'info')).toHaveLength(2)
    expect(sleep).toHaveBeenCalledOnce()
  })

  it('does not retry a non-network status failure', async () => {
    const runNotarytool = vi.fn(async (args) => {
      if (args[0] === 'submit') return { id: 'submission-id' }
      throw new Error('HTTP status code: 401. Invalid credentials')
    })
    const sleep = vi.fn(async () => {})

    await expect(notarizeSubmission('/tmp/石间.zip', {
      authorizationArgs: ['--key', '/tmp/AuthKey.p8'],
      runNotarytool,
      sleep,
      timeoutMs: 60_000,
    })).rejects.toThrow('401')

    expect(runNotarytool).toHaveBeenCalledTimes(2)
    expect(sleep).not.toHaveBeenCalled()
  })
})
