import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import { describe, expect, it, vi } from 'vitest'

import artifactHook from './macos-notarize-artifact.cjs'

const { prepareDmgArtifact } = artifactHook

describe('macOS DMG notarization', () => {
  it('notarizes and staples the DMG before replacing its blockmap metadata', async () => {
    const notarize = vi.fn(async () => {})
    const buildBlockMap = vi.fn(async () => ({
      size: 42,
      sha512: 'stapled-dmg-sha512',
    }))
    const event = {
      file: '/tmp/SheJane-0.1.36-arm64.dmg',
      updateInfo: { size: 40, sha512: 'unstapled-dmg-sha512' },
    }

    await prepareDmgArtifact(event, { notarize, buildBlockMap })

    expect(notarize).toHaveBeenCalledWith(event.file)
    expect(buildBlockMap).toHaveBeenCalledWith(
      event.file,
      'gzip',
      `${event.file}.blockmap`,
    )
    expect(event.updateInfo).toEqual({ size: 42, sha512: 'stapled-dmg-sha512' })
    expect(notarize.mock.invocationCallOrder[0]).toBeLessThan(
      buildBlockMap.mock.invocationCallOrder[0],
    )
  })

  it('enables electron-builder Developer ID signing for the DMG', () => {
    const config = readFileSync(
      resolve(process.cwd(), 'electron-builder.yml'),
      'utf8',
    )

    expect(config).toContain('\ndmg:\n  sign: true\n')
  })

  it('ignores non-DMG artifacts', async () => {
    const notarize = vi.fn(async () => {})
    const buildBlockMap = vi.fn(async () => ({}))

    await prepareDmgArtifact({ file: '/tmp/SheJane.zip' }, { notarize, buildBlockMap })

    expect(notarize).not.toHaveBeenCalled()
    expect(buildBlockMap).not.toHaveBeenCalled()
  })
})
