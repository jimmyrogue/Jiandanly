# Contributing to SheJane (石间)

Thanks for your interest in contributing! This guide covers the dev
setup, the project layout, and the workflow we expect for pull requests.

SheJane uses `AGPL-3.0-only` for community releases and a separate commercial
license. Before a pull request can be accepted, each contributor must agree to
the [Contributor License Agreement](./CLA.md). The CLA lets TAO LIANG publish
the contribution under both licensing paths while the contributor keeps
ownership of their work.

## Architecture in one minute

SheJane is one monorepo with two product modules:

```
Electron/React Client  ──/v1/*──▶ Python Agent Runtime   ──▶ BYOK providers
                                             │
                                             └── Skills / MCP / local tools

```

- `client/`: Electron, React, Vite, and Tailwind Client with a local projection of Runtime-owned conversations.
- `runtime/`: independently runnable Python Runtime plus its owned SDK, plugins, schemas, and tests.
- `runtime/sdk/` is separately buildable and publishable, but it is a Runtime submodule rather than a third product module.

**Read [CLAUDE.md](./CLAUDE.md) first.** It has the full architecture, the request flow (`docs/run-loop.md`), and the four non-negotiable invariants. [AGENTS.md](./AGENTS.md) has the backend, frontend, and testing rules.

## Prerequisites

- **Node** 22+
- **pnpm** 11.7.0 through Corepack
- **Python** 3.12+ with [`uv`](https://docs.astral.sh/uv/)
- macOS or Linux (the dev launcher is macOS-tuned; Linux works with minor tweaks)

## First-time setup

```bash
make setup-hooks            # installs lefthook + wires git hooks
corepack enable && pnpm install
make dev           # Runtime + Vite + Electron, with log tail
```

Connect a model service from Client after startup. Runtime stores its API Key
in the operating-system credential store.

If anything looks wrong, `make doctor` is the first stop.

## The four invariants (don't break these)

1. **Model-service API Keys never come from process env.** BYOK keys live in the Runtime credential store. Enforced by `scripts/check.sh`.
2. **The Runtime's Pydantic models are the source of truth for the HTTP shape.** After editing `api_schemas.py` or a handler's `response_model`, run `make schemas` and commit the regenerated `openapi.json` and `runtime/sdk/src/generated.ts`.
3. **The SSE wire envelope is fixed.** See `docs/runtime-protocol.md` before touching streaming.
4. **Runtime owns accepted commands, conversations, task state, checkpoints, and tool receipts.** Client stores only pending commands and a disposable projection.

## Workflow

1. Branch off `main` (`feat/…`, `fix/…`, `chore/…`, `docs/…`).
2. Make your change with a focused test where practical (we lean TDD for Runtime state, permissions, SSE/chat-store, model services, and import/export).
3. Run the checks below until green.
4. Open a PR against `main` and include this statement in the description:

   ```text
   I have read and agree to the SheJane Contributor License Agreement (CLA.md).
   ```

5. If the contribution belongs to an employer or another legal entity, identify it and confirm that you are authorized to contribute on its behalf.

## Tests & lint

```bash
make lint                   # ruff + project guards
make test                   # Runtime + Client + Runtime SDK
make test-e2e               # Runtime 黑盒 + 崩溃恢复 + Playwright Electron E2E
make test-e2e-real MODEL=local:connection:model  # 正常 Agent/Tool/Client 流程 + 真实 BYOK LLM

# focused:
make test-runtime           # uv run python -m pytest
make test-client            # Client Vitest
make test-runtime-sdk       # public SDK Vitest
make test-contract          # real Runtime HTTP/SSE + SDK, no Electron
```

CI runs the same lint, deterministic test, build, and Runtime E2E jobs on every PR. The real-LLM suite is an explicit manual/release gate because provider output and availability are external inputs. See [Runtime E2E testing](./docs/runtime-e2e-testing.md) for the test boundary and coverage map.

## Commit messages

Conventional-ish prefixes (`feat:`, `fix:`, `chore:`, `docs:`, `ci:`,
`perf:`, `refactor:`). Pre-commit only enforces non-empty (≥5 chars);
the history follows the convention by habit.

## Reporting bugs / requesting features

Use the GitHub issue templates. For anything security-sensitive, follow
[SECURITY.md](./SECURITY.md) instead of opening a public issue.
