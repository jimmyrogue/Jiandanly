const FIXED_PLUGINS_BY_PLATFORM = new Map([
  ['darwin/arm64', [
    { id: 'org.shejane.computer-use', version: '0.2.3' },
    { id: 'org.shejane.browser-qa', version: '0.1.3' },
    { id: 'org.shejane.ocr', version: '0.1.5' },
  ]],
  ['win32/x64', [
    { id: 'org.shejane.browser-qa', version: '0.1.3' },
    { id: 'org.shejane.ocr', version: '0.1.5' },
  ]],
])

export const UPGRADE_PERSISTENCE_PLUGIN_IDS = Object.freeze([
  'org.shejane.browser-qa',
  'org.shejane.ocr',
])

export function expectedFixedPlugins(platform, arch) {
  const key = `${platform}/${arch}`
  const expected = FIXED_PLUGINS_BY_PLATFORM.get(key)
  if (!expected) {
    throw new Error(`unsupported fixed-plugin release platform: ${key}`)
  }
  return expected
}

export function assertExpectedFixedPlugins(plugins, expected, label) {
  for (const identity of expected) {
    const plugin = plugins.find(
      (candidate) => candidate.id === identity.id && candidate.version === identity.version,
    )
    if (!plugin) {
      throw new Error(`${label} is missing fixed plugin ${identity.id}@${identity.version}`)
    }
    if (plugin.retired !== false || plugin.compatibility !== 'compatible') {
      throw new Error(`${label} fixed plugin ${identity.id}@${identity.version} is not active`)
    }
  }
}

export function assertFixedPluginsEnabled(plugins, pluginIds, label) {
  for (const pluginId of pluginIds) {
    const plugin = plugins.find((candidate) => candidate.id === pluginId)
    if (plugin?.enabled !== true) {
      throw new Error(`${label} fixed plugin ${pluginId} is not enabled`)
    }
  }
}

export async function enableFixedPluginsForUpgrade(plugins, pluginIds, label, requestJson) {
  for (const pluginId of pluginIds) {
    const plugin = plugins.find((candidate) => candidate.id === pluginId)
    if (!plugin) {
      throw new Error(`${label} is missing fixed plugin ${pluginId} before upgrade`)
    }
    await requestJson('/v1/commands', `enable ${pluginId}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        type: 'plugin.enable',
        command_id: `cmd_upgrade_smoke_enable_${pluginId}`,
        plugin_id: pluginId,
        expected_digest: plugin.digest,
      }),
    })
  }
}
