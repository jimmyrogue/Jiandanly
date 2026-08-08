import { afterEach, describe, expect, it } from 'vitest'
import { workspaceStore, workspaceStoreActions } from './workspaceStore'

describe('workspaceStore', () => {
  afterEach(() => {
    workspaceStoreActions.resetForTests()
  })

  it('starts with no pending selection and an empty authorization list', () => {
    const state = workspaceStore.getState()
    expect(state.pendingWorkspace).toBeUndefined()
    expect(state.pendingProject).toBeUndefined()
    expect(state.pendingAttachments).toEqual([])
    expect(state.authorizedWorkspaces).toEqual([])
    expect(state.localRuns).toEqual([])
    expect(state.pendingCommandDeliveryVersion).toBe(0)
  })

  it('tracks the pending workspace, project, and attachments', () => {
    workspaceStoreActions.setPendingWorkspace({ path: '/work', label: 'Work' } as never)
    workspaceStoreActions.setPendingProject({ name: 'Work' })
    workspaceStoreActions.setPendingAttachments([{ path: '/work/a.md', name: 'a.md' } as never])

    const state = workspaceStore.getState()
    expect(state.pendingWorkspace?.path).toBe('/work')
    expect(state.pendingProject?.name).toBe('Work')
    expect(state.pendingAttachments).toHaveLength(1)
  })

  it('clears the whole pending selection', () => {
    workspaceStoreActions.setPendingWorkspace({ path: '/work', label: 'Work' } as never)
    workspaceStoreActions.setPendingAttachments([{ path: '/work/a.md', name: 'a.md' } as never])
    workspaceStoreActions.setPendingWorkspace(undefined)
    workspaceStoreActions.setPendingAttachments([])

    const state = workspaceStore.getState()
    expect(state.pendingWorkspace).toBeUndefined()
    expect(state.pendingAttachments).toEqual([])
  })

  it('accepts functional updaters like React setState', () => {
    workspaceStoreActions.setPendingAttachments([{ path: '/work/a.md', name: 'a.md' } as never])
    workspaceStoreActions.setPendingAttachments((current) => [
      ...current,
      { path: '/work/b.md', name: 'b.md' } as never,
    ])
    expect(workspaceStore.getState().pendingAttachments.map((item) => item.path)).toEqual([
      '/work/a.md',
      '/work/b.md',
    ])
  })

  it('replaces the authorized workspaces and local runs lists', () => {
    workspaceStoreActions.setAuthorizedWorkspaces([{ id: 'ws-1', path: '/work' } as never])
    workspaceStoreActions.setLocalRuns([{ id: 'run-1' } as never])

    const state = workspaceStore.getState()
    expect(state.authorizedWorkspaces.map((item) => item.id)).toEqual(['ws-1'])
    expect(state.localRuns.map((item) => item.id)).toEqual(['run-1'])
  })

  it('bumps the pending command delivery version monotonically', () => {
    workspaceStoreActions.setPendingCommandDeliveryVersion((version) => version + 1)
    workspaceStoreActions.setPendingCommandDeliveryVersion((version) => version + 1)
    expect(workspaceStore.getState().pendingCommandDeliveryVersion).toBe(2)
  })
})
