import { useEffect, useRef } from 'react'
import type { Translator } from '@/shared/i18n/i18n'
import {
  getRuntimeConnection,
  hasRuntimeAuthorization,
  listAuthorizedWorkspaces,
  listLocalRuns,
  listLocalSchedules,
  markLocalScheduleNotified,
  probeRuntime,
  type RuntimeConnection,
  type RuntimeProbe,
} from '@/runtime/client'
import { notifyScheduledRun } from './runStreaming'
import { runtimeStoreActions } from '../state/runtimeStore'
import { workspaceStoreActions } from '../state/workspaceStore'

const scheduledRunNotificationPollMs = 30_000
const runtimeHealthPollMs = 2_000

export function useRuntimeObservers({
  runtime,
  runtimeConnection,
  t,
}: {
  runtime: RuntimeProbe | null
  runtimeConnection: RuntimeConnection | null | undefined
  t: Translator
}): void {
  const scheduledNotificationIDs = useRef(new Set<string>())

  useEffect(() => {
    const clientBridge = window.shejaneClient
    const config = getRuntimeConnection()
    if (!config) return

    runtimeStoreActions.setConnection(config)
    let disposed = false
    let polling = false
    let catalogLoaded = false
    if (clientBridge?.runtime?.ready === false) {
      runtimeStoreActions.setRuntime({ online: false })
    }

    const loadRuntimeCatalog = async () => {
      if (catalogLoaded || !hasRuntimeAuthorization(config)) return
      catalogLoaded = true
      try {
        const [workspaces, runs] = await Promise.all([
          listAuthorizedWorkspaces(config),
          listLocalRuns(config),
        ])
        if (!disposed) {
          workspaceStoreActions.setAuthorizedWorkspaces(workspaces)
          workspaceStoreActions.setLocalRuns(runs)
        }
      } catch {
        catalogLoaded = false
      }
    }

    const poll = async () => {
      if (polling) return
      polling = true
      try {
        const probe = await probeRuntime(config.baseURL)
        if (disposed) return
        runtimeStoreActions.setRuntime(probe)
        if (probe.online) await loadRuntimeCatalog()
        else catalogLoaded = false
      } finally {
        polling = false
      }
    }

    void poll()
    const interval = window.setInterval(() => void poll(), runtimeHealthPollMs)
    return () => {
      disposed = true
      window.clearInterval(interval)
    }
  }, [])

  useEffect(() => {
    if (!runtime?.online || !hasRuntimeAuthorization(runtimeConnection)) return

    let disposed = false
    const config = runtimeConnection
    const poll = async () => {
      try {
        const schedules = await listLocalSchedules(config, { notifyPending: true })
        if (disposed || schedules.length === 0) return

        const unnotified = schedules.filter(
          (schedule) => !scheduledNotificationIDs.current.has(schedule.id),
        )
        for (const schedule of unnotified) {
          scheduledNotificationIDs.current.add(schedule.id)
          notifyScheduledRun(schedule, t)
        }
        await Promise.all(
          unnotified.map((schedule) => markLocalScheduleNotified(schedule.id, config)),
        )
        const freshRuns = await listLocalRuns(config)
        if (!disposed) workspaceStoreActions.setLocalRuns(freshRuns)
      } catch {
        // Best-effort observer; the next poll will retry.
      }
    }

    void poll()
    const interval = window.setInterval(() => void poll(), scheduledRunNotificationPollMs)
    return () => {
      disposed = true
      window.clearInterval(interval)
    }
  }, [runtime?.online, runtimeConnection, t])
}
