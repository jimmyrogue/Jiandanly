<div align="center">

# 石间 · SheJane

### 本地优先的 Client 与 Agent Runtime

在自己的电脑上运行带工作区、权限、检查点、Skill、MCP 和确定性插件的工具型 Agent。

[![CI](https://img.shields.io/github/actions/workflow/status/jimmyrogue/SheJane/ci.yml?branch=main&style=flat-square&logo=githubactions&label=CI)](https://github.com/jimmyrogue/SheJane/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-AGPL--3.0--only-B3532F?style=flat-square&logo=gnu)](./LICENSE)
![Client](https://img.shields.io/badge/desktop-macOS%20%7C%20Windows-2B2A28?style=flat-square&logo=electron)

[English](./README.md) · 简体中文

</div>

## 为什么做 SheJane

- 本地 Runtime 负责 Agent 循环、工具执行、权限、检查点和工作区访问。
- Electron 是官方桌面客户端，不是执行内核。未来其他客户端可以使用同一套 Runtime 协议。
- Skill、MCP、Subagent 和确定性插件负责扩展能力，业务平台集成不进入 Runtime 内核。

## 整体结构

```mermaid
flowchart LR
    C["Client<br/>Electron + React"] -->|"本地 HTTP + SSE"| R["Runtime<br/>Python + LangGraph"]
    R --> W["本地工作区<br/>文件 · 工具 · 检查点"]
    R --> E["扩展能力<br/>Skill · MCP · 插件 · Subagent"]
    R --> B["BYOK 供应商<br/>OpenAI 兼容接口 · 本地模型端点"]
```

Client 通过 loopback HTTP 和配对凭证连接 Runtime。Runtime 失败时明确报告本地错误，不静默切换执行路径。

仓库只保留两个产品模块：

```text
client/                 # UI、Electron 生命周期和 Runtime 状态投影
runtime/                # 执行核心、协议、SDK、插件和测试
├── src/shejane_runtime/
├── sdk/
└── plugins/
```

Runtime 可以脱离 Client 独立启动和测试。SDK 与插件放在 Runtime 目录下，是因为它们的契约由 Runtime 拥有；它们仍可独立构建和发布，但不再形成第三个产品模块。

## 当前包含什么

| 领域 | 当前实现 |
|---|---|
| Runtime | LangGraph 和 Deep Agents 循环、流式事件、检查点、恢复、规划、验证、记忆和人工审批 |
| 本地工具 | 工作区文件、工作区只读且无网络的沙箱 Shell、Office、网页抓取、剪贴板审批和定时任务 |
| 扩展能力 | Skill、MCP、确定性的 WASI/Managed Worker 插件、首方 macOS Computer Use 适配器、Subagent 和可配置 middleware |
| Client | Electron 和 React、Runtime 权威对话的本地投影、文件预览、供应商设置与工作区控制 |
| Runtime SDK | 面向命令、SSE、快照、错误和生成协议类型的公共 TypeScript 客户端 |

业务平台连接器统一通过标准工具或 MCP 接入。

插件平台目前处于预览阶段。WASI 插件包已经可以通过 Runtime 权威的 Action 协议安装和执行；Managed Worker 插件在当前平台的生产隔离与发布 Gate 通过前保持 fail-closed。SheJane 的固定 [Computer Use](./runtime/plugins/computer-use)、[Browser QA](./runtime/plugins/browser-qa) 和 [OCR](./runtime/plugins/ocr) 包使用 Runtime 保留适配器，并继续经过 Action 审批、回执、严格 Schema 与内容寻址的包/资产存储。Browser QA 与 OCR 已有 macOS arm64 和 Windows AMD64 原生资产；Computer Use 仍仅支持 macOS arm64。公开包规范和本地工具见[插件开发者指南](./docs/plugins/developer-guide.md)。

### 当前的多 Agent 协作

SheJane 目前采用 manager-as-tools 模式：父 Agent 通过同步 `task()` 调用内置或用户定义的子 Agent。子 Agent 拥有隔离上下文和受限的模型/工具预算，并继续使用同一套 Runtime 权限与回执边界；父 Agent 可以在同一模型回合并行派发多个任务，再统一汇总结果。

这属于受控并行委派，还不是真正的 handoff 或自治 Agent 团队。目前不计划开发独立后台 Agent、handoff、共享任务板或 swarm；现有子 Agent 已足够覆盖有边界的调研、审查和写作。

## 路线图

第一组可靠性门禁已经落地：

- 预置 BYOK 模型必须先通过与正式 Agent 共用适配器的流式 `模型 → 工具 → 模型` 回环，才能进入可用状态；
- CI 与 Client 发布使用确定性 Agent 结果门禁，`make eval` 另外保存真实 Provider 轨迹和基线差异；
- Runtime diagnostics 会从持久记录导出脱敏的 `run → model → tool/subagent → checkpoint → terminal` Trace。

当前继续收敛 Runtime 状态所有权，并补齐插件/安装包正式发布证据。可审计语义记忆、结构化输出和周期任务属于后续工作。

周期任务、Remote Client、fresh-context handoff 和实时语音属于后续工作。详见完整[路线图](./docs/roadmap.md)与[当前 Agent Harness 能力审计](./docs/agent-harness-capabilities-latest-2026-07-26.md)。

## 快速开始

开发需要**支持 Corepack 的 Node.js 22+**、**Python 3.12+** 与 [uv](https://docs.astral.sh/uv/)。

```bash
make setup-hooks
corepack enable && pnpm install
make dev
```

根目录不需要 `.env`。启动 Client 后，在 Runtime 设置中添加 OpenAI 兼容供应商并选择模型即可。启动异常时运行 `make doctor`。

## 开发检查

```bash
make dev-client          # 只启动 Client，使用 SHEJANE_RUNTIME_URL 与 SHEJANE_RUNTIME_TOKEN
make dev-runtime         # 只启动 Runtime
make test-client         # React 与 Electron 行为
make test-runtime        # Agent 循环、状态、工具、插件和 HTTP
make test-runtime-sdk    # 生成类型、HTTP client 与 SSE parser
make test-contract       # 真实 Runtime HTTP/SSE + SDK，不启动 Electron
make test-e2e            # 完整 Client + Runtime 路径
make lint && make test && make build
```

故障定位也按边界分层：Runtime 测试失败就留在 `runtime/`；Client 测试失败就留在 `client/`；两边单测通过但 contract 失败，就检查协议边界；contract 通过但 E2E 失败，优先检查 Client 投影或 Electron 进程编排。

## 从源码构建 Runtime

Runtime 暂不作为独立程序发布到 GitHub Release。请在实际运行它的操作系统和 CPU 架构上构建：

```bash
make package-runtime
```

构建结果位于 `runtime/dist/shejane-runtime/`。Windows 可执行文件名为 `shejane-runtime.exe`。PyInstaller 会打包平台相关的原生依赖，因此不能跨操作系统或 CPU 架构构建。

## Client 安装包

Client 发布工作流会从同一次提交构建 Runtime，并将它放进安装包。GitHub Actions 生成两个产物：

```text
client-macos-arm64
client-windows-x64
```

手动运行工作流可以测试安装包。推送 `client-vX.Y.Z` 标签才会创建 GitHub Release。Runtime SDK 继续使用 `runtime-sdk-vX.Y.Z` 标签发布。

## 文档

- [Runtime 阶段总览](./docs/harness-runtime-stages.md) 定义目标 P1-P12 架构。
- [当前运行链路](./docs/run-loop.md) 说明代码现在如何运行。
- [Runtime 协议](./docs/runtime-protocol.md) 定义 HTTP、SSE、事件与恢复游标。
- [路线图](./docs/roadmap.md) 将当前能力缺口整理为有顺序的交付 Gate。
- [Agent Harness 能力审计](./docs/agent-harness-capabilities-latest-2026-07-26.md) 对照当前 OpenAI、Anthropic、Deep Agents/LangGraph 与 Pi 模式检查实现。
- [贡献指南](./CONTRIBUTING.md) 说明开发、测试和 CLA 流程。
- [运维手册](./docs/operations.md) 说明部署和排障。
- [插件开发者指南](./docs/plugins/developer-guide.md) 定义 WASI/Managed Worker 包、Action、校验和发布检查。

## 授权

Copyright © 2026 [TAO LIANG](mailto:tliang92@gmail.com)。

SheJane 采用双重授权：

- 社区使用遵循 [GNU AGPL v3.0 only](./LICENSE)。
- 闭源分发、闭源修改、集成和白标使用需要取得[独立商业授权](./COMMERCIAL_LICENSE.md)。

SheJane 名称和 Logo 适用[商标与品牌政策](./TRADEMARKS.md)。外部贡献者需要同意[贡献者许可协议](./CLA.md)。第三方组件继续适用[第三方声明](./THIRD_PARTY_NOTICES.md)中列出的各自许可证。
