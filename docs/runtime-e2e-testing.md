# Runtime 端到端测试

这套测试把 Runtime 当作独立产品验证，并用一层薄的 Playwright Electron 测试验证用户可见关键路径。Runtime 测试会启动真实进程，只通过公开的 `/v1` HTTP、命令和 SSE 协议操作，不读取数据库，也不调用 Python 内部函数；Client 测试再通过真实 Electron Main、Renderer 和系统目录选择接口操作同一个隔离 Runtime。

当前套件包含 **固定插件执行 E2E**、**Runtime black-box E2E**、**真实进程恢复 E2E**、**官方 MCP client conformance** 和 **Electron 窗口级 Client E2E**。完整分层、外部优秀案例与后续缺口见 [`e2e-testing-research.md`](e2e-testing-research.md)。

## 一条命令

```bash
make test-e2e
```

命令先运行三个固定插件的执行级 E2E：Browser QA 启动真实 Chromium 并完成打开、填写、点击、截图和关闭；Computer Use 启动独立桥接进程并完成观察、`stateId` 约束下的操作与进程回收；OCR 经生产 Plugin adapter 写入文本与 JSON Artifact，并在当前平台存在 RapidOCR Runtime Asset 时追加真实识别质量、确定性和恶意输入门禁。Browser QA 的固定浏览器或 Computer Use 所需的 Node 缺失会直接失败，不会静默跳过；可设置 `SHEJANE_REQUIRE_NATIVE_OCR_E2E=1` 将原生 OCR 资产缺失也升级为失败。

Windows AMD64 另有强制的 `Windows AMD64 fixed capabilities production gate`：OCR 从锁定输入双构建 Runtime Asset、冻结 Worker、生成 digest-bound 插件，并以冻结 Worker/引擎运行真实质量与 hostile-input 测试；Browser QA 安装 Playwright 1.61.1 固定的 Chromium 1228，双构建 Runtime Asset 后使用最终插件包完成打开、填写、点击、截图和关闭。两条路径都在原生 Windows runner 执行，不依赖开发机已有资产，也不允许缺少原生产物时跳过。

随后脚本会创建临时数据目录、用户目录、workspace 和 Electron `userData`，启动启用了确定性测试模型的 Runtime，等待健康检查，执行 Runtime 契约与崩溃恢复测试，再运行固定版本的官方 MCP conformance `initialize` / `tools_call` / `sse-retry` 场景，最后启动 Vite 和真实 Electron 窗口执行 Playwright。所有 conformance 调用都经过生产 `_MCPServerSupervisor` 的 Streamable HTTP、目录发现和 Tool adapter，不使用已知失败 baseline。脚本结束时关闭所有进程并清理数据。失败时会打印 Runtime 日志，并把 Playwright 截图、trace、Runtime 日志、完整 diagnostics JSON 和逐行 SSE 事件保留在 `.tmp/e2e-artifacts`；事件行包含 run ID、command ID、seq、event type 和 payload。它不读取真实用户配置、Skill、MCP 或密钥，也不访问外部模型服务网络。

## 真实 LLM 正常流程矩阵

真实供应商测试是显式执行的手动/发布前门禁，不进入默认 CI。指定 Client 设置中已经启用并可用的具体模型：

```bash
make test-e2e-real MODEL=local:deepseek:deepseek-v4-flash
```

该命令创建独立临时 Runtime 数据目录，只从本机正式 Runtime 数据库复制所选 provider 的非秘密配置与系统凭据引用；不会复制用户的 conversation、Run、workspace 数据，也不会打印或写入 API key。测试仍通过系统凭据库访问真实 BYOK 密钥。模型必须显式指定，测试不会自动选择或静默切换供应商。

默认命令按类别执行完整正常路径：

1. **基线轨迹（3）**：中文算术、英文事实回答、必须调用 `read_file` 的工作区读取；同时断言真实模型调用次数与非零 Token 用量。
2. **公开 Tool 矩阵（44 条检查）**：从 Runtime `/tools` 反向核对 inventory，并让真实 LLM 逐个调用所有发布 Tool；检查 Tool 名、参数、permission、receipt、结果与文件/系统副作用。包含文件、执行、任务、记忆、网络守卫、OS 集成与全部 Office Tool。
3. **正常 Agent 能力（6）**：附件读取、Skill、Subagent、Todo、写文件批准/恢复、`user.ask` 中断/回答/恢复。
4. **Client 可见流程（3）**：真实 Electron 中发送并持久化回复、从 UI 批准写文件、从 UI 回答结构化问题。

真实套件只承担正常 Agent、正常流程和 Tool 的真实供应商兼容性。断线、超时、拒绝、崩溃、重放、无效参数等需要精确故障注入的边缘矩阵继续由 `make test-e2e` 的 scripted LLM 承担；否则模型的随机选择会把回归洗成偶发通过。

开发时可只运行一个分类，默认仍是全部：

```bash
SHEJANE_E2E_REAL_PHASE=tools make test-e2e-real MODEL=local:deepseek:deepseek-v4-flash
SHEJANE_E2E_REAL_PHASE=agents make test-e2e-real MODEL=local:deepseek:deepseek-v4-flash
SHEJANE_E2E_REAL_PHASE=client make test-e2e-real MODEL=local:deepseek:deepseek-v4-flash
```

可用模型可在 Client 设置中查看，或从已启动 Runtime 查询：

```bash
curl -fsS \
  -H 'Authorization: Bearer dev-local-token' \
  http://127.0.0.1:17371/v1/models
```

如 Runtime 使用了自定义地址或配对 Token，可设置 `SHEJANE_EVAL_RUNTIME_URL` 和 `SHEJANE_EVAL_TOKEN`。真实调用会产生供应商费用，也会受到供应商网络和账户状态影响。

## 覆盖范围

| 阶段 | 端到端证据 |
|---|---|
| P1-P3 连接与接纳 | 启动、健康检查、Bearer 认证、请求校验、命令幂等与冲突 |
| P4 快照与订阅 | SSE 解析、游标续传、线程快照、变更游标、诊断包 |
| P5-P9 Agent 执行 | 设置、模型目录、工具目录、真实 LangGraph 编译、Skill 加载、Subagent、Todo 状态、流式模型回合和最终回答 |
| P7/P10 恢复与等待 | 权限等待持久化后 SIGKILL Runtime、复用同一 data dir 重启并恢复；Tool 已产生副作用且 receipt 为 running 时 SIGKILL，租约过期后 quarantine 为 `outcome_unknown` 且绝不重放；首个 `llm.delta` 后 SIGKILL，按 durable cursor 重连并安全收敛到唯一 `run.cleanup_required`，diagnostics 与 thread snapshot 一致 |
| P10 工具与等待 | 每个公开 Runtime Tool、附件 PDF 的只读虚拟路径、工作区读写、权限批准/拒绝/幂等重放、混合 Tool 批次整批暂停、结构化用户提问和取消运行 |
| P11-P12 结算 | 完成和取消终态、事件持久化、线程投影、定时任务、记忆清理 |
| 可配置资源 | 模型服务、Skill 和 MCP 的创建、读取、修改、删除；Skill 在 Run 接纳时绑定完整发现树指纹，等待期间修改后旧 Run 安全失败、fork 继续继承旧绑定、新 Run 才读取新版本；真实本地 stdio MCP 的 opaque cursor 分页目录、搜索、普通/structured output、单调 progress、用户取消与旧进程回收、成功、失败、超时/崩溃后自动新 PID、reconciliation、等待批准时配置漂移的安全失效；真实 Streamable HTTP session 404 后重新 initialize 并由新 Run 恢复；官方 conformance `initialize`、`tools/list`、`tools/call`、SSE retry timing/Last-Event-ID；秘密不回传 |
| 固定插件 | Browser QA 真实 Chromium 操作与截图；Computer Use 真实桥接子进程、状态约束和回收；OCR adapter/Artifact 全链路及有资产时的真实 RapidOCR 质量门禁 |
| Plugin 纵向流 | 打包真实 WASI fixture、公开命令安装/启用、不可用 capability 的结构化拒绝、guest 结构化失败与确定性 fuel trap 均形成 `tool.failed` 且 Run 继续、零错误 Artifact、下一次健康调用成功、读取 Artifact 原始字节、审批等待期间冻结 Plugin 版本、移除 package |
| Client 用户流 | Electron 启动与 Runtime 握手、发送/流式完成、窗口重启后会话持久、真实 SSE 帧中途 half-close 后不重载按 cursor 自动续流、目录选择与 workspace 绑定、Tool 批准/拒绝、`user.ask` 回答与恢复、瞬态模型失败 CTA 与同任务 retry、validation 失败 CTA 启动携带 repair 元数据的新 Run 并完成修复、Markdown 外链通过 Electron Main allowlist 调用系统 handler、危险协议零调用且系统错误被归一化、真实 OS clipboard 写入与权限拒绝可见失败、授权 workspace 中真实 PPTX 从回答文件按钮打开 Runtime outline 预览并验证系统打开成功/权限错误、无 workspace 写入失败后从 CTA 选择/授权目录并自动 retry、设置页在 Run 等待期间关闭子代理后旧 Run 继续使用接纳快照而新 Run 禁止 `task`、真实 Runtime SIGKILL 后无需重载即主动显示离线并以同一 data dir 重启恢复、打开 `cleanup_required` 诊断面板并从 Electron Main 下载/解析 JSON、恢复后继续新任务、故障窗口外控制台零错误 |

### 可搜索的 P1-P12 测试 ID

阶段号只来自 `harness-runtime-stages.md`。下面的 ID 是真实测试收集名，不是另一份阶段定义：

| 阶段 | black-box test ID |
|---|---|
| P1 | `flow:P1 > reports online=true via {status: "ok"} envelope` |
| P2-P3 | `flow:P2-P3 > replays the same create command and rejects changed content` |
| P3 | `flow:P3 > authenticates Runtime discovery and rejects a wrong token` |
| P4-P5 | `flow:P4-P5 > parses SSE and observes the worker-started transition` |
| P6 | `flow:P6 > binds an enabled Skill and loads it through read_file` |
| P7-P10 | `tool:write_file ... > flow:P7-P10 resume` |
| P8-P9 | `tool:read_file ... > flow:P8-P9 attachment` |
| P10 | `flow:P10 > contract: every Runtime Tool`，其动态 case 名包含 Tool/family/effect/risk/traits/outcome |
| P11 | `flow:P11 > cancels an in-flight model call and reaches a durable terminal state` |
| P12 | `flow:P12 > commits a complete run into the authoritative thread and diagnostics` |

平台与打包门禁按实际发布能力分类，不能用一个平台的绿色结果替代另一个平台：

| 平台/产物 | 强制证据 | 当前边界 |
|---|---|---|
| macOS arm64 Client | 当前源码冻结 Runtime；最终 `.app` 内 Runtime 独立 preflight VM manifest/schema/架构/asset-set ID/全部文件摘要与权限；正常 Main 启动交付随机 token、health/plugin catalog、VM 参数注入、正常退出和 Runtime 回收；包内 VM 14 模式覆盖成功、协议失败、取消、资源上限、宿主逃逸、worker/Runtime/launcher crash | 本轮已在原生 arm64 对当前 unsigned `--dir` 包执行；Developer ID、公证、staple 仍由 release runner 证明 |
| macOS x64 Client | 同一 packaged Runtime lifecycle；明确断言不注入 arm64 VM manifest | Managed Worker 保持 fail-closed |
| Windows x64 Client | `win-unpacked` 真实 executable → bundled Runtime → token/health/plugin catalog → quit → Runtime 回收；CI 另跑 LPAC/Job Object launcher self-test | QEMU guest/Managed Worker Registry 尚未开放 |
| Linux arm64 Runtime | 冻结 Runtime、packaged bubblewrap/launcher、特权 cgroup v2 hostile gate 及真实 systemd delegation gate | 当前没有 Linux Client 安装包，不把 Runtime gate 写成 Client smoke |
| 其他 target | 明确无匹配资产/Registry blocker | 不用 skip 或其它架构结果冒充支持 |

Tool 覆盖由 Runtime 的 `/v1/tools` 目录反向检查。新增公开 Tool 却没有对应执行用例时，测试会直接失败。Deep Agents 额外注入的 `write_todos` 和 `task` 也有独立纵向测试；MCP 工具达到目录阈值时使用的 `mcp.search_tools` 同样会实际执行。

Tool case 在测试输出中按 `filesystem`、`runtime-context`、`network`、`host-integration`、`task-state`、`memory`、`human-in-the-loop` 和 `office` 分类；每个 case 还声明 `read-only`、`workspace-write`、`runtime-state`、`host-interaction` 或 `human-interaction` effect，`low` / `workspace` / `host` / `human` risk，以及诸如 `permission`、`workspace`、`side-effect`、`cancel`、`timeout`、`process-tree`、`network`、`ssrf`、`allowlist` 的 traits。公共边缘流程另按 `validation and failure containment`、`workspace isolation` 和 `guarded-failure` 命名。目前真实进程套件还会验证：

- 参数错误在审批与执行前失败，receipt 的 `attempt_count` 保持为 0；
- `/tools` 中每个声明 required input 的 Tool 都会从其真实成功参数删除一个必填项并重新走公开 Agent 流；每个 closed schema 还会在真实成功参数中注入未声明字段。两种错误都必须在 permission 前、attempt 0 失败，反向证明 schema 目录与执行校验没有漂移；不适用项必须有稳定原因；
- 未知 Tool 形成可观察的 `tool.failed`，不会让 Runtime 崩溃；
- Unicode 路径/内容完整写入；workspace 内 symlink 不能读取授权根目录之外的内容；
- 超过模型交接上限的 Tool 输出保存为可取回 Artifact，最终模型上下文保持有界；
- `execute` 从授权 workspace 作为 cwd 运行，保留 stdout、带来源标记的 stderr 与非零退出码；自定义 timeout 和 Run cancel 都必须杀死并回收完整 shell 进程组，不能留下脱离 Run 的子进程；
- `execute` 能处理含空格和单引号的路径，非法 UTF-8 stdout 被包含而不会击穿 Agent loop；
- 每个 workspace-write Tool 都在批准前验证目标文件/输出副作用仍为零，批准后才验证预期输出；空文件可以经 `write_file` 创建并由 `read_file` 作为成功空状态读取；offset/limit 只返回指定分页窗口；超过 20 MiB 的 workspace 文件在 backend 读取前被拒绝并形成 failed receipt；`edit_file` 的零匹配与多匹配都失败且原文件字节不变；同一模型批次对同一路径的两个写入按模型顺序结算，第一个成功结果不被第二个冲突写覆盖，receipt 顺序保持一致；
- `open.url` 与 `open.file` 在隔离的系统 handler 中证明允许路径成功，同时危险协议和缺失文件仍形成可观察的 guarded failure；
- `web.fetch` 的公开 E2E 证明 SSRF 拒绝、`tool.failed`、failed receipt 与 final 回传完全一致；生产 Tool 集成测试另证明受控成功响应、真正达到 2 MB 就停止消费响应流、超长 header 丢弃、303 后 POST 转 GET、超时与重定向耗尽；
- 同一个批准命令重放不会重复执行副作用；拒绝后副作用次数为 0；
- Runtime 在 Tool 写出一次外部副作用后被 SIGKILL，会在原执行租约到期时发出 `run.cleanup_required`，receipt 保持 `outcome_unknown`、attempt 为 1，重启过程不会重放 Tool；
- Runtime 在模型已输出首个临时 `llm.delta` 后被 SIGKILL，不会把未知结果重放成第二次模型调用；租约到期后唯一收敛为 `run.cleanup_required`，事件 seq 单调无重复、没有假 `run.completed`/`run.failed`、没有 Tool receipt，diagnostics 和 thread snapshot 均为 `cleanup_required`；
- Runtime 在 Tool receipt 已 completed、最终模型回合只输出首个临时 delta 时被 SIGKILL，重启后 Tool receipt 仍为 completed/attempt 1、磁盘副作用恰好一次且绝不重放；未知模型结果使 Run 安全收敛到 `cleanup_required`；
- edit 决策的兼容协议仍只执行修改后的参数，冲突的旧决策被拒绝；确认卡只显示允许一次、不再询问和拒绝；两个写 Tool 的批次逐项决策，只产生被批准的一次副作用；`scope=run` 对合格工具按相同 Tool version 与风险复用，新参数仍重新校验，删除类操作被服务端拒绝授予该范围；
- 同一轮只读 + 写入 Tool 批次在批准前全部保持 `prepared`，批准后各执行一次；
- MCP Tool 的显式错误形成 failed receipt；真实 stdio server 通过 opaque cursor 返回多页 Tool、structured output 按发布 schema 进入 Agent、progress 以单调序列投影为 `tool.progress`；用户在首个 progress 后取消会形成 `run.canceled`/`outcome_unknown`、终止旧进程并自动建立新 session。stdio Tool 阻塞超过 Runtime 上限或进程在调用中崩溃时同样必须 reconciliation，不能静默重试；Streamable HTTP server 返回过期 session 的 404 后也会建立第二个 session；等待批准期间 MCP 配置改变会使旧 Run 以 `tool_receipt_conflict` 安全失效，绝不拿旧批准执行新实现，只有新 Run 使用新配置；
- Plugin 请求未开放的 `network.http` capability 会返回结构化 `capability_denied`、failed receipt 与零 Artifact；WASI guest 的结构化失败同样回到模型，不会击穿 Agent loop；合法组件耗尽确定性 fuel 时会归一化为 `resource_exhausted`；guest 返回与已发布 output schema 不一致时会归一化为 `protocol_violation`，且在任何 Artifact 持久化前失败；同一健康 Plugin 随后仍能完成并提升 Artifact；
- Plugin Tool 等待批准期间更新 package，旧 Run 仍执行接纳时冻结的 v1，新 Run 才使用 v2；测试用 v2 新增的拒绝 capability 证明两者没有静默漂移；
- Skill 开启时，Run 接纳会绑定所有配置根、目录、`SKILL.md` 与辅助文件内容的 SHA-256 指纹；等待 Tool 批准时修改 Skill，旧 Run 恢复后必须以 `ExecutionSkillBindingError` 在执行 Tool 前失败且事件流不泄露新内容，新 Run 才读取新版本；从已完成 Run 的 checkpoint fork 继续继承原绑定，不能借 fork 静默切到第三个版本；
- Client 可见配置冻结从真实设置页切换 `subagents`：已等待批准的旧 Run 保留 `subagents=true` 快照并完成 `task` receipt，新 Run 持久化 `subagents=false` 且没有 completed `task` receipt；该场景实际发现并修复了 DeepAgents 在 `subagents=None` 时仍自动注入 `general-purpose` 子代理的问题，Runtime 现在同时在模型可见 Tool 集和执行边界关闭 `task`；
- Client 在 Runtime kill-point 后从可见 `cleanup_required` 消息打开诊断面板，展开技术详情核对 Run ID，并通过 Electron Main 的真实 `will-download` 生命周期验证下载完成、文件名与落盘 JSON schema/终态；
- Client 在旧 Run 崩溃并进入 `cleanup_required` 后创建新对话时，旧发送状态不能把新 Composer 锁成“停止生成”；可见发送操作使用独立 token，旧异步流稍后结束也不能覆盖新发送状态；
- Client 在无 workspace 时先完成一次结构化 `user.ask` 并批准写 Tool、验证零副作用，再从失败 CTA 选择/授权目录；自动 retry 保留并复用原问题答案、不再重复提问，仍需第二次权限批准，最终文件只写入授权 workspace 一次；
- Client 在 validation 失败后从可见 CTA 启动 repair Run；原用户消息只保留一条，Runtime 注入 repair workflow 元数据，最终步骤收敛为“修复完成 1/3”并返回正常结果；
- Client 实际点击模型回答中的 HTTPS Markdown 链接，证明请求经过 Electron Main 并只调用一次系统 handler；`file:` URL 被 allowlist 拒绝且零系统调用，允许协议遇到 OS handler 异常时通过 preload IPC 返回稳定错误而不污染 Renderer；
- Client 用真实 `python-pptx` 文件验证“已选 workspace → 回答中的 `.pptx` 文件按钮 → Runtime outline → PowerPoint 系统打开”纵向链路；系统返回权限错误时预览面板显示可访问的 alert。该场景实际发现并修复了首次 workspace 未传给消息线程、文件识别器漏掉 `.pptx`、预览 config 身份变化导致请求无限重启，以及系统打开错误被静默丢弃四个问题；
- Client 从真实消息复制按钮写入 Electron 原生 clipboard 并从 Main 读回精确文本；Renderer 遇到 `NotAllowedError` 时显示“复制失败”、不显示假成功且不产生未处理错误；
- 真实 SSE 响应即使在 UTF-8 字节和 SSE 帧边界被切成 1-7 字节碎片也能完成；内核 TCP half-close 与 RST 后从最后 durable seq 恢复，seq 保持单调且唯一终态；并发订阅者得到同一有序日志；消费者暂停读取超过 1 MiB 的 burst 时 Run 仍完成，恢复读取后 256 个 delta、唯一终态与 `[DONE]` 全部到达；Electron 还会在代理中途 half-close 后不重载自动续流。

附件测试会确认模型收到 Runtime 授权的 `/attachments/...` 虚拟路径，并直接调用 `read_file`。如果流程错误地再次询问用户本机文件路径，测试会失败。`user.ask` 测试验证问题编号、单问题结构、选项对象、提问刚显示时立即点击仍能提交回答命令，以及恢复后的最终结果；Client 在持久投影尚未追上可见投影时会用当前可见消息完成命令接纳，不再静默丢失第一次选择。

端到端测试验证模块之间的真实契约。Runtime 内部的异常分支、数据库迁移等继续由 `runtime/tests` 的集成测试覆盖。两层都由 `make test` 与 `make test-e2e` 执行，不能用端到端测试替代内部集成测试。

当前仍未完成的黑盒层包括：尚未开放平台的 Managed Worker guest/Registry，以及真实 Developer ID/公证 release runner 的最终结果。macOS arm64 的当前源码 `--dir` 包已真实完成 Runtime preflight、packaged lifecycle 和 14-mode VM gate，macOS x64/Windows lifecycle 与 Linux arm64 Runtime confinement 也已进入各自原生 release job；但没有对应 runner 的一次实际成功记录时，不能把 workflow wiring 冒充已经发布。Runtime Skill 已覆盖等待、恢复、fresh Run 与 checkpoint fork 的配置漂移，但这里采用的是“接纳内容指纹 + 恢复前失败关闭”，不是把 Skill 文件复制到每个 Run 的私有归档；因此旧 Run 保留安全性，但修改后需要创建新 Run，不能继续执行旧版本。当前 HTTP session 测试覆盖 404 失效，不冒充 auth、elicitation 或全部 transport 错误已支持。官方 conformance 当前只声明并验证 Runtime 实际使用的 client 场景 `initialize`、`tools_call` 与 `sse-retry`。当前 17 条 Electron critical flows 已完成。执行顺序与验收表见研究文档。

真实模型服务、第三方 MCP 和操作系统凭据库具有网络、账户或平台依赖，不进入每次提交都运行的确定性套件。发布前应运行一次 `make test-e2e-real MODEL=...` 完整正常路径矩阵；外部服务网络或账户故障应与代码回归分开诊断，不能把未执行的真实门禁写成已通过。

## 新增功能时

1. 先在对应层添加最小失败测试。
2. 公共协议或跨模块行为必须增加真实 Runtime 端到端用例。
3. 纯内部分支放在 Runtime 集成测试中。
4. 测试数据必须唯一并在结束后清理；禁止依赖执行顺序和用户主目录。
5. 最终运行 `make lint`、`make test`、`make build`、`make test-e2e` 和 `git diff --check`。
