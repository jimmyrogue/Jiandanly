# Runtime 插件平台实施计划

- 状态：In Progress（Phase 0-4 complete；Phase 5-7 参考实现与平台发布 Gate 继续推进）
- 日期：2026-07-15
- 对应 ADR：[ADR-0001：Runtime 原生插件平台](../adr/0001-runtime-plugin-platform.md)
- 2026-08-01 更新：本计划中“macOS VM 随 Client 打包、Main 注入 manifest、release workflow 执行 VM Gate”的内容均为历史验收记录，不再描述当前发行路径。当前 Client 不含 VM 资产，Managed Worker 受 packaged backend 与 release Gate 双重阻断；现状以 [`managed-worker-isolation.md`](../plugins/managed-worker-isolation.md) 为准。
- 调研依据：
  - [Agent 插件系统调研](../agent-plugin-systems-research.md)
  - [插件系统案例与方案调研](../plugin-system-landscape-research.md)
  - [Phase 0 Runtime 能力调研](../plugins/phase0-runtime-capability-research.md)

## 1. 计划目标

把 ADR-0001 分阶段落地，在不建立第二套 Run Loop、不改变 Runtime 权威状态边界、不静默切换模型的前提下，交付：

1. 可安装、可版本化、可回滚的插件控制面。
2. 一个 ActionExecutor Interface 和 WASI、Managed Worker 两个生产 Adapter。
3. 与现有审批、收据、重试和 Artifact 协议整合的 Action 执行路径。
4. Client “插件”Tab。
5. Composer 中结构化的 `@插件` 和 `/插件:命令`。
6. Documents、Spreadsheets、Presentations 三个 Office 插件。
7. 后续可承载 PDF、图片、音频和视频的 MIME/Artifact 基础。

本计划追求最终最优结构，不以“最快把现有 Office 包起来”为目标。

## 2. Runtime 阶段记录

- `primary_stage`：P6，绑定资源并取得 Agent 定义。
- 上游：P5，从权威存储恢复只读 execution context。
- 下游：P7，以固定 Agent definition 启动或恢复 LangGraph。
- 受影响阶段：P2、P3、P10、P11、P12。
- Canonical state owner：
  - 插件安装和启用状态：Runtime PluginRegistry。
  - 单次 Run 插件版本：Run 的 frozen plugin bindings。
  - 执行资源：P6 的 PluginExecutionLease。
  - Action 结果：现有 tool receipts 与 artifacts。
- 被替换旧路径：
  - `agent/builder.py` 直接拼装全部静态工具。
  - `tools/registry.py` 静态注册 Office。
  - `tool_visibility.py` 的 Office 专属判断。

实施过程中不得新建第二套 P1-P12 编号。每次修改当前流程后，同步更新 `docs/run-loop.md`；只有目标阶段契约本身改变时才修改 `docs/harness-runtime-stages.md`。

## 3. 交付原则

### 3.1 先冻结契约，再迁移重型能力

先用两个 reference fixture 验证公共契约：

- Archive：小型 WASI 插件，验证沙箱执行、输入只读和 Artifact 输出。
- Documents：Managed Worker 插件，验证重型依赖、进程隔离和真实文档质量。

两者都通过后，才把 Action Interface 视为可稳定发布。

### 3.2 Runtime 保持唯一权威

- Client 不扫描插件目录。
- Client 不直接启动 Worker。
- Client 不推断真实启用版本。
- 插件变更统一提交 Runtime Command。
- Pydantic schema 继续作为 Runtime SDK 的协议真相源。

### 3.3 一个执行 Seam

WASI 和 Managed Worker 都实现 ActionExecutor Interface。PluginToolAdapter 是 Action 进入现有 ToolExecutionMiddleware 的唯一 Adapter，不允许每个插件自己实现审批、重试和持久化。

### 3.4 版本先于便利

- Run 接受时冻结精确摘要。
- 更新不影响已接受 Run。
- 缺失旧版本时明确失败。
- v1 手动更新，不做静默自动更新。

### 3.5 Office 以结果质量为验收标准

迁移不是“工具能够调用”即完成。必须比较生成文件的结构、格式、公式、样式、渲染结果和错误恢复。

## 4. 总体完成定义

只有同时满足以下条件，插件平台 v1 才算完成：

- WASI、Managed Worker 两种执行类型及各自请求的权限能够被正确展示和强制执行。
- 签名状态、发行者、执行类型和能力授权彼此独立。
- 同一 Run 在恢复或 Tool receipt replay 时不会换用新插件版本。
- `@插件` 和 `/插件:命令` 以结构化协议到达 Runtime。
- steering 不能改变活动 Run 的固定插件目录。
- WASI 插件无法访问未授权文件、网络、Shell 或宿主密钥。
- Managed Worker 崩溃、超时和输出违规不会影响 Runtime 主进程或提交脏 Artifact。
- 非受信任 Managed Worker 在当前平台缺少强制 OS isolation adapter 时拒绝启用；普通子进程不得标记为 `sandboxed`。
- 安装、更新、回滚和移除具有 CommandReceipt 幂等性。
- Documents、Spreadsheets、Presentations 达到既定 golden fixture 质量。
- Runtime 核心不再静态注册 Office 工具。
- `make test`、`make build` 和 `git diff --check` 全部通过。
- 打包后的 macOS、Windows、Linux Runtime 至少完成对应 runner 的安装和执行冒烟验证。

## 5. 阶段总览

| Phase | 目标 | 对外可见结果 |
|---|---|---|
| 0 | 冻结契约与威胁模型 | ADR 可转 Accepted |
| 1 | PluginRegistry 与控制面 | 可安全安装、查询、启停和回滚本地测试插件 |
| 2 | P3/P6 固定目录 | Run 能冻结并恢复精确插件版本 |
| 3 | P10 Action 执行 | WASI 与 Managed Worker 共用收据和 Artifact |
| 4 | Composer、SDK 与 Plugin Tab | 用户可管理并在对话中直接指定插件 |
| 5 | Office 插件迁移 | Documents、Spreadsheets、Presentations 脱离核心 |
| 6 | PDF 与多模态基础 | 图片、音频、视频可经插件转成结构化内容与 Artifact |
| 7 | 开发者制作与文件分发 | 开发者规范、打包检查和本地导入完整 |

Phase 0-4 是平台主链。Phase 5 证明重型插件方案达到生产质量。Phase 6-7 可以在 Phase 5 稳定后分别排期，但不能反向破坏 Action 契约。

## 6. Phase 0：契约、reference fixtures 与威胁模型

### 6.1 产物

新增：

- `docs/plugins/manifest-v1.md`
- `docs/plugins/action-protocol-v1.md`
- `docs/plugins/security-model.md`
- `docs/plugins/developer-guide.md`
- `docs/plugins/office-behavior-baseline.md`
- `docs/plugins/managed-worker-isolation.md`
- `runtime/plugins/schemas/plugin-manifest.v1.schema.json`
- `runtime/plugins/schemas/plugin-action-input.v1.schema.json`
- `runtime/plugins/schemas/plugin-action-result.v1.schema.json`
- `runtime/plugins/fixtures/wasi-archive/`
- `runtime/plugins/fixtures/worker-documents/`
- `docs/plugins/phase0-runtime-capability-research.md`

### 6.2 工作项

- 冻结插件 ID、SemVer、publisher、Runtime compatibility 和 package digest 规则。
- 冻结 `runtime.execution.kind`，允许开发者选择 `wasi` 或 `managed_worker`，并定义平台兼容性与安装确认规则。
- 定义 manifest 的规范化与签名覆盖范围。
- 定义 ActionInvocation、ActionResult、ArtifactCandidate 和错误 envelope。
- 定义 JSON Schema 方言和拒绝未知字段策略。
- 定义 MIME 匹配、文件 reference 和只读输入协议。
- 定义 effect、determinism、limits 和 capability vocabulary。
- 定义 stable operation identity 的规范化算法。
- 定义 Worker IPC 版本协商、心跳、取消和超时。
- 完成压缩炸弹、路径穿越、符号链接、恶意 schema、供应链、Worker 逃逸、TOCTOU 和产物欺骗威胁模型。
- 完成 WASI runtime 比较 spike：v1 选择直接 Wasmtime Component Model，默认不链接完整 WASIp2。
- 完成可自托管插件签名格式 spike：canonical package SHA-256 + detached Ed25519 envelope。
- 完成三个桌面平台的 Managed Worker 沙箱可行性 spike，并冻结各平台独立验收原则；后续 macOS arm64 VM 证据见 9.7，任何仍有 blocker 的平台继续禁用非受信任 Worker。
- 为当前 Office 实现建立行为清单和 golden fixture 基线；当前 51 项 core behavior tests 是迁移基线，渲染/重算另设生产 Gate。

### 6.3 Gate

- manifest 与 Action envelope 可以表达 Archive 和 Documents 两个 fixture。
- 两个 Adapter 不需要扩大 ActionExecutor Interface。
- Security review 明确 v1 禁止网络、Shell、Secret 和工作区写入。
- Worker 的 `process_isolated`、`access_isolated`、`resource_isolated` 与最终 `sandboxed` 是不同状态，非受信任 Worker 在缺少后两项时 fail closed。
- ADR-0001 评审后从 Proposed 转为 Accepted。

未通过 Gate 前，不对外承诺第三方插件兼容性。

### 6.4 Phase 0 验收结果

- ADR-0001 已转为 Accepted。
- `wasi` 使用直接 Wasmtime Component Model；未定义 import trap，真实文件/网络/时钟/随机能力未开放。
- `managed_worker` 使用单次短命进程与有界 NDJSON JSON-RPC；Phase 0 只证明进程监督，不把它称为权限沙箱。当前各平台状态由 9.7 和 release matrix 单独判定。
- 两个 Adapter 共享 `ActionExecutor.invoke(invocation, input_root, output_root)`。
- canonical package digest、detached Ed25519、stable plugin tool version 已有测试。
- Archive/Documents fixture 入口均真实存在；Office 当前基线为 51 项通过。
- Phase 0 聚焦验证总计 68 项通过。

## 7. Phase 1：PluginRegistry 与控制面

### 7.1 Runtime Module

建议新增：

```text
runtime/src/shejane_runtime/plugins/
├── __init__.py
├── manifest.py
├── registry.py
├── store.py
├── policy.py
├── package_reader.py
└── errors.py
```

职责：

- `manifest.py`：解析和校验版本化 manifest。
- `package_reader.py`：限额解包、路径规范化和摘要计算。
- `store.py`：内容寻址 blob、staging 和原子激活。
- `policy.py`：签名、执行类型、能力和用户确认策略。
- `registry.py`：对外提供安装、启用、更新、回滚、retire 的 Deep Module。
- `errors.py`：协议稳定错误码。

### 7.2 数据库

在 `runtime/src/shejane_runtime/store/sqlite.py` 的迁移体系中新增：

- `plugin_versions`
- `plugin_installations`

必须包含：

- 唯一键和摘要约束。
- 安装状态机与失败回收。
- principal 范围的启用状态。
- source、signature status 和 execution kind 记录。
- created/updated/retired 时间。
- 并存版本与 active digest。

不把插件包二进制或密钥存进 SQLite。

### 7.3 Command 与读取 API

修改协议真相源 `runtime/src/shejane_runtime/api_schemas.py`：

- 新增 plugin install/enable/disable/update/rollback/remove Command payload。
- 新增 PluginSummary、PluginDetail、ActionSummary、PluginCommandSummary。
- 所有对象 `extra="forbid"`，并设置字符串、数组、JSON 深度和包大小限制。

修改 Runtime server：

- `GET /v1/plugins`
- `GET /v1/plugins/{plugin_id}`
- 在现有 `POST /v1/commands` 分发插件变更。

在 `runtime/src/shejane_runtime/runs.py` 的 capabilities 中增加 `plugins`。

运行 `make schemas` 生成 SDK 类型，并为 `runtime/sdk/src/client.ts` 增加对应读取与 Command helper。

### 7.4 安全测试

至少覆盖：

- 同一 idempotency key 重试安装只产生一个版本。
- 相同 digest 重复安装不复制 blob。
- 同版本不同 digest 被明确拒绝。
- 路径穿越、绝对路径、危险链接和大小写碰撞。
- 压缩炸弹、文件数和展开大小限制。
- manifest 未知字段、schema 递归深度和恶意正则。
- 签名有效、无效、过期和发行者不匹配。
- 未签名包需要符合部署策略并经过明确用户确认。
- WASI 与 Managed Worker 都能由普通插件 manifest 声明。
- Managed Worker 未经更高风险确认时拒绝安装。
- 激活事务中断后不出现半安装状态。
- 被引用版本只能 retire，不能物理删除。

### 7.5 Gate

- Runtime 可通过 Command 幂等安装 Archive fixture。
- Client/SDK 可只读列出精确版本、摘要、签名状态、执行类型和 compatibility。
- 签名包和未签名包都遵循同一安装协议；是否允许由用户或部署策略决定。
- 安装目录中没有从来源路径直接执行的文件。

### 7.6 Phase 1 验收结果

- Runtime 已实现安全 ZIP ingestion、严格 manifest/Action schema 校验、内容寻址存储、并存版本、principal 安装状态和 CommandReceipt 幂等事务。
- `plugin.install/enable/disable/update/rollback/remove` 与列表、详情 API 已进入 Pydantic/OpenAPI 真相源；SDK 类型与 typed client methods 已生成。
- 未签名包必须显式确认；签名包通过部署方 trust store 校验 publisher、key ID、撤销和时间窗。Managed Worker 在 OS sandbox 缺失时继续 fail closed。
- Client 左侧新增 lazy-loaded“插件”Tab，只渲染 Runtime 返回的结构化只读目录。
- 插件聚焦测试 27 项、Client 相关测试 13 项、Runtime SDK 测试 8 项通过。
- `make test`、`make build`、`make package-runtime` 和 `git diff --check` 通过；冻结 Runtime 可启动，并从 `/v1/runtime` 宣告 `plugins`、从 `/v1/plugins` 返回权威目录。

## 8. Phase 2：P3 冻结与 P6 PluginCatalog

### 8.1 Run 协议

在 `CreateRunRequest`、相关 fork/retry payload 和 SDK input 中新增：

```text
plugin_refs: PluginReference[]
plugin_command: PluginCommandReference | null
```

约束：

- stable plugin ID 必填。
- `expected_digest` 可选，仅作前置条件。
- 列表去重并有上限。
- 每次提交最多一个 Plugin Command。
- Plugin Command 所属插件自动成为 required binding。

P3 必须在接受 Run 的同一事务中：

1. 校验所有显式选择。
2. 解析当前已启用且兼容的自动可用集合。
3. 冻结精确版本和摘要。
4. 记录选择来源：explicit、command 或 enabled。
5. 写入 `run_plugin_bindings`。

### 8.2 PluginCatalog Module

建议新增：

```text
runtime/src/shejane_runtime/plugins/catalog.py
runtime/src/shejane_runtime/plugins/lease.py
```

PluginCatalog 的唯一主要入口：

```text
acquire_snapshot(frozen_bindings, execution_context)
    -> PluginExecutionLease
```

返回：

- 精确 package handles。
- 固定 Action descriptors。
- 固定 Skills 和 Commands。
- action catalog hash。
- runner leases。

### 8.3 Agent definition

修改 `runtime/src/shejane_runtime/agent/builder.py`：

- 从 PluginCatalog 取得固定目录。
- 通过 PluginToolAdapter 将 Actions 变成工具描述。
- 把插件 ID、digest、Action schema hash 纳入 Agent definition fingerprint。
- 保持 MCPToolCatalog 的固定快照语义。
- 不在 builder 中读取文件系统安装目录或解析 manifest。

### 8.4 恢复、更新和清理

- P7 比较 checkpoint 所需插件 definition。
- 版本缺失返回 `plugin_version_unavailable`。
- 更新期间已接受 Run 保持旧 digest。
- P11 关闭 PluginExecutionLease。
- 非终态 Run 引用计入包垃圾回收。

### 8.5 测试

- 接受 Run 后更新插件，运行仍使用旧 digest。
- 等待审批后恢复，目录 hash 不变。
- Runtime 重启后可按 binding 恢复。
- 旧版本 retired 但仍被 Run 引用时继续可用。
- 物理版本缺失时明确失败，不使用当前 active 版本。
- 显式引用未启用插件时拒绝 Command。
- Plugin Command 引用不存在的 Action 时拒绝。
- fork/retry 的 binding 语义与 checkpoint 兼容。
- PluginCatalog 使用 Fake lease 时 Agent builder 不触碰包存储。

### 8.6 Gate

- 一个无副作用 Fake Action 能进入固定 Agent definition。
- checkpoint 恢复和插件更新竞争测试通过。
- `docs/run-loop.md` 已反映 P3/P6 当前实现。

### 8.7 Phase 2 验收结果

- `CreateRunRequest` 与 Runtime SDK 已支持有界、去重的 `plugin_refs` 和单个 `plugin_command`；显式使用时声明 `plugins` capability。
- P3 在 `accept_run_command` 的同一 `BEGIN IMMEDIATE` 事务内冻结 enabled、explicit 或 command 来源的精确版本、digest 与 Action catalog hash。
- `PluginCatalog` 是 P6 唯一包读取入口：重新计算完整包 digest，返回固定 descriptors 和 `PluginExecutionLease`；精确包缺失明确失败，不回退 active 版本。
- Agent builder 仅消费 lease 中的 descriptors，通过 task-local proxy 注册 Action，并把 aggregate catalog hash 纳入 definition fingerprint。
- checkpoint fork 在插件更新后继续绑定源 Run 的旧 digest，并恢复到相同 graph definition；lease 由执行 `AsyncExitStack` 在 P11 关闭。
- Phase 2 插件聚焦测试 33 项及 Runtime SDK 9 项通过；P10 Action 调用仍由下一阶段接管。

## 9. Phase 3：P10 Action 执行与 Artifact 提交

### 9.1 Module 与 Adapter

建议新增：

```text
runtime/src/shejane_runtime/plugins/execution/
├── __init__.py
├── executor.py
├── tool_adapter.py
├── invocation.py
├── staging.py
├── wasi.py
└── managed_worker.py
```

- `executor.py`：ActionExecutor Interface。
- `tool_adapter.py`：Action descriptor 到现有 Tool 的 Adapter。
- `invocation.py`：operation identity、capability grant 和 envelope。
- `staging.py`：输入 materialization、输出校验和 ArtifactCandidate。
- `wasi.py`：WASI Adapter。
- `managed_worker.py`：Worker IPC Adapter。

### 9.2 现有中间件整合

Action 必须走现有：

- ToolReviewMiddleware
- ToolExecutionMiddleware
- tool receipts
- Artifact persistence
- SSE 进度与终态

不得新增：

- plugin-specific approval store
- plugin-specific receipt table
- plugin-specific retry loop
- plugin-specific Artifact store

需要扩展现有 operation identity，使其包含插件 digest、Action ID、规范化参数、输入文件 hash 和 capability grant hash。

### 9.3 WASI Adapter

- 默认空 capability set。
- 只映射已授权输入和 staging output。
- 实施时间、内存、燃料/CPU 和输出限制。
- 禁止网络和子进程。
- 捕获 trap、超时、取消和非法输出。
- 清理实例和临时目录。
- 不因发行者或安装来源改变其安全模型。

### 9.4 Managed Worker Adapter

- Runtime 主进程外启动。
- v1 每次 Action 启动一个短命 Worker，不复用进程。
- 使用有硬帧限制的 UTF-8 NDJSON JSON-RPC 2.0；stdout 只允许协议帧，stderr 是有界诊断流。
- 支持 hello/version negotiation、invoke、progress、cancel、result、shutdown。
- Worker 超时或协议违规后强制终止并隔离本次 staging。
- 终止必须覆盖整个进程树；P11 验证没有遗留 child、pipe 或 staging lease。
- 任何开发者都可以选择此执行类型；安装时展示实际平台隔离状态和能力。
- 普通子进程仅是故障边界。非受信任 Worker 只有在平台 OS isolation adapter 强制落实文件、网络、凭据、进程和资源策略后才能启用，否则 fail closed。
- 平台 backend 按 [`managed-worker-agent-sandbox-research.md`](../plugins/managed-worker-agent-sandbox-research.md) 独立实现：Linux 使用 bubblewrap + seccomp + cgroup v2 + sized tmpfs；macOS 使用 Virtualization.framework 本地短命 Linux VM；Windows 使用 LPAC/Job 包裹的 QEMU Linux VM、只读 ISO 与固定 RAW ext4 scratch。统一协议，不伪造统一 primitive。
- 包签名不能替代能力授权，也不能使 Worker 获得隐式权限。

### 9.5 测试

共同 conformance tests：

- 相同输入得到符合 schema 的结果。
- 只能读取声明的输入。
- 只能写 staging output。
- 超时、取消、崩溃和非法 JSON。
- 输出 MIME、数量、大小和 hash 校验。
- capability denied。
- completed receipt replay 不二次执行。
- running 后进程失联进入 `outcome_unknown` 或安全失败语义。
- 未提交 staging 在重启后被清理。

Adapter 专项：

- WASI 无网络、无宿主目录、无子进程。
- Managed Worker 崩溃不影响 Runtime。
- 普通子进程不会被错误报告为 `sandboxed`。
- 缺少平台强制隔离时，非受信任 Managed Worker 拒绝启用。
- Worker 版本不匹配被拒绝。
- Worker 日志敏感字段脱敏。

### 9.6 Gate

- Archive fixture 通过 WASI conformance。
- Documents fixture 的一个真实读 Action 和一个真实 Artifact Action 通过 Managed Worker。
- 两个 Adapter 的结果都由同一 ToolExecutionMiddleware 收据恢复。
- P10/P11/P12 的错误和终态无平行实现。

### 9.7 Phase 3 历史验收记录

- WASI Action 已从固定 descriptor 进入真实 Agent 图，经过现有 ToolReviewMiddleware、ToolExecutionMiddleware、`local_tool_receipts` 与 Runtime Artifact 存储。
- P5 冻结附件 size/SHA-256/MIME；插件专属 tool version 包含精确插件、Action schema、输入、capabilities、limits 与 environment，P10 使用中间件已生成的同一 operation ID。
- P10 只授予 v1 平台能力 `input.read` / `artifact.write`，验证 Action input/output schema、结果身份、候选路径、MIME、大小与 digest；staging 在完成、失败或取消后清理。
- WASI 同时验证完整 package digest（P6）与 component digest（P10），并使用 fuel、Store limits 与 epoch deadline；Archive fixture 已通过真实模型→工具→模型链路和 completed receipt replay。
- Managed Worker 继续 fail closed。最新一手资料复核已冻结三种 backend：Linux 使用冻结 Bubblewrap + seccomp + cgroup v2 + sized tmpfs + Artifact broker；macOS 使用 Virtualization.framework 本地短命 Linux VM；Windows 原生 LPAC/Job 路线因普通用户权限下没有硬磁盘额度而停止，改为 LPAC/Job 包裹的 QEMU Linux VM，优先 WHPX、兼容 TCG，并用只读 ISO 与固定 RAW ext4 scratch 保留完整契约。普通子进程、trusted-native、SRT 初始化成功或远程 provider 都不会被偷换成默认 `sandboxed=true`。
- Windows Guest ABI 已改为 `linux/<arch>`。同一 `guestd` 现能确定构建 arm64/amd64；pure-Python Rock Ridge ISO builder 已通过 Unicode、空目录、内部 symlink、权限、确定性和逃逸拒绝检查。Docker Desktop Linux/arm64 上的 QEMU 10.1.5 TCG 已真实启动 AMD64 Guest，在无网卡、只读 package/input ISO、64 MiB scratch 和双 Virtio serial 下跑通完整 Worker 往返与 UID/cgroup/只读/noexec 探针；该证据不属于 Windows host，Windows `proved` 仍为空。
- Runtime 已新增 host-owned `SandboxLimits` 与 `SandboxEvidence`：统一派生 wall/CPU/memory/process/scratch/output/frame budgets，只有 process/access/resource 三层宿主证据同时成立才计算为 sandboxed。Linux/arm64 native backend 已通过生产 Executor seam，证明只读路径、宿主文件/凭据/PID/Unix/TCP/外网隔离、seccomp、禁止嵌套 user namespace、固定 scratch `ENOSPC`、只提升声明 Artifact、原子 cgroup 入组、内存超限、忽略取消的 descendant 清理与 `populated=0`。P6 也已接入包内资产和 systemd delegation preflight；真实 systemd/最终 PyInstaller release job 尚未运行，因此仍由 `systemd_delegation_gate` 与 `release_ci_gate` fail closed。
- macOS 最初在 Apple M4 Pro/macOS 26.5.1 上通过 Virtualization.framework 启动无网络设备的 Fedora 44 ARM64 kernel/initramfs，证明 entitlement、配置和 boot chain 可行；后续条目已补齐固定容量磁盘、Action 协议、guest 资源限制、冻结发布资产、最终打包及静态 Worker 逃逸/清理 Gate。Linux/arm64 Debian 只读 rootfs、真实 Python onedir Worker 和 Node.js LTS Runtime Asset 动态 Gate 也已完成并接入 release workflow；只剩真实 Developer ID/公证发布 runner，Registry 继续 fail closed。
- Runtime 现可用 Go 1.26.5 标准库交叉编译静态 Linux/arm64 `guestd`，并原子生成 metadata 固定的 `newc` initramfs；双构建逐字节一致，真实 VM 已在 1 vCPU/256 MiB/无网络设备下执行 `/init`。release gate 新增 `deterministic_minimal_guest_boot`，但不因此移除 production guest、协议、磁盘与 Gate 阻塞项。
- 精简 guest 现可仅嵌入与 Fedora kernel 配对的三个 VSOCK 模块；1.7 MiB initramfs 已在同一 1 vCPU/256 MiB VM 中完成 `ready → shutdown → stopped` 有界握手和 guest 主动关机。真实门禁随后覆盖 `succeeded`、`failed`、hostile Artifact、忽略取消和非法 JSON，release gate 新增 `virtio_socket_handshake`、`cooperative_guest_shutdown`、`guest_host_protocol`。
- macOS 数据盘路径采用 Firecracker 同类的预格式化 ext4 RAW image，不使用 VirtioFS 宿主目录共享。Host builder 只接受固定摘要与版本的 `mke2fs` helper，归一化输入树并拒绝 symlink/特殊文件；真实 VM 已证明只读 input、非特权 remount 被拒、固定容量 scratch、写满返回 `ENOSPC`、超出 `output_mb` 的 Artifact fail closed，以及成功/失败/取消后的 image staging 清理。release gate 新增 `deterministic_ext4_disk_images`、`read_only_input_mount`、`fixed_capacity_scratch_mount`、`input_output_disk_limits`。
- macOS 新增无第三方依赖的单文件 Swift launcher，固定无网络 VM 配置并做有界 stdio↔VSOCK 转发、wall timeout 和整机停止。release 构建会保留 launcher 专属 Virtualization entitlement，并在最终 `.app` 上运行严格签名与 self-test；包内 launcher 已实际启动 Guest 并完成磁盘证明、`shutdown → stopped` 往返。完整冻结资产集现已随包进入固定路径并由 Runtime 生产 Executor 完成 13-mode 往返，因此 release gate 记录 `packaged_launcher_entitlement`、`packaged_launcher_vm_transport` 与 `packaged_vm_asset_set`，旧 `packaged_launcher` blocker 已移除。
- macOS Guest 已在真实 VM 中挂载 cgroup v2 并精确写入、回读 memory/swap/OOM-group、pids 与 CPU policy；launcher 只有完成 `ready → configure → configured` attestation 才公开 ready。真实 descendant 攻击随后证明 16 MiB 上限触发 group OOM 并由 Guest 映射为 `resource_exhausted`，第 16 个 descendant 被 `pids.max` 拒绝且 shutdown 后 leaf 达到 `populated 0`；CPU 同时受 1 vCPU VM 与 `cpu.max=100000 100000` 封顶。release gate 新增 `guest_cgroup_v2_resource_policy`、`hard_cpu_memory_process_tree_limits`。
- macOS host 的 Managed Worker package 与 Runtime Asset 现按 `linux/<arch>` guest ABI 选择，release gate 仍按 `darwin/<arch>` host backend 判定。VM 新增独立只读 executable package disk；`/input` 保持只读 `noexec`，scratch 保持固定容量读写。真实三盘 VM 往返已通过并新增 `read_only_package_mount`；后续条目已补齐 package/runtime-asset staging 与非特权 Worker exec。
- macOS Guest 现以 UID/GID 65534 启动静态 Linux Worker，并用 `UseCgroupFD` 原子加入已验证的 leaf；真实 `initialize → invoke → shutdown` 已证明授权 input 读取、package/input 拒写、scratch Artifact 写入及 `cgroup.kill → populated 0 → rmdir` 清理。环境驱动黑盒 pytest 直接调用生产 `ManagedWorkerActionExecutor`，成功、失败、hostile symlink、取消、malformed-frame、descendant OOM、PID exhaustion、宿主逃逸和 hard-crash recovery 路径均实跑通过；生产 Runtime factory、冻结打包资产与 P1 Client 正常入口均已接线。
- Artifact 不由 Host 解析 ext4，而由 Guest agent 通过第二条 VSOCK 在 invoke response 前流式回传；两端都用安全 `openat` 路径链，Host 还复核总量与 SHA-256。成功提取、hostile symlink 和超限 Artifact fail-closed 黑盒 Gate 均已通过，release gate 新增 `vsock_artifact_extraction`；多 Artifact/大文件及最终 Runtime Artifact 提升仍由后续平台/产品 Gate 覆盖。
- macOS release evidence 已按架构拆分：上述真实证明只属于 `darwin/arm64`；`darwin/amd64` 的 proved 集合为空并保留 `architecture_conformance_gate`，ARM64 证据不能再误开放 Intel 安装包。
- Guest 控制代理识别 Runtime 的版本化 `cancel` 帧：先给 Worker 50 ms 协作窗口，忽略取消的 Worker 随后由 `cgroup.kill` 连同 descendants 清理；只有 `populated 0`、leaf 删除并发送 `stopped` 后 launcher 才成功退出。真实 VM 使用故意忽略取消的 Worker 验证 Runtime 生产取消路径在一秒窗口内完成，release gate 新增 `guest_cancel_process_tree_cleanup`；打包 CI 与宿主逃逸 Gate 仍保留。
- macOS VM 资产分发已冻结为 v1 构建时内置、按架构独立的方案；不新增首次运行下载器。Runtime 已实现严格资产 manifest、canonical asset-set ID、host/guest 架构、HTTPS provenance、无 symlink、size/SHA-256/executable preflight；Client 从包内固定路径通过 CLI 注入，P6 只对含 Managed Worker 的冻结 lease 加载一次，失败返回 `executor_unavailable`。`darwin/arm64` 构建器现在验证 Fedora 已签名 RPM/SRPM 与 e2fsprogs OpenPGP 签名，冻结全部原生构建/验证工具，并产出逐字节可复现的 kernel/initramfs/host-native `mke2fs`/launcher/manifest/SBOM/许可证。Electron Builder 按 `${arch}` 只装入匹配资产；最终 `.app` 资产与构建输出一致，生产 Executor 用包内 manifest 的 14-mode 静态 Worker Gate 全部通过。Worker 与 descendant 的宿主文件/credential/PID/Unix socket/loopback/外网探针，以及 Worker crash cleanup 均已实跑；可继承 `flock` lease 还证明 Runtime `SIGKILL` 后不会抢删活跃 VM，launcher 退出后下一次 Runtime 会安全回收孤儿 staging，launcher 被强杀也由 Runtime `finally` 清理。最终 `.app` 已从正常 Client 入口建立 P1 会话、注入包内 manifest 并正常退出 bundled Runtime；此黑盒已接入 release workflow。冻结 Debian rootfs、Python onedir Worker 与经 Node.js 官方签名验证的 LTS Runtime Asset 已分别完成可复现构建和真实 VM 往返，`guest_userspace_runtime` 已转为 proved。发布 workflow 已接入 Developer ID、Hardened Runtime、timestamp、公证/staple 与 Gatekeeper 验证；只有真实 runner `release_ci_gate` 继续关闭生产能力。LibreOffice 与 MuPDF 属于产品 Gate，不再是通用 Managed Worker 沙箱 blocker。
- Phase 3 当前插件聚焦测试 35 项通过。Documents Managed Worker 的生产 Gate 必须等三个平台中对应 adapter 的真实隔离测试通过，不能用开发态子进程替代。

## 10. Phase 4：Composer、SDK 与 Plugin Tab

### 10.1 Composer

复用现有 Lexical 编辑器：

- 在 `SkillEditor.tsx` 增加 `@` typeahead，仅列出已安装且可用插件。
- 在 `/` 菜单增加 Plugin Commands 分组。
- 在 `SkillNode.tsx` 中增加 Plugin 和 Plugin Command token，或在此阶段将其整理为通用 capability token Module。
- 在 `skillDraft.ts` 增加不会与普通文本碰撞的结构化编码。
- parser 返回 `pluginRefs` 和 `pluginCommand`。
- App 构造 `CreateLocalRunInput` 时直接写结构化字段。
- 不把插件选择添加到 `goal` 或 `user_input` 的隐藏 directive。

现有 `SkillEditor` 和 `skillDraft` 已同时承载 Skill、Function、MCP。在本 Phase 完成行为测试后，可以一次性重命名为更准确的 `CapabilityEditor` 和 `composerDraft`，避免长期保留误导性命名；重命名不得与功能变更混在同一个难以审查的提交中。

### 10.2 交互语义

- `@` chip 展示插件名，内部保存 stable ID 和当时看到的 digest。
- `/插件:命令` chip 保存 plugin ID 和 command ID。
- 删除 chip 会同步删除结构化引用。
- 多个相同 `@` 自动去重。
- 命令最多一个；选择新命令替换旧命令并显式提示。
- 插件卸载或版本变化后，旧 draft 标记为 stale，提交时由 Runtime 最终判断。
- steering 模式禁用新增 Plugin token，并解释“新插件需要新建一次运行”。
- 发送后的历史消息从 Runtime 规范化元数据渲染，不依赖本地 draft。

### 10.3 Plugin Tab

建议新增：

```text
client/src/features/plugins/
├── api/
├── components/
├── hooks/
├── pluginStore.ts
└── types.ts
```

Phase 4 最初只包含 Installed 与 Local import。Phase 7 的自托管来源协议现已接入同一页面：用户可以添加签名来源、手动刷新、查看包、安装精确版本和移除来源。

操作经 pending Runtime Command 提交：

- install
- enable/disable
- update
- rollback
- remove
- source add/refresh/install/remove

详情使用宿主 UI 展示：

- 版本、摘要、发行者和签名状态。
- WASI 或 Managed Worker 执行类型及风险说明。
- Actions、Skills、Commands。
- MIME 类型。
- 权限和资源限制。
- compatibility 与安装错误。

遵循 `docs/ui/shejane-design-system.md`：warm paper + ink；seal red 只用于品牌、运行和关键状态；moss 只用于在线和成功；文件类型使用单色字形。

### 10.4 SDK

- 更新 generated protocol types。
- `CreateLocalRunInput` 支持结构化插件字段。
- 插件列表和详情有 typed client methods。
- 变更操作复用 Command API。
- Client contract tests 校验发送 body 与 Pydantic schema 一致。

### 10.5 测试

重点文件：

- `client/src/features/chat/components/Composer.test.tsx`
- `client/src/features/chat/skillDraft.test.ts`
- `client/src/features/chat/components/ConversationSidebar.test.tsx`
- `client/src/runtime/runtime-tools.contract.test.ts`
- `client/src/runtime/client.contract.test.ts`
- `runtime/sdk/src/client.test.ts`

场景：

- `@` 搜索、键盘选择、删除、撤销和粘贴。
- `/` 命令搜索和唯一性。
- stable ID 与显示名分离。
- 名称冲突、重名发行者和禁用插件。
- draft 恢复与 stale digest。
- 正常发送 body 中存在结构化字段且 goal 未被注入。
- steering 禁止新增插件。
- Runtime 拒绝时 UI 保留用户输入并展示可恢复错误。
- Plugin Tab 的 pending/accepted/rejected 投影。

### 10.6 Gate

- 用户可只用键盘完成 `@插件` 和 `/插件:命令`。
- Network contract 测试证明没有 prompt directive 注入。
- Client 重启后插件状态从 Runtime 重建。
- Plugin Tab 不执行任何插件自定义 UI。

### 10.7 Phase 4 当前验收结果

- Composer 已增加 `@` 插件搜索和 `/` Plugin Commands 分组；私有字符 token 内分别冻结 stable plugin ID、显示名、command ID 与当时的 package digest。
- parser 直接产生 `pluginRefs` / `pluginCommand`，App 写入 `CreateLocalRunInput`；插件选择不进入 `goal` 或 `user_input` 的隐藏 directive。
- 相同插件引用按 stable ID 去重；新命令替换旧 Plugin Command 节点；恢复草稿会比较当前 digest 并显示 stale 提示，steering 模式禁用新增插件并提示新建任务。
- Plugin Tab 使用宿主 React UI 展示版本、digest、发行者、签名、执行类型、Actions、Commands、MIME、capabilities 和 limits；支持本地包选择、安装、启停、更新、历史版本回滚与移除。
- Runtime `PluginDetail` 新增当前身份可见的不可变版本历史；插件生命周期操作进入现有 IndexedDB pending Runtime Command 队列，按 command ID 幂等重投，非重试错误结算后删除。
- P3 会丢弃客户端提供的 `plugin_selection`，根据已经通过接纳校验的精确绑定重建名称、digest 与命令标题并写入用户消息 metadata；Client 历史、编辑重发和重新生成均使用这份 Runtime 规范化选择。
- `@` 与 `/` 菜单只展示启用、兼容且未退役的插件；已覆盖键盘选择、同名发行者区分、stable ID 去重、命令唯一替换、stale、steering 和原子删除。
- Phase 4 当前聚焦验收：Client Composer/Plugin/Runtime projection/local outbox 100 项、Runtime SDK 10 项、Runtime 插件 36 项通过；完整发布 Gate 仍在 Phase 5/6 统一执行。

## 11. Phase 5：Office 插件迁移

### 11.1 目标插件

源码建议：

```text
runtime/plugins/office/
├── documents/
├── spreadsheets/
└── presentations/
```

每个插件有独立：

- manifest
- Managed Worker
- 依赖锁定
- Action schema
- golden fixtures
- 许可证与 SBOM
- 平台打包产物

这些插件遵循与其他开发者插件相同的 manifest、安装、授权和执行协议，不拥有隐藏权限或专用 Adapter。

### 11.2 Documents

先迁移 Documents，因为它能验证结构化文档、渲染和重型依赖。

验收至少覆盖：

- 读取段落、标题、表格、批注和基础元数据。
- 创建和修改文档。
- 样式、列表、分页、页眉页脚。
- 渲染为 PDF/PNG Artifact。
- 不修改原始输入。
- 对损坏、加密和不支持特性的文档给出稳定错误。

引擎选择：每个平台发布一个由三个 Office Worker 精确摘要引用的共享 Runtime Asset，固定 LibreOffice headless、MuPDF、字体基线、许可证与 SBOM；每次 invocation 使用私有 user profile。结构编辑仍按 OOXML 契约完成，LibreOffice 负责独立 reopen、PDF 导出和后续 Calc 重算，PNG 由同一 Asset 中固定的 PDF renderer 生成。不得探测用户机器上的 LibreOffice/Microsoft Office 作为运行时 fallback。LibreOfficeKit 仅在 PDF/render Gate 证明现有路径不足时替换，不并行维护第二条渲染路径。

### 11.3 Spreadsheets

验收至少覆盖：

- 工作表、范围、单元格类型和公式读取。
- 新建、修改、格式和图表。
- 公式与缓存值的明确区分。
- 大表格的范围化读取与输出限制。
- XLSX Artifact 和可预览渲染。
- 日期、时区、精度和区域格式 golden cases。

### 11.4 Presentations

验收至少覆盖：

- 幻灯片、文本、图片、表格和备注读取。
- 创建和修改 deck。
- 主题、布局、母版和字体 fallback。
- 页面预览与 PDF Artifact。
- 不支持动画/媒体时的明确降级记录。

### 11.5 切换与删除

每个插件迁移遵循：

1. 当前 `office.py` 生成契约与 golden baseline。
2. 新插件在测试环境并行比较，不对同一用户操作双写。
3. 质量和性能 gate 通过后，在 feature flag 下切换插件能力来源。
4. 完成一个发布周期验证。
5. 删除对应核心工具注册。
6. 三个插件都完成后，删除 Office 专属 visibility 规则和不再使用的核心依赖。

不得长期保留“核心 Office”和“插件 Office”两套自动 fallback。

### 11.6 Gate

- `runtime/tests/test_tools_office.py` 的行为意图已迁移为插件契约测试。
- 所有 golden fixture 在支持平台一致通过。
- 插件不可用时返回明确错误，不回退到旧核心工具。
- Runtime 基础安装不再携带无关 Office 依赖。
- `docs/operations.md` 和打包说明更新。

### 11.7 当前实施状态

- 已固定并打包 `@anthropic-ai/sandbox-runtime@0.0.65` 候选 launcher；Electron Node bootstrap 解决 ASAR 内 ESM CLI 启动，native vendor 从 ASAR 解包。
- macOS source-tree 与实际 `.app` 产物均通过 descendant conformance：未授权宿主文件、loopback、Unix socket 和宿主进程探测被阻止，package/input 只读且仅 output 可写。
- Managed Worker executor 已能读取 Client Main 提供的绝对 launcher command，并生成 root-deny SRT policy；Linux 已接入冻结 Bubblewrap、native launcher、seccomp、私有定容 tmpfs、Artifact broker、P6 包内资产校验和 systemd delegation preflight。Linux/arm64 Docker Gate 已完整通过；最终 PyInstaller + systemd release job 尚未真实运行，Windows 全部 native Gate 也仍未完成，因此 PluginRegistry 保持禁用。
- Office 引擎已完成官方方案复核并实现“共享内容寻址 Runtime Asset 中固定 LibreOffice headless + MuPDF”；精确 Asset 契约、安装、P6 租约和 P10 只读映射已完成。Linux/arm64 Asset 验证 LibreOffice OpenPGP 签名，离线双构建 MuPDF，并且两次完整归档逐字节一致；Documents、Spreadsheets 与 Presentations 的 Linux/arm64 PyInstaller onedir Worker、确定性插件包和真实 LibreOffice/MuPDF rich golden 已全部在 macOS arm64 的生产 VM 中通过。发布 workflow 已接入 Asset/Worker 双构建、最终 `.app` VM 资产和三类 golden，但在真实签名/公证 runner 成功前仍不宣称为已发布能力。其他执行 ABI 仍需各自的 Asset、Worker、sandbox 与 golden Gate。
- Managed Worker 资源 Gate 已复核 Codex、Pi、SRT、Deep Agents 与本机 `setrlimit`：macOS 原生进程没有可复用方案能对任意不可信 Worker 提供每次 invocation 的硬内存和进程树配额，因此 `darwin/arm64` 选择 Virtualization.framework 短命 Linux VM。现在静态 Worker 的资源、逃逸与清理 Gate、冻结 Debian 只读 rootfs、invocation 私有且 `noexec,nodev,nosuid` 的 scratch-backed `/tmp`、Python onedir Worker、Node.js LTS Runtime Asset 和三类 Office 动态 Gate 均已完成；Registry 只保留真实发布 runner `release_ci_gate`，不得退回 RSS 轮询、本地原生进程或宿主 Office fallback。

## 12. Phase 6：PDF 与多模态基础

### 12.1 插件划分

- PDF
- Media Foundation
- OCR
- Speech
- Vision

### 12.2 公共协议验证

确认现有 Action 契约能表达：

- `image/*`、`audio/*`、`video/*` 和 PDF MIME。
- 大文件流式输入或 Runtime 管理的 file reference。
- 多 Artifact 输出。
- 进度事件。
- 带时间码的结构化结果。
- provenance：插件版本、模型、输入摘要和生成参数。

### 12.3 最小交付顺序

1. Media Foundation：元数据、抽帧、缩略图、音轨分离。
2. OCR：图片/PDF 转结构化文本。
3. Speech：音频转写与时间戳。
4. Vision：图片/关键帧理解。
5. 跨插件工作流：视频 -> 音轨/关键帧 -> Speech/Vision -> 汇总。

### 12.4 模型规则

- Vision Action 必须显式配置具体模型。
- 本地模型和云模型使用不同配置项，并分别展示来源和风险。
- 云模型必须走 Runtime provider 与 credential store。
- 不使用聊天模型的隐式多模态能力作为 fallback。
- 结果记录 provenance；用户可以知道内容由哪个模型产生。

### 12.5 Gate

- 单模态聊天模型也能通过插件获得可引用的文本和 Artifact。
- 无授权文件和未配置云模型不能被插件访问。
- 大视频不会被整体塞入模型上下文。
- 取消和超时能清理中间文件与 runner。

### 12.6 当前实施状态

- Run 附件已在接纳时流式导入 Runtime-owned、内容寻址的不可变输入存储；插件 Action 只收到 MIME、大小、摘要和 `/input` 虚拟路径。任务附件和 PDF 文件的模型读取上限为 200 MiB，其他 workspace、Skill、Memory 与子任务文件读取上限为 20 MiB。
- 插件文件产物已从 SQLite base64 改为内容寻址正文 + Artifact 目录记录，正文端点支持所属 Run 鉴权和 HTTP Range；桌面端按需下载，不把大正文加载进 DOM。
- Managed Worker `notifications/progress` 已有严格 schema、连续 sequence、帧/phase 上限和 250ms 合并；进度是瞬态 UI 投影，不进入模型上下文或持久事件日志。
- 超时和取消会先发送协作式 `cancel`，短暂等待后再强制清理进程树。WASI byte-map ABI 明确限制为输入、输出各 16 MiB，超限要求使用 Managed Worker，不静默切换执行类型。
- Runtime 生成 provenance，记录精确插件版本/摘要、Action、Operation、输入摘要和显式参数；多 Artifact 会投影为现有 `artifact.created` 事件。
- 同一 Run 内已结算的文件 Artifact 现在可通过后续 Action 的 `input_id` 或有序 `input_ids` 重新绑定为只读输入；Runtime 校验 Run 所有权、MIME、正文类型、大小和 SHA-256，跨 Run/内联/不兼容 Artifact 均不可见。整组 Artifact 摘要同时加强下游 receipt tool version，因此媒体抽帧、OCR、Speech、Vision 可组合而无需共享可写目录。
- Media Foundation 已完成四个严格 Action/schema、真实 `linux/arm64` FFmpeg 8.1.2 Asset、冻结 onedir Worker 和确定性插件包。Asset 从签名验证源码构建，锁定 Debian OCI/toolchain/package closure，禁网且无 GPL/nonfree，双归档一致（archive `1a8e20a1...e93`，canonical `sha256:64026538...4d55`）。生产 VM 已通过 probe、精确缩略图/帧/音频 hash、hostile corpus、取消无部分输出和重放；全部 Gate 已接入最终签名/公证 `.app` workflow。真实 runner 成功前 `release_ci_gate` 与 Registry 继续关闭，其他平台需独立原生 Gate。
- PDF 已完成独立 `org.mupdf.runtime`、三个严格 Action/schema、Managed Worker、确定性插件包及真正匹配 macOS arm64 guest ABI 的 Linux/arm64 Asset/onedir Worker。Asset 包含精确源码、许可证、SBOM、build provenance，冻结 Debian OCI/toolchain/package closure，离线双构建且两份归档逐字节一致。真实生产 VM 已通过 inspect、显式页窗 Unicode 文本、无文本层 OCR 标记、精确选页 PNG golden、hostile/truncated corpus、中途取消无部分输出及取消后重放；不把密码写入 Action provenance，也不把 OCR 或模型 PDF 能力作为隐式 fallback。Asset/Worker/package 与全部 PDF Gate 已接入最终签名/公证 `.app` 的 release workflow，但真实 runner 成功前 `release_ci_gate` 仍关闭；Linux amd64、Windows 需各自的原生资产和 Gate。Office 在独立资产通过同等跨平台 golden 前保留现有组合资产，避免回退已验证基线。
- OCR 已完成严格 schema、Ordered `input_ids`、macOS arm64 原生 Runtime Asset、冻结 onedir Worker 和确定性插件包。固定资产使用 RapidOCR 3.9.1、ONNX Runtime 1.27.0、PP-OCRv6 small、CPU 单线程、三个精确模型和全部 package hash；双归档一致（archive `9fc292f7...fbba`，canonical `sha256:253253d7...1f6e`）。真实门禁已通过确定性重放、简体中文、繁体中文、日文、英文、低对比度、多栏、手写风格、180° 方向和 hostile 图片。Windows 与 Linux 资产必须以新模型锁重新构建并通过各自原生 Gate；真实发布 runner 成功前 Registry 继续 fail closed。
- Speech 已冻结 `whisper.cpp 1.8.6` + `large-v3-turbo Q5_0`、CPU 单线程 greedy、无 fallback，并复用精确 FFmpeg 资产。真实 `linux/arm64` Asset 双归档一致（archive `883900b6...5cdd`，canonical `sha256:dc6ec9da...4f11`），冻结 onedir Worker 和插件包可复现。生产 VM 已通过重复转写/Artifact hash、显式中英文、带确定性背景噪声/双音干扰和四秒停顿的日文 `auto`、66.7 秒且 45% 音量的印度英语技术长文、三类 hostile 音频、取消无部分输出、300 秒双运行预算，以及真实 `media.extract_audio` FLAC Artifact 到 Speech 的文件链路；引擎报告 7,200,001ms 会在 Artifact 创建前拒绝，专名失败证据也确认 `initial_prompt` 不是词典保证。最终 `.app` workflow 已接入，但真实音乐、混合语种/拉丁文字广度、真实编码两小时边界、过量 segment/text/output、其他平台和真实签名/公证 runner 仍待完成，所以 Registry 继续 fail closed。
- Vision Cloud 已形成真实 `linux/arm64` release candidate：幂等 `plugin.model.bind` 只允许绑定具体、启用且声明 `image_inputs` 的 provider/model，并在 Run 接纳/分支时冻结。Managed Worker 双向协议只允许一次有界 `model/vision/invoke`；Runtime adapter 校验授权图片 MIME/size/SHA-256/20 MiB/40MP 预算，从系统凭据库取 key、执行出站脱敏、禁重试并返回规范化 text/model/usage。冻结 onedir Worker 双构建一致、cloud 包检查通过（digest `sha256:33ff82dc...381f8`），生产 VM 已通过 host-call bridge，最终 `.app` release workflow 也已接入；真实签名/公证 runner 成功前 Registry 继续关闭。Local 候选仍明确拒绝：SmolVLM2 500M 虽可复现，但质量 Gate 仅 3/5，中文、图表和品牌图失败；不得发布、不得回退到聊天模型。详见 `docs/plugins/phase6-vision-research.md`。

## 13. Phase 7：开发者制作与文件分发

### 13.1 文件分发

官方和第三方开发者使用相同的 `.shejane-plugin` 文件格式：

- 插件元数据和包摘要。
- 平台产物。
- Runtime compatibility。
- 发行者签名。
- 权限与 MIME 摘要。
- 更新说明。

SheJane 可以随应用提供必要插件；其他插件由开发者自行发布文件，用户下载、分享并本地导入。Runtime 不维护插件商店、远程来源、来源公钥或后台自动更新。

### 13.2 开发者体验

提供独立工具或 Runtime Command：

- init
- validate
- pack
- test
- inspect
- install-local

开发者模板至少包含：

- WASI Action 示例。
- Managed Worker Action 示例。
- JSON Schema 示例。
- Artifact 输出示例。
- conformance test harness。
- 权限和确定性说明。

两种执行类型都向开发者开放。Managed Worker 指南必须明确说明本机代码执行风险、平台打包要求和用户授权要求。

### 13.3 发布检查

- manifest/schema 校验。
- conformance tests。
- 安全扫描。
- SBOM 和许可证。
- 平台二进制签名或明确的未签名状态。
- 可重复构建或构建 provenance。
- package digest 和可选的发行者签名。

### 13.4 Gate

- 本地包损坏或结构不合法时安装失败。
- rollback 可恢复到本地已存在的旧摘要。
- 开发者可以只依赖公开规范构建 WASI 或 Managed Worker 插件。

### 13.5 当前实施状态

- Runtime 已提供 `shejane-plugin validate / pack / inspect`。三条命令复用生产 manifest、canonical digest 和安全解包逻辑；`pack` 原子生成确定性 ZIP 并在写入前后复核源码摘要，`validate`/`inspect` 不执行插件代码。
- Runtime SDK 与 Plugin Tab 支持本地 `.shejane-plugin` 导入、启停、更新、回滚和移除；安装继续校验归档、canonical digest、manifest、兼容性和运行 Gate。
- Runtime 不提供远程来源、索引、来源公钥、后台刷新、自动安装或自动更新。
- `init` 与独立 conformance test runner 继续延后。现有两个 reference fixture 已覆盖模板和仓库内 conformance；只有第三方开发者脱离 SheJane 源码测试的真实需求出现时再增加第二层工具。

## 14. 协议与数据变更清单

| 层 | 变更 | 真相源 |
|---|---|---|
| Runtime capability | 新增 `plugins` | `runtime/src/shejane_runtime/runs.py` |
| Run request | `plugin_refs`、`plugin_command` | `runtime/src/shejane_runtime/api_schemas.py` |
| Plugin reads | list/detail schemas | `api_schemas.py` |
| Plugin mutations | 新 Command payload | `api_schemas.py` + command handler |
| SDK types | generated models | `make schemas` |
| SDK client | list/detail/helpers | `runtime/sdk/src/client.ts` |
| Runtime state | versions/installations/bindings | SQLite migrations |
| User history | 规范化插件引用元数据 | Runtime message metadata |
| Agent definition | digest + action catalog hash | P6 builder/catalog |
| Action result | 现有 receipt + Artifact | ToolExecutionMiddleware |

协议变更必须同时更新 contract tests；不得通过 Client 私有字段绕过 SDK。

## 15. 文件影响地图

预计主要修改：

```text
runtime/src/shejane_runtime/api_schemas.py
runtime/src/shejane_runtime/server.py
runtime/src/shejane_runtime/runs.py
runtime/src/shejane_runtime/store/sqlite.py
runtime/src/shejane_runtime/agent/builder.py
runtime/src/shejane_runtime/middleware/tool_execution.py
runtime/src/shejane_runtime/middleware/tool_visibility.py
runtime/src/shejane_runtime/plugins/**
runtime/sdk/src/client.ts
client/src/App.tsx
client/src/features/chat/components/Composer.tsx
client/src/features/chat/components/SkillEditor.tsx
client/src/features/chat/components/SkillNode.tsx
client/src/features/chat/skillDraft.ts
client/src/features/plugins/**
runtime/plugins/fixtures/**
runtime/plugins/office/**
```

迁移尾声删除或缩减：

```text
runtime/src/shejane_runtime/tools/office.py
runtime/src/shejane_runtime/tools/registry.py
runtime/src/shejane_runtime/middleware/tool_visibility.py
```

这里只删除 Office 相关分支；文件中仍有其他职责时保留 Module 本身。

## 16. 测试策略

### 16.1 单元测试

- manifest、schema、digest、signature。
- package reader 安全边界。
- registry 状态机和幂等性。
- catalog snapshot 与 lease。
- operation identity。
- Action result 与 ArtifactCandidate 校验。
- Composer parser/token。

### 16.2 契约测试

- Pydantic 与 generated TypeScript 类型。
- SDK 序列化 body。
- Worker IPC。
- WASI/Managed Worker conformance。
- Plugin Command 所需 Action。

### 16.3 集成测试

重点扩展：

- `runtime/tests/test_agent_builder.py`
- `runtime/tests/test_tool_visibility.py`
- `runtime/tests/test_run_commands.py`
- `runtime/tests/test_runs_http.py`
- `runtime/tests/test_tool_receipts.py`
- Client Composer、Sidebar 和 runtime contract tests。

关键场景：

- install -> enable -> mention -> run -> artifact。
- run accepted -> update -> resume old version。
- permission pause -> Runtime restart -> resume。
- uninstall while Run active。
- completed receipt replay。
- runner crash and cleanup。
- plugin unavailable and retry。
- Client offline pending Command 与 Runtime rejection。

### 16.4 质量与性能

- 安装大包内存峰值。
- P3/P6 目录冻结延迟。
- Worker cold start/warm reuse。
- 大文档/表格/视频的内存与临时空间。
- Artifact 提交耗时。
- 大量已启用 Actions 对模型工具选择的影响。

达到工具规模阈值后再启动统一 `tool.search` 工作，不预先增加平行搜索系统。

## 17. 发布、Feature Flag 与回滚

建议 Feature Flags：

- `plugin_registry_enabled`
- `plugin_execution_enabled`
- `plugin_ui_enabled`
- `office_plugins_enabled`
- `unsigned_plugins_enabled`

原则：

- Flags 由 Runtime 设置所有，不由 Client 私有开关改变安全策略。
- 每个 Phase 的数据库迁移向前兼容上一稳定 Client。
- 禁用执行时仍可只读查看已安装插件。
- 回滚应用版本不能删除新表或插件 blob。
- Office 切换只允许明确选择核心旧路径或插件路径，不允许失败时静默 fallback。

## 18. 文档更新规则

随实际实现同步：

- `docs/run-loop.md`：每次 P3/P6/P10/P11/P12 当前路径改变。
- `docs/operations.md`：安装、未签名包策略、签名、存储、清理和排障。
- `README.md`：用户/开发者安装与基础使用发生变化时。
- `docs/roadmap.md`：ADR Accepted 且进入正式排期后。
- `docs/harness-runtime-stages.md`：仅当目标阶段契约需要修改，不因新增一种工具来源而改编号。
- `CLAUDE.md` 和 `AGENTS.md`：插件目录成为长期架构入口后补充导航和不变量。

所有未实现能力必须标注为 future work，不能在用户文档中写成已交付。

## 19. 每个 Phase 的验证命令

先执行相关 focused tests，再执行仓库级验证：

```bash
cd runtime && uv run python -m pytest <focused tests>
pnpm --filter @shejane/runtime-sdk test
pnpm --filter @shejane/client test --run
make schemas
make test
make build
git diff --check
```

`make schemas` 只在协议源变化后执行，但提交前必须确认 generated files 与源一致。

## 20. 里程碑建议

### M1：平台契约可证明

完成 Phase 0。ADR Accepted；两个 fixture 可由同一 Action Interface 表达。

### M2：本地插件闭环

完成 Phase 1-4。用户可安装 Archive fixture，在 Plugin Tab 启用，并通过 `@Archive` 或 `/Archive:extract` 运行。

### M3：重型能力闭环

完成 Documents。证明任何开发者都可采用的 Managed Worker、Artifact 和 Office 质量方案成立。

### M4：Office 完整迁移

完成 Documents、Spreadsheets、Presentations，删除核心 Office 路径。

### M5：多模态与生态

完成 Phase 6-7 的首批插件、可自托管签名来源和两种执行类型的开发规范。

## 21. 开始实施前的最终检查

- [ ] ADR-0001 已评审并 Accepted。
- [ ] Phase 0 三个 spike 有书面结论。
- [ ] manifest、Action protocol 和错误码完成版本化。
- [ ] Archive 与 Documents fixture 范围明确。
- [ ] Office golden fixtures 及质量指标已选定。
- [ ] SQLite 迁移和旧版本回滚策略评审完成。
- [ ] macOS、Windows、Linux runner 打包负责人明确。
- [ ] 安全评审认可 v1 capability 边界。
- [ ] Feature Flag 与发布顺序明确。
- [ ] 不存在通过 prompt directive 实现插件引用的计划项。
