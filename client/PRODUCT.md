# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

SheJane prioritizes Chinese desktop users, including people who can register for and sign in to model providers but do not understand concepts such as APIs, API keys, model protocols, or gateway configuration. They want an agent to complete practical work on their own computer through natural-language conversation without first becoming an AI infrastructure expert.

## Product Purpose

SheJane is a local-first agentic chat product for macOS and Windows. It lets people ask an agent to work with local files, use tools, and complete multi-step tasks while keeping execution, permissions, checkpoints, and workspace access under a local Runtime.

Success means a user can connect a model service, describe a task in ordinary language, understand what the agent is doing, make required decisions, and receive a useful result without managing the underlying execution system.

## Positioning

SheJane combines an approachable desktop conversation interface with a locally owned Agent Runtime. Unlike a cloud-only chat product, the Runtime on the user's computer remains authoritative for execution, permissions, workspaces, and task state. Unlike a developer-oriented agent harness, the Client turns model services and extensibility into user-facing workflows.

## Operating Context

- Users work in the official Electron desktop Client on macOS or Windows.
- Conversations can be bound to explicitly authorized local workspaces.
- Users connect their own model services and select a concrete model.
- Tasks may use local files, previews, tools, approvals, checkpoints, recovery, memory, Skills, MCP servers, deterministic plugins, and subagents.
- Sensitive or consequential actions remain visible to the user and follow Runtime-owned permission rules.

## Capabilities and Constraints

- The Client is the user interface and a disposable projection of Runtime-owned conversation and task state; it is not the execution kernel.
- The local Runtime owns the agent loop, tool execution, permissions, checkpoints, credentials, workspaces, and authoritative state.
- Model-service API keys are stored in the operating-system credential store.
- SheJane does not require a product account, provide a default model service, automatically choose a model, or silently fall back to another provider.
- Model services must be understandable to non-technical users while preserving explicit provider, connection, and model choices.
- Extensions use Skills, MCP, plugins, and subagents instead of adding business-platform integrations to the Runtime core.
- The desktop Client and bundled Runtime communicate over authenticated loopback HTTP and SSE.
- The plugin platform is currently a preview; availability depends on the capability and operating system.
- Remote clients and a remote-access gateway are future product boundaries, not current Client behavior.

## Brand Commitments

- The official product names are 石间 and SheJane.
- The name and official logo assets identify the official project and are governed by `TRADEMARKS.md`.
- Product language should be calm, direct, and understandable without unnecessary infrastructure terminology.
- The incumbent Client identity and its source of truth are documented in `docs/ui/shejane-design-system.md`.

## Evidence on Hand

- The working Client implementation lives in `client/src/` with Electron integration in `client/electron/`.
- The implemented product boundaries and capabilities are documented in `README.md`, `CLAUDE.md`, `docs/run-loop.md`, and `docs/roadmap.md`.
- Automated Client, Runtime, contract, and end-to-end tests provide implementation evidence for core task, permission, recovery, workspace, model-service, and extension flows.
- The repository contains no approved testimonials, customer logos, adoption metrics, or external performance claims; future product work must not fabricate them.

## Product Principles

1. Make powerful local agent work understandable to people who are not infrastructure experts.
2. Keep execution, data access, credentials, and task state locally owned and explicit.
3. Prefer clear user decisions over automatic model selection, silent fallback, or hidden side effects.
4. Extend through stable capability boundaries instead of accumulating product-specific integrations.
5. Treat recovery, permission, and failure states as part of the primary experience.
