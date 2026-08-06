# Run Loop —— 当前能力实现态

创建 Run 前，Runtime 会检查协议版本、客户端所需能力、资源归属和当前模型绑定。接纳成功后保存版本化的有效设置快照与不可变凭据引用；真实密钥不进入 Run 或作业记录。模型必须使用 `local:<连接编号>:<模型编号>`。任务开始或恢复时会重新核对工作区、设置快照、模型服务版本和凭据引用，然后才进入模型循环。模型失败不会触发静默回退。模型资料明确保存是否支持图片输入；文本模型的文件工具结果不会包含图片块，而会得到清晰的能力限制说明。图片工具可以把当前 Run 已冻结的一个图片附件作为参考源；模型只传 `/attachments/...` 虚拟路径，Runtime 在工具边界解析为私有快照，不开放任意磁盘路径。

模型服务设置按连接读取操作系统凭据；单个旧凭据不可读时只把该连接投影为“需要 API Key”，不再让整个连接列表失败。重新连接会先验证并写入新的不可变凭据引用，再切换 SQLite 连接行；旧凭据清理失败只保留脱敏日志，不回滚已经可用的新凭据。Run 接纳仍要求其具体冻结凭据可读，不能把设置页的恢复投影当作执行授权。

MCP Server 只从 Runtime 自有配置读取，不会隐式启动 Claude Desktop、Cursor 或 Codex 的全局配置。Runtime 级监督器按单个 Server 的配置指纹维护工具目录和长会话；连续 Run 取得固定目录快照并复用连接，`build_agent()` 不再为每个 Run 顺序发现或启动全部 Server。新增、修改或删除 Server 时，旧目录会退休；已有 Run 释放最后一个租约后才关闭旧会话。连接失败会进入 30 秒退避，避免每个 Run 重复等待；MCP 列表接口返回当前连接状态、工具数和不含密钥的错误类型。

每次目录刷新会把配置指纹、已校验工具元数据、版本、状态和最后成功时间写入 Runtime SQLite；密钥、连接对象和会话不进入该表。Runtime 启动时只恢复配置指纹仍匹配的目录，并构造惰性工具代理，不连接 Server、不执行 `tools/list`；惰性代理第一次真正执行工具时才建立连接。目录缺失、配置变化或失败退避到期时，普通 Run 立即使用当前有效快照，首次发现和刷新在 Runtime 后台执行，完成后只影响后续 Run。

支持目录变化通知的 MCP Server 发出 `notifications/tools/list_changed` 后，监督器会后台刷新并替换目录；正在执行的 Run 继续使用原快照和会话。MCP 工具达到 12 个时，模型默认只看到 `mcp.search_tools`，搜索结果中的工具结构从下一模型回合起按需暴露；静态 Runtime 工具保持常驻。该本地目录工具不额外调用模型，也不要求供应商支持原生 Tool Search。

> **范围**：`runtime/` 中一个 run 从 `POST /v1/runs` 到终态的完整路径。
> **关联**：[harness-runtime-stages.md](harness-runtime-stages.md) · [runtime-protocol.md](runtime-protocol.md) · [operations.md](operations.md) · [roadmap.md](roadmap.md)
> **状态**：本文只记录当前代码如何运行，不定义 P1-P12 目标编号。阶段编号以 [harness-runtime-stages.md](harness-runtime-stages.md) 为准。
> **边界**：业务平台连接器不属于 Runtime 核心，通过标准工具或 MCP 接入。

桌面发行版由 Electron Main 为自带 Runtime 分配本机端点、数据目录和一次性配对 Token，并通过源码与打包产物共用的命令行入口启动进程。Client 不提供 Runtime 连接设置；开发者仍可通过 Main 进程配置接入自己管理的 loopback Runtime，地址和加密 Token 不会回传 Renderer，Electron 也不会关闭外部进程。Runtime 拒绝非 loopback 监听，未来远程客户端必须通过独立接入网关或用户自管的同机私网代理。两种本机模式都使用带认证的 `/v1/runtime` 握手，要求协议版本为 1 且具备 `agent.run`、`agent.stream` 能力。托管子进程只有明确报告“地址已占用”并退出时才换端点重试，所有尝试共享 120 秒期限；外部本机 Runtime 的连接检查仍使用 30 秒期限。Browser QA 与 OCR 的大型固定 Runtime Asset 不参与 P1 冷启动。其他启动错误或仍存活却未在期限内就绪时直接失败。连接失败时 Client 进入离线状态，提示用户重启应用或检查开发配置。桌面托管进程在应用退出时先收到 `SIGTERM`，有限等待后仍未退出才会被强制结束。

---

## 全景图

```
╔════════════════════════════════════════════════════════════════════════════════════════════╗
║                         RUN LOOP — 当前能力实现态                                          ║
╚════════════════════════════════════════════════════════════════════════════════════════════╝

  ┌─ 1. 入口与状态机 ────────────────────────────────────────────────────────────────┐
  │                                                                                  │
  │  POST /v1/runs                                                              │
  │       ├─ Electron Main 为 Runtime 请求注入 Bearer Token；Renderer 只持有地址和会话标记 │
  │       ├─ 配对 Token 映射为稳定 Runtime 身份 local:owner                          │
  │       ├─ 认证后、JSON 解析前限制请求体为 1 MiB                                   │
  │       ├─ command_id 与 client_message_id 必填                                    │
  │       ├─ Composer 的 @插件 与 /插件命令编码为 plugin_refs/plugin_command；不注入 goal │
  │       ├─ Renderer 先在 IndexedDB 同一事务保存临时投影和待确认命令                 │
  │       ├─ 断网或重启后按原编号重投；同线程保序，不同线程互不阻塞                  │
  │       ├─ 收到 Runtime 回执后才删除命令；传输中断只保留待确认投影                 │
  │       ├─ 删除投递中对话时持久标记取消；接纳后先取消 Run，终态后再删线程          │
  │       ├─ 已结算取消记录阻止旧 Runtime 快照重新写回已删除对话                     │
  │       ├─ 幂等命中先返回原回执；新命令在接纳事务内检查工作区和父任务               │
  │       ├─ Run、事件、产物、审批和定时任务的读写都按身份过滤                       │
  │       │                                                                          │
  │       ▼                                                                          │
  │  RunCoordinator.start_run(principal_id, command_id, client_message_id, ...)      │
  │       ├─ store.accept_run_command(...)                                            │
  │       │    同一事务写命令 + 对话消息 + Run + pending 作业 + 线程变化              │
  │       │    P3 同事务解析启用/显式/Command 插件，并写精确 run_plugin_bindings       │
  │       │    显式插件选择按已验证 manifest 规范化到用户消息 metadata；Client 历史只投影该值 │
  │       │    fork 继承源 Run 的精确 digest；更新或退休不改写已接受 Run               │
  │       │    已有对话的 history 从 Runtime 消息生成；客户端历史只用于一次旧数据迁入 │
  │       │    同编号同内容返回原 run；同编号不同内容返回 409                         │
  │       └─ 返回“已持久化”回执，不在 HTTP 请求中启动 Agent                          │
  │                                                                                  │
  │  POST /v1/runs/{source_run_id}/fork                                        │
  │       ├─ Renderer 先在同一待发队列保存 run.fork 和新对话临时投影                │
  │       ├─ 请求携带协议版本与所需能力；不兼容时不写入任何分支状态                 │
  │       ├─ Runtime 原子写命令、分支对话、消息、Run、作业和稳定回执                │
  │       └─ 断网或重启后复用同一待发分支，不改写旧分支                            │
  │                                                                                  │
  │  POST /v1/commands  （支持取消、四类等待决定与插件生命周期命令）            │
  │       ├─ Renderer 先把命令写入同一个 IndexedDB 待发队列                         │
  │       ├─ Runtime 在同一事务保存命令、取消请求和稳定回执                         │
  │       ├─ 等待态取消会同时关闭权限、问题、计划审批和其他等待候选                 │
  │       ├─ 同编号同内容返回原回执；同编号不同内容返回 409                         │
  │       ├─ 回执后协调器停止当前执行；权威终态仍由事件与快照返回                   │
  │       ├─ 四类等待事务写决定、事件和回执；等待周期齐全时同事务创建恢复作业       │
  │       ├─ 插件 install/enable/disable/update/rollback/remove 与 source add/refresh/install/remove 使用同一待发队列与幂等回执 │
  │       ├─ 来源刷新先验证精确索引字节和独立 Ed25519 签名，失败时保留 last-known-good │
  │       └─ 对应旧接口暂时兼容，桌面客户端已不再调用                              │
  │                                                                                  │
  │  Runtime dispatcher                                                              │
  │       ├─ 先取得本机并发槽位                                                       │
  │       ├─ 原子领取 pending job，写 owner / generation / expiry / attempt          │
  │       ├─ 每次开始或恢复前重新检查 Run 所属工作区仍然有效                          │
  │       ├─ Run.id、graph_thread_id 和 graph_checkpoint_id 分别表示产品任务、图线程和分支头 │
  │       ├─ asyncio.create_task(_drive_run)                                          │
  │       ├─ 状态、事件、Artifact 和 LangGraph checkpoint 写入前校验 generation      │
  │       └─ 心跳续租；过期后安全失败；完成时按 owner + generation 结束 job          │
  │                                                                                  │
  │       状态机：                                                                    │
  │       ┌──────┐                                                                   │
  │       │queued│──▶┌───────┐──┬──▶┌──────────────────┐                            │
  │       └──────┘   │running│  │   │waiting_permission│──┐                          │
  │                  │       │  │   └──────────────────┘  │                          │
  │                  │       │  │   ┌──────────────────┐  │ 权限 / 问题决定          │
  │                  │       │  ├──▶│waiting_input     │──┤ 计划审批 / 工具对账      │
  │                  │       │  │   └──────────────────┘  │ 校验后创建恢复作业       │
  │                  │       │  ├──▶canceled              │                          │
  │                  │       │  ├──▶failed                │                          │
  │                  │       │  └──▶completed             │                          │
  │                  └───────┘ ◀──────────────────────────┘                          │
  └──────────────────────────────────────────────────────────────────────────────────┘
                                       │
                                       ▼

  ┌─ 2. driver loop（每个 run 一份） ─────────────────────────────────────────────────┐
  │                                                                                   │
  │   agent = build_agent(按结构指纹复用或编译)                                       │
  │   execution_resources = AsyncExitStack      ← 本次执行结束时关闭模型客户端         │
  │     ├─ RuntimeContext                    ← P7 注入身份、任务和本次执行依赖          │
  │     ├─ PluginCatalog.acquire_snapshot    ← P6 重验精确包/资产 digest 并持有 lease  │
  │     │   ├─ 固定 Action/Skill/Command 描述与 catalog hash；缺包不回退最新版         │
  │     │   └─ 首次绑定 Browser QA/OCR 时按当前 Client Release 获取精确资产并原子安装  │
  │     ├─ backend factory → FilesystemBackend(授权工作区或本次执行临时目录)            │
  │     │                                      ← 文件工具和子代理共享当前执行边界      │
  │     ├─ SkillsMiddleware sources = [skills_dir]   ← 只读挂载，渐进披露 Markdown Skills │
  │     ├─ MemoryMiddleware  sources = [AGENTS.md]   ← 只读挂载，注入 system prompt    │
  │     ├─ SubAgentMiddleware subagents = [general-purpose, researcher, writer]       │
  │     ├─ team.run → 同一 Runtime Run 的独立 LangGraph checkpoint thread             │
  │     │   ├─ Send 并行 fan-out；reducer 合并 finding/claim/review                    │
  │     │   └─ depends_on fan-in；受冻结 roster/edge 校验的显式 handoff                │
  │     ├─ ToolReviewMiddleware + ToolExecutionMiddleware + FileWriteConflictMiddleware │
  │     │   ← 主 Agent 和子 Agent 共用参数校验、人工确认、持久回执和文件冲突澄清       │
  │     ├─ checkpointer = lease-fenced AsyncSqliteSaver ← 当前任务租约保护写入          │
  │     ├─ agent_store  = AsyncSqliteStore           ← 显式 memory 工具的持久存储      │
  │     ├─ RuntimeContext.model = 本次模型连接        ← 主模型、摘要和子 Agent 共用代理  │
  │     └─ RuntimeContext.dynamic_tools = Runtime MCP 目录快照 ← 图内只保留无密钥结构代理 │
  │        ├─ 插件 Action 通过 task-local proxy 进入固定 Agent definition              │
  │        │   └─ P10 复用 review/receipt，私有 staging 后提升 Runtime Artifact         │
  │        │      Computer Use builtin 在 Run 内保持一个 state-scoped 宿主服务，P11 关闭 │
  │        │      Linux native backend 组合 bwrap namespace、seccomp、私有 tmpfs、       │
  │        │      Artifact broker 与 delegated cgroup；发布 Gate 未通过时仍拒绝执行      │
  │        └─ MCP 工具 ≥ 12：模型先调用 mcp.search_tools，再按搜索结果加载结构          │
  │                                                                                   │
  │   ┌─ agent.astream(version="v2", durability="sync",                            │    │
  │   │                 context=RuntimeContext,                                       │    │
  │   │                 stream_mode=["updates","messages","custom","checkpoints"]) ┐ │
  │   │                                                                          │    │
  │   │   每个 LangGraph 事件                                                     │    │
  │   │      │                                                                   │    │
  │   │      ▼                                                                   │    │
  │   │   event_translator.translate(kind, payload)                              │    │
  │   │      │     → llm.round.started / llm.delta / llm.reasoning /            │    │
  │   │      │       llm.tool_call_chunk /                                       │    │
  │   │      │       tool.completed / tool.failed / agent.custom                 │    │
  │   │      │                                                                   │    │
  │   │      ├─ task Tool Receipt 同事务投影                                     │    │
  │   │      │       subagent.spawned / started / waiting /                      │    │
  │   │      │       completed / failed / canceled / outcome_unknown             │    │
  │   │      ▼                                                                   │    │
  │   │   状态事件 → SQLite + seq       临时增量 → 有界订阅队列（无 seq）          │    │
  │   │      │                          │                                       │    │
  │   │      └──────────┬───────────────┘                                       │    │
  │   │                 ▼                                                       │    │
  │   │            sse-starlette 推流（sep="\n"）                              │    │
  │   │                                /v1/runs/:id/stream                 │    │
  │   │                                每帧 data: <envelope JSON>                │    │
  │   │                                最后 data: [DONE] sentinel                │    │
  │   └──────────────────────────────────────────────────────────────────────────┘    │
  │                                                                                   │
  └───────────────────────────────────────────────────────────────────────────────────┘
                                       │
                                       ▼

  ┌─ 3. 单次 LLM 调用穿过的能力管道（按发火位置画在时间轴上） ──────────────────┐
  │                                                                                     │
  │   ❶ 入循环前一次性                                                                   │
  │     ┌─────────────────────────────────────────────────────────────────────────┐     │
  │     │  • Runtime identity + safety         ← 主 Agent/子 Agent、所有供应商共用 │     │
  │     │ before_agent (顺序)                                                      │     │
  │     │  • TodoListMiddleware                  ┐  ✅ cap 7  写 write_todos 工具  │     │
  │     │  • SkillsMiddleware                    ├  ✅ cap 6  Markdown Skills 注入  │     │
  │     │  • FilesystemMiddleware                ├  注入 ls/read_file/write_file/  │     │
  │     │                                        │   edit_file/glob/grep/execute    │     │
  │     │  • SubAgentMiddleware                 ┤  ✅ cap 2  注入同步 task 工具      │     │
  │     │  • SummarizationMiddleware             │  上下文压缩                       │     │
  │     │  • PatchToolCallsMiddleware            │  orphan tool_call 自愈            │     │
  │     │  • InputGuardMiddleware (P1)           ┘  注入/越狱启发式                  │     │
  │     │                                                                          │     │
  │     │ MemoryMiddleware                          AGENTS.md → system prompt        │     │
  │     │ ToolReviewMiddleware                      按调用参数和风险生成等待候选      │     │
  │     └─────────────────────────────────────────────────────────────────────────┘     │
  │                                                                                     │
  │   ❷ 每一轮 LLM 调用                                                                  │
  │     ┌─────────────────────────────────────────────────────────────────────────┐     │
  │     │ before_model                                                             │     │
  │     │  • ToolCallLimitMiddleware(web.search, run_limit=3) ← 仅在 MCP 提供同名工具时生效 │     │
  │     │  • ToolRetryMiddleware       transient 工具异常重试（max_tool_retries）    │     │
  │     │  • ToolResultRetryMiddleware retryable tool envelope 重试                 │     │
  │     │  • 模型自动重试已禁用；产生输出后不得从头重试               │     │
  │     │  • 本地 direct model fallback 已禁用；没有静默供应商切换             │     │
  │     │  • local_model_calls 原子预留持久调用预算；子调用关联当前 task operation  │     │
  │     │  • OutboundPolicy 只处理出站副本：强制过滤凭据，外部供应商按策略脱敏   │     │
  │     │  • 按模型 max_input_tokens 、工具结构和安全余量建立硬上下文边界     │     │
  │     │  • 插件已产生产物时注入交付指令，并隐藏定位产物的兜底工具与同一 Action │     │
  │     │  • CompletionRouter 是唯一完成候选路由：验证失败时有界返回 model       │     │
  │     │                                                                          │     │
  │     │ wrap_model_call (栈式 nested)                                             │     │
  │     │       ↓                                                                  │     │
  │     │ LedgerChatModel 记录首次输出 → 已绑定的唯一供应商模型               │     │
  │     │       ↓                                                                  │     │
  │     │   OpenAI 兼容或 Anthropic 原生流式接口                                    │     │
  │     │       ▼                                                                  │     │
  │     │   返回 llm.delta × N  +  llm.tool_call  +  llm.done                      │     │
  │     │       ↓ 完整结束时结算 token 用量；中断或重启标记结果不明     │     │
  │     │       ↓ 顶层 AIMessage 按版本写入草稿；终态合并本轮工具前正文与最终回答      │     │
  │     │       ↓                                                                  │     │
│     │ after_model：仅 CompletionRouter 可决定最终候选、修复或明确失败          │     │
│     │  • 需要用户补充信息的正文提问会有界修复为 user.ask，不提交伪终态       │     │
│     │  • user.ask 在进入等待前做一次有界必要性审查；历史已回答则返回 model    │     │
│     │  • 审查使用独立 clarification_review 账本预算；不可用时放行可见问题卡   │     │
│     │  • 有当前轮工具回执的最终候选走 completion_review；遗漏交付物最多修复一次│     │
│     │  • 修复后仍不合格则 blocked；审查不可用时退回确定性完成判定             │     │
  │     │                                                                          │     │
  │     │ 有 tool_calls?                                                            │     │
  │     │   no → 出循环 → after_agent                                                │     │
  │     │   yes ↓                                                                  │     │
  │     │                                                                          │     │
  │     │ after_model 的 ToolReviewMiddleware 先解析完整工具批次                   │     │
  │     │  • 校验工具是否存在、参数结构、图定义版本和撤销状态                      │     │
  │     │  • 计算 operation_id、arguments_hash 和风险等级                         │     │
  │     │  • 准备 Tool Receipt，并优先复用 operation 已持久化的审批决定            │     │
  │     │  • 按 Run 冻结的 ask / auto / full_access 权限模式决定是否询问           │     │
  │     │  • auto 先走确定性规则；只有外部/未知灰区交给当前冻结模型批量审查         │     │
  │     │  • execute 进入无网络、工作区只读的 OS 沙箱；无启动器时 fail closed      │     │
  │     │  • 删除等不可恢复工具始终询问，且不能取得整段任务授权                    │     │
  │     │  • 审查模型无工具且只返回 allow/ask；超时、异常或非法结果回退人工确认     │     │
  │     │  • 审查调用写入同一模型账本的 approval_review 独立预算                   │     │
  │     │  • full_access 只取消普通询问，不扩大工作区、系统权限或参数校验边界      │     │
  │     │  • 任一调用需确认时，整批执行前 interrupt 并保存等待候选                 │     │
  │     │  • approve / edit / reject 必须与 SQLite 中的同一决定相符               │     │
  │     │                                                                          │     │
  │     │ wrap_tool_call 的 ToolExecutionMiddleware                               │     │
  │     │  • prepared → running / paused → completed / failed / canceled / outcome_unknown │     │
  │     │  • task 每次真实转换与 durable subagent.* 事件同事务，不从 stream chunk 猜状态 │     │
  │     │  • Receipt 开始事务会同时检查当前租约取消标记；取消已接纳后不再启动工具   │     │
  │     │  • 父 Run 进入终态/cleanup_required 时取消未执行或暂停的回执；真正执行中的租约丢失记 unknown │
  │     │  • 插件 tool version 固定 digest/schema/input/grant/limits；ContextVar 仅传本调用 operation │
  │     │  • 已完成回执直接复用；结果不明进入人工核对，不自动重跑                  │     │
  │     │  • 纯读取可并行；冲突调用按模型批次原始顺序通过公平读写门                │     │
  │     │  • 大结果保存为有配额的工作产物，只把短预览和引用交给模型                │     │
  │     │                                                                          │     │
  │     │  ┌─ 工具分派 ─────────────────────────────────────────────────┐         │     │
  │     │  │  task(subagent_type=..., description=...) → 子 agent 隔离 context + LLM │     │     │
  │     │  │       │                       (researcher / writer)        │         │     │
  │     │  │       ├─ operation_id 是一次调用身份；parent_operation_id 记录嵌套关系 │     │     │
  │     │  │       └─ 跑完 → task receipt 结算 + 生命周期事件；ToolMessage 仅是结果 │     │     │
  │     │  │                                                            │         │     │
  │     │  │  其他 tool → ToolNode 只并行明确只读调用                     │         │     │
  │     │  │              只读暂时错误 → ToolRetryMiddleware 有界重试     │         │     │
  │     │  │              超 ToolCallLimit → 返 "已达上限" 不再调        │         │     │
  │     │  └────────────────────────────────────────────────────────────┘         │     │
  │     │                                                                          │     │
  │     │  ToolMessage 进 messages → 回到 before_model                              │     │
  │     └─────────────────────────────────────────────────────────────────────────┘     │
  │                                                                                     │
  │   ❸ 出循环一次性（终态前）                                                            │
  │     ┌─────────────────────────────────────────────────────────────────────────┐     │
  │     │ 不执行隐藏的 after_agent 业务：                                           │     │
  │     │  • 不调用反思模型                                                         │     │
  │     │  • 不自动写入长期记忆                                                     │     │
  │     │  • memory.write 只能写明确姓名事实/指令，或自然确认/指代的用户原文   │     │
  │     │  • 统计、最终回答和用量从助手草稿、模型账本和工具回执读取                  │     │
  │     └─────────────────────────────────────────────────────────────────────────┘     │
  │                                                                                     │
  │   ❹ 完成或等待分支                                                                  │
  │     while True:                                                                     │
  │       snapshot = await agent.aget_state(config)                                     │
  │       if snapshot.next is empty:                                                    │
  │           return 候选结果：completed + run.completed { final_text }                 │
  │                                                                                     │
  │       interrupts = snapshot.interrupts + 每个 snapshot.task.interrupts              │
  │                                                                                     │
  │       waiting_status = waiting_input if all interrupts are user.ask questions,       │
  │                        otherwise waiting_permission                                  │
  │       若 next 非空但 interrupts 为空：明确失败，不提交无法恢复的等待状态             │
  │       for each interrupt:                                                           │
  │         if value.kind == "question":                                                │
  │           store.create_question(...)                                                │
  │           event: question.asked { request_id, questions }                          │
  │         else (tool_review):                                                         │
  │           for each action_request:                                                  │
  │             保存调用编号、操作编号、参数指纹、风险和完整等待候选                    │
  │             event: permission.required { request_id, tool, arguments, risk }        │
  │       return 候选结果：waiting + run.waiting { next, interrupts, handoff }          │
  │                                                                                     │
  │     执行器收到候选结果后：                                                           │
  │       退出 AsyncExitStack，关闭模型客户端、PluginExecutionLease 和其他执行资源      │
  │       若清理无法确认：写 run.cleanup_required 并封存当前执行代次，不允许自动重试    │
  │       若租约过期：旧执行者完成清理并提交证明后，才把隔离任务结算为 failed           │
  │       LocalStore.commit_run_result(...)                                             │
  │         普通执行必须持有属于当前 Run、未过期且未隔离的租约                         │
  │         无租约默认拒绝；启动恢复显式授权且确认无 pending / leased 作业后才可提交    │
  │         同一事务：更新 Run + 助手消息 + 线程版本 + 结果事件 + 结束旧执行作业        │
  │       事务提交后只唤醒 SSE；每个订阅者按自己的数据库游标读取结果事件                │
  │       客户端随后读取 /threads/{id}；漏掉 SSE 也能得到相同结果                       │
  │                                                                                     │
  │     permission.resolve 命令流程：                                                    │
  │       客户端先持久保存不可变决定，再由 /commands 幂等接纳                           │
  │       scope=run 只允许合格的同一工具、风险和图版本，参数变化仍逐次校验，并持续到当前 Run 结束 │
  │       event: permission.resolved { request_id, decision, scope }                   │
  │       current_batch = permission.required since latest run.started/run.resumed     │
  │       if any current_batch permission still pending: return {resumed:false}        │
  │       resume_payload = {"decisions": [...]} 按 permission.required 原顺序          │
  │       接纳事务创建恢复作业，协调器只负责唤醒作业                                   │
  │                                                                                     │
  │     plan.resolve 命令流程：                                                          │
  │       approve / modify / reject 先作为不可变命令持久化                              │
  │       event: plan.approval_resolved { request_id, decision, instructions }          │
  │       与同一等待周期中的问题、权限共同结算；全部解决后才创建恢复作业               │
  │                                                                                     │
  │     tool.reconcile 命令流程：                                                        │
  │       用户确认已完成、确认未执行或停止重试，决定先作为不可变命令持久化             │
  │       同一事务结算工具回执、等待候选、事件和命令回执                              │
  │       与同一等待周期的其他候选全部解决后，才创建恢复作业                          │
  │                                                                                     │
  │     CancelledError                                                                  │
  │       正在执行的 task 及同批 queued/paused receipt 先结算为 canceled，再返回 run.canceled │
  │                                                                                     │
  │     Exception                                                                       │
  │       return 候选结果：failed + run.failed { error, type, retryable,                │
  │                                               action_kind, recovery_action }         │
  │                                                                                     │
  │     finally:                                                                        │
  │       唤醒订阅者并删除进程内唤醒信号；数据库事件日志仍是唯一事实来源                │
  │       每个 SSE 订阅者读到终态且作业结束后退出，生成器最后写 data: [DONE]            │
  └─────────────────────────────────────────────────────────────────────────────────────┘
                                       │
                                       ▼

  ┌─ 4. 横切：观测层 ─────────────────────────────────────────────────────────────────┐
  │   RuntimeObserver(AsyncCallbackHandler) 通过 config["callbacks"] 注入 agent       │
  │                                                                                  │
  │   on_chat_model_start  → llm.start  {model_name, message_count, tags}            │
  │   on_llm_end           → llm.end    {input_tokens, output_tokens, elapsed_ms}    │
  │   on_llm_error         → llm.error  {error_type, error_message}                  │
  │   on_tool_start        → tool.start {tool, input_preview≤200}                    │
  │   on_tool_end          → tool.end   {output_preview≤200, elapsed_ms}             │
  │   on_tool_error        → tool.error                                              │
  │   on_agent_action      → agent.action                                            │
  │   on_agent_finish      → agent.finish                                            │
  │                                                                                  │
  │   每条经过 structlog → JSON (无 TTY) 或 console (TTY) → stderr                    │
  │   可选 + Langfuse（LANGFUSE_*）/ OpenTelemetry                                     │
  └──────────────────────────────────────────────────────────────────────────────────┘
```

---

## 能力清单（测试覆盖）

### Runtime 集成测试（`tests/test_e2e_capabilities.py`）

| # | 能力 | 触发位置 | 测试 case |
|---|---|---|---|
| 1 | **参数化工具确认** | `ToolReviewMiddleware` 在整批执行前生成等待候选 | `capability_1_humanintheloop_pauses_on_destructive_tool` ✅ |
| 1b | 审批恢复 | `permission.resolve` 命令幂等保存决定，再翻译成 `{"decisions": [...]}` | `capability_1b_permission_approve_resumes_the_run` ✅ |
| 1b2 | HITL 多 action 批次审批 | 当前 pause 批次全部 resolved 后按 `permission.required` 顺序 resume | `test_multi_permission_batch_waits_for_all_decisions_before_resume` ✅ |
| 1c | `permission.resolved` 清空审批卡 | HTTP 决策事件持久化，并在恢复流先于 `run.resumed` 重放 | `cap_1c_permission_resolved_event_clears_card` ✅ |
| 1d | **运行级授权** | 合格工具在同风险、同图版本下允许参数变化，并持续到当前 Run 结束；删除类不合格 | `capability_1d_scope_run_stops_asking_for_new_arguments` ✅ |
| 1e | **拒绝回执** | 拒绝不进入工具，保存 `rejected` 回执 | `capability_1e_denied_tool_is_not_executed_and_has_rejected_receipt` ✅ |
| 1f | **整批先暂停** | 混合只读和写入调用时，确认前一个也不执行 | `capability_1f_review_pauses_the_entire_mixed_tool_batch` ✅ |
| 1g | **参数前置校验** | 无效参数不询问用户、不进入工具 | `capability_1g_invalid_tool_arguments_fail_before_review` ✅ |
| 1i | **任务级权限模式** | `ask`、`auto`、`full_access` 在 Runtime 工具审查层裁决，Client 只提交选择 | `test_permission_mode_*` ✅ |
| 2 | **SubAgent 派发** | LLM 返 `task` tool_call；Receipt 持久投影完整生命周期、父子关系和独立用量 | `test_subagent_lifecycle` + `runtime-agent.contract.test.ts` 真实 Runtime 纵向链路 ✅ |
| 2c | **子 Agent 同一执行边界** | 子 Agent 内工具也经过确认和回执 | `capability_2c_subagent_tools_share_review_and_receipt_boundary` ✅ |
| 2d | **同一 Run Team Graph** | `team.run` 用独立 checkpoint thread 执行有界 fan-out、fan-in 和显式 handoff，成员继续复用 `task` Receipt | `test_team_graph` + `capability_2d_team_graph_handoff_uses_nested_task_receipts` ✅ |
| 2e | **Durable Child Run** | `child.spawn/list/check/wait/cancel` 复用 Run/Job/Attempt/lease/checkpoint；父事件原子投影 child 生命周期 | `test_durable_child_runs` + `test_child_runs` + `capability_2e_durable_child_spawn_wait_and_parent_projection` ✅ |
| 2f | **Durable Agent Mailbox** | 同 root 的根/child/同级 child 以持久 typed envelope 通信；发送、回复、投递和确认受租约、Receipt、TTL、背压、幂等与稳定 message id 约束 | `test_agent_mailbox` + `test_agent_mailbox_tools` + `capability_2f_root_and_durable_child_exchange_acknowledged_mailbox_messages` ✅ |
| 3 | 供应商缓存边界 | Runtime 不注入供应商私有缓存标记，由标准供应商适配器决定 | `capability_3_prompt_caching_is_gateway_owned` ✅ |
| 4 | 自动模型回退禁用 | Runtime 不会在失败后自行更换模型或供应商 | `capability_4_local_direct_modelfallback_ignored` ✅ |
| 6 | **AGENTS.md 注入** | `MemoryMiddleware` → system prompt | `cap_6_memory_md` ✅（出站 system 含 marker） |
| 7 | TodoList | `before_agent` 注入 write_todos 工具 | `cap_7_write_todos` + `runtime-agent.contract.test.ts` 状态写入和结果回传 ✅ |
| 8 | 快路径 happy run | 全链 | `cap_8_happy_path` ✅ |

### SSE wire 契约（`tests/test_sse_envelope.py`）

| 契约 | 验证 |
|---|---|
| `data:` 体是 AgentRunEvent envelope；持久事件含 `seq`，临时事件无 `seq` | `test_sse_stream_envelope_and_event_names` ✅ |
| 终止 sentinel 是 `data: [DONE]`，不是 `event: stream.end` | `test_stream_emits_done_sentinel` ✅ |
| `seq` 单调递增（dedupe） | `test_seq_monotonic_per_run` ✅ |
| run 完成后 stream replay 也用 envelope | `test_replay_after_run_completion_has_same_envelope` ✅ |
| 多个实时订阅者各自收到完整、有序的同一事件日志 | `test_each_live_stream_subscriber_receives_the_complete_ordered_event_log` ✅ |

### 间接 / 单测覆盖

| 能力 | 触发位置 | 主要 test 模块 |
|---|---|---|
| 工具异常重试 | `wrap_tool_call` 内，白名单工具的 transient exception 按 `max_tool_retries` 重试 | `test_middleware` |
| 工具结果重试 | `wrap_tool_call` 内，白名单工具返回 `{ok:false,retryable:true}` envelope 时走 `failure_policy.build_retry_decision`，按 `max_tool_retries` 做有界退避；用户/配置/账单/工作区/校验/实现类错误即使误带 `retryable:true` 也 fail-fast | `test_middleware` / `test_agent_builder` |
| Run budget clamps | Settings/env 和 per-run Advanced 覆盖都限制 model calls、tool retries 和 search limit 的安全范围 | `test_advanced_overrides` |
| 模型重试防重复 | 不装配通用 `ModelRetryMiddleware`；仅在模型账本边界对“尚无可见输出、结果确定未产生、失败策略判定为 transient”的同模型调用最多重试 2 次。每次尝试独立记账并共享 `logical_call_id`；已输出、取消、认证、额度、配置、校验和结果不明错误不重试 | `test_model_ledger` / `test_e2e_capabilities` / `test_agent_builder` |
| 步上限 | `before_model` 计数 | `test_middleware` |
| 上下文压缩 | Runtime 把已接纳历史原样交给 Deep Agents 的令牌感知摘要；不再按消息数二次截断或生成关键词摘要。旧对话首次迁入 Runtime 时，client 只保留 256 条 / 750000 字符的传输安全边界 | `test_runs_http` / `conversationHistory.test` |
| 工具结构可见性 | 完整工具集固定属于图定义；Runtime 在模型请求副本中确定性隐藏无关工具。普通事实查询隐藏 `task/team/child/mailbox` 并限制为 12 次模型调用；复杂任务、明确协作意图和 durable child 保留协作工具。明确 blocked 或策略隐藏的工具同时在 `wrap_tool_call` 拒绝，即使模型猜中名字也不能执行 | `test_tool_visibility` / `test_model_ledger` / `test_agent_builder` / `test_e2e_capabilities` |
| 供应商上下文硬限制 | 每次真实模型调用前按声明窗口扣除工具结构并裁剪请求副本；剩余空间不足最小合法请求时在预留调用账本和联系供应商之前明确失败 | `test_model_ledger` |
| 输出、时间与用量边界 | 模型资料限制最大输出，所有模型服务调用有硬超时；Runtime 记录 token，并用调用次数、输出和时间限制资源 | `test_model_ledger` / model-service tests |
| research 收敛 | `before_model` per-tool 计数 | `test_middleware` |
| 大工具结果转存与摘要 | Deep Agents `FilesystemMiddleware` + `SummarizationMiddleware` | `test_agent_builder` / `test_runs_http` |
| orphan tool_call 自愈 | `before_agent` 扫 messages | `test_middleware` |
| Input guard | `@before_agent` | `test_middleware` |
| Output guard | `@after_model` observe-only flag for empty/refusal finals | `test_middleware` |
| 验证回环 | `@after_model + jump_to="model"`，`task.verify` 失败后最多按 `SHEJANE_RUNTIME_VERIFY_REPAIR_MAX` 重做 | `test_middleware` / `test_agent_builder` |
| 用户确认 retry workflow | `metadata.intent=retry` → `<state>` 重试上下文；普通恢复重试携带 source run/message、attempt 和失败分类，帮助模型避免盲目重复失败路径 | `test_runs_http` / `App.test` |
| 编辑重跑 | 复用持久 `run.start`，以当前未替换的用户消息为前置条件；Runtime 原子隐藏旧投影并创建新 Run，旧记录不删除 | `test_run_result_commit` / `App.test` |
| 检查点分叉 | 客户端持久保存 `run.fork`；Runtime 从公开检查点创建新产品对话和明确分支头，同编号重放返回原 Run | `test_runs_http` / `client.test` / `App.test` |
| 用户触发 repair workflow | `metadata.intent=repair` → `<state>` 修复上下文 + `repair.workflow` started/completed/failed/rejected/canceled；client 按 `{conversation_id, assistant_message_id}` 给 repair action 加 in-flight guard，避免同一失败消息被连续点击创建重复替换 run；attempt 超过 `SHEJANE_RUNTIME_REPAIR_WORKFLOW_MAX` 时 fail-fast，不调用模型 | `test_runs_http` / `test_context_builder` / `test_run_recovery` / `App.test` |
| 复杂任务小步执行 | `PlanFirstMiddleware` 按当前 Run 的 `task_input` 写入 `incremental_execution` 状态，不注入 Plan-First 文案；`CompletionRouter` 在 P9 强制先写 2–8 个 todos、只保留一个 `in_progress`、已完成任务不可回退，并阻止未全部完成时提交最终答案；并行研究任务可以在同一次状态更新中共同完成。默认 `auto`，可显式关闭 | `test_plan_first` / `test_middleware` / `test_e2e_capabilities` |
| 同一 Run Team Graph | P6 将成员名、说明、工具面和允许的 handoff 边写入 Agent definition 指纹；`team.run` 在独立 LangGraph checkpoint thread 中用 `Send` 并行派发、reducer 合并结构化结果，并用 `Command` 返回 dispatcher 完成 fan-in / handoff。每个成员仍通过现有 `task` Receipt，因此审批、取消、用量、父子关联和生命周期不旁路；超过 64 KiB 的成员结论只向后继传预览与 Runtime Artifact 引用 | `test_team_graph` / `test_e2e_capabilities` / `test_subagents` |
| Runtime-owned durable child Run | P3.5 在父 Run 当前租约与 `child.spawn` Receipt 下原子创建 child Run 和待执行 Job；P6 冻结 Agent 定义版本与父权限的工具子集；P5/P7 以独立 Attempt/checkpoint 执行和恢复；P4 将 child 快照嵌入父 Run，并提供 `/v1/runs/{id}/children`；P10 的 list/check/wait/cancel 只读写 Runtime 权威状态。单执行槽等待会临时归还执行名额；spawn 提交后崩溃可从 `spawn_operation_id` 自动恢复回执；租约丢失先隔离，迟到 Attempt 不能提交结果 | `test_durable_child_runs` / `test_child_runs` / `test_runs_http` / `test_e2e_capabilities` |
| Durable Agent mailbox | P10 的 `mailbox.send/inbox/reply/ack` 复用当前 Attempt 租约和 Tool Receipt；P6 给 root 与冻结 child 定义加入同一受限工具面；P4/P12 从 Runtime SQLite 投影 typed envelope、inbox/outbox 和 `sent/received/acknowledged/expired` 持久事件。只允许同 root 的 root-child 或 sibling-child，使用稳定 operation/message id 实现重放幂等和 checkpoint 级 at-least-once 投递，并以 TTL、hop、pending 和 root 总量限制阻止循环与无界堆积 | `test_agent_mailbox` / `test_agent_mailbox_tools` / `test_runs_http` / `test_e2e_capabilities` / `runtime-sdk client.test.ts` |
| Coordinator 与远程客户端投影 | P3.5 在 child 接纳事务冻结 dependency、`required/best_effort/quorum` 与精确文件 owner；P5 只领取依赖已成功的 Job，依赖失败原子取消 dependent；P11 在父终态前自动等待 required/quorum、取消 best-effort/多余 quorum，并把失败/取消传播到 child 树。P4 的 `/v1/runs/{root}/collaboration` 用单一 SQLite 读取快照返回成员、等待、mailbox、artifact metadata、依赖、owner 与逐 Run 高水位；SDK 暴露 `getCollaborationSnapshot()`。Runtime 仍只监听 loopback，手机远程网关不是本阶段实现 | `test_collaboration_coordinator` / `test_e2e_capabilities::test_capability_2g_*` / `test_runs_http` / `runtime-sdk client.test.ts` |
| 独立 A2A Gateway | 外部 P1/P3 由单独进程完成 TLS/peer/OIDC/mTLS、版本/正文校验、租户限域和幂等映射；Gateway 经 Runtime `/v1` 创建 Run，并在外部 P4 把 Runtime 快照/事件/Artifact 投影为 A2A Task、JSON-RPC SSE 和持久 push。Gateway SQLite 只拥有外部身份、ID 映射、outbox 和审计；Runtime SQLite 仍拥有执行真相，本地 Runtime 不新增公网路由 | `test_a2a_gateway*` / `test_a2a_runtime_client` / `test_a2a_outbound_client` / 固定 TCK/ITK，见 `docs/a2a-conformance.md` |
| 首轮对话标题 | 首个 Runtime thread 的回答通过 P9 后，使用当前冻结模型做一次独立 `title_generation` 账本调用；生成标题仅在原始种子标题未被用户改名时随 P11 `run.completed` 原子写入，失败则保留种子标题且不影响回答 | `test_e2e_capabilities` / `test_model_ledger` / `test_run_result_commit` |
| 执行结算与资源清理 | 所有结束方式先关闭执行级 `AsyncExitStack`，再从助手草稿、模型账本、工具回执和验证记录生成结构化结果；清理不明时进入不可自动重试的隔离态 | `test_run_jobs` / `test_model_ledger` |
| 长期记忆 | “我的名字是/我叫/My name is”这类明确姓名事实在本轮直接获得写入能力，无需二次确认；本轮明确指令、“记录一下”确认的上一条用户消息，或“记住我的名字”指代的上一条姓名事实也会提取精确事实。`memory.write` 只能写入该能力允许的用户原文，子 Agent 不拥有写权限；工作区检索继承同一所有者的全局事实；旧 `notes.global` 只兼容读取其中的 `user_fact`，清空接口会删除全部旧记录 | `test_memory` / `test_memory_http` / `test_subagents` / `runtime-tools.contract.test.ts` |
| Skills 渐进披露 | `SkillsMiddleware` | `test_agent_builder` / `runtime-agent.contract.test.ts` |
| MCP 目录与调用 | `MCPToolCatalog` 固定目录快照；达到阈值后先用 `mcp.search_tools` | `test_mcp` / `runtime-tools.contract.test.ts` |
| Computer Use 固定能力 | Client 在 Run 外通过 revision 化 readiness 快照逐步完成 Helper/TCC 设置；模型只看到操作 Action。`builtin/computer_use` 复用插件 Action 校验、审批与回执；同一 Run 保存 `stateId`/UI refs，P11 关闭宿主服务 | `test_computer_use_package` / `test_plugins_http` / 插件协议 smoke |
| 文件系统沙箱 | `FilesystemMiddleware` + backend；项目目录是可写根目录，本次附件仅通过 `/attachments/` 暴露被选中的单个文件并保持只读；PDF 在读取边界转换为 UTF-8 文本，不把 Base64 二进制交给模型 | `test_agent_builder` / `test_memory` / `test_runs_http` |
| Shell execute | `FilesystemMiddleware` execute tool | `test_agent_builder` |
| 进展账本与交接新鲜度 | `task.progress` 写入 `progress_ledger` artifact，diagnostics 暴露最新 ledger，并在 handoff 标记 `not_required` / `fresh` / `missing` / `stale`；`run.waiting` 也携带同样的轻量 pause snapshot，client timeline 会保留 missing/stale 状态并在等待中的聊天进度行提示暂停交接风险 | `test_smoke` / `test_runs_http` / `test_user_ask` / `chatStore.test` / `AgentProgress.test` |
| 错误分类诊断 | `handoff.failure` 将最近 `run.failed` / `tool.failed` 归类并标记 recoverable / retryable / action_kind / recovery_action / suggested action；结算不变量错误单独归为 `execution_invariant`。Diagnostics v2 同时导出构建身份、有效执行策略、模型重试链与 Receipt 父子关系 | `test_runs_http` / `test_failure_policy` / `test_observability` |
| 确定性工具失败熔断 | 同一工具连续两次返回完全相同的非临时错误时，CompletionRouter 在下一次模型调用前停止循环并提交 `repeated_tool_failure`，避免耗尽模型调用预算 | `test_middleware` / `test_e2e_capabilities` |
| 文本读取 | `read_file` 未指定 `offset` / `limit` 时读取后端默认的最多 2000 行，显式分页参数保持不变，工具输出仍受上下文长度上限保护 | `test_e2e_capabilities` |
| 文件名冲突 | `write_file` 保持只新建语义。首次撞名返回结构化 `file_exists` 和已探测可用的 `suggested_path`，模型可直接换名或改用读后编辑；同一用户轮次再次提交相同路径时，工具执行暂停并询问“自动换名 / 覆盖原文件 / 取消写入”。选择自动换名后，本轮后续对原路径的读写编辑复用实际新路径；覆盖分支读取当前文本后以精确内容匹配编辑，Runtime 核心不猜测用户意图 | `test_e2e_capabilities` / `test_user_ask` |
| 模型错误 durable failure | 供应商错误进入统一失败策略；不可恢复或重试耗尽后写入结构化 `run.failed` | `test_model_ledger` / `test_runs_http` / `test_agent_builder` / `test_failure_policy` |
| 验证结果诊断 | `handoff.verification` 暴露最新 `task.verify` 结构化结果；最新验证通过时不再把更早的 `task.verify` 失败作为当前 failure/blocker | `test_runs_http` / `DiagnosticsPanel.test` |
| 工具 envelope 失败翻译 | `ToolMessage` content 为 `ok:false` JSON/dict envelope 时翻译成 `tool.failed`，并保留 error_code / recoverable / retryable | `test_event_translator` / `test_runs_http` |
| **流式 token** | `messages` 模式先用 `llm.round.started` 标记新的模型回合，再用 `llm.delta` 发送增量；Client 在新回合开始时替换旧的临时草稿 | `test_streaming_latency` / `chatStore.test` |
| 取消 | 客户端持久保存 `run.cancel` → `POST /v1/commands` → Runtime 在同一事务向 durable child 树保存取消请求与回执；Tool Receipt 从准备态进入执行态时检查当前租约的取消标记，已取消则不启动 handler；运行中的 root/child 执行分别由 `task.cancel()` → `CancelledError` 收口 | `test_runs_http` / `test_run_commands` / `test_subagent_lifecycle` / `test_collaboration_coordinator` / `App.test` |
| 权限决定 | 客户端持久保存 `permission.resolve` → Runtime 原子保存决定、事件与回执；同批候选齐全时创建恢复作业 | `test_runs_http` / `test_tool_receipts` / `client.test` / `App.test` |
| 计划审批 | 客户端持久保存 `plan.resolve` → Runtime 原子保存决定、事件与回执；与同一等待周期的其他候选共同结算 | `test_runs_http` / `test_plan_approval` / `client.test` / `App.test` |
| 工具对账 | 客户端持久保存 `tool.reconcile` → Runtime 原子结算工具回执、等待候选、事件和命令回执 | `test_runs_http` / `test_tool_receipts` / `client.test` / `App.test` |
| 恢复 | 只接受权限、问题、计划审批和工具对账的类型化决定；通用 `/resume` 已删除 | `test_runs_http` / `test_user_ask` |
| 检查点持久化 | `durability="sync"` 保证每个 superstep 在下一步前提交；`checkpoints` 流用租约保护的比较交换更新当前 Run 分支头；diagnostics 只读取该明确分支头 | `test_agent_builder` / `test_runs_http` / `test_run_jobs` |
| 快照与事件恢复 | 助手消息投影原子记录正文覆盖的事件高水位；截断快照用有限 `/events?after=` 补齐等待动作，实时 SSE 再用同一游标只回放后续事件 | `test_run_result_commit` / `test_runs_http` / `test_sse_envelope` / `client.test` / `runtimeProjection.test` / `App.test` |
| 游标重同步 | Runtime 拒绝超出事件窗口的游标；客户端读取完整线程快照后继续订阅 | `test_sse_envelope` / `client.test` / `App.test` |
| 临时增量 | 模型回合边界、逐字文本、推理、临时用量和未完成调用片段只走每订阅者有界队列，不写事件日志或重连重放；模型开始新回合或进入工具调用时清空上一回合的临时正文，失败时不把未完成正文显示为最终回答 | `test_sse_envelope` / `test_run_jobs` / `chatStore.test` / `MessageBubble.test` |
| 观测层 | `RuntimeObserver` callback | `test_observability` ✅ 9 case |

### Failure recovery contract

SheJane follows the same split as LangGraph's fault-tolerance model:

- **Runtime layer** decides retryability and recovery shape. `failure_policy` maps structured failure payloads to `category`, `action_kind`, `retryable`, and the UI-facing `recovery_action`.
- **Persistent layer** stores failure provenance in `run.failed` / `tool.failed` events and diagnostics handoff. Checkpoints remain the resume anchor for interrupted work; failed runs remain inspectable.
- **UI layer** derives CTAs from persisted events. It may deduplicate clicks and show restart reminders, but it must not invent recovery semantics or auto-run after restart.

`waiting_permission` and `waiting_input` are pause states produced by LangGraph interrupts. They are intentionally not `run.failed`, and recovery buttons must not replace the permission/question/plan-approval resume flow.

---

## 关键源码位置

| 概念 | 文件 |
|---|---|
| 入口 + 路由 | [`shejane_runtime/server.py`](../runtime/src/shejane_runtime/server.py) |
| RunCoordinator + driver loop | [`shejane_runtime/runs.py`](../runtime/src/shejane_runtime/runs.py) |
| build_agent + middleware 装配 | [`shejane_runtime/agent/builder.py`](../runtime/src/shejane_runtime/agent/builder.py) |
| Subagent 定义 | [`shejane_runtime/agent/subagents.py`](../runtime/src/shejane_runtime/agent/subagents.py) |
| Runtime middleware | [`shejane_runtime/middleware/`](../runtime/src/shejane_runtime/middleware/) |
| 模型适配、上下文与调用账本 | [`shejane_runtime/llm/`](../runtime/src/shejane_runtime/llm/) |
| LangGraph → 客户端事件翻译 | [`shejane_runtime/event_translator.py`](../runtime/src/shejane_runtime/event_translator.py) |
| structlog + RuntimeObserver | [`shejane_runtime/observability.py`](../runtime/src/shejane_runtime/observability.py) |
| 工具注册 | [`shejane_runtime/tools/registry.py`](../runtime/src/shejane_runtime/tools/registry.py) |
| 持久化 store | [`shejane_runtime/store/sqlite.py`](../runtime/src/shejane_runtime/store/sqlite.py) |
| 独立 A2A Gateway | [`shejane_runtime/a2a_gateway/`](../runtime/src/shejane_runtime/a2a_gateway/) |

---

## 未覆盖（留作 live-credentials 验证）

| 维度 | 为什么 mock 不够 |
|---|---|
| Todo 内容是否语义完整 | 结构与单步状态已由 Runtime 强制；内容质量仍需真 LLM 与结果 grader |
| SubAgent 真完成研究 | 需真 LLM 推理在 researcher 子 agent 里跑完 |
| Memory AGENTS.md 真被遵守 | 验证规则真改变模型输出，需真 LLM |

这些是“真模型是否按预期行动”的问题；框架接入正确性由 Runtime 集成测试覆盖。
