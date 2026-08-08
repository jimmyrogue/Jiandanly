import { afterEach, describe, expect, it } from 'vitest'
import { runtimeStore, runtimeStoreActions } from './runtimeStore'

describe('runtimeStore', () => {
  afterEach(() => {
    runtimeStoreActions.resetForTests()
  })

  it('starts disconnected with an empty catalog', () => {
    const state = runtimeStore.getState()
    expect(state.runtime).toBeNull()
    expect(state.connection).toBeNull()
    expect(state.models).toEqual([])
    expect(state.imageMode).toBeUndefined()
    expect(state.imageModels).toEqual([])
    expect(state.catalogVersion).toBe(0)
    expect(state.settingsConfig).toBeNull()
  })

  it('publishes connection and health probe changes to subscribers', () => {
    const seen: Array<string | null> = []
    const unsubscribe = runtimeStore.subscribe(() => {
      seen.push(runtimeStore.getState().connection?.baseURL ?? null)
    })

    runtimeStoreActions.setConnection({ baseURL: 'http://127.0.0.1:17371' } as never)
    runtimeStoreActions.setConnection(null)
    unsubscribe()

    expect(seen).toEqual(['http://127.0.0.1:17371', null])
  })

  it('replaces the model catalog and image catalog', () => {
    runtimeStoreActions.setModels([{ id: 'local:openai:gpt-4o' } as never])
    runtimeStoreActions.setImageModels([{ id: 'local:openai:dall-e-3' } as never])
    runtimeStoreActions.setImageMode('local:openai:dall-e-3')

    expect(runtimeStore.getState().models.map((model) => model.id)).toEqual(['local:openai:gpt-4o'])
    expect(runtimeStore.getState().imageModels.map((model) => model.id)).toEqual(['local:openai:dall-e-3'])
    expect(runtimeStore.getState().imageMode).toBe('local:openai:dall-e-3')
  })

  it('bumps the catalog version monotonically', () => {
    runtimeStoreActions.bumpCatalogVersion()
    runtimeStoreActions.bumpCatalogVersion()
    expect(runtimeStore.getState().catalogVersion).toBe(2)
  })

  it('tracks the projected runtime settings config', () => {
    const config = { baseURL: 'http://127.0.0.1:17371' } as never
    runtimeStoreActions.setSettingsConfig(config)
    expect(runtimeStore.getState().settingsConfig).toBe(config)
    runtimeStoreActions.setSettingsConfig(null)
    expect(runtimeStore.getState().settingsConfig).toBeNull()
  })
})
