import { join, resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

import macosSign from './macos-sign.cjs'

const { RUNTIME_SIGNING_IDENTIFIER, withStableRuntimeIdentifier } = macosSign

describe('macOS release signing', () => {
  it('adds the stable identifier only to the bundled Runtime', async () => {
    const app = '/tmp/石间.app'
    const base = { hardenedRuntime: true, additionalArguments: ['--preserve-metadata=flags'] }
    const options = withStableRuntimeIdentifier({
      app,
      optionsForFile: () => base,
    })

    expect(options.optionsForFile(
      join(app, 'Contents', 'Resources', 'runtime', 'shejane-runtime'),
    )).toEqual({
      ...base,
      additionalArguments: [
        '--preserve-metadata=flags',
        '--identifier',
        RUNTIME_SIGNING_IDENTIFIER,
      ],
    })
    expect(options.optionsForFile(
      join(app, 'Contents', 'MacOS', '石间'),
    )).toBe(base)
  })

  it('matches an absolute Runtime path when the app path is relative', async () => {
    const app = join('release', 'mac-arm64', '石间.app')
    const options = withStableRuntimeIdentifier({
      app,
      optionsForFile: () => ({}),
    })

    expect(options.optionsForFile(resolve(
      app,
      'Contents',
      'Resources',
      'runtime',
      'shejane-runtime',
    ))).toMatchObject({
      additionalArguments: ['--identifier', RUNTIME_SIGNING_IDENTIFIER],
    })
  })
})
