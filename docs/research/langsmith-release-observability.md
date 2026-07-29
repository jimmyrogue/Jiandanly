# LangSmith release observability research

> Researched on 2026-07-28 against the current SheJane repository and official LangSmith documentation.

## Conclusion

SheJane already has most of the Agent-tracing substrate, but the installed Client release cannot currently enable LangSmith safely.

- The Runtime uses LangChain/LangGraph, passes callbacks and stable Runtime/attempt/thread identifiers into `agent.astream()`, and already builds a durable redacted local trace.
- `langsmith==0.10.2` is present transitively through `langchain-core`, and the current PyInstaller analysis includes the SDK.
- The bundled Runtime launcher deliberately forwards an environment allowlist that excludes every `LANGSMITH_*` variable. The Client has no LangSmith settings or credential path.
- Existing tests prove the local observer and deliberately disable external tracing; no positive packaged-release test proves that a failed Agent run reaches a LangSmith project.

Therefore the current state is **framework-ready but not release-ready**.

## What LangSmith gives us

For LangChain/LangGraph applications, setting `LANGSMITH_TRACING=true` and an API key enables automatic traces without adding a second callback to every model/tool call. A project, regional endpoint, and workspace can also be selected. [Trace LangChain applications](https://docs.langchain.com/langsmith/trace-with-langchain) [Create an account and API key](https://docs.langchain.com/langsmith/create-account-api-key)

The trace UI exposes nested LLM/tool activity, timing, token usage, errors, metadata, and child runs. That fits Agent execution failures in Runtime stages P7-P11. It does not instrument Electron renderer/main crashes, Runtime bootstrap failures before LangGraph starts, installer/update failures, or native process crashes; those require a separate crash-reporting path.

LangSmith sends traces in the background. A normal Runtime shutdown should call `wait_for_all_tracers()` (with a bounded timeout in SheJane) or final traces can be lost. [Ensure traces are submitted before exiting](https://docs.langchain.com/langsmith/trace-with-langchain#ensure-all-traces-are-submitted-before-exiting)

## Release blockers

### 1. Configuration does not reach the bundled Runtime

`client/electron/main.cjs` constructs a clean environment for the packaged Runtime and forwards only OS basics plus explicit SheJane values. Exporting `LANGSMITH_TRACING`, `LANGSMITH_API_KEY`, `LANGSMITH_PROJECT`, or `LANGSMITH_ENDPOINT` before opening the installed app therefore has no effect.

The development launcher also uses `env -i` and does not forward LangSmith variables. Directly launching the Runtime can work, but that is not the normal Client or release path.

### 2. There is no safe credential owner

LangSmith recommends PATs for personal scripts/tools and service keys for applications and production services. Service keys may be workspace-scoped and expiring. [Create an account and API key](https://docs.langchain.com/langsmith/create-account-api-key)

A shared SheJane service key must not be embedded in an Electron installer: an end user controls the machine and can extract it. Two safe product shapes are:

1. **User BYOK:** the user enters their own LangSmith key, Runtime stores it in the operating-system credential store, and traces go to the user's workspace.
2. **Central SheJane telemetry:** Runtime sends a consented, redacted diagnostic payload to a SheJane relay; only the relay holds the LangSmith service key.

BYOK is the smallest implementation for advanced-user diagnosis. A relay is required if SheJane wants one central project containing failures from public installations.

### 3. Default LangSmith payloads are too sensitive for public release

Automatic tracing can include prompts, outputs, tool inputs/results, and metadata. LangSmith can hide or transform all three before transmission with `LANGSMITH_HIDE_INPUTS`, `LANGSMITH_HIDE_OUTPUTS`, `LANGSMITH_HIDE_METADATA`, client callbacks, anonymizers, or per-request conditional tracing. [Prevent logging sensitive data](https://docs.langchain.com/langsmith/mask-inputs-outputs) [Conditional tracing](https://docs.langchain.com/langsmith/conditional-tracing)

SheJane's existing `SHEJANE_RUNTIME_PII_REDACT` only changes the outbound provider request copy; it does not rewrite LangGraph state and therefore does not protect LangSmith traces.

The first release-safe policy should be:

- tracing off by default with explicit user consent;
- hide full inputs and outputs by default;
- metadata allowlist only: Runtime run/attempt ID, release version, platform, model/provider category, tool name, duration, token counts, and redacted failure category;
- never send provider keys, connector credentials, file contents, attachment bodies, full local paths, tool headers/tokens, or raw large results;
- allow temporary, visibly consented content tracing only for a support reproduction.

### 4. Release validation is only negative today

The current tests ensure inherited developer credentials cannot leak into pytest. Add one positive packaged smoke test against a mock ingestion endpoint that proves:

- the frozen Runtime contains the LangSmith SDK;
- a failing Agent run produces an errored trace with expected release/run metadata;
- hidden inputs, outputs, and disallowed metadata are absent;
- LangSmith network/auth failure never fails or delays the Agent run materially;
- normal shutdown performs a bounded flush.

## Smallest recommended implementation

Primary Runtime stage: **P7 (start or resume LangGraph)**. Immediate upstream: P6 Agent/resource binding. Immediate downstream: P8 model round. Runtime owns tracing configuration and credentials; Client only presents settings/consent.

1. Add an optional Runtime observability configuration: enabled, project, endpoint/region, sampling, content-sharing mode. Keep it off by default.
2. Store a BYOK LangSmith key in the existing OS credential-store pattern, never SQLite, environment, logs, or Client renderer state.
3. At the existing `agent.astream()` boundary, use a programmatic `langsmith.Client` plus `tracing_context(...)`. This avoids weakening the packaged Runtime environment allowlist. If SheJane imports `langsmith` directly, declare it as a direct Runtime dependency instead of relying on transitive installation.
4. Apply input/output hiding and a metadata allowlist before the first production trace. Tag traces with release channel/version/platform and preserve current Runtime run/attempt correlation IDs.
5. Add bounded shutdown flushing and the positive packaged smoke test above.
6. Keep the existing durable, redacted Runtime diagnostics as the source of truth; LangSmith remains an optional external diagnostic view.

Do not build an offline retry queue in the first version. Official documentation promises background/batched submission, not a durable cross-restart desktop queue. Add one only if real support cases show that lost offline traces are a material problem. [Trace with API](https://docs.langchain.com/langsmith/trace-with-api)

## Operations and cost controls

- `LANGSMITH_TRACING_SAMPLING_RATE` accepts `0` through `1`; use sampling for successful traffic volume, not as the only error-reporting guarantee. [Sample traces](https://docs.langchain.com/langsmith/sample-traces)
- Conditional tracing is preferable for sensitive runs, zero-retention users, and temporary support sessions. [Conditional tracing](https://docs.langchain.com/langsmith/conditional-tracing)
- Regional accounts require the matching API endpoint; EU, APAC, and AWS US are not selected automatically by a US endpoint. [Create an account and API key](https://docs.langchain.com/langsmith/create-account-api-key)
- Base traces have short retention, while automations, evaluators, feedback, and datasets can extend or preserve data and increase cost. Review retention before enabling production rules. [Manage billing](https://docs.langchain.com/langsmith/billing) [Data purging](https://docs.langchain.com/langsmith/data-purging-compliance)
- Self-hosted LangSmith is an Enterprise infrastructure product, not a lightweight component to bundle with the desktop app. [Self-host LangSmith](https://docs.langchain.com/langsmith/self-hosted)

## Decision

For the next release, implement **BYOK + explicit consent + hidden content by default** if the goal is developer/advanced-user Agent debugging. If the goal is automatic centralized error collection from all public users, stop at the design boundary and first add a SheJane-owned relay and privacy policy. In either case, use a separate crash reporter for Electron, startup, updater, and native crashes.

## Implementation update (2026-07-29)

The product chose the centralized-relay shape, so the earlier BYOK recommendation is not the active
implementation contract. `shejane-cloud/docs/shejane-central-diagnostics.md` now defines that contract.
Runtime keeps automatic LangSmith/LangChain tracing disabled, mints a distinct `st-` credential through
the fixed official Cloud connection, stores it in a separate OS credential-store service, and submits one
strict metadata-only terminal event after the local Run commit. Only Cloud can hold the LangSmith service
key. The Client defaults to opt-out and enables failure-only diagnostics after explicit consent; no prompt,
output, tool argument/result, local path, model ID, inference Token, or diagnostics Token reaches the relay.

Electron and Runtime native crash dumps are collected locally with uploads disabled. Launcher and updater
failures add only bounded local metadata, and packaged smoke deliberately generates a sacrificial Runtime
native dump. Remote crash reporting remains blocked on vendor, endpoint, privacy, sampling, and retention
decisions and must remain separate from the Agent diagnostics credential and LangSmith relay.
