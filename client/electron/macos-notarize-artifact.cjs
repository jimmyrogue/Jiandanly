const { buildBlockMap } = require('app-builder-lib/out/targets/blockmap/blockmap')
const macosNotarize = require('./macos-notarize.cjs')

async function prepareDmgArtifact(event, options = {}) {
  if (!event.file.endsWith('.dmg')) return
  const notarize = options.notarize || macosNotarize
  const rebuildBlockMap = options.buildBlockMap || buildBlockMap

  await notarize(event.file)
  event.updateInfo = await rebuildBlockMap(
    event.file,
    'gzip',
    `${event.file}.blockmap`,
  )
}

async function artifactHook(event) {
  await prepareDmgArtifact(event)
}

artifactHook.prepareDmgArtifact = prepareDmgArtifact

module.exports = artifactHook
