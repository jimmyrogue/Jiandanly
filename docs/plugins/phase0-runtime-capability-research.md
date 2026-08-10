# Phase 0 Runtime 能力调研与选择建议

> 调研日期：2026-07-15
>
> 范围：插件 Action 的执行、隔离、耐久性与本地 Managed Worker
>
> 资料约束：外部事实只引用当前官方文档、官方规范或官方源码；SheJane 现状以当前仓库代码和运行文档为准

> 实施结果更新：后续可执行 spike 选择了直接 Wasmtime Component Model，而不是 Extism。v1 不链接完整 WASIp2，也不把 guest 映射到宿主目录；Host 只传入授权 bytes，并将未定义 import 设为 trap。此结果取代本文第 6 节的初始优先级建议。

> Phase 5 复核（2026-07-16）：Managed Worker 的候选权限层更新为固定版本的 Anthropic Sandbox Runtime（SRT），但在三个原生平台分别通过打包与逃逸测试前仍保持 fail closed。该选择只替换 OS policy launcher，不改变 SheJane 的 Action、receipt、Artifact 或幂等协议。

> Phase 5 资源复核（2026-07-16）：当前 Codex macOS 路径在 Seatbelt 中启动普通子进程，没有 Worker 级内存/进程树配额；Pi 示例直接委托 SRT；SRT 当前公开 schema 仍只覆盖文件、网络、凭据和 IPC；Deep Agents 将更强的资源隔离交给 Daytona、Modal、Runloop、AgentCore、LangSmith 等 sandbox provider。[Codex Seatbelt launcher](https://github.com/openai/codex/blob/main/codex-rs/core/src/seatbelt.rs) [SRT schema](https://github.com/anthropic-experimental/sandbox-runtime/blob/main/src/sandbox/sandbox-schemas.ts) [Pi sandbox extension](https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/examples/extensions/sandbox/index.ts) [Deep Agents sandboxes](https://docs.langchain.com/oss/python/deepagents/sandboxes)

> 本机验证同样不能补齐该 Gate：`RLIMIT_CPU` 可执行；`RLIMIT_NPROC` 是 user-id 级而不是 invocation/process-tree 级；`RLIMIT_AS` 与 `RLIMIT_RSS` 映射到同一资源并拒绝当前进程设置有限值。Apple 文档也把 `RLIMIT_RSS` 描述为内存紧张时的调度偏好，而不是可配置的 Worker 硬上限。[Apple setrlimit(2)](https://developer.apple.com/library/archive/documentation/System/Conceptual/ManPages_iPhoneOS/man2/setrlimit.2.html)

因此不采用“定时采样 RSS 后 kill”冒充硬资源隔离，也不因 SRT/Codex 能限制文件和网络就开放任意 Managed Worker。保持 Registry fail closed；若产品坚持任意来源的本地原生插件也必须视为不可信，macOS 需要 VM/远程 sandbox provider 级边界，或者另行作出“用户明确完全信任本地原生代码”的产品决策。这是 trust policy 选择，不应藏在 Adapter 实现里。

## Phase 5 Managed Worker 方案复核

本轮先重新核对了当前仓库能力：WASI 已经通过 Wasmtime 执行；Managed Worker 已把握手拆成 `process_isolated`、`access_isolated`、`resource_isolated` 与最终 `sandboxed`。macOS SRT 只满足 access，资源隔离和最终 sandbox 仍为 false；Client 的 Electron renderer sandbox 与 Worker 无关。macOS 打包当前也没有启用 Hardened Runtime、App Sandbox 或 helper entitlements，因此不能假设已经拥有 Apple App Sandbox helper。

外部实现给出的共同边界如下：

- Codex 按宿主平台选择实现：macOS 使用 Seatbelt，Linux 当前优先 bubblewrap 并叠加 `no_new_privs`、seccomp、user/PID/network namespace，Windows 使用 restricted token。它没有把普通子进程或 Electron renderer 当作跨平台 sandbox。[Codex Rust CLI](https://github.com/openai/codex/blob/main/codex-rs/README.md) [Codex Linux sandbox](https://github.com/openai/codex/blob/main/codex-rs/linux-sandbox/README.md)
- Pi 没有给所有 extension 提供宿主强制隔离；其官方 sandbox extension 只是把 bash 委托给 `@anthropic-ai/sandbox-runtime`，验证了“Agent 保留自己的工具协议，权限边界交给独立 OS launcher”的组合方式。[Pi sandbox extension](https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/examples/extensions/sandbox/index.ts)
- Deep Agents 的 `BaseSandbox` 仍以任意 shell `execute()` 为最小 provider 接口，官方生产选择主要是 Daytona、Modal、Runloop、AgentCore、LangSmith 等隔离环境。它适合远程 coding sandbox，不应替代 SheJane 的结构化本地 ActionExecutor。[Deep Agents sandboxes](https://docs.langchain.com/oss/python/deepagents/sandboxes)
- Anthropic SRT 0.0.65 已覆盖 macOS `sandbox-exec`、Linux bubblewrap、Windows 专用本地用户 + WFP + working-tree ACL，并把策略应用到整个进程树；项目仍标记为 beta research preview。[SRT README](https://github.com/anthropic-experimental/sandbox-runtime) [SRT package](https://github.com/anthropic-experimental/sandbox-runtime/blob/main/package.json)
- Apple 支持把 helper/XPC 嵌入 App Sandbox，但要求应用、helper、entitlements 和签名形成完整发布链；这适合未来收紧 SheJane 自身组件，不适合作为动态第三方 Worker 已经具备的能力。[Apple sandboxed helper](https://developer.apple.com/documentation/Xcode/embedding-a-helper-tool-in-a-sandboxed-app)

### 决定

Phase 5 不复制 Codex/SRT 的三套高风险 platform policy，也不把 SRT 的通用 shell 暴露给模型。选择固定 `@anthropic-ai/sandbox-runtime@0.0.65` 作为候选 OS launcher，外层继续使用现有一次一进程的 Managed Worker JSON-RPC 和 Artifact staging：

1. `denyRead` 从文件系统根开始，只有系统运行库、精确 package root、只读 input 和 output staging 可读；不能采用 SRT 默认的“读取普遍允许”。
2. `allowWrite` 只有本次 output staging；输入、package、sandbox 配置和宿主其余路径不可写。
3. 网络 allowlist 为空；Unix socket、Apple Events、弱化 network/nested 模式全部关闭。
4. Worker entrypoint 必须是包内当前平台的自包含产物；不借用宿主 Python/Node 环境或用户 PATH。
5. SRT 必须固定版本、进入 SBOM/许可证清单并由 Client 打包；初始化失败、平台未 provision、原语缺失或 conformance 失败时拒绝安装/启用 Managed Worker。
6. 每个平台状态独立。macOS 本机 spike 已实测：授权文件可读、同目录未授权文件返回 `Operation not permitted`、仅 output 可写、外部 HTTPS 被拒；这只证明候选可行，不等于 Windows/Linux 或发布包已经通过 Gate。

若 SRT 的 beta API、发布方式或安全修复节奏无法满足固定版本维护要求，再替换 launcher；ActionExecutor 和插件协议不随之改变。

## 结论先行

Phase 0 不需要再引入一套 Agent Runtime。SheJane 已经具备插件 Action 最难替代的上层执行语义：固定 Run 上下文、LangGraph 同步检查点、工具审批、稳定 operation identity、持久 tool receipt、`outcome_unknown` 对账、Artifact 持久化、租约和原子结算。缺口集中在**受控代码执行层**：插件清单与版本冻结、Action 协议、Wasm executor、Managed Worker IPC、进程资源监督，以及真正可强制的文件/网络 capability。

建议 Phase 0 冻结以下选择：

1. **继续以 P6 为主要阶段，P10 为执行接缝**。PluginCatalog 在 P6 提供固定 Action 目录和执行租约；每个 Action 仍通过现有 ToolReviewMiddleware 与 ToolExecutionMiddleware 进入 P10。
2. **不要复制 pi 的同进程 extension 模型**。它适合可信本地开发工具，官方明确说明 package 具有完整系统权限，不是第三方确定性组件 sandbox。
3. **不要把 Deep Agents `BaseSandbox` 当成 ActionExecutor**。它的最小抽象是任意 shell `execute()`，适合 coding sandbox；插件 Action 应是结构化 `invoke(action, input, capabilities, limits)`，不应暴露通用 shell。
4. **Wasm 路径的 Phase 0 首选 Extism spike**，由 Extism Host SDK 承担 module 装载、调用、timeout、内存、文件和网络 allowlist；默认关闭 WASI、不给自定义 Host Functions、不给文件和网络。若后续必须使用 WIT Component Model 或确定性 fuel，再评估直接 Wasmtime adapter。
5. **公开执行类型建议先命名为 `wasm`，不要过早冻结成 `wasi`**。Extism 不启用 WASI 也能运行插件并提供更窄的 Host Function；`wasi` 应是 capability/ABI 选择，而不是包类型的同义词。
6. **Managed Worker v1 采用“每次 Action 一个短命本地进程 + 有界 JSON-RPC stdio”**。大文件只通过 staging 目录交换，stdout 只传控制消息，stderr 只做有界日志；Runtime 拥有超时、取消、进程树终止和 Artifact 提交。
7. **独立进程只算故障边界，不算权限 sandbox**。在 macOS、Windows、Linux 的强制隔离 adapter 完成前，第三方 `managed_worker` 默认禁用；首个 Documents fixture 只能作为受信任、固定摘要的官方 worker 验证执行协议。第三方低信任插件先走 Wasm。

## Runtime 阶段记录

```text
主要阶段：P6，绑定资源并取得 Agent 定义
上游输入：P5 的只读 execution context、冻结插件绑定、工作区授权和执行租约
下游输出：P7 可恢复的固定 Agent definition、Action 目录摘要和 PluginExecutionLease
状态所有者：Runtime PluginRegistry / Run frozen bindings / P10 tool receipts / P12 commit transaction
替换的当前路径：builder.py 静态拼装工具，以及 Office 工具由 tools/registry.py 直接常驻
```

Action 真正执行发生在 P10；P11 必须关闭 Wasm instance、Worker 进程树和 staging 资源，P12 继续原子提交 receipt、Artifact 和终态。阶段职责来自项目的[目标 Runtime 阶段文档](../harness-runtime-stages.md)，当前链路以[当前 Run Loop](../run-loop.md)为准。

## 1. SheJane 当前能力基线

### 1.1 已经具备

| 能力 | 当前实现证据 | 对插件平台的价值 |
| --- | --- | --- |
| Run 接纳、作业租约与冻结执行上下文 | [`runs.py`](../../runtime/src/shejane_runtime/runs.py)；[`run-loop.md`](../run-loop.md) | 插件版本可沿用同一冻结/租约模型，不需要第二套 Run Loop |
| LangGraph SQLite checkpoint，`durability="sync"` | [`runs.py`](../../runtime/src/shejane_runtime/runs.py)；[`builder.py`](../../runtime/src/shejane_runtime/agent/builder.py) | Action 前后的图状态可恢复，等待审批也能持久化 |
| 参数校验、风险分类、HITL 和整批暂停 | [`tool_review.py`](../../runtime/src/shejane_runtime/middleware/tool_review.py) | PluginToolAdapter 可复用现有审批入口 |
| 稳定 operation identity 与参数摘要 | [`tool_execution.py`](../../runtime/src/shejane_runtime/middleware/tool_execution.py) | Action 可以复用同一 tool receipt，不需要 plugin-specific receipt |
| `prepared/running/completed/failed/outcome_unknown` 与回放 | [`tool_execution.py`](../../runtime/src/shejane_runtime/middleware/tool_execution.py)；[`sqlite.py`](../../runtime/src/shejane_runtime/store/sqlite.py) | 对外部副作用不盲目重试，恢复时复用已完成结果 |
| 大工具结果转 Artifact 并限制模型返回 | [`tool_execution.py`](../../runtime/src/shejane_runtime/middleware/tool_execution.py) | Worker/Wasm 只需提交候选产物，Runtime 继续拥有最终落库 |
| 只读附件虚拟路径、200 MiB 流式接纳限制、附件与 PDF 200 MiB / 其他文件 20 MiB 模型读取限制、工作区路径校验 | [`runs.py`](../../runtime/src/shejane_runtime/runs.py)；[`backends.py`](../../runtime/src/shejane_runtime/agent/backends.py) | 可作为 Action input materialization 的起点 |
| Office 读写能力 | [`office.py`](../../runtime/src/shejane_runtime/tools/office.py)；[`pyproject.toml`](../../runtime/pyproject.toml) | 当前已有 DOCX/XLSX/PPTX 能力与回归基线，迁移目标不是从零实现 |
| 图片能力标记和文本模型明确降级 | [`builder.py`](../../runtime/src/shejane_runtime/agent/builder.py)；[`tool_execution.py`](../../runtime/src/shejane_runtime/middleware/tool_execution.py) | 已能阻止把图片块交给声明为 text-only 的模型 |
| MCP stdio 子进程、有界入站 frame 和进程树终止 | [`stdio.py`](../../runtime/src/shejane_runtime/tools/mcp/stdio.py) | Managed Worker IPC 可复用相同的传输与清理经验 |

当前 Office 并非完全缺失：Runtime 已内置 DOCX/XLSX 的读取和编辑，以及 PPTX 的创建、读取和编辑。当前图片文件可由 Deep Agents `read_file` 形成多模态块，但只在所选模型声明 `image_inputs` 时交付；文本模型收到明确限制。当前工具目录没有通用视频解码、抽帧、OCR/视觉 fallback 或音视频转写 Action。[`office.py`](../../runtime/src/shejane_runtime/tools/office.py) [`registry.py`](../../runtime/src/shejane_runtime/tools/registry.py) [Deep Agents backends](https://docs.langchain.com/oss/python/deepagents/backends)

### 1.2 尚未具备

| 缺口 | 当前事实 |
| --- | --- |
| PluginRegistry、manifest、安装/启停/回滚 | Runtime capabilities 目前没有 `plugins`，工具仍由 builder/registry 静态装配。[`runs.py`](../../runtime/src/shejane_runtime/runs.py) [`registry.py`](../../runtime/src/shejane_runtime/tools/registry.py) |
| Run 冻结插件 ID、版本、digest 与 Action schema hash | 当前只冻结模型、工作区、MCP 目录和图定义；没有插件 binding 表或 package lease。[`run-loop.md`](../run-loop.md) [`sqlite.py`](../../runtime/src/shejane_runtime/store/sqlite.py) |
| 统一 ActionInvocation / ActionResult / ArtifactCandidate | 当前工具直接遵循 LangChain Tool 形态，没有可供 Wasm 与 Worker 共用的结构化执行协议。[`registry.py`](../../runtime/src/shejane_runtime/tools/registry.py) [`tool_execution.py`](../../runtime/src/shejane_runtime/middleware/tool_execution.py) |
| Wasm Runtime | 依赖中没有 Wasmtime 或 Extism。[`pyproject.toml`](../../runtime/pyproject.toml) |
| Managed Worker runner | 除 MCP server 外，没有插件 worker handshake、版本协商、invoke/cancel/shutdown 协议。[`stdio.py`](../../runtime/src/shejane_runtime/tools/mcp/stdio.py) [`builder.py`](../../runtime/src/shejane_runtime/agent/builder.py) |
| 插件级 capability 强制 | 当前文件工具有虚拟路径边界，但 shell 并不受该边界限制；没有插件文件/网络 capability lease。[`builder.py`](../../runtime/src/shejane_runtime/agent/builder.py) |
| 跨平台进程资源与权限 sandbox | 当前 Runtime 依赖和执行路径中没有 macOS XPC/App Sandbox、Windows AppContainer 或 Linux namespace/Landlock adapter。[`pyproject.toml`](../../runtime/pyproject.toml) [`runs.py`](../../runtime/src/shejane_runtime/runs.py) |
| 插件产物 staging/验证/原子晋升 | 当前 Artifact 能保存结果，但没有“插件只能写临时输出，Runtime 验证后再晋升”的隔离目录协议。[`tool_execution.py`](../../runtime/src/shejane_runtime/middleware/tool_execution.py) [`sqlite.py`](../../runtime/src/shejane_runtime/store/sqlite.py) |

### 1.3 当前必须正视的边界

SheJane 当前 `_build_agent_backend()` 使用 `LocalShellBackend(root_dir=workspace, virtual_mode=True)`。Deep Agents 官方明确说明：`LocalShellBackend` 直接用 `subprocess.run(shell=True)` 在宿主机执行命令，工作目录只是 cwd，shell 仍能访问系统任意路径；`virtual_mode=True` **只限制文件工具，不限制 shell，也不构成安全边界**。[SheJane builder](../../runtime/src/shejane_runtime/agent/builder.py) [Deep Agents backends](https://docs.langchain.com/oss/python/deepagents/backends)

因此 Phase 0 不能把现有 `RuntimeBackend.execute()` 或 `LocalShellBackend` 透传给插件。HITL 可以降低误操作概率，但不是 capability enforcement；插件执行层必须另建窄接口。

## 2. pi coding agent / pi 的 Extension 与 Tool 模型

`badlogic/pi-mono` 的当前官方仓库已重定向到 `earendil-works/pi`。Pi package 可以从 npm、Git 或本地路径安装，并组合 extensions、skills、prompts 和 themes；固定 npm 版本或 Git ref 可阻止普通 update 移动版本。[Pi packages](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/packages.md)

Extension 是在 Pi Runtime 中装载的 TypeScript/JavaScript factory。官方 loader 使用 `jiti.import()` 加载 module 后，由宿主直接执行 `await factory(api)`，没有独立 worker 或进程。它直接取得 `ExtensionAPI`，可以注册或动态替换工具、命令、事件 handler、provider 和 UI；Node built-ins 与 extension 自己的 npm dependencies 可直接 import。Extension 甚至可以覆盖 `read`、`bash`、`edit`、`write` 等内置工具。[Pi extension loader](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/src/core/extensions/loader.ts#L390-L460) [Pi extensions](https://pi.dev/docs/latest/extensions)

Pi 官方安全提示非常直接：package 以完整系统权限运行，extension 能执行任意代码，安装第三方 package 前必须审查源码。Pi 本身也不提供默认 permission popup，README 建议需要者在 container 中运行或自己用 extension 实现确认流程。[Pi packages security notice](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/packages.md) [Pi README design choices](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/README.md)

Pi 的 Project Trust 只决定是否加载项目级设置、extensions 和 packages，并不是 sandbox。Tool 定义支持 sequential/parallel execution、AbortSignal 和 progress callback；`tool_call` event 可以修改参数或 fail-closed 阻断，RPC 模式也支持 `abort`。这些是有价值的生命周期控制，但没有工具调用级 idempotency key、结果去重或事务协议。[Pi security](https://pi.dev/docs/latest/security) [Pi extension types](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/src/core/extensions/types.ts#L438-L473) [Pi RPC](https://pi.dev/docs/latest/rpc)

Pi 提供一个**示例 extension**，使用 `@anthropic-ai/sandbox-runtime` 包装 bash，包含 domain/path 配置、timeout、AbortSignal 和进程组终止；它只支持 macOS/Linux，并且是可禁用、初始化失败时关闭的可选 extension，不是所有 extensions 的默认隔离层。[Pi sandbox extension source](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/examples/extensions/sandbox/index.ts)

### 对 SheJane 的判断

- 可借鉴：package source/pinning、约定目录、动态工具目录刷新、工具 override 的显式警告、AbortSignal 和进程组终止。
- 不可照搬：同进程 JS/TS extension、完整 Node API、extension 自行实现权限和 tool semantics。
- Pi 没有宿主强制的通用 operation ID、tool receipt、Artifact 原子提交或 exactly-once 协议；这些正是 SheJane 当前已有、应保留的优势。

## 3. LangGraph / LangChain：编排耐久性不是代码 sandbox

LangGraph 定位是长任务编排 Runtime，核心能力包括 checkpoint、durable execution、streaming 和 HITL；它不负责把任意工具代码隔离在操作系统 sandbox 中。[LangGraph overview](https://docs.langchain.com/oss/python/langgraph/overview)

Graph API 在恢复时从中断 node 的开头重新进入；官方要求 node 内副作用设计为幂等。Functional API 会从 entrypoint 开头 replay，并用已经持久化的 `@task` 结果跳过已完成工作；时间、随机数、网络调用等非确定性操作必须包进 task，否则 replay 可能改变控制流。[LangGraph backward compatibility](https://docs.langchain.com/oss/python/langgraph/backward-compatibility) [Functional API](https://docs.langchain.com/oss/python/langgraph/functional-api)

持久层在每个 step 保存 checkpoint，并可保存同一 super-step 中已经完成 node 的 pending writes，避免另一个并行 node 失败时重复计算成功 node。它仍不承诺外部 API 或本地进程副作用的 exactly-once。[LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence)

LangChain `ToolNode` 负责工具并行、错误处理和 state/runtime 注入；HITL middleware 能在匹配工具调用时 interrupt，检查点保存后接受 approve/edit/reject 再恢复。它没有为任意工具提供跨进程 sandbox，也没有自动生成可重放的业务幂等 key。[LangChain tools](https://docs.langchain.com/oss/python/langchain/tools) [LangChain HITL](https://docs.langchain.com/oss/python/langchain/human-in-the-loop)

### 对 SheJane 的判断

- SheJane 已采用正确的分层：LangGraph 负责 checkpoint/HITL，Runtime 自己的 ToolExecutionMiddleware 负责 operation identity、receipt 和 `outcome_unknown`。
- Plugin Action 必须继续进入现有 receipt 层；只把 Action 包成普通 `@tool` 而绕开它，会丢失 SheJane 比 LangGraph 默认更强的副作用保证。
- `durability="sync"` 保证 checkpoint 写入时序，不等于 worker 的外部副作用已经原子提交；Artifact 必须先 staging，P12 后才成为权威结果。

## 4. Deep Agents Backend 与 Sandbox 能力

### 4.1 `LocalShellBackend`

官方将它限定为可信的本地开发 CLI/CI 场景，并明确列出：宿主用户权限的任意命令、任意可访问文件、不可逆修改、无限 CPU/内存/磁盘和无进程隔离。timeout 和输出大小只限制等待/采集，不限制子进程访问范围。[Deep Agents backends](https://docs.langchain.com/oss/python/deepagents/backends)

这与 SheJane 当前实现完全对应：传入的精简 env 降低了直接泄露面，但 worker 仍能读取宿主文件、启动子进程和联网；`HOME`、`PATH` 也仍被传入。[SheJane builder](../../runtime/src/shejane_runtime/agent/builder.py)

### 4.2 `BaseSandbox` / `SandboxBackendProtocol`

Deep Agents 当前 `SandboxBackendProtocol` 增加 `execute()/aexecute()` 和 sandbox ID。`BaseSandbox` 只要求 provider 实现 `execute()`、upload、download 和 ID；其余 read/write/edit/ls/glob/grep 大多通过 sandbox 内 shell 脚本派生。官方文档特别说明 BaseSandbox 本身不会缩小 `execute()` 的信任边界。[Deep Agents BaseSandbox reference](https://reference.langchain.com/python/deepagents/backends/sandbox) [Deep Agents sandboxes](https://docs.langchain.com/oss/python/deepagents/sandboxes)

这使它很适合 coding agent 的“远程 shell + 文件系统”抽象，但不适合 SheJane 的确定性 Action：

- 接口以任意 command string 为中心，而不是 schema 化 Action。
- filesystem helper 也依赖 shell，难以证明只访问声明的 input/output。
- 不包含 plugin digest、capability grant、operation ID、receipt 或 Artifact commit。
- `write()` 官方源码还提示 preflight 与实际写入之间存在 TOCTOU 窗口。[BaseSandbox reference](https://reference.langchain.com/python/deepagents/backends/sandbox)

### 4.3 官方可替代 sandbox backend

当前官方列表包含 LangSmith、AgentCore、Daytona、E2B、Modal、NVIDIA OpenShell、Runloop 和 Vercel 等 provider。官方推荐的“sandbox as tool”模式让 Agent 留在宿主、密钥留在宿主，sandbox 失败不丢 Agent state；文件通过 provider upload/download API 进出。[Deep Agents sandboxes](https://docs.langchain.com/oss/python/deepagents/sandboxes) [Sandbox integrations](https://docs.langchain.com/oss/python/integrations/sandboxes/index)

这些 backend 可用于企业部署的可选远程 executor，但不适合 SheJane Phase 0 默认路径：它们需要外部服务、网络、凭据和生命周期计费，违背当前本地优先与零私有云依赖边界。已归档的 `langchain-sandbox` 也不应成为新平台基线。[langchain-sandbox repository](https://github.com/langchain-ai/langchain-sandbox)

## 5. Wasmtime / WASI capability

WebAssembly core 要求 guest 通过 import 使用宿主功能，实例内存与宿主隔离。Wasmtime 的安全文档把运行不可信代码作为主要目标，并说明 WASI filesystem 遵循 capability model：只能访问被授予的文件和目录。[Wasmtime security](https://docs.wasmtime.dev/security.html)

`WasiCtxBuilder` 默认没有 preopened directory；host 必须把具体 host directory 映射为 guest path，并分别授予 directory/file permissions。WASI 阻止用 `..` 越出 preopen 根，因此只读 input 和可写 staging 可以成为两个独立 capability。[Wasmtime WasiCtxBuilder](https://docs.wasmtime.dev/api/wasmtime_wasi/struct.WasiCtxBuilder.html)

网络同样由 host 决定是否提供。Wasmtime CLI/API 把 `inherit_network`、DNS lookup、TCP 和 UDP 作为显式开关；不把 sockets imports/capability 暴露给 guest，就可以做到无网络。[Wasmtime WASI options source](https://docs.wasmtime.dev/api/src/wasmtime_cli_flags/lib.rs.html)

资源限制需要 host 主动配置。`Store::limiter`/`StoreLimits` 可以限制 linear memory、tables 和 instances；Store 默认不会额外限制 linear memory 的增长。Fuel 能确定性地在相同程序/输入/预算下中断同一位置，epoch timeout 开销更低但不确定。[Wasmtime Store](https://docs.wasmtime.dev/api/wasmtime/struct.Store.html) [Interrupting Wasm](https://docs.wasmtime.dev/examples-interrupting-wasm.html)

### 对 SheJane 的判断

- 直接 Wasmtime 的优点是边界最清晰：WIT/import、preopen、Store、fuel 都由 host 强制。
- 成本是 SheJane 需要自行设计 ABI、Python binding、文件传递、错误映射和每个平台打包。
- 如果选择直接 Wasmtime，Phase 0 必须显式设置 memory/instance limits、fuel 或 epoch deadline；不能依赖默认 Store。

## 6. Extism：更接近 Phase 0 的 Wasm 插件框架

Extism 在 Wasm 之上提供 Host SDK/PDK、插件函数调用、Manifest、host-owned config、Host Functions、timeout 和内存限制。Host Function 以 WebAssembly import 注入，是宿主选择性暴露数据库或其他能力的入口。[Extism Host Functions](https://extism.org/docs/concepts/host-functions/) [Extism Runtime APIs](https://extism.org/docs/concepts/runtime-apis/)

Manifest 可以设置 module SHA-256、最大 memory pages、HTTP response/var 大小、允许的 HTTP hosts 和 host→guest 文件路径。`allowed_hosts` 留空时 HTTP 全部失败；`allowed_paths` 留空时不给文件访问，且只有启用 WASI 后路径映射才生效。[Extism Manifest](https://extism.org/docs/concepts/manifest/)

Extism CLI/SDK 还能设置 wall-clock timeout；WASI 是显式开关。官方说明 host 拥有配置，plugin 可以读取但不能修改 runtime config；Extism 自带的非 WASI HTTP 也仍由 allowlist 控制。[Extism CLI](https://extism.org/docs/install/) [Extism configuration](https://extism.org/docs/concepts/configuration/)

### Phase 0 取舍

| 维度 | 直接 Wasmtime | Extism |
| --- | --- | --- |
| ABI | WIT/Component Model 可做强类型，但需自行设计 | 输入 bytes → 导出函数 → 输出 bytes，Host SDK 已封装 |
| Capability | imports、WASI preopen、sockets、fuel 最细 | Host Functions、WASI 开关、allow_hosts/paths，足够覆盖 v1 |
| 资源 | Store limiter + fuel/epoch，控制最强 | memory pages + timeout + response/var limits，接入更快 |
| Python Runtime 接入 | 需直接维护 wasmtime binding/ABI | 有官方 Host SDK，接近插件调用模型 |
| Phase 0 风险 | 规范和实现工作量较大 | 需要接受 Extism ABI/PDK，并防止 Host Function 授权过宽 |

建议先用 Extism 实现 Archive fixture，验证公共 Action envelope 是否足够。初始策略：`with_wasi=false`、零 Host Function、空 `allowed_hosts`、空 `allowed_paths`；需要文件时只映射只读 input 与单独可写 staging。若 Extism 无法满足 deterministic fuel、WIT 兼容或打包要求，再以相同 ActionExecutor conformance tests 替换为直接 Wasmtime。

## 7. Managed Worker：协议、监督与真实隔离边界

### 7.1 IPC 建议

MCP stdio 已给出成熟的本地子进程规则：host 启动子进程；stdin/stdout 只传 UTF-8 JSON-RPC；每条消息换行分隔且不能含嵌入换行；stderr 可记录日志；stdout 不得混入其他文本。[MCP transports](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)

MCP 同时定义初始化版本/能力协商、ping、progress token、request cancellation 和 shutdown。Cancellation 是竞态友好的 best effort，接收方应停止工作并释放资源；超时后调用方停止等待。SheJane Worker 协议无需变成 MCP server，但可以复用这些已经验证的控制语义。[MCP lifecycle](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle) [MCP cancellation](https://modelcontextprotocol.io/specification/2025-11-25/basic/utilities/cancellation) [MCP progress](https://modelcontextprotocol.io/specification/2025-11-25/basic/utilities/progress) [MCP ping](https://modelcontextprotocol.io/specification/2025-11-25/basic/utilities/ping)

Pi 也提供一个官方 subprocess 集成参考：`pi --mode rpc` 用 stdin 接收 LF 分隔 JSONL command、stdout 返回 response 与异步 event，以 request `id` 和 `toolCallId` 关联调用，并提供 `abort`；SDK 将 RPC 列为需要 process isolation 时的集成选项。它证明 JSONL 适合作为本地 Agent/host 控制面，但 Pi RPC 本身没有业务幂等或文件/网络 sandbox，因此只能借鉴 framing 和生命周期。[Pi RPC](https://pi.dev/docs/latest/rpc) [Pi SDK](https://pi.dev/docs/latest/sdk) [Pi RPC client source](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/src/modes/rpc/rpc-client.ts)

Phase 0 Worker frame 建议保持最小：

```text
initialize(protocol_version, plugin_id, digest, actions, granted_capabilities, limits)
invoke(id=operation_id, action_id, input_json, input_refs, staging_dir)
notifications/progress(operation_id, completed, total?, message?)
notifications/cancelled(operation_id, reason)
result(operation_id, output_json, artifact_candidates[])
shutdown()
```

- 每个 frame 有硬字节上限；未知字段、未知 method 和嵌套深度拒绝。
- stdout 只允许协议；stderr 有界采集并标记 plugin/digest/operation_id。
- 输入引用包含 MIME、size、SHA-256 和只读 guest path。
- 产物只返回 staging 内相对路径、MIME、size 和 digest；Runtime 重算并验证，worker 不能直接创建权威 Artifact。
- `operation_id` 来自现有 ToolExecutionMiddleware，worker 不自行生成幂等身份。
- 大内容不放 JSON-RPC；只经只读 input/staging 文件交换。

### 7.2 生命周期建议

Phase 0 采用**每次 Action 一个短命进程**，不做后台常驻或跨 Action 内存复用：

1. P10 在 receipt 进入 `running` 后创建私有 staging 和最小环境。
2. 启动 worker，完成 initialize 后只允许一次 invoke。
3. 超时或取消先发通知并给极短 grace period，随后终止整个进程树。
4. 进程退出后关闭 pipe，校验 result 与 staging。
5. 只有校验成功才把 ArtifactCandidate 交给现有 Artifact/receipt 流。
6. P11 再确认没有遗留 child、pipe 或 staging lease。

这比进程池启动成本高，但能显著减少状态串扰、泄漏、版本混用和清理歧义。Office fixture 稳定后，才能以同一 digest、单并发和显式 reset 的 conformance test 评估池化。

### 7.3 独立进程能解决什么

- crash 不直接破坏 Python Runtime heap。
- stdout/stderr、环境和 cwd 可独立设置。
- 可施加 wall-clock timeout、输出上限和进程树清理。
- Windows Job Object 可把 child tree 作为一个单元管理，限制 working set/CPU 并在关闭 handle 时终止整个 tree。[Microsoft Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)
- POSIX 可用新 session/process group 管理 descendants，并使用 Unix resource limits；这属于监督和资源治理，不是文件/网络权限模型。[Python subprocess](https://docs.python.org/3/library/subprocess.html#popen-constructor) [Python resource limits](https://docs.python.org/3/library/resource.html)

### 7.4 独立进程不能解决什么

普通 child process 默认继承当前用户的文件、网络和进程权限。清空 env、改变 cwd、HITL、Job Object 或 `setrlimit` 都不能阻止它直接打开用户文件或 socket。

跨平台的强制边界不是一个通用 `subprocess` flag：

- **macOS**：sandboxed app 的 child 可以继承 parent App Sandbox，但 Apple 明确说明普通 child 不具备 XPC 的 privilege-separation 安全性；XPC service 可拥有独立、更窄 entitlement，且由 launchd 管理生命周期。任意第三方 Python/Node payload 不能自动变成一个独立签名的 XPC service。[Apple Process](https://developer.apple.com/documentation/foundation/process) [App Sandbox inheritance](https://developer.apple.com/library/archive/documentation/Miscellaneous/Reference/EntitlementKeyReference/Chapters/EnablingAppSandbox.html) [XPC](https://developer.apple.com/documentation/xpc)
- **Windows**：Job Object 是进程树/资源管理；真正的文件、registry、network、credential 和 process isolation 需要 AppContainer capability 与 ACL 配置。[AppContainer isolation](https://learn.microsoft.com/en-us/windows/win32/secauthz/appcontainer-isolation) [Launch an AppContainer](https://learn.microsoft.com/en-us/windows/win32/secauthz/implementing-an-appcontainer)
- **Linux**：Landlock 能让非特权进程限制自身及 children 的 ambient filesystem/network rights；bubblewrap 能组装 user/mount/PID/network namespaces 和 seccomp，但官方强调其安全强度完全取决于调用参数，它不是开箱即用的完整 policy。[Linux Landlock](https://www.kernel.org/doc/html/latest/userspace-api/landlock.html) [bubblewrap](https://github.com/containers/bubblewrap)

因此 Phase 0 必须把 `managed_worker` 的安全状态表示为 `process_isolated` 或 `trusted_worker`，不能写成 `sandboxed`。若产品允许任何第三方 worker，必须先完成三个平台的强制 isolation adapter 与逃逸测试；否则默认策略应拒绝非受信任来源的 Managed Worker。

## 8. 能力对照与 Phase 0 决策

| 能力 | SheJane 当前 | pi | LangGraph/LangChain | Deep Agents sandbox | Wasmtime/Extism | Phase 0 决策 |
| --- | --- | --- | --- | --- | --- | --- |
| 工具注册 | 静态 builder + MCP 固定快照 | 动态同进程注册/override | Tool/ToolNode | backend 注入 fs/execute | 导出函数/host imports | PluginCatalog 固定 Action descriptor，再适配成现有 Tool |
| 版本冻结 | 图/MCP/模型已有，插件没有 | package ref 可 pin | 最新 graph code 作用于恢复线程 | sandbox ID 可关联 | module digest 可固定 | Run 原子冻结 plugin digest + action schema hash |
| HITL | 已有参数级审批 | extension 自建 | 官方 middleware | 可叠加 middleware | 不负责 | 复用现有 ToolReviewMiddleware |
| 幂等/恢复 | receipt + outcome_unknown | 无通用协议 | checkpoint/task result，不保证外部 exactly-once | 无 | 不负责 | 复用 operation_id/receipt；副作用结果不明必须对账 |
| 文件边界 | 文件工具有，shell 无 | full system access | 不负责 | provider 决定 | preopen/allow_paths 可强制 | Wasm 明确 preopen；Worker staging 不代表 OS sandbox |
| 网络边界 | web/MCP 受工具策略，shell 可直接联网 | full system access | 不负责 | provider 决定 | sockets/import 或 allow_hosts | v1 Wasm 无网络；Worker 只有平台 sandbox 后才能声称无网络 |
| CPU/内存 | Run budget，但 shell 无硬资源限额 | extension/OS 决定 | node timeout/retry | provider 决定 | Store/fuel/epoch 或 Extism limits | Wasm 强制；Worker timeout + OS resource adapter |
| 故障隔离 | Agent/工具同 Runtime；MCP 是子进程 | extension 同进程 | 编排恢复 | remote container/VM | Wasm sandbox | Worker 独立进程；Wasm 每次 invocation 新 instance |
| Artifact 原子性 | 已有持久 Artifact/receipt | 无通用协议 | state/checkpoint | upload/download | host 自行实现 | staging → 校验 → P12 commit |

## 9. 推荐的 Phase 0 spike 与 Gate

### 9.1 Spike A：Archive Wasm Action

使用 Extism Host SDK，验证：

- module digest 固定并在执行前复核；
- 无 WASI、无 Host Function 时无法读文件或联网；
- 只读 input 与可写 staging 分别授权，`..`/symlink/absolute path 逃逸失败；
- timeout、memory pages、输入/输出/frame/Artifact 数量和总大小上限生效；
- 每次 invocation 新 Plugin instance；同 input、digest 和 config 得到相同结构化结果；
- guest 输出全部按不可信数据校验，Runtime 重算 artifact digest；
- Runtime 在 invoke 前后崩溃时，receipt 恢复不会重复提交已完成 Artifact。

### 9.2 Spike B：Documents Managed Worker

先只允许仓库内 fixture/固定摘要，验证协议而非宣称第三方 sandbox：

- 最小 env 不含模型密钥、credential store 或 Runtime token；
- bounded JSON-RPC stdio、initialize 版本不兼容、stdout 污染、stderr flood 全部 fail closed；
- crash、hang、timeout、cancel、child spawn 后整个进程树都能回收；
- 只接受 staging 内候选产物；临时或损坏 DOCX/XLSX/PPTX 不晋升；
- 进程结束到 receipt settlement 之间崩溃会进入 `outcome_unknown`/对账，而不是自动重跑；
- 当前 Office golden fixtures 与迁移后输出做结构、公式、样式和可打开性对比。

### 9.3 Spike C：三个桌面平台隔离可行性

分别证明而不是用统一布尔值掩盖差异：

- macOS：打包后的 Runtime/Helper 是否能使用 App Sandbox/XPC；插件 payload 如何签名与授权动态文件。
- Windows：AppContainer profile、capability、staging ACL、Job Object 和 child inheritance。
- Linux：bubblewrap namespace policy 或 Landlock ruleset，缺失 kernel capability 时的 fail-closed 行为。

若任一平台无法强制“无宿主文件、无网络、无凭据”，该平台的第三方 Managed Worker 必须保持 disabled；独立进程 fixture 仍可作为受信任官方组件运行，但 UI 和 manifest 不能标记为 sandboxed。

### 9.4 Phase 0 Gate

只有同时满足以下条件才冻结 manifest/action protocol：

1. 同一个 Action envelope 能表达 Archive Wasm 与 Documents Worker，不暴露通用 shell。
2. 两个 adapter 都通过同一 operation_id、receipt、Artifact staging 和错误分类 conformance tests。
3. Wasm 默认 capability 是空集合；需要文件时只显式授予 input/staging。
4. Managed Worker 的 `process_isolated` 与 `sandboxed` 在 schema/UI 中是不同状态。
5. 非受信任 Managed Worker 的平台策略 fail closed，而不是“提示用户后拥有完整用户权限”。
6. 恢复/取消/超时测试证明不会盲目重复副作用或提交半成品。
7. 当前 Office、附件、图片 profile 与 MCP 能力不因插件 spike 回归。

## 10. 最终选择清单

| 问题 | 建议选择 | 不选择 |
| --- | --- | --- |
| Extension model | Runtime-owned PluginCatalog + PluginToolAdapter | pi 式同进程任意 extension API |
| Durable execution | 继续 LangGraph sync checkpoint + SheJane receipt | 把 checkpoint 误当 exactly-once |
| Action interface | 结构化 ActionExecutor | `BaseSandbox.execute(command)` 作为公共插件协议 |
| Wasm v1 | Extism spike；公开 kind 暂用 `wasm` | 未验证就冻结 `wasi` 或直接开放 Host Functions |
| Wasm capability | 默认空；显式 RO input / RW staging；无网络 | inherit env/network、工作区整目录读写 |
| Worker IPC | 每 Action 短命进程；bounded JSON-RPC stdio | 长驻 runtime、自定义无版本文本协议、大文件走 stdout |
| Worker trust | 官方 fixture 可 `trusted_worker`；第三方默认禁用 | 把普通 subprocess 宣称为 sandbox |
| Worker cleanup | process tree + timeout + cancel + P11 静止证明 | 只 kill 直接 child 或只等进程自行退出 |
| Remote sandbox | 后续可选 enterprise executor | Phase 0 默认依赖 LangSmith/Daytona/Modal 等云服务 |

这一选择保留了 SheJane 已有的确定性执行优势，同时把 Phase 0 的新增风险限制在两个可替换 Adapter。最重要的是：**插件分发、Action 幂等、Wasm capability 和 Worker OS sandbox 必须分别验证，不能用一个“插件已签名”或“独立进程”标记替代其他三层。**
