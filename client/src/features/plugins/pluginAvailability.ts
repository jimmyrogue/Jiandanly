export function isAvailablePlugin(plugin: {
  enabled: boolean
  retired: boolean
  compatibility: string
}): boolean {
  return plugin.enabled && !plugin.retired && plugin.compatibility === 'compatible'
}
