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

  it('persists the bounded Runtime startup error that explains the launcher failure', async () => {
    const directory = await mkdtemp(join(tmpdir(), 'shejane-crash-test-'))
    const child = {
      exitCode: 3,
      signalCode: null,
      runtimeStartupErrorOutput: [
        'PluginVersionConflictError: plugin org.shejane.ocr failed',
        'Authorization: Bearer top-secret',
        'https://user:password@example.test/path?token=query-secret',
      ].join('\n'),
    }

    expect(recordRuntimeFailure({
      child,
      directory,
      release: '0.1.29',
      wasReady: false,
      isQuitting: false,
    })).toBe(true)

    const content = await readFile(join(directory, 'shejane-runtime-startup.log'), 'utf8')
    expect(content).toContain('release=0.1.29')
    expect(content).toContain('exit_code=3')
    expect(content).toContain('PluginVersionConflictError')
    expect(content).not.toContain('top-secret')
    expect(content).not.toContain('password')
    expect(content).not.toContain('query-secret')
  })
})
