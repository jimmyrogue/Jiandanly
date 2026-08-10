import { useCallback, useState } from 'react'
import {
  getCentralDiagnostics,
  listModelCapabilityBindings,
  listModelServicePresets,
  listModelServices,
  type CentralDiagnosticsStatus,
  type ModelCapabilityBinding,
  type ModelServiceConnection,
  type ModelServicePreset,
  type RuntimeConnection,
} from '@/runtime/client'

export function useModelServiceCatalog(config: RuntimeConnection | null | undefined) {
  const [presets, setPresets] = useState<ModelServicePreset[]>([])
  const [services, setServices] = useState<ModelServiceConnection[]>([])
  const [bindings, setBindings] = useState<ModelCapabilityBinding[]>([])
  const [diagnostics, setDiagnostics] = useState<CentralDiagnosticsStatus>()

  const load = useCallback(async () => {
    if (!config) {
      setPresets([])
      setServices([])
      setBindings([])
      setDiagnostics(undefined)
      return []
    }
    const [nextPresets, nextServices, nextBindings] = await Promise.all([
      listModelServicePresets(config),
      listModelServices(config),
      listModelCapabilityBindings(config),
    ])
    setPresets(nextPresets)
    setServices(nextServices)
    setBindings(nextBindings)
    try {
      setDiagnostics(await getCentralDiagnostics(config))
    } catch {
      setDiagnostics(undefined)
    }
    return nextServices
  }, [config])

  return {
    bindings,
    diagnostics,
    load,
    presets,
    services,
    setDiagnostics,
  }
}
