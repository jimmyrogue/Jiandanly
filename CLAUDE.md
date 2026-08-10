# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

石间 / SheJane is one monorepo with two product modules. Client owns the Electron/React experience and Runtime projections. Runtime independently owns execution, authoritative state, HTTP/SSE, its TypeScript SDK, and plugins.

## Architecture

The diagram below describes the current implementation. Target P1-P12 stage numbering lives only in [docs/harness-runtime-stages.md](docs/harness-runtime-stages.md).

```
┌─────────────────────────────────────────────────────────────────┐
│ Client: client/                                                 │
│ React + Vite; talks only through the public Runtime SDK         │
└──────────────────────────────┬──────────────────────────────────┘
                               │ /v1/* (loopback + token)
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│ Runtime: runtime/src/shejane_runtime/                           │
│ FastAPI · LangGraph · Deep Agents · SQLite checkpoints          │
│ BYOK · credential store · tools · Skills · MCP · plugins        │
└─────────────────────────────────────────────────────────────────┘
```

The two runtime truth sources answer different questions:

- **[docs/harness-runtime-stages.md](docs/harness-runtime-stages.md)**: Canonical target P1-P12 stages, migration seams, and the mandatory pre-change comparison order.
- **[docs/run-loop.md](docs/run-loop.md)**: Current implementation from `POST /v1/runs` to terminal state, including middleware, HITL, and SSE behavior.

Never use `run-loop.md` to invent target phase numbers, and never describe unimplemented target behavior as current code.

**Sibling guides:** this file is the architecture + invariants. [AGENTS.md](AGENTS.md) is the day-to-day rulebook. [CONTRIBUTING.md](CONTRIBUTING.md) has dev setup + PR workflow. Current priorities live in [docs/roadmap.md](docs/roadmap.md).

## Critical invariants

These are not arbitrary style rules. Each one corresponds to a class of bug that has caused real failures in this repository.

1. **Model-service API keys MUST live in the Runtime credential store, never process env.** Runtime has no private platform Gateway path. Enforced by `scripts/check.sh`.

2. **The Runtime's Pydantic models in `runtime/src/shejane_runtime/api_schemas.py` are the single source of truth for the HTTP shape.** FastAPI emits `openapi.json` from them; `openapi-typescript` regenerates `runtime/sdk/src/generated.ts`; `client.ts` re-exports the generated types as aliases. Whenever you edit a model or a handler's `response_model=` annotation, run `make schemas` and commit both `openapi.json` and `generated.ts`. The CI lint job fails if they drift.

3. **The SSE wire envelope is non-negotiable.** Every event in `/v1/runs/:id/stream` ships as `data: {"event_type": ..., "payload": {...}, "id": ..., "run_id": ..., "created_at": ...}`; durable events also carry a monotonic `seq`, while temporary model output deliberately has no replay cursor. The separator is **LF** double-newline (not CRLF), and the terminator is `data: [DONE]`. Full spec in `docs/runtime-protocol.md`.

4. **`make dev` always performs a hard restart.** It sends SIGKILL to any remaining Runtime, Vite, or Electron processes and frees their ports before starting. Opt out only with `SHEJANE_DEV_REUSE=1`. Uvicorn traps SIGTERM and can outlive a graceful restart, leaving the next session attached to a Runtime process with stale code in memory. If you suspect this, run `make doctor` to see the Runtime PID and start time.

5. **Configuration has one owner.** There is no root `.env`. Client, Runtime, and Runtime SDK require zero user env by default.

6. **Runtime model selection is BYOK-owned.** Client reads models from Runtime and submits a concrete `local:<connection>:<model>` selection. Runtime does not silently choose another model or connection.

7. **Product-specific connectors do not belong in the Runtime core.** Business-platform integrations use standard tools or MCP, not private Runtime routes, tables, or client state.

## Common commands

```bash
make dev                 # Runtime + Vite + Electron
make dev-client          # Client only
make dev-runtime         # Runtime only
make restart-runtime      # hard-restart Runtime only
make doctor              # local diagnostic
make help                # list commands

make test                # Runtime + Client + Runtime SDK
make test-client
make test-runtime
make test-runtime-sdk
make test-contract       # real Runtime SDK ↔ Runtime HTTP/SSE, no Electron
make build
make lint
make ci

make schemas            # regenerate OpenAPI + TypeScript types

make release COMPONENT=client VERSION=0.1.4
make logs-runtime
make logs-client
```

## Where things live

| If you're asking about... | Read |
|---|---|
| Canonical target P1-P12 chain, stage number for a change, pre-change comparison checklist | `docs/harness-runtime-stages.md` |
| One run from POST to terminal: middleware order, HITL, `scope=run`, and SSE events | `docs/run-loop.md` |
| Runtime HTTP and SSE: event names, envelope keys, endpoints, and cursors | `docs/runtime-protocol.md` |
| Runtime packaging and local operations | `docs/operations.md` |
| Plugin architecture, package contracts, isolation, and developer workflow | `docs/adr/0001-runtime-plugin-platform.md`, `docs/plugins/`, `runtime/src/shejane_runtime/plugins/`, `runtime/plugins/` |
| Current priorities | `docs/roadmap.md` |
| Runtime model services | `runtime/src/shejane_runtime/model_service_routes.py` (connection lifecycle), `runtime/src/shejane_runtime/model_catalog_routes.py` (model catalog entries + capability bindings), `runtime/src/shejane_runtime/model_authorization_routes.py` (official authorization), `runtime/src/shejane_runtime/model_services.py`, `runtime/src/shejane_runtime/runs.py`, `runtime/src/shejane_runtime/llm/`, `client/src/features/settings/ModelServicesSettings.tsx` |
| Runtime code | `runtime/src/shejane_runtime/`: `server.py` assembles the local API, `run_routes.py` / `run_decision_routes.py` expose Run HTTP surfaces, `run_admission.py` owns P3 admission, `run_coordinator_lifecycle.py` owns coordinator startup/recovery/dispatch/shutdown, `run_model_connection_coordination.py` fences model-connection changes against P3 admission and active execution, `run_job_execution.py` owns P5 leased attempts, `run_graph_driver.py` owns P7 LangGraph execution while `run_assistant_projection.py` / `run_failure_projection.py` own its durable projections, `run_interrupts.py` owns P10 waiting-state bridging, `run_settlement.py` owns P11 deterministic settlement, `run_event_publisher.py` owns event/result publication, and `runs.py` assembles their stable coordinator interface; `agent/builder.py` assembles reusable Agent definitions and `agent/tool_bundle.py` owns each attempt's Runtime/MCP/plugin tool catalog; `agent/subagents.py` defines Deep Agents subagents; `middleware/` owns input observation, output policy, tool visibility, human approval, tool receipts, and the single completion route; `tools/` contains tool implementations; `store/sqlite.py` is the stable persistence facade and `store/` owns domain state and durable job leases (see `docs/runtime-store.md`) |
| Client code | `client/src/`: `App.tsx` is the chat shell; `features/chat/runtimeProjection.ts` rebuilds snapshots and `runtimeStreamProjection.ts` owns incremental event reduction; `features/` contains chat, MCP, Skill, plugin, and settings features; `runtime/client.ts` adapts `@shejane/runtime-sdk` to Electron |
| Client visual system | `docs/ui/shejane-design-system.md`: June 2026 SheJane design tokens, brand mark, app-shell rules, and attachment and artifact glyph language |
| Contract tests (real HTTP, not MockTransport) | `client/src/runtime/client.contract.test.ts` |

## Conventions

### Python (runtime/)

- `uv` manages dependencies. Never edit `uv.lock` by hand. Run `uv add <pkg>` or `uv remove <pkg>`.
- Ruff handles linting and formatting through `pyproject.toml`. `make lint` enforces both.
- `from __future__ import annotations` everywhere; PEP 604 syntax (`str | None`, not `Optional[str]`).
- Tests use pytest and `httpx.MockTransport` for Runtime HTTP. Use `shejane_runtime.config.reset_settings_for_tests(**overrides)` to swap settings.
- New tool: add `@tool("name.action")` in `runtime/src/shejane_runtime/tools/` and append it to `tools/registry.py`. External capabilities should use MCP instead of product-private routes.
- New endpoint: add a Pydantic model in `api_schemas.py`, declare `response_model=Model` on the handler, run `make schemas`, commit the regenerated files.

### TypeScript (client/, runtime/sdk/)

- Vite + React 18 + TypeScript strict mode. Tailwind 4 + shadcn/ui.
- Runtime types come from `runtime/sdk/src/generated.ts` and are re-exported as aliases in `client.ts`. Don't hand-write a new interface for Runtime data. Add it to `api_schemas.py` and regenerate.
- Hand-written bridge types in `client/src/runtime/client.ts` have no Runtime equivalent; protocol types belong in Runtime schemas and SDK generation.
- SSE parsing and the `AgentRunEvent` union live in `runtime/sdk`. New Runtime events also need a Client projection in `features/chat/chatStore.ts` and/or `App.tsx`.

### Commits

- Use conventional-style messages such as `feat:`, `fix:`, `ci:`, and `docs:`. Lefthook only requires a nonempty message of at least five characters, but the existing history follows this convention.
- Pre-commit runs ruff/no-platform-keys/no-env-files in parallel. To bypass for a WIP commit: `LEFTHOOK=0 git commit`.

## Automations already wired

| Type | Where | What |
|---|---|---|
| Pre-commit | `lefthook.yml` | Ruff/no-platform-keys/no-env-files |
| CI | `.github/workflows/ci.yml` | lint / unit + build / E2E / contract round-trip |
| Release | `.github/workflows/release-*.yml` | `client-v*` installers and `runtime-sdk-v*` npm packages; Client CI builds Runtime from the same commit |
| Deps | `.github/dependabot.yml` | weekly grouped PRs for npm, pip, and GitHub Actions |
| Skills (Claude Code) | `.claude/skills/` | `sync-schemas`, `runtime-restart` |
| Subagents (Claude Code) | `.claude/agents/` | `contract-shape-reviewer` |
| Hooks (Claude Code) | `.claude/settings.json` + `.claude/hooks/` | auto-ruff after Python edits; auto `make schemas` after api_schemas.py edits |
| MCP servers (project scope) | `.mcp.json` | `context7` (live library docs) |

## Newcomer setup

```bash
# Once
make setup-hooks          # installs lefthook (brew) and wires git hooks
# Start everything
make dev         # opens Electron + Vite + Runtime + auto-tail log
```

If anything is wrong, `make doctor` checks Runtime, Client ports, workspace dependencies, and secret leakage.

## Things to never do

- Don't read model-service keys from Runtime env or restore a private platform Gateway adapter. Use the Runtime credential store and standard model-service/MCP interfaces.
- Don't hand-edit `runtime/sdk/src/generated.ts` or `runtime/sdk/openapi.json`. They're build artifacts of `make schemas`.
- Don't `pkill -f 'shejane-runtime'` and assume the process died. Uvicorn traps SIGTERM. Use `make restart-runtime` (or `lsof -ti :17371 | xargs kill -9`). The `runtime-restart` Skill encapsulates this.
- Don't change SSE event names without checking `chatStore.ts` and `App.tsx` for switch cases that match. The whole pipeline silently no-ops on a typo'd event name.
- Don't return raw `dict[str, Any]` from a new endpoint. Declare a Pydantic response model. Otherwise, `openapi.json` says `additionalProperties: true`, and the schema pipeline has nothing from which to generate types.
- Don't add automatic model selection or silent model-service fallback. Use concrete Runtime BYOK model selections.
