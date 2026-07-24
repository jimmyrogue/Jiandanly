# SheJane Plugin Manifest v1

> Status: v1 contract under implementation. WASI packages install and execute; Managed Workers remain fail-closed until their platform isolation Gate passes.

The manifest describes what a plugin contributes and what the Runtime must authorize. It is data, not executable setup code. Installing a package must never evaluate its entrypoint.

The canonical machine-readable contract is [`runtime/plugins/schemas/plugin-manifest.v1.schema.json`](../../runtime/plugins/schemas/plugin-manifest.v1.schema.json).

## Package layout

Every package has exactly one manifest at `.shejane-plugin/plugin.json`. All paths in the manifest are package-relative POSIX paths. Absolute paths, backslashes, empty segments, `.` and `..` segments are invalid.

```text
example-plugin/
├── .shejane-plugin/
│   └── plugin.json
├── actions/
│   ├── archive.extract.input.json
│   └── archive.extract.output.json
├── payload/
│   └── archive.wasm
├── commands/
│   └── extract.md
└── skills/
    └── archive/SKILL.md
```

The package digest is computed by the Runtime from canonical package entries. It is not supplied by the manifest. A Run freezes `plugin id + version + digest + Action schema digest`; changing any covered package byte produces a different binding.

Canonical digest v1 is `SHA-256` over the domain `shejane-plugin-package-v1\0`, followed by every regular file sorted by NFC-normalized, case-collision-free POSIX path. Each entry contributes an unsigned 64-bit big-endian path length, UTF-8 path bytes, unsigned 64-bit big-endian content length, and content bytes. Links, special files, control characters, non-NFC paths, traversal, and case-fold collisions are invalid.

`.shejane-plugin/signature.json` is a detached envelope and is the only excluded file. Its v1 fields are `schema_version`, `algorithm=ed25519`, `key_id`, `package_digest`, and base64 `signature`. Ed25519 signs `shejane-plugin-signature-v1\0` plus the ASCII package digest. `key_id` is `ed25519:sha256:<SHA-256(raw public key)>`. The verifier receives the trusted public key from installation policy; a key bundled by the package does not establish trust.

## Identity and compatibility

- `schema_version` is exactly `1`.
- `id` and `publisher.id` use reverse-domain identifiers such as `dev.example.archive`.
- `version` and `runtime.min_version` use semantic versions.
- Publisher identity describes provenance. It does not grant capabilities and does not make code safe.
- Signature and registry metadata belong to the package installation record, not the manifest. A valid signature proves that the supplied trusted key signed the digest; it does not prove those bytes are harmless.

Unknown fields fail validation. A future contract must use a new schema version rather than silently changing v1 meaning.

## Execution types

Public plugin developers choose `wasi` or `managed_worker`. The Runtime also reserves a narrow `builtin` selector for audited first-party host adapters. Runtime code allowlists the exact package identity, version, handler, and canonical digest; any changed package is rejected.

### `wasi`

Use `wasi` for compact, deterministic transformations that fit a WebAssembly/WASI component:

```json
{
  "kind": "wasi",
  "entrypoint": "payload/archive.wasm",
  "platforms": ["any"]
}
```

The product-level name remains `wasi` rather than generic `wasm`: it promises a concrete host ABI and capability model. The Runtime implementation may evaluate Wasmtime and Extism, but an implementation library must not change this public contract.

A WASI Action starts with no ambient filesystem, network, environment, clock, random source, or credential access. The Runtime exposes only capabilities present in the effective grant. Each invocation uses a fresh instance.

### `managed_worker`

Use `managed_worker` when the plugin needs native libraries, a language runtime, or large existing ecosystems:

```json
{
  "kind": "managed_worker",
  "entrypoint": "payload/worker",
  "platforms": ["darwin/arm64"],
  "runtime_assets": [
    {
      "id": "org.libreoffice.runtime",
      "version": "25.8.7",
      "digest": "sha256:<canonical-runtime-asset-digest>"
    }
  ]
}
```

Each Managed Worker package targets exactly one OS/architecture pair. Publish another package with the same plugin identity and version for another platform. One Action invocation starts one short-lived process and exchanges bounded JSON-RPC messages over stdio.

`runtime_assets` is an optional, bounded list of exact references to shared, platform-specific engine bytes. It is not general plugin dependency resolution: an asset has no Actions, UI, lifecycle hooks, or executable entrypoint, and cannot reference another asset. The installer requires every referenced digest to be present before admitting the Worker package. See [Runtime Asset v1](runtime-assets.md).

An ordinary child process is only a crash boundary. It still has the current user's host permissions. A platform may enable untrusted Managed Workers only after its OS isolation adapter proves filesystem, network, credential, process, and resource restrictions. Otherwise installation or enablement fails closed.

### `builtin`

`builtin` is not a general third-party execution type. It selects one named adapter already shipped and audited inside the Runtime:

```json
{"kind":"builtin","handler":"computer_use","platforms":["darwin/arm64"]}
```

The package contributes schemas, user-facing commands, and pinned audited bridge assets. It cannot replace those assets without a matching Runtime release that updates the exact digest allowlist. The adapter remains bound to P10 Action authorization. The first handler is `computer_use`, which keeps one state-scoped desktop service for a Run and closes it during P11.

## Contributions

`contributions.actions` is required. `skills`, `commands`, and `mcp_servers` are optional discoverability or orchestration layers; they never bypass Action authorization.

### Actions

Each Action declares:

| Field | Meaning |
| --- | --- |
| `id` | Stable local identifier, combined with the plugin ID at Runtime |
| `input_schema` / `output_schema` | Package-relative JSON Schema documents for Action arguments and output |
| `consumes` / `produces` | MIME types used for discovery and preflight checks |
| `effects` | `read`, staged `artifact`, and/or explicit `external` state |
| `determinism` | `pure`, `input_stable`, or `nondeterministic` |
| `capabilities` | Maximum capabilities the Action may request |
| `limits` | Requested timeout, memory, and staged output ceilings |

The Runtime computes effective capabilities and limits by intersection:

```text
effective capabilities = manifest request ∩ installation policy ∩ Run grant ∩ platform support
effective limit         = min(manifest request, installation policy, Run budget, platform ceiling)
```

The Action receives only the effective values in its invocation. A manifest request is never authority by itself.

The initial capability vocabulary is deliberately small:

- `input.read`: read only the materialized `/input` references listed in the invocation.
- `artifact.write`: write only to the private `/output` staging root.
- `model.vision.invoke`: a Managed Worker may make one bounded request to the Runtime-owned Vision provider adapter using only authorized image input IDs and its frozen model binding. It does not grant network or credential access and is invalid for WASI Actions.
- `computer.observe`: inspect the local desktop through the Runtime-owned Computer Use adapter.
- `computer.control`: deliver checked input to the local desktop through that adapter.
- `computer.setup`: install/register the pinned helper and open the relevant operating-system permission panes.

New capabilities require an explicit platform vocabulary revision, an executor implementation, threat-model coverage, and conformance tests. The extensible string shape in schema v1 is not permission for a Runtime to accept an unknown capability.

### Vision model binding

Declaring `model.vision.invoke` does not select a provider. The user or deployment binds the installed plugin explicitly through the Runtime command plane:

```json
{
  "type": "plugin.model.bind",
  "command_id": "bind-vision-1",
  "plugin_id": "dev.example.vision",
  "expected_digest": "sha256:<package-digest>",
  "binding_id": "vision-default",
  "model": "local:<connection-id>:<model-id>"
}
```

Admission requires an enabled concrete provider model advertising `image_inputs`. Runtime stores the provider/model identity and provider configuration revision, not an API key, and freezes that binding into each accepted Run and fork. Rebinding an installation affects only future Runs. A missing, stale, or mismatched binding fails explicitly; it never falls back to the chat model, another provider, or a local backend.

A plugin declaring `model.vision.invoke` cannot be enabled until this binding exists. The enable command checks that condition in the same Runtime transaction that changes installation state, so the UI cannot project an unusable cloud Vision plugin as enabled.

### Commands and direct chat references

A command maps an explicit user phrase such as `/archive:extract` to instructions and a declared set of required Actions. The composer resolves it into structured metadata; it does not paste a magic string into the model prompt.

`@plugin-id` pins a plugin for the turn. `/plugin-id:command-id` selects one declared command. The Runtime still performs schema validation, capability checks, review, receipt creation, and Action execution. A reference never means “trust all code in this package.”

### Skills and MCP bindings

Skills provide guidance; MCP bindings expose external tool servers. Both remain separate concepts:

- a Skill may explain when to call an Action but cannot execute it directly;
- an MCP binding uses the existing MCP policy and lifecycle, not the plugin Action ABI;
- a plugin may bundle these resources for installation convenience, but every resource keeps its own security boundary.

## Validation order

The installer must validate without running plugin code:

1. archive structure, canonical paths, size and file-count ceilings;
2. manifest against the v1 schema;
3. every referenced file exists once and remains inside the package root;
4. Action schemas are valid Draft 2020-12 JSON Schemas;
5. local IDs and references are unique and resolvable;
6. entrypoint kind, single target platform, exact Runtime Asset availability, and platform compatibility;
7. package digest, optional signature, and installation policy;
8. capability support and Managed Worker isolation availability.

Install into a content-addressed, immutable directory. Enabling a version updates registry state atomically; it never modifies the package in place.

## Reference packages

- [`runtime/plugins/fixtures/wasi-archive`](../../runtime/plugins/fixtures/wasi-archive) exercises `wasi` and archive extraction.
- [`runtime/plugins/fixtures/worker-documents`](../../runtime/plugins/fixtures/worker-documents) exercises `managed_worker` and document rendering.

They are contract fixtures, not yet executable plugins.
