import type { ChatMode } from '@/shared/local-data/types'
import type { ModelOption } from '@/features/chat/components/ModeSelector'
import type { RuntimeConnection, RuntimeProbe } from '@/runtime/client'
import { createStore } from './store'

export interface RuntimeStoreState {
  /** Health probe of the local Runtime process. */
  runtime: RuntimeProbe | null
  /** The loopback connection the Client is paired with. */
  connection: RuntimeConnection | null
  /** BYOK model catalog for the composer picker. */
  models: ModelOption[]
  /** Image-generation mode selected for the composer. */
  imageMode: ChatMode | undefined
  /** Catalog of verified image-generation models. */
  imageModels: ModelOption[]
  /** Bumped when the Runtime reports a settings change (new model service). */
  catalogVersion: number
  /** Set once Runtime advanced settings have been projected into the form. */
  settingsConfig: RuntimeConnection | null
}

const initialState: RuntimeStoreState = {
  runtime: null,
  connection: null,
  models: [],
  imageMode: undefined,
  imageModels: [],
  catalogVersion: 0,
  settingsConfig: null,
}

export const runtimeStore = createStore<RuntimeStoreState>(initialState)

export const runtimeStoreActions = {
  setRuntime: (runtime: RuntimeProbe | null): void => {
    runtimeStore.setState((state) => ({ ...state, runtime }))
  },
  setConnection: (connection: RuntimeConnection | null): void => {
    runtimeStore.setState((state) => ({ ...state, connection }))
  },
  setModels: (models: ModelOption[]): void => {
    runtimeStore.setState((state) => ({ ...state, models }))
  },
  setImageMode: (imageMode: ChatMode | undefined): void => {
    runtimeStore.setState((state) => ({ ...state, imageMode }))
  },
  setImageModels: (imageModels: ModelOption[]): void => {
    runtimeStore.setState((state) => ({ ...state, imageModels }))
  },
  bumpCatalogVersion: (): void => {
    runtimeStore.setState((state) => ({ ...state, catalogVersion: state.catalogVersion + 1 }))
  },
  setSettingsConfig: (settingsConfig: RuntimeConnection | null): void => {
    runtimeStore.setState((state) => ({ ...state, settingsConfig }))
  },
  /** Reset module state between test cases. */
  resetForTests: (): void => {
    runtimeStore.setState(initialState)
  },
}
