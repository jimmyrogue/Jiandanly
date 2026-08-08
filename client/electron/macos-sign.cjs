const { signAsync } = require('@electron/osx-sign')
const { join, resolve } = require('node:path')
const macosNotarize = require('./macos-notarize.cjs')

const RUNTIME_SIGNING_IDENTIFIER = 'com.shejane.runtime'

function withStableRuntimeIdentifier(options) {
  const runtime = resolve(join(
    options.app,
    'Contents',
    'Resources',
    'runtime',
    'shejane-runtime',
  ))
  const baseOptionsForFile = options.optionsForFile
  return {
    ...options,
    optionsForFile: (filePath) => {
      const perFile = baseOptionsForFile ? baseOptionsForFile(filePath) : {}
      if (resolve(filePath) !== runtime) return perFile
      return {
        ...perFile,
        additionalArguments: [
          ...(perFile.additionalArguments || []),
          '--identifier',
          RUNTIME_SIGNING_IDENTIFIER,
        ],
      }
    },
  }
}

async function macosSign(options) {
  const signedOptions = withStableRuntimeIdentifier(options)
  let lastError
  let result
  let signed = false
  for (let attempt = 1; attempt <= 3; attempt += 1) {
    try {
      result = await signAsync(signedOptions)
      signed = true
      break
    } catch (error) {
      lastError = error
      if (attempt < 3) {
        await new Promise((resolve) => setTimeout(resolve, attempt * 5_000))
      }
    }
  }
  if (!signed) throw lastError
  await macosNotarize(options.app)
  return result
}

macosSign.RUNTIME_SIGNING_IDENTIFIER = RUNTIME_SIGNING_IDENTIFIER
macosSign.withStableRuntimeIdentifier = withStableRuntimeIdentifier

module.exports = macosSign
