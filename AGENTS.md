# AGENTS.md — SheJane Contributor Guide

The first stop for coding agents (and humans) working in this repository. Keep it practical: follow the existing project shape, protect secrets, and verify changes before calling them done.

For the full architecture, the critical invariants, and "where things live", read **[CLAUDE.md](./CLAUDE.md)** first. Dev setup + workflow live in **[CONTRIBUTING.md](./CONTRIBUTING.md)**.

## Runtime Stage Discipline

For any work touching Client ↔ Runtime startup, Commands, Runs, Events, Workers, Agent execution, Tools, Checkpoints, or terminal state:

1. Read **[docs/harness-runtime-stages.md](./docs/harness-runtime-stages.md)** and identify one canonical `primary_stage` before changing code.
2. Read the stage's immediate upstream and downstream contracts in order.
3. Compare the target stage with the current implementation in **[docs/run-loop.md](./docs/run-loop.md)**.
4. Record the primary stage, affected adjacent stages, canonical state owner, and old path being replaced in the implementation plan or handoff.

Do not invent a second P1-P12 numbering scheme. `run-loop.md` describes current code; `harness-runtime-stages.md` alone owns target stage numbers.

## Product shape

SheJane (石间) is an agentic chat product. Code-level identifiers (package names, the `SHEJANE_*` env prefix, on-disk paths) use the lowercase form `shejane`.

- `client/` — the Client product module: Electron/React UI and a local projection of Runtime-owned conversations.
- `runtime/` — the Runtime product module: Python/LangGraph execution core over loopback HTTP. It also owns:
  - `runtime/sdk/` — the public TypeScript SDK for commands, SSE, snapshots, and generated protocol types.
  - `runtime/plugins/` — public WASI and Managed Worker packages, fixtures, workers, schemas, and locked Runtime Asset recipes.
- `docs/plugins/` — public plugin contracts, security model, isolation decisions, and developer guide.
- `docs/operations.md` — operator runbook.
- `docs/roadmap.md` — current priorities and intentionally deferred work.

See CLAUDE.md for the architecture map and critical invariants. Use the canonical stage document above for target request flow and `run-loop.md` for current request flow.

## Commands

Do not run automated tests, lint, builds, or `make ci` after routine edits, bug fixes, refactors, or commits. Run the full local gate only when preparing a release or when the user explicitly requests verification:

```bash
make ci          # everything CI runs locally: lint + test + build + test-e2e
make build
git diff --check
```

Outside release preparation, `git diff --check` is the only default handoff check because it does not execute the product or a test suite.

Focused checks by fault domain are reference commands for release investigation or explicit user requests; do not run them automatically during routine work:

```bash
make test-client         # Vitest (pnpm --filter @shejane/client test --run)
make test-runtime        # pytest (cd runtime && uv run python -m pytest)
make test-runtime-sdk    # SDK Vitest (pnpm --filter @shejane/runtime-sdk test)
make test-contract       # real Runtime HTTP/SSE ↔ SDK, no Electron
make eval-gate           # deterministic Agent outcome gate (CI + releases)
make test-e2e-real MODEL=local:<connection>:<model>   # real BYOK LLM; manual/release gate only
```

Gotchas an agent will otherwise get wrong:

- **Schema drift**: editing `runtime/src/shejane_runtime/api_schemas.py` or a handler's `response_model=` requires `make schemas` and committing `runtime/sdk/openapi.json` + `runtime/sdk/src/generated.ts`. The CI lint job fails on drift. Never hand-edit those two files.
- **Stale Runtime**: after Python edits, restart with `make restart-runtime`, not `pkill` — Uvicorn traps SIGTERM and can leave stale code running. `make dev` always hard-restarts too (opt out: `SHEJANE_DEV_REUSE=1`).
- **`make dev-client` alone** needs `SHEJANE_RUNTIME_URL` and `SHEJANE_RUNTIME_TOKEN` set; `make dev` wires everything itself.
- **`make package-runtime`** must run on the OS/arch where the frozen Runtime will run — PyInstaller cannot cross-compile (output in `runtime/dist/shejane-runtime/`).
- First stop when the local stack misbehaves: `make doctor`.

## Environment And Secrets

- There is no root `.env`. Never print or commit real secrets from module env files.
- Runtime BYOK keys enter through Runtime settings and live in the operating-system credential store.
- Local default ports:
  - Client Vite: `http://localhost:55173`
  - Runtime: managed dynamically by Electron; source default `http://127.0.0.1:17371`

## Runtime Model Rules

- Client reads enabled models from Runtime and submits concrete `local:<connection>:<model>` selections.
- Do not add automatic model selection or silent model-service fallback in Client or Runtime.
- Runtime model-service connections live in SQLite; API keys live in the operating-system credential store.

## Fixed Capability Release Discipline

- A fixed plugin's `(plugin_id, version)` identifies immutable package bytes. Any byte change—including native binaries rebuilt by another Xcode, compiler, OS image, or CI runner—requires a plugin version bump.
- Keep that version aligned everywhere it is named: the plugin allowlist constant, package builder, development launcher, frozen Runtime config, PyInstaller spec, platform build scripts, release workflow, and distribution tests.
- Never fix a same-version digest conflict by weakening digest validation or deleting user data. Preserve the old `plugin_versions` row and install the rebuilt package under its new version.
- Before a Client release, `scripts/test-packaged-runtime-upgrade.mjs` must start the previous published frozen Runtime and the candidate frozen Runtime against the same temporary data directory. It must retain the old fixed-plugin versions, activate the new digests, and reach `/v1/health` within the Client startup deadline; never skip or downgrade this release gate.

## macOS Release Signing Discipline

- Public Client releases must fail closed unless all Developer ID and notarization secrets are present; never publish an ad-hoc fallback.
- The nested Runtime must keep signing identifier `com.shejane.runtime`, use the Client's Apple Team ID, and have a certificate-based designated requirement rather than a version-specific `cdhash` requirement.
- Native executables needed by fixed plugins must remain outside immutable `.shejane-plugin` archives so the final app signer can discover them. Verify every nested helper has Developer ID, secure timestamp, Hardened Runtime, and the expected stable identifier before notarization.
- Keep `.p12` and `.p8` material only in GitHub Actions secrets and runner-private temporary files. Never commit, log, or pass their contents through Runtime or Client configuration.

## Frontend Rules

Client UI expectations:

- Runtime owns authoritative conversations and task state; Client stores a disposable local projection and pending commands.
- Keep import/export behavior intact.
- Local documents stay inside authorized Runtime workspaces; Client must not upload them to an external private path.
- New attachment support must use a Runtime-owned persistence and permission protocol, not S3 IDs or product-specific download URLs.
- Follow the SheJane visual system in `docs/ui/shejane-design-system.md`: warm paper + ink, seal red only for brand/running/critical states, moss only for online/success, and single-color typographic attachment glyphs instead of colorful file icons.

## Testing Expectations

Do not execute tests during routine feature work, bug fixes, refactors, documentation changes, or commits. Execute tests only as part of release preparation or when the user explicitly asks for tests or verification.

Add or update tests when touching:

- Runtime provider/model validation or model picker behavior
- local conversation projection and data import/export
- SSE parsing or chat store behavior

## Documentation Expectations

- Update `README.md` for user/developer setup changes.
- Update `docs/operations.md` for operational, Runtime settings, packaging, or release changes.
- Keep docs truthful about boundaries. Mark unimplemented future work as future work, not hidden capability.

## Git And Generated Files

- Do not revert user changes.
- Do not commit or reset unless the user asks.
- Do not check in build output from `client/dist`.
- Prefer `rg` and `rg --files` for repository searches.
