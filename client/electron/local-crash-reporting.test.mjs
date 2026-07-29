import { mkdtemp, readFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'

import crashReporting from './local-crash-reporting.cjs'

const { recordLocalCrash, recordRuntimeFailure } = crashReporting

describe('local crash reporting', () => {
  it('records only bounded coarse launcher and updater metadata', async () => {
    const directory = await mkdtemp(join(tmpdir(), 'shejane-crash-test-'))

    expect(recordLocalCrash({
      directory,
      component: 'runtime_launcher',
      category: 'launch_error',
      release: '0.1.19',
      timestamp: '2026-07-29T00:00:00.000Z',
    })).toBe(true)
    expect(recordLocalCrash({
      directory,
      component: 'attacker',
      category: '/Users/alice/private.txt',
      release: 'secret',
    })).toBe(false)

    const content = await readFile(join(directory, 'shejane-local-crash-events.jsonl'), 'utf8')
    expect(JSON.parse(content.trim())).toEqual({
      schema: 1,
      component: 'runtime_launcher',
      category: 'launch_error',
      release: '0.1.19',
      timestamp: '2026-07-29T00:00:00.000Z',
    })
    expect(content).not.toContain('private.txt')
    expect(content).not.toContain('secret')
  })

  it('classifies a Client-managed Runtime exit before readiness as a launch error', async () => {
    const directory = await mkdtemp(join(tmpdir(), 'shejane-crash-test-'))
    const child = {}

    expect(recordRuntimeFailure({
      child,
      directory,
      release: '0.1.19',
      wasReady: false,
      isQuitting: false,
    })).toBe(true)
    expect(recordRuntimeFailure({
      child,
      directory,
      release: '0.1.19',
      wasReady: false,
      isQuitting: false,
    })).toBe(false)

    const content = await readFile(join(directory, 'shejane-local-crash-events.jsonl'), 'utf8')
    expect(JSON.parse(content.trim()).category).toBe('launch_error')
  })
})
