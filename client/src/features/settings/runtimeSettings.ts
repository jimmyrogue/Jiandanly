import type {
  AdvancedAgentSettings,
  RuntimeSettings,
  UpdateRuntimeSettingsRequest,
} from '@/runtime/client'

const runtimeDefaults = {
  maxModelCalls: 100,
  maxToolRetries: 2,
  researchSearchLimit: 10,
  subagents: true,
  browserHeadless: true,
  inputGuard: 'observe' as const,
  planFirst: 'off' as const,
}

/** Project Runtime-owned persisted defaults into the Client settings form. */
export function advancedSettingsFromRuntime(settings: RuntimeSettings): AdvancedAgentSettings {
  return {
    maxModelCalls: settings.max_model_calls,
    maxToolRetries: settings.max_tool_retries,
    researchSearchLimit: settings.research_search_limit,
    subagents: settings.subagents,
    browserHeadless: settings.browser_headless,
    inputGuard: settings.input_guard,
    planFirst: settings.plan_first,
  }
}

/** Build a partial Runtime update; untouched fields remain owned by Runtime. */
export function advancedSettingsPatchToRuntime(
  previous: AdvancedAgentSettings,
  next: AdvancedAgentSettings,
): UpdateRuntimeSettingsRequest {
  const patch: UpdateRuntimeSettingsRequest = {}
  if (previous.maxModelCalls !== next.maxModelCalls) {
    patch.max_model_calls = next.maxModelCalls ?? runtimeDefaults.maxModelCalls
  }
  if (previous.maxToolRetries !== next.maxToolRetries) {
    patch.max_tool_retries = next.maxToolRetries ?? runtimeDefaults.maxToolRetries
  }
  if (previous.researchSearchLimit !== next.researchSearchLimit) {
    patch.research_search_limit = next.researchSearchLimit ?? runtimeDefaults.researchSearchLimit
  }
  if (previous.subagents !== next.subagents) {
    patch.subagents = next.subagents ?? runtimeDefaults.subagents
  }
  if (previous.browserHeadless !== next.browserHeadless) {
    patch.browser_headless = next.browserHeadless ?? runtimeDefaults.browserHeadless
  }
  if (previous.inputGuard !== next.inputGuard) {
    patch.input_guard = next.inputGuard ?? runtimeDefaults.inputGuard
  }
  if (previous.planFirst !== next.planFirst) {
    patch.plan_first = next.planFirst ?? runtimeDefaults.planFirst
  }
  return patch
}
