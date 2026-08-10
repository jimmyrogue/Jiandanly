# SheJane Plugin Platform Security Model

> Status: normative for the implemented plugin contracts.
>
> Review date: 2026-07-15

## Overview

SheJane is a local-first agentic chat application. The Electron Client submits commands to a loopback Python Runtime; the Runtime owns conversations, Runs, model/provider configuration, tool policy, receipts, workspaces, credentials, and Artifacts. The plugin platform adds installable code and data to that Runtime boundary.

The security goal is not “plugins are trusted after installation.” The goal is that a malicious, compromised, buggy, or prompt-influenced plugin cannot exceed the exact capability and resource lease issued for one Action invocation, and cannot forge a durable result.

The platform supports two public developer-selected execution types and one reserved host selector:

- `wasi`: capability-oriented WebAssembly/WASI execution with no ambient host access;
- `managed_worker`: native local executable supervised out of process and, for untrusted code, enclosed by a platform-specific OS isolation adapter.
- `builtin`: selects an audited Runtime-owned host adapter and exact canonical package digest. Currently limited to the fixed `computer_use`, `browser_qa`, and `ocr` identities; modified package bytes fail installation and P6 revalidation.

Both use the same manifest and Action protocol. Publisher identity, signature validity, execution type, capability grant, and platform isolation status are independent facts.

## Scope and security-relevant evidence

This model covers package ingestion, registry state, Run binding, Action dispatch, WASI execution, Managed Worker execution, staging/Artifact promotion, chat references, recovery, update, rollback, and removal.

Current repository evidence shaping the design:

- `runtime/src/shejane_runtime/agent/builder.py` uses a local Deep Agents backend; its shell execution is host-capable and must not be exposed as a plugin executor.
- `runtime/src/shejane_runtime/middleware/tool_review.py` and `tool_execution.py` provide approval, operation identity, receipts, recovery, and Artifact conversion that plugin Actions must reuse.
- `runtime/src/shejane_runtime/runs.py` and `store/sqlite.py` own Run acceptance, leases, checkpoints, and durable state.
- `runtime/src/shejane_runtime/tools/mcp/stdio.py` provides useful bounded subprocess lifecycle patterns, but MCP transport is not the plugin security boundary.
- `runtime/src/shejane_runtime/tools/office.py` is the behavior baseline for future Office plugins.
- `client/electron/main.cjs` currently sets Electron renderer sandboxing independently of Runtime plugin isolation. Renderer sandbox state does not sandbox Runtime workers.
- `SECURITY.md` treats credentials, workspaces, tool permissions, loopback pairing, and outbound requests as explicit trust boundaries.

The canonical stage is P6 for frozen resource binding, with P5/P7 adjacent. P2/P3 carry and freeze explicit plugin selections, P10 executes through tool review and receipts, P11 proves cleanup, and P12 commits authoritative results.

## Assets

Critical assets include:

- provider API keys and operating-system credential-store entries;
- Runtime pairing/session tokens and local control APIs;
- authorized workspace files, attachments, imported conversations, and user home data;
- Runtime SQLite databases, checkpoints, receipts, plugin registry, and Run bindings;
- Artifact bytes and metadata;
- the integrity and availability of the Runtime and Client;
- package identity, version, digest, signature record, and Action schemas;
- user intent represented by explicit grants, approvals, `@plugin`, and `/plugin:command` selections.

## Adversaries

The design assumes any of the following can be hostile:

- an unsigned package or a package signed by an unknown publisher;
- a legitimate publisher account or distribution source that has been compromised;
- a package with safe metadata but malicious payload bytes;
- a buggy plugin that accidentally loops, emits malformed data, follows links, or writes outside staging;
- document/media input crafted to exploit a parser inside a plugin;
- a model or prompt injection that asks an enabled Action to misuse valid parameters;
- a local unprivileged process racing plugin files, staging paths, or loopback requests;
- a malicious worker child process attempting to outlive its parent or escape resource supervision.

Root/administrator compromise, a compromised SheJane release signing key, malicious kernel/hypervisor, physical attacks, and vulnerabilities below the OS isolation primitive are outside the containment guarantee. They remain supply-chain and platform risks.

## Trust boundaries

### TB1: Client to Runtime control plane

Client is a client projection. Runtime authenticates the loopback peer, validates structured Commands, resolves chat references, and owns installation truth. Client must not scan packages, launch workers, or infer enabled versions.

### TB2: Package source to immutable package store

Archives, manifests, schemas, signatures, and publisher claims are untrusted input. Validation and digesting occur before activation and without executing package code. The immutable content-addressed store is more trusted than the source archive, but its contents remain untrusted executable payloads.

### TB3: PluginRegistry to frozen Run binding

Mutable enabled/update state cannot affect an accepted Run. P3 freezes exact package and schema digests. P6 acquires a lease for those bytes and produces a fixed Action catalog. Missing bytes fail explicitly; no compatible-looking version substitution is allowed.

### TB4: Runtime to WASI guest

The guest receives bounded authorized bytes through the Component call. Guest memory and output are hostile. v1 provides no filesystem preopen and no ambient host filesystem, network, process, environment, clock, real randomness, credential, or Runtime object.

### TB5: Runtime to Managed Worker

JSON-RPC frames, stderr, exit status, staged files, and child processes are hostile. Process separation alone protects the Runtime heap from a crash but does not reduce host-user permissions. The OS isolation adapter is a distinct mandatory boundary for untrusted workers.

### TB6: Staging to authoritative Artifact store

`/output` is untrusted temporary state. Runtime reopens candidates safely, recomputes digest/type/size, applies quotas, and atomically promotes only validated files. Paths returned by a plugin are never Artifact authority.

### TB6a: Runtime to Computer Use helper

The first-party adapter and its pinned native helper have the current user's explicit macOS Accessibility and Screen Recording grants. This is intentionally stronger authority than a Worker. Every Action still uses frozen package identity, an exact Runtime allowlisted digest, schema validation, P10 review/receipt, bounded frames/timeouts, one Run-scoped service, and P11 shutdown. Unknown handlers and modified payload bytes fail closed.

### TB6b: Runtime to Browser QA and fixed OCR helpers

Browser QA runs a pinned Chromium in a dedicated profile and forces HTTP/HTTPS connections through a loopback Runtime proxy. The proxy validates and pins public destinations per connection; private, loopback, link-local, credential-bearing, and unsupported URLs fail closed. Page text and console/network evidence remain untrusted data. Fixed OCR runs only SheJane's allowlisted Worker against one exact native RapidOCR Runtime Asset; Runtime owns input materialization, time/output bounds, schema validation, Artifact promotion, and process cleanup. Neither reserved adapter can be selected by an external plugin package.

### TB7: Plugin output to model and user interface

Structured output, logs, names, media metadata, and errors may contain prompt injection, terminal escapes, misleading text, or secret-looking material. They are data, not trusted instructions or markup.

## Security assumptions

- Runtime package storage and SQLite files are writable only by the current OS user and trusted SheJane processes.
- Platform isolation adapters fail closed when required primitives, entitlements, kernel features, or policies are unavailable.
- Runtime does not place provider keys, credential handles, pairing tokens, or arbitrary inherited environment in an invocation.
- Input authorization is complete before materialization and remains leased for the invocation.
- Action schemas and envelopes are validated with bounded depth/size; schema validation itself is resource-limited.
- Package and staged file access uses descriptor-based or equivalent race-resistant checks, not only string prefix tests.
- Plugin output is treated as untrusted at every subsequent model/UI boundary.

If an implementation cannot maintain an assumption, it must narrow or disable the affected capability rather than silently weakening the model.

## Required controls

### Package ingestion

- Limit archive bytes, entry count, expanded bytes, compression ratio, individual entry size, manifest/schema size, and schema complexity.
- Reject absolute paths, backslashes, `.`/`..`, duplicate normalized or case-folded paths, alternate data streams, device files, FIFOs, sockets, hard links, and symlinks.
- Parse the manifest and every referenced schema without executing payload code.
- Require all references to resolve once inside the canonical package root.
- Canonically hash normalized paths and all security-relevant bytes; store them immutably by digest. Only the detached signature envelope is excluded.
- Verify the v1 Ed25519 envelope over the domain-separated canonical package digest using a public key supplied by policy, and record signer identity separately from policy decisions.
- Activate registry state only after all validation succeeds, in one transaction.

### Authorization and Run binding

- Effective capability is the intersection of manifest request, installation policy, user/Run grant, and platform support.
- Effective resource limits are the strictest applicable values.
- A manifest request, signature, publisher name, chat mention, or prior approval never grants authority by itself.
- P3 freezes plugin ID, semantic version, package digest, Action schema digest, and selection source.
- P6 verifies the digest again and holds an execution lease until P11.
- Plugin update/removal is copy-on-write/retire; active or recoverable Runs keep exact old bytes.

### Action execution

- PluginToolAdapter enters the existing ToolReviewMiddleware and ToolExecutionMiddleware path.
- Runtime creates `operation_id`; the plugin only echoes it.
- Validate Action arguments before launch and result output after return.
- Inputs are immutable, individually listed, hashed, MIME-labelled references under `/input`.
- Candidate output is restricted to a private `/output` staging root.
- Time, memory, CPU/fuel, frames, logs, file count, individual size, total output, and nesting depth are bounded.
- Cancellation and timeout close the entire instance/process tree and all pipes.
- A potentially effectful lost invocation becomes `outcome_unknown`; it is not blindly retried.

### WASI

- Fresh instance per invocation and empty default capability set.
- No inherited stdio beyond bounded protocol data, no host environment, no network, no process creation, and no arbitrary Host Functions.
- Pass authorized input bytes through the typed Component call; Runtime alone writes staging.
- Deny symlink/reparse traversal and revalidate objects at use.
- Configure explicit memory/table/instance limits and deterministic fuel.
- Verify the module digest immediately before instantiation.
- Do not call `add_wasip2()`; unresolved imports trap. A fixed deterministic insecure seed is the only current Rust runtime shim.

### Managed Worker

- Fresh short-lived process per Action in v1, with minimal fixed environment and private directories.
- UTF-8 NDJSON JSON-RPC 2.0 on stdout only; bounded stderr diagnostics; no large payloads in control frames.
- Require initialize/version negotiation, exactly one invoke, exactly one terminal result, then shutdown.
- Unknown methods/fields, identity mismatch, stdout contamination, oversized frames, malformed messages, duplicated results, or protocol-order violations terminate the call and quarantine its staging.
- Supervise the full descendant tree and prove P11 quiescence.
- Distinguish `process_isolated`, `access_isolated`, `resource_isolated`, and their
  conjunction `sandboxed` in policy, storage, API, and UI.
- Do not enable an untrusted worker unless the current platform adapter enforces filesystem, network, credentials, process visibility/spawn, IPC, and resource policy. User confirmation cannot waive this rule.

Candidate platform mechanisms require independent escape tests: macOS App Sandbox/XPC or an equivalently enforceable helper design; Windows AppContainer plus ACL and Job Object policy; Linux namespaces/Landlock/seccomp or an equivalently enforceable policy. A common interface must not conceal weaker platform behavior.

### Artifact promotion and rendering

- Treat plugin-declared path, MIME, filename, size, hash, and success status as claims.
- Open staged objects without following links or special files and verify they remain beneath the staging descriptor/root.
- Recompute size and cryptographic digest from bytes actually promoted.
- Validate format structure where available; reject partial or corrupt Office/media files.
- Move/copy into Runtime-owned storage and write Artifact plus receipt state atomically or with a recoverable transaction protocol.
- Escape untrusted names/messages in Client, strip control characters, cap display, and never render plugin HTML/JS in v1.

## Attack surfaces and prioritized attacker stories

### P0: Managed Worker reads credentials or user files

**Story:** A user installs a document Worker. The child process ignores its staging directory, reads the home directory or credential files using ordinary OS APIs, and exfiltrates them over the network.

**Why it works without mitigation:** cwd, a clean environment, process separation, timeout, signing, and a warning dialog do not remove current-user file or socket permissions.

**Required mitigation:** untrusted Worker enablement is denied unless a tested OS isolation adapter blocks all undeclared host paths, network, credential stores, process inspection, and IPC. Runtime secrets remain outside the worker environment and filesystem view. Network denial is tested from the worker and descendants.

**Residual risk:** kernel, sandbox implementation, entitlement, ACL, or policy-compiler vulnerabilities. Severity: Critical.

### P0: Package extraction escapes the content store

**Story:** An archive uses `../`, absolute paths, case collisions, Unicode/path normalization differences, symlinks, hard links, device entries, or a decompression bomb to overwrite Runtime files or exhaust disk/memory.

**Required mitigation:** bounded streaming inspection, canonical path policy, entry-type allowlist, duplicate detection under target filesystem semantics, no links/special files, extraction to a new private directory, post-write verification, and atomic activation only after digest/validation.

**Residual risk:** filesystem-specific normalization edge cases and parser/library vulnerabilities. Severity: Critical for overwrite; High for exhaustion.

### P0: Staged Artifact escapes or forges authoritative output

**Story:** A plugin returns `/output/report.pdf` that is a symlink/reparse point, swaps the file after validation, lies about MIME/hash/size, or races promotion to read/copy a host file.

**Required mitigation:** descriptor-relative no-follow access, reject links/special files, lock or move from a private non-shared staging root, recompute properties from the promoted bytes, validate format, and commit only Runtime-generated Artifact metadata.

**Residual risk:** platform-specific TOCTOU if descriptor-safe primitives are unavailable. Such a platform must copy while holding verified handles or disable the path. Severity: Critical.

### P1: Frozen Run executes different plugin bytes

**Story:** A plugin updates or is removed after Run acceptance; recovery resolves “latest compatible” or reads mutable package files and executes different code or schemas.

**Required mitigation:** freeze exact package and schema digests at P3; immutable content-addressed storage; P6 digest recheck and lease; no in-place update; explicit `plugin_version_unavailable` instead of fallback.

**Residual risk:** compromised local user can modify both package bytes and database; detect with digest mismatch but cannot defend against full same-user Runtime compromise. Severity: High.

### P1: Worker protocol confusion or stdout injection

**Story:** A dependency prints to stdout, sends huge/deep JSON, duplicates a result, guesses another operation identity, or emits success after cancellation to trick the Runtime into committing an invalid result or consuming resources.

**Required mitigation:** stdout protocol exclusivity; frame/depth/rate limits; strict unknown-field rejection; state-machine ordering; identity echo checks; one terminal result; stderr-only bounded logs; quarantine staging after any protocol fault.

**Residual risk:** JSON/parser defects. Severity: High.

### P1: Parser exploit through authorized input

**Story:** A validly authorized DOCX, archive, image, or video exploits a vulnerable native library inside a plugin.

**Required mitigation:** sandbox both execution types, pin dependencies, apply memory/time/output limits, keep input read-only, minimize host imports, update vulnerable packages independently, and test malformed corpora. Managed Worker OS isolation remains mandatory even for “read-only” Actions.

**Residual risk:** zero-days inside WASI Runtime, native decoders, or OS isolation. Severity: High.

### P1: Undeclared network access or SSRF

**Story:** A plugin contacts the internet, loopback Runtime endpoints, cloud metadata, or LAN services despite declaring a local transformation.

**Required mitigation:** no WASI socket imports; empty Extism host allowlist if Extism is used; OS-level network denial for untrusted Workers; do not rely on proxy environment variables; isolate loopback too. Future network capability requires explicit destinations, DNS/rebinding handling, receipt semantics, and separate threat review.

**Residual risk:** covert channels through authorized outputs/timing and platform firewall bypasses. Severity: High.

### P1: Resource exhaustion and orphan descendants

**Story:** A module loops, allocates aggressively, expands a small archive, floods stdout/stderr, fills staging, or spawns descendants that survive cancellation.

**Required mitigation:** fuel/CPU, wall-clock, memory, file count/size, disk, frame/log, process-count limits; private quotas; whole-tree termination; P11 quiescence checks; startup scavenging of expired staging leases.

**Residual risk:** host-level disk pressure and platform accounting gaps. Severity: High for system-wide denial, Medium for one failed Run.

### P1: Vision host call becomes a credential or network confused deputy

**Story:** A Vision Worker asks the Runtime to call an attacker-selected URL/model, injects arbitrary provider headers/body fields, references an image outside its grant, or turns a verbose provider error into credential/base-URL disclosure.

**Required mitigation:** do not grant Worker network or credentials. The `model.vision.invoke` host call accepts only an invocation-scoped request ID, immutable installation-owned provider/model binding, authorized input IDs, a bounded prompt, and normalized generation parameters. Runtime rechecks `image_inputs`, MIME/digest/Run ownership and quotas, builds the provider request itself, disables retries, redacts errors, and returns only bounded normalized output/usage. No arbitrary URL, header, SDK option, tool, or raw request/response field crosses the protocol.

**Residual risk:** provider SDK defects, provider-side data handling, and model output that contains sensitive information visible in an authorized image. Severity: High.

### P1: Vision inference amplifies cost or exfiltrates authorized images

**Story:** A malicious or prompt-injected Worker repeatedly submits high-resolution images, silently switches to a remote backend, or sends more images/tokens than the user intended, creating unexpected billing or remote disclosure.

**Required mitigation:** local/cloud is an explicit immutable binding with visible remote-processing consent and no fallback. Enforce per-invocation image count/bytes/pixels, output tokens, timeout, one in-flight host request, provider usage/cost policy, cancellation, and receipt-based single settlement. Provenance records backend, provider/model, normalized parameters, usage, and input digests without recording image bodies.

**Residual risk:** provider price/model changes and nondeterministic token usage within the configured cap. Severity: High for unbounded billing/disclosure; Medium after enforced limits and consent.

### P2: Signature or publisher UI creates false trust

**Story:** A signed malicious package or misleading publisher name causes users to believe Managed Worker code is safe or “official,” and the product grants broader rights.

**Required mitigation:** show signature, publisher claim, source, execution kind, requested/effective capabilities, and actual sandbox status as separate fields. Never create an official/community privilege tier. Policy is based on explicit grants and enforceable platform support.

**Residual risk:** social engineering and compromised publisher keys. Severity: Medium to High depending on granted capability.

### P2: Plugin output prompt-injects the Agent or spoofs UI

**Story:** Extracted text tells the model to ignore policy, an error contains terminal/control sequences, or a filename imitates a trusted system message.

**Required mitigation:** label outputs as untrusted plugin data, preserve tool/message provenance, escape UI content, strip controls, bound logs/messages, never treat output as system instructions, and keep tool policy outside model text.

**Residual risk:** model may still follow malicious content within its allowed tool set; existing approval and capability boundaries limit impact. Severity: Medium.

### P2: Chat reference ambiguity selects the wrong capability

**Story:** Similar display names, localization, or prompt text causes `@name` or `/name:command` to resolve to a different plugin or silently broaden an active Run.

**Required mitigation:** composer selection resolves to stable plugin/command IDs and shows the selected identity; Runtime validates enabled exact versions; P3 freezes selection; steering cannot mutate the active Run's Action catalog; conflicts require explicit user choice.

**Residual risk:** user chooses the wrong similarly named package. Signature/source display reduces confusion. Severity: Medium.

## Severity calibration

| Severity | Plugin-platform meaning |
| --- | --- |
| Critical | Crosses a trust boundary to execute host code, read credentials/arbitrary files, escape sandbox, or forge/overwrite authoritative state without a meaningful grant |
| High | Bypasses a material capability, executes different package bytes, exposes sensitive workspace data, or causes broad persistent denial of service |
| Medium | Requires user-enabled plugin and limited grant, affects one Run/Artifact, causes bounded denial, spoofing, or policy confusion without boundary escape |
| Low | Minor diagnostic leakage, local correctness issue, or hardening gap with no demonstrated capability expansion |

Severity is reduced only by an enforced prerequisite, not by publisher reputation or a warning dialog. A vulnerability reachable only by an untrusted Managed Worker is still Critical if the product claims that Worker is sandboxed.

## Phase 0 release gates

ADR-0001 may move from Proposed to Accepted only after:

1. manifest, invocation, result, and Action schemas validate both reference fixtures and reject traversal/unknown fields;
2. WASI runtime spike proves empty default authority, bounded execution, isolated input/output, and digest binding;
3. Managed Worker spike proves bounded protocol, full process-tree cleanup, staged Artifact validation, and honest `process_isolated` state;
4. each desktop platform either proves enforceable Worker isolation or explicitly disables untrusted Workers;
5. conformance tests cover crash/recovery boundaries and do not duplicate the existing receipt/Artifact stores;
6. Office golden fixtures establish quality and malicious-file baselines;
7. security review confirms no plugin path exposes LocalShellBackend, arbitrary Host Functions, secrets, or host paths.

Until those gates pass, fixtures are development artifacts and the product must not advertise third-party plugin compatibility.

## Residual risk and review triggers

Re-run this threat model when adding network, secrets, direct workspace writes, background services, plugin UI/WebView, inter-plugin calls, dependencies, auto-update, remote registries, process pools, custom WASI Host Functions, or a new platform isolation implementation.

Review upstream sandbox/runtime advisories continuously. A valid plugin signature does not protect against a newly disclosed vulnerability in its parser, WASI runtime, native dependency, or OS isolation primitive.

---
Repository: `/Users/MediaStorm/Desktop/ColdFlame/SheJane`

Version: `codex-security-snapshot/v1:sha256:ee24def43541e8c8d346536df441bd6784e32d0171c766b5839c3b2393f3d819` (all tracked and unignored files except this generated threat-model file)
