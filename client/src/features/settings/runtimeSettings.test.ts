import { describe, expect, it } from 'vitest'
import { advancedSettingsFromRuntime, advancedSettingsPatchToRuntime } from './runtimeSettings'

describe('Runtime settings projection', () => {
  it('projects persisted Runtime values into the Client form', () => {
    expect(advancedSettingsFromRuntime({
      browser_headless: false,
      input_guard: 'off',
      max_model_calls: 9,
      max_tool_retries: 1,
      model_request_timeout_seconds: 120,
      pii_redact: '',
      plan_first: 'always',
      repair_workflow_max: 3,
      research_search_limit: 5,
      subagents: false,
      unknown_model_max_input_tokens: 32768,
      unknown_model_max_output_tokens: 8192,
      verification_repair_max: 1,
      version: 4,
    })).toEqual({
      maxModelCalls: 9,
      maxToolRetries: 1,
      researchSearchLimit: 5,
      subagents: false,
      browserHeadless: false,
      inputGuard: 'off',
      planFirst: 'always',
    })
  })

  it('updates only changed fields and restores cleared fields to Runtime defaults', () => {
    expect(advancedSettingsPatchToRuntime(
      { maxModelCalls: 9, planFirst: 'always', subagents: true },
      { subagents: true },
    )).toEqual({
      max_model_calls: 100,
      plan_first: 'off',
    })
  })
})
