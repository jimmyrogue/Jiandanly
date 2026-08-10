# Stack-Cairn/LiveAgent 对 SheJane 的参考价值

> 调研日期：2026-08-02
> 上游固定版本：[`7de95a20bf93cfe026a57f6367c453e74a50acef`](https://github.com/Stack-Cairn/LiveAgent/tree/7de95a20bf93cfe026a57f6367c453e74a50acef)（2026-07-31）
> 范围：只使用上游仓库、源码、测试、GitHub API、Release 与 Actions 等一手资料。以下建议是机制级参考，不假设 SheJane 当前一定缺少相同能力。

## 结论

LiveAgent 值得参考，但不适合作为 SheJane 的整体架构模板。逐项对照当前源码后，很多醒目的能力——Runtime-owned 附件、幂等命令、SSE 游标恢复、HITL、Checkpoint、Skills/MCP 和同步 Subagent——SheJane 已经有等价或更强实现。

真正值得行动的顺序是：

1. **Now：补齐现有同步 Subagent 的生命周期投影。** Runtime 已发出 `subagent.*`，但 spawned 是可丢的流式猜测，失败仍叫 completed，Client 又没有对应投影，运行中已完成的子任务可能继续显示为执行中。
2. **Conditional：只在首个模型内容交付前，对同一个 Provider/模型做有限瞬时错误重试。** 先从现有 model ledger 证明失败量级；每次 attempt 仍须持久记账，首输出后绝不重试。
3. **Conditional：用现有 Tool Receipt / Artifact 派生机器文件账本。** 只有 compaction eval 证明模型摘要会忘记文件状态时再加；不新建事实表。
4. **Explore：把 Gateway 的恢复不变量留作 Remote Client 参考。** 借鉴 `seq + epoch + bounded replay + reset-to-snapshot`，不把 Go Gateway 或 WebUI 搬进当前本机链路。
5. **Later：开放多文件 Skill 安装来源时再采用 stage-validate-swap。** 当前单文件编辑和固定插件已有自己的原子写入、版本与 digest 纪律。

所以最合理的借法是扩深现有 P4/P10 合同，并把 P8 的安全重试与文件事实作为数据驱动的条件实验；不增加第二套 Agent loop、Checkpoint、消息总线、Gateway 或扩展抽象。

## 项目与架构快照

LiveAgent 是一个 local-first 桌面 Agent：本地可执行文件、Shell、MCP、Skills、Memory 与 Cron；可选的 Gateway 让浏览器远程控制桌面 Agent。[README 对产品边界的定义](https://github.com/Stack-Cairn/LiveAgent/blob/7de95a20bf93cfe026a57f6367c453e74a50acef/README.md#L79-L110)明确表示 Gateway 非必需，桌面端可独立工作。[架构文档](https://github.com/Stack-Cairn/LiveAgent/blob/7de95a20bf93cfe026a57f6367c453e74a50acef/docs/architecture/overview.md#L5-L21)给出的实际分层是：

| 层 | 实现 | 权威职责 |
|---|---|---|
| Desktop UI / Agent runtime | React + TypeScript | 模型调用、工具循环、上下文压缩、交互状态 |
| Desktop privileged backend | Tauri + Rust + SQLite | 本地文件、Shell、MCP、持久化、Gateway bridge；本地高权限真相源 |
| Optional Gateway | Go + WebSocket + Protobuf | 鉴权、会话中继、短期恢复窗口、静态 WebUI；不直接执行本地工具 |
| Browser WebUI | React | 远程 UI，通过 Gateway 间接操作桌面端 |

主要依赖以当前 manifest 为准：桌面前端使用 `@earendil-works/pi-agent-core` / `pi-ai`、React 19、Vite 8，[桌面 package.json](https://github.com/Stack-Cairn/LiveAgent/blob/7de95a20bf93cfe026a57f6367c453e74a50acef/crates/agent-gui/package.json#L20-L68)；Rust 侧使用 Tauri、Tokio、rusqlite、MCP bridge、WebSocket 与 Protobuf，[Cargo.toml](https://github.com/Stack-Cairn/LiveAgent/blob/7de95a20bf93cfe026a57f6367c453e74a50acef/crates/agent-gui/src-tauri/Cargo.toml#L18-L71)；Gateway 使用 Go、gorilla/websocket、protobuf 与 SQLite，[go.mod](https://github.com/Stack-Cairn/LiveAgent/blob/7de95a20bf93cfe026a57f6367c453e74a50acef/crates/agent-gateway/go.mod#L1-L15)。

## 与 SheJane 当前实现的对照

| 领域 | SheJane 当前事实 | 结论 |
|---|---|---|
| Agent 与状态所有权 | Client 只通过 SDK 访问 Python Runtime；Run、Job、lease、Checkpoint、工具回执与最终状态都由 Runtime 持有。[架构不变量](../../CLAUDE.md#L15-L50) | **不要复制** LiveAgent 把 Agent loop 放在 WebView/TypeScript 的边界。 |
| 附件 | Run 接纳时已把本机文件导入 Runtime 的不可变、内容寻址输入存储，模型只看到只读 `/attachments/...` 虚拟路径。[协议](../runtime-protocol.md#L180)、[当前链路](../run-loop.md#L3) | **已覆盖**；LiveAgent 只能作旁证，不需要新建暂存协议。 |
| Schema 与恢复 | Pydantic → OpenAPI → Runtime SDK 已有单一事实源和 drift 门禁；本机命令、seq、replay、cursor reset、snapshot convergence 已存在。[架构不变量](../../CLAUDE.md#L40-L44)、[阶段所有权](../harness-runtime-stages.md#L100-L115) | **已覆盖**；不为 Protobuf 换技术。远程 epoch 留给未来独立 Gateway。 |
| HITL 与工具安全 | SheJane 在 P10 统一做参数化审批、批次暂停、持久决定、Tool Receipt、恢复和 fail-closed 结算。[运行链](../run-loop.md#L135-L149) | **SheJane 更强**；不采用 LiveAgent 的未知/Bash/MCP 默认 allow。 |
| Subagent | 已有 `general-purpose` / `researcher` / `writer`、独立 context、并行 fan-out/fan-in，并复用 P10 审批和回执。[定义](../../runtime/src/shejane_runtime/agent/subagents.py#L142-L240) | 执行主干已覆盖；**真实缺口是 lifecycle contract 与 Client 投影**。 |
| Compaction 事实 | 已有 Deep Agents 压缩、持久 Tool Receipt / Artifact 和模型维护的 `task.progress.files_touched`。[路线图](../roadmap.md#L125-L131) | 文件账本是**有数据再做的实验**，不是 Now 基建。 |
| Skill / 插件 | 单个 `SKILL.md` 已原子替换；固定插件另有不可变 `(plugin_id, version)`、digest 与 release gate。 | 仅在多文件远程安装出现时借 stage-then-swap；不能替代固定插件纪律。 |

## P1–P12 Harness 逐阶段对应

读法：这里沿用 SheJane 唯一的 P1–P12 编号。LiveAgent 没有同名阶段，表中写的是其“最接近机制”，不能因为名称相似就当成等价实现。

| 阶段 | SheJane 当前 Harness | LiveAgent 最接近机制 | 核心差异与结论 |
|---|---|---|---|
| [P1 连接 Runtime](../harness-runtime-stages.md#L119) | Electron Main 管理独立 Runtime 进程、loopback 地址和 bearer；Renderer 不持长期密钥。[当前实现](../../client/electron/main.cjs#L289) | Agent Runtime 实际在 React/WebView；Tauri 连接的 Go Gateway 是可选远程中继。[进程边界](https://github.com/Stack-Cairn/LiveAgent/blob/7de95a20bf93cfe026a57f6367c453e74a50acef/docs/architecture/overview.md#L7-L21) | **本地 Harness 边界是 SheJane 更清楚；远程产品能力是 LiveAgent 领先。** 不把 Agent loop 搬回 Client，未来只借 Gateway hello/reconnect。 |
| [P2 提交幂等命令](../harness-runtime-stages.md#L133) | Client 用 IndexedDB 保存未确认投递、稳定 `command_id` / `client_message_id`，收到回执前可重送。[当前实现](../../client/src/App.tsx#L1768) | 远程 WebUI 有 `client_request_id`；本地桌面仍直接启动 renderer Agent。[协议字段](https://github.com/Stack-Cairn/LiveAgent/blob/7de95a20bf93cfe026a57f6367c453e74a50acef/crates/agent-gateway/proto/v2/gateway.proto#L606-L640) | **SheJane 更完整。** LiveAgent 的 ID 解决远程短时重试，不等于持久 outbox。 |
| [P3 Runtime 原子接纳](../harness-runtime-stages.md#L146) | Runtime 在一个事务内校验并写 Command、消息、Run、Job、冻结绑定和稳定回执。[当前实现](../../runtime/src/shejane_runtime/runs.py#L977) | Gateway 在进程内 mutex 中去重、创建 canonical run 并返回 accepted。[接受路径](https://github.com/Stack-Cairn/LiveAgent/blob/7de95a20bf93cfe026a57f6367c453e74a50acef/crates/agent-gateway/internal/session/conversation_stream.go#L919-L1017) | **SheJane 明显更强。** LiveAgent 去重不跨 Gateway 重启，也不是持久接纳事务。 |
| [P4 快照与变化订阅](../harness-runtime-stages.md#L192) | Runtime DB 是事实源；持久事件有 seq，临时 token 无 seq；Client 断线后 replay，游标过期则读完整 snapshot。[当前投影](../../client/src/features/chat/projection/runtimeProjection.ts#L7) | Gateway 提供 `stream_epoch`、单调 seq、有界 replay、gap/reset 和 active-run snapshot。[窗口不变量](https://github.com/Stack-Cairn/LiveAgent/blob/7de95a20bf93cfe026a57f6367c453e74a50acef/crates/agent-gateway/internal/session/conversation_stream.go#L12-L41) | **各有长处。** SheJane 的事实更耐久；LiveAgent 的 remote epoch/reset/backpressure 值得未来网关参考。SheJane 当前真实缺口是 `subagent.*` Client 投影。 |
| [P5 Worker 领取 Job](../harness-runtime-stages.md#L205) | 持久 Job、lease owner、generation fence、heartbeat 和 lost-lease cancellation。[当前实现](../../runtime/src/shejane_runtime/runs.py#L1462) | 远程 chat inbox 也有 worker、attempt、lease expiry、claim 和 heartbeat，但存在进程内 `HashMap`。[inbox lease](https://github.com/Stack-Cairn/LiveAgent/blob/7de95a20bf93cfe026a57f6367c453e74a50acef/crates/agent-gui/src-tauri/src/services/gateway/chat_inbox.rs#L261-L370) | **SheJane 的恢复与 fencing 更强。** LiveAgent 的 WebView worker liveness 可作远程前端 worker 的失败模式参考。 |
| [P6 绑定资源与 Agent 定义](../harness-runtime-stages.md#L229) | Runtime 绑定唯一模型、credential reference、workspace、MCP snapshot/lease、Skills、固定插件 digest 和 Subagent roster；可复用 Agent 定义不含任务/密钥。[builder](../../runtime/src/shejane_runtime/agent/builder.py#L967) | Renderer 每轮按当前 workdir、provider、Skills、MCP 和设置动态构建工具 registry。[轮次组装](https://github.com/Stack-Cairn/LiveAgent/blob/7de95a20bf93cfe026a57f6367c453e74a50acef/crates/agent-gui/src/pages/chat/turns/runAgentConversationTurn.ts#L394-L453) | **SheJane 的冻结和资源所有权更强。** LiveAgent 的稳定 Subagent 身份、worktree/apply policy 和 Skill stage-swap 是可借的产品细节。 |
| [P7 启动/恢复图](../harness-runtime-stages.md#L241) | LangGraph checkpoint 是执行状态事实源，`durability="sync"`，支持 resume、interrupt 和公开 checkpoint fork。[checkpointer](../../runtime/src/shejane_runtime/store/fenced_checkpointer.py#L20) | JS 内存中构造 `Agent` 并 `continue()`；所谓 summary checkpoint 用于压缩对话上下文。[Agent](https://github.com/Stack-Cairn/LiveAgent/blob/7de95a20bf93cfe026a57f6367c453e74a50acef/crates/agent-gui/src/lib/chat/runner/agentRunner.ts#L1404-L1487)、[summary](https://github.com/Stack-Cairn/LiveAgent/blob/7de95a20bf93cfe026a57f6367c453e74a50acef/crates/agent-gui/src/lib/chat/conversation/conversationState.ts#L417-L461) | **不是同一种 checkpoint。** LiveAgent 能恢复上下文，不等于从工具/中断处恢复执行；P7 不应照搬。 |
| [P8 一轮模型调用](../harness-runtime-stages.md#L252) | Model ledger 和 assistant draft 持久记录预算、首输出、usage 与不明结果；模型在 P6 已冻结且不静默 fallback。[ledger](../../runtime/src/shejane_runtime/llm/ledger.py#L51) | `agentRunner` 处理 provider 参数、上下文、text/thinking/tool-call/usage 流和三种压缩触发。[模型流](https://github.com/Stack-Cairn/LiveAgent/blob/7de95a20bf93cfe026a57f6367c453e74a50acef/crates/agent-gui/src/lib/chat/runner/agentRunner.ts#L1258-L1374) | **SheJane 的持久账本更强。** LiveAgent 的“首内容提交前才允许同模型重试”值得条件性借鉴；机器文件账本只在 compaction 评测证明需要后再做。 |
| [P9 判断模型输出](../harness-runtime-stages.md#L267) | 唯一 `CompletionRouter` 把输出分成工具批次、最终候选或失败，并做 todo/验收证据/review/有限 repair。[router](../../runtime/src/shejane_runtime/middleware/completion_router.py#L24) | 主要依据 `stopReason`、tool calls/results 决定继续或结束；正常结束即形成完成结果。[输出分支](https://github.com/Stack-Cairn/LiveAgent/blob/7de95a20bf93cfe026a57f6367c453e74a50acef/crates/agent-gui/src/lib/chat/runner/agentRunner.ts#L1561-L1677) | **SheJane 更强。** LiveAgent 未显示等价的持久 validation receipt、完成证据和 blocked/repair 路由。 |
| [P10 工具或等待用户](../harness-runtime-stages.md#L279) | 参数校验、整批 preflight、`operation_id`、arguments hash、持久 Tool Receipt、HITL checkpoint、结果未知对账、Sandbox 与恢复共用一个边界。[execution](../../runtime/src/shejane_runtime/middleware/tool_execution.py#L240) | `beforeToolCall` 做 allow/ask/deny，审批 Promise 支持超时/中止/会话校验；工具在桌面宿主直接执行。[审批](https://github.com/Stack-Cairn/LiveAgent/blob/7de95a20bf93cfe026a57f6367c453e74a50acef/crates/agent-gui/src/pages/chat/turns/runAgentConversationTurn.ts#L481-L543) | **SheJane 的安全与恢复明显更强。** LiveAgent 默认允许未知/内置/MCP，且等待占住 JS turn；只借 Subagent lifecycle/UI，不借执行边界。 |
| [P11 结算与释放资源](../harness-runtime-stages.md#L295) | `AsyncExitStack` 先关闭本次模型、插件、MCP/宿主资源，再对账 draft、ledger、receipts 和 checkpoint；清理不明进入 quarantine。[当前实现](../../runtime/src/shejane_runtime/runs.py#L1696) | 分散的 `finally` / finalization 做 best-effort 清理，等待历史持久化后才发布 terminal snapshot，整体有 2 秒等待上限。[finalization](https://github.com/Stack-Cairn/LiveAgent/blob/7de95a20bf93cfe026a57f6367c453e74a50acef/crates/agent-gui/src/pages/chat/runtime/chatRunFinalization.ts#L13-L82) | **SheJane 更完整。** 可借 `persist-before-done`，以及 [ManagedProcess PID/start-time orphan journal](https://github.com/Stack-Cairn/LiveAgent/blob/7de95a20bf93cfe026a57f6367c453e74a50acef/crates/agent-gui/src-tauri/src/runtime/managed_process_journal.rs#L1-L9)；不借其分散的 best-effort cleanup 模型。 |
| [P12 原子提交结果](../harness-runtime-stages.md#L308) | 一个 Runtime 事务提交 assistant message、Run、Job/Attempt、usage、receipts、artifacts、verification、事件和 thread version，再发可丢失唤醒。[当前实现](../../runtime/src/shejane_runtime/runs.py#L3114) | SQLite 事务原子写 conversation header + history segments；UI state 已先应用，再持久化并发布 history sync。[历史事务](https://github.com/Stack-Cairn/LiveAgent/blob/7de95a20bf93cfe026a57f6367c453e74a50acef/crates/agent-gui/src-tauri/src/commands/history/chat_history/commands.rs#L236-L289)、[UI-before-persist](https://github.com/Stack-Cairn/LiveAgent/blob/7de95a20bf93cfe026a57f6367c453e74a50acef/crates/agent-gui/src/pages/chat/turns/runAgentConversationTurn.ts#L1213-L1227) | **SheJane 明显更强。** LiveAgent 的事务只覆盖聊天历史，没有 stale-attempt fence，也不是 Runtime 权威结果提交。 |

### 阶段结论

- **SheJane 的核心优势**：P1–P3、P5、P7、P9–P12 的独立 Runtime、持久状态、租约 fencing、Checkpoint、Tool Receipt、验证和原子终态。
- **LiveAgent 真正值得借**：P4 的远程 epoch/reset/replay、P5 的 WebView worker liveness、P6 的工具 registry 与路径 scope 细节、P8 的首输出前安全重试和条件性文件账本、P11 的 persist-before-done 顺序。
- **近期唯一明确缺口**：深化现有 P4/P10，把 Subagent 从可丢的工具事件补成可恢复、可投影的生命周期；不新建第二套 Agent Runtime。

## P4 深聊：远程连接与手机端

先分清两件事：**P4 不是“让手机连进来”的全部功能**。P1 负责建立远程会话，P2/P3 负责命令只接纳一次，P4 负责手机断网、切后台或被系统挂起后，重新得到同一份权威状态。远程产品要一起贯通 P1–P4，但执行、工具权限、Checkpoint 和最终结果仍留在桌面 Runtime。

```text
iPhone / Android（可丢的本地投影）
        ⇅  HTTPS + 前台实时流；每设备凭证
独立 Remote Gateway（TLS、配对、撤销、限流、路由、Push 提示）
        ⇅  由桌面端主动建立的出站连接
Desktop Connector
        ⇅  现有 loopback HTTP / SSE
SheJane Runtime（对话、Run、事件、权限和 Artifact 的唯一事实源）
```

这样不要求用户给家里电脑开入站端口，也不把 Runtime 的本地 owner token 发到手机。Gateway 只提供一个经过 allowlist 的远程安全投影，不应成为第二个 Agent Runtime、第二个 Command owner 或第二份聊天数据库。

### 与 LiveAgent 的 P4 逐项比较

LiveAgent 当前没有原生移动 App；最接近的实现是 Browser WebUI → Gateway → Desktop Agent。它证明了远程中继和恢复机制可行，但不是可以原样复制的移动端安全模型。

| 关注点 | SheJane 当前 P4 | LiveAgent 固定 SHA | 手机端取舍 |
|---|---|---|---|
| 权威状态 | Runtime DB 保存 thread snapshot、Run、持久事件与游标；Client 只是可重建投影。[P4 所有权](../harness-runtime-stages.md#L100-L115) | Desktop history 是事实源，Gateway 只保留当前进程内的短期事件窗口。[Gateway 状态](https://github.com/Stack-Cairn/LiveAgent/blob/7de95a20bf93cfe026a57f6367c453e74a50acef/docs/architecture/gateway.md#L52-L71) | 保留 SheJane 现状；手机和 Gateway 都不能拥有最终状态。 |
| 命令与订阅 | `POST /v1/commands` / `POST /v1/runs` 与 `GET .../stream?after=` 已分开；P2 outbox 使用稳定 ID。[当前协议](../runtime-protocol.md#L174-L183) | `chat.prepare → chat.command → chat.subscribe` 分开，ACK 丢失时复用 `client_request_id`。[Chat 协议](https://github.com/Stack-Cairn/LiveAgent/blob/7de95a20bf93cfe026a57f6367c453e74a50acef/docs/architecture/protocols.md#L105-L118) | 借这个分工，不借其进程内去重；手机 outbox 原样重送，最终由 Runtime P3 持久去重。 |
| 游标与恢复 | 每个 Run 的 durable event 有单调 `seq`；thread 另有 change cursor；越界返回 `event_cursor_reset_required`。[恢复规则](../runtime-protocol.md#L243-L248) | conversation 级 `seq + stream_epoch`，窗口内 replay；epoch/gap/淘汰后 reset。[恢复机制](https://github.com/Stack-Cairn/LiveAgent/blob/7de95a20bf93cfe026a57f6367c453e74a50acef/crates/agent-gateway/internal/session/conversation_stream.go#L383-L500) | Runtime `seq` 继续是持久游标；Gateway epoch 只描述远程连接代次。reset 后一定回到 Runtime snapshot。 |
| 实时增量 | token、reasoning、未完成 tool chunk 可丢；最终消息、状态和用量可由 snapshot/event 收敛。[事件边界](../runtime-protocol.md#L67-L78) | 10 分钟、4096 条、约 8 MiB 的内存窗口；断层后依赖桌面 snapshot。[窗口上限](https://github.com/Stack-Cairn/LiveAgent/blob/7de95a20bf93cfe026a57f6367c453e74a50acef/docs/architecture/gateway.md#L63-L71) | 手机切后台丢几个字的动画可以接受，不能丢最终答案、等待审批或终态。 |
| 慢网络 | 当前 SSE 以 snapshot/replay 保证最终一致，临时队列只负责低延迟通知。 | control/data 双队列，ACK、reset 等控制帧优先；单订阅过载后 reset，而不是拖垮全部连接。[传输分层](https://github.com/Stack-Cairn/LiveAgent/blob/7de95a20bf93cfe026a57f6367c453e74a50acef/docs/architecture/gateway.md#L73-L85) | 借控制流优先和“慢流单独 reset”；第一版继续用现有 JSON/HTTP/SSE 语义，不为移动端换 Protobuf。 |
| 连接与任务 | P4 明确“连接结束不代表任务结束”。[阶段定义](../harness-runtime-stages.md#L192-L203) | Desktop Agent 继续执行，WebUI 前台恢复后重连；没有 APNs/FCM。 | 这正适合手机：锁屏后 Runtime 继续做，手机不需要常驻。 |
| 设备安全 | 当前 pairing token 是 loopback 本地 owner 凭证，统一映射为 `local:owner`。[当前认证](../../runtime/src/shejane_runtime/auth.py#L20-L55) | Browser 共用高权限 Gateway token 并存入 `localStorage`；per-Agent token 反而只给 Desktop 链路。[Web token](https://github.com/Stack-Cairn/LiveAgent/blob/7de95a20bf93cfe026a57f6367c453e74a50acef/crates/agent-gateway/web/src/lib/storage.ts#L1-L12) | 不能照搬。需要一次性 QR 配对、每设备独立凭证、过期/轮换/撤销；iOS 凭证放 Keychain。 |
| 后台提醒 | 当前桌面产品没有移动通知层。 | WebUI 依靠 `focus/visibility/resume` 后重连，没有系统 Push。 | APNs/FCM 只发低敏感 `sync_hint`；打开 App 后仍按 snapshot/cursor 对账。 |

### 手机从断线到恢复，实际发生什么

1. 手机前台连接 Gateway，带设备身份；Gateway 通过桌面 Connector 找到用户自己的 Runtime。
2. 手机先读取 thread/Run snapshot 和其中的安全高水位，再从 `after=<seq>` 订阅后续变化，避免“先订阅还是先读取”的竞态。
3. 用户发消息时，手机先把不可变命令写入本地 outbox；断网重试始终使用相同 `command_id` 和 payload，由 Runtime 返回持久 receipt 后才删除。
4. 手机锁屏或被 iOS 挂起时连接可以自然断开，桌面 Runtime 继续执行；逐字动画允许丢失。
5. Gateway 在 `permission.required`、`question.asked` 或 Run 进入终态时可以发 Push，但 payload 只放不敏感的同步提示。手机回到前台后重新读 snapshot，再 replay 游标后的事件；若收到 reset，就丢弃临时尾巴并完整重建。

Apple 官方明确说明，App 进入后台后可能被挂起；后台通知是低优先级、可能延迟或不送达，还会被限流。因此 WebSocket 和静默 Push 都不能承担正确性。[后台状态](https://developer.apple.com/documentation/uikit/about-the-background-execution-sequence)、[后台 Push](https://developer.apple.com/documentation/usernotifications/pushing-background-updates-to-your-app) 设备凭证应使用系统 Keychain，而不是普通偏好设置或数据库明文列。[Keychain Services](https://developer.apple.com/documentation/security/keychain-services/)

### 移动端可发布最小版本

| 第一版要做 | 第一版明确不做 |
|---|---|
| 桌面显示一次性 QR；查看并撤销每台手机 | 手机管理 Provider/API Key、Skills、MCP 或插件 |
| Thread 列表、完整对话、发文字、新建/继续任务 | 手机授权新 workspace、浏览任意主机文件 |
| 前台实时状态与文本；断线后 snapshot + cursor 恢复 | 为追求逐字不断流而维持后台 WebSocket |
| approve/deny、回答问题、确认计划、取消 Run | 附件上传；当前接口接受的是 Runtime 主机路径，不能伪装成手机路径 |
| 已完成/需处理/失败的 Push 提示 | 终端、SSH、远程 Shell、诊断与管理后台 |

第一版可继续复用 Runtime 的 `/v1/runtime`、threads、runs、commands、SSE 和只读 models 语义；Gateway 只公开所需子集并隐藏主机绝对路径。原生 Swift/Kotlin 客户端从同一 OpenAPI/schema 生成适配层即可，不应再维护一套移动端 wire schema。

从工程验证角度，最小的第一步不是先造完整 Gateway，而是用仅开发环境可见的私网/VPN 做一个前台 P4 探针，验证“snapshot → stream → 断网 → replay/reset → 收敛”。验证通过后再实现产品级 Gateway、设备配对和 Push；私网探针不能作为公开发布架构。

## P6 深聊：先把本轮工具箱封好

用人话说，P6 不是 Agent 已经开始干活，而是开工前发工牌、配工具箱并贴封条：**这次只能用哪个模型、哪个工作区、哪些工具、Skills、MCP、插件和子 Agent**。它输出的是可复用的 Agent 结构，以及本次执行持有的资源租约；API Key、任务 ID 和临时权限不能混进可复用结构。

```text
P6 配好并封存工具箱
        ↓
P8 调一次模型 → P9 判断输出 → P10 执行工具/等用户
        ↑                              ↓
        └──────── 有工具结果后再调一次 ────────┘
```

所以一个 Run 通常只在执行 Attempt 开头经过一次 P6，却可能反复经过很多次 P8。

### 与 LiveAgent 的 P6 逐项比较

| 关注点 | SheJane | LiveAgent 固定 SHA | 判断 |
|---|---|---|---|
| 谁来装配 | Runtime 根据已接纳的 Run 绑定模型、workspace、Skills、插件和 MCP，再构建 Agent。[当前装配](../../runtime/src/shejane_runtime/runs.py#L2435-L2590) | Renderer 每个 conversation turn 读取当前 workdir、provider、Skills、MCP 和设置，动态创建工具 registry。[当前装配](https://github.com/Stack-Cairn/LiveAgent/blob/7de95a20bf93cfe026a57f6367c453e74a50acef/crates/agent-gui/src/pages/chat/turns/runAgentConversationTurn.ts#L394-L453) | SheJane 的所有权边界更稳；不要把装配搬回 Client。 |
| 资源是否冻结 | 模型选择与 credential reference 在接纳时冻结；插件绑定精确 version/digest 并持租约；MCP 使用固定目录快照；Skill 目录指纹在接纳和执行前复核，但执行中仍由 middleware 读取真实目录。[P6 目标](../harness-runtime-stages.md#L229-L239)、[Skill 复核](../../runtime/src/shejane_runtime/runs.py#L933-L946) | MCP 明确 live-read 当前设置；加载失败默认可警告后继续，Skills 也能在回合中安装或更新。[MCP live read](https://github.com/Stack-Cairn/LiveAgent/blob/7de95a20bf93cfe026a57f6367c453e74a50acef/crates/agent-gui/src/lib/tools/builtinRegistry.ts#L144-L175)、[Skill 变更](https://github.com/Stack-Cairn/LiveAgent/blob/7de95a20bf93cfe026a57f6367c453e74a50acef/crates/agent-gui/src/lib/tools/skillTools.ts#L595-L674) | SheJane 更强，但 Skill 尚非整个 Attempt 的字节级快照；LiveAgent 则连内容绑定也没有。 |
| 定义能否安全复用 | 模型画像、工具 schema、Skill/插件哈希与中间件形成结构指纹；有界进程内 LRU 复用结构，真实模型与工具由本 Attempt 注入。[定义指纹](../../runtime/src/shejane_runtime/agent/builder.py#L1413-L1513) | 没有独立、不可变、带版本或 digest 的 Agent Definition；主要是当场创建 registry 和闭包。 | SheJane 已经做对；持久化编译缓存目前只是未经证明的性能需求。 |
| 工具与路径细节 | 工具全集由定义拥有，后续阶段只能隐藏，不能加权；workspace/attachment/plugin 也有自己的权限边界。 | 单一 registry 统一 metadata/executor；可信 builtin 冲突报错，第三方冲突告警并保留先到者；绝对路径按“最具体根目录”判断 scope。[冲突规则](https://github.com/Stack-Cairn/LiveAgent/blob/7de95a20bf93cfe026a57f6367c453e74a50acef/crates/agent-gui/src/lib/tools/builtinRegistry.ts#L49-L105)、[路径 scope](https://github.com/Stack-Cairn/LiveAgent/blob/7de95a20bf93cfe026a57f6367c453e74a50acef/crates/agent-gui/src/lib/tools/pathUtils.ts#L369-L515) | 这些是可作测试清单的小细节，不需要改 P6 架构。 |

### P6 结论

**P6 暂时不用立项重构。** 我们真正重要的部分——Runtime 所有权、唯一模型、不可变插件字节、MCP snapshot/lease、结构与密钥分离——都比 LiveAgent 更完整。LiveAgent 可借的是 registry 冲突规则、typed path scope 和 MCP 连接淘汰等实现细节，不是它的动态装配边界。

唯一值得留作未来门槛的是：若以后出现长时间运行、远程更新 Skill，或复现“复核后、读取前目录变化”的真实问题，再把 Skill 从“执行前校验目录指纹”升级为“本 Attempt 持有内容寻址快照/租约”。现在没有证据，不新增一套 Skill package 系统。

## P8 深聊：真正问模型一次，并把这笔账记清

P8 是一次模型请求，不是整个 Agent Run。它把当前消息、可见工具 schema 和上下文预算交给 **P6 已选定的唯一模型**，接收正文、思考、工具调用和 usage，再把这一轮完整结果写回 Runtime。工具执行完后，通常会再进入下一次 P8。

### 与 LiveAgent 的 P8 逐项比较

| 关注点 | SheJane | LiveAgent 固定 SHA | 判断 |
|---|---|---|---|
| 调用与工具权限 | `LedgerChatModel` 只调用 P6 绑定的模型；本轮工具可见性只能从既有集合中缩小。[P8 目标](../harness-runtime-stages.md#L252-L265)、[工具收窄](../../runtime/src/shejane_runtime/middleware/tool_visibility.py#L155-L233) | 每轮清理 messages、附加规则、过滤工具，再交给 provider adapter。[请求组装](https://github.com/Stack-Cairn/LiveAgent/blob/7de95a20bf93cfe026a57f6367c453e74a50acef/crates/agent-gui/src/lib/chat/runner/agentRunner.ts#L1258-L1374) | 两者思路接近；SheJane 的冻结定义更明确。 |
| 记账与崩溃边界 | 请求前持久预留；首个可见输出交付前落账；结束后结算 usage/request ID；异常写 `failed` 或 `outcome_unknown`。[持久账本](../../runtime/src/shejane_runtime/llm/ledger.py#L51-L250) | usage、provider、model 和 stop reason 主要写进 assistant metadata；没有等价的持久 call receipt 或 `outcome_unknown`。[usage metadata](https://github.com/Stack-Cairn/LiveAgent/blob/7de95a20bf93cfe026a57f6367c453e74a50acef/crates/agent-gui/src/pages/chat/turns/runAgentConversationTurn.ts#L599-L623) | SheJane 明显更强，不能换成 UI/消息级 usage。 |
| 瞬时错误重试 | 当前不做自动模型重试，也不静默换 Provider；这样最容易保证不产生重复输出和副作用。 | 先缓存本 attempt 事件；若首个正文、thinking 或 tool-call 交付前发生瞬时错误，就丢弃这次事件并用同一模型重试；一旦提交首内容便绝不重试。[retry-before-commit](https://github.com/Stack-Cairn/LiveAgent/blob/7de95a20bf93cfe026a57f6367c453e74a50acef/crates/agent-gui/src/lib/providers/runtime/streamRetry.ts#L74-L163) | **这是 P8 最值得条件性借的机制。** 但每个 attempt 必须进入 SheJane 持久账本，且先证明真实瞬时失败值得增加复杂度。 |
| 压缩与成本 | 目标顺序是 prune/offload/retrieve/summarize，工具 schema 也计入窗口；当前自动压缩主要委托 Deep Agents `SummarizationMiddleware`。[当前边界](../../runtime/src/shejane_runtime/runs.py#L2396-L2404) | 把 output reserve、旧 tool result 确定性 prune、同模型 summary、校验/修复、checkpoint/resume 拆成显式可测流水线；也可在流中 abort、压缩、重新请求。[压缩控制](https://github.com/Stack-Cairn/LiveAgent/blob/7de95a20bf93cfe026a57f6367c453e74a50acef/crates/agent-gui/src/lib/chat/compaction/controller.ts#L130-L377) | 可借作 compaction eval/checklist；不照搬流中 abort，因为旧请求可能已计费，LiveAgent 又缺少 SheJane 的持久 attempt 对账。 |
| Skill 渐进披露 | P8 目标只加载本轮需要的 Skill；当前由 Skills middleware 暴露目录并按需读取。[Skill 目录](../../runtime/src/shejane_runtime/agent/builder.py#L1267-L1279) | 识别本轮显式 Skill mention，先给 metadata，需要时再读全文。[mention 装配](https://github.com/Stack-Cairn/LiveAgent/blob/7de95a20bf93cfe026a57f6367c453e74a50acef/crates/agent-gui/src/pages/chat/runtime/useSendChatTurn.ts#L1208-L1272) | 方向已一致；把显式 mention 与 progressive disclosure 当 UX/Eval 检查项，不另建加载框架。 |
| 压缩后文件事实 | 已有持久 Tool Receipt / Artifact，可作为机器事实来源；assistant draft 由完整 `AIMessage` 更新，不依赖 token 动画。[当前模型链](../run-loop.md#L185-L200) | 额外从成功的 `Read/Write/Edit/Delete` 提取有界文件账本，跨 checkpoint 合并；作者明确它不覆盖 Shell/MCP 间接副作用。[文件账本](https://github.com/Stack-Cairn/LiveAgent/blob/7de95a20bf93cfe026a57f6367c453e74a50acef/crates/agent-gui/src/lib/chat/compaction/fileLedger.ts#L3-L16) | 只有 eval 证明压缩后确实忘文件时，才从现有 Receipt/Artifact 派生；不创建第二个事实源。 |

### P8 结论

**P8 也不需要重写。** SheJane 的 durable reservation、首输出 commit、usage settlement、`outcome_unknown` 和完整 assistant draft，解决的是“钱有没有算清、崩溃后发生过什么”，这比 LiveAgent 的消息元数据更可靠。

只保留两个条件实验：

1. **同模型首输出前安全重试**：先统计 model ledger 中可判定的瞬时失败；确实频繁时，做少量有界重试，每次 attempt 独立记账，首输出后绝不重试，也绝不换 Provider/模型。
2. **Receipt 派生文件账本**：只在 compaction eval 复现文件遗忘后做；Shell/MCP/间接副作用明确保持 unknown。

前者是可靠性优化，后者是模型记忆优化；两者都不是当前正确性缺口。

LiveAgent 更显式的 compaction pipeline 和 Skill mention 可直接进入现有测试清单，但不构成第三、第四个基础设施项目。

## 建议矩阵

| 优先级 | 上游机制与证据 | SheJane 可借鉴的最小部分 | 采用门槛 |
|---|---|---|---|
| **Now** | **Subagent lifecycle taxonomy**：LiveAgent 为 child 保存稳定 id、run status、模型、轮次、工具次数与取消状态，并给 UI 单独的 subagent 协议。[类型](https://github.com/Stack-Cairn/LiveAgent/blob/7de95a20bf93cfe026a57f6367c453e74a50acef/crates/agent-gui/src/lib/subagents/types.ts#L20-L106)、[文档](https://github.com/Stack-Cairn/LiveAgent/blob/7de95a20bf93cfe026a57f6367c453e74a50acef/docs/features/tools.md#L67-L89) | 不复制其表结构；用现有 P10 Tool Receipt 的 `operation_id` 投影 durable `spawned/running/completed/failed/cancelled`，SDK 和 Client 只补对应判别类型与投影。[当前生产端](../../runtime/src/shejane_runtime/event_translator.py#L106-L178)、[当前 Client 缺口](../../client/src/features/chat/projection/chatStore.ts#L19-L26)、[in-flight 清理](../../client/src/features/chat/components/progress/AgentProgress.tsx#L1155-L1184) | 先修完整纵向合同与 UI 收敛；不增加 child 表、稳定人格、worktree 或 Message Bus。 |
| **Conditional** | **首内容提交前重试**：LiveAgent 缓冲单次 attempt 的流事件；只在 text/thinking/tool-call 尚未对外提交时，才允许同模型处理瞬时错误。[实现](https://github.com/Stack-Cairn/LiveAgent/blob/7de95a20bf93cfe026a57f6367c453e74a50acef/crates/agent-gui/src/lib/providers/runtime/streamRetry.ts#L74-L163) | 保留现有唯一模型与 `outcome_unknown` 纪律；每个 retry attempt 都走 durable reservation/settlement，并使用小而明确的上限。 | 先用 ledger 数据证明瞬时失败率和可恢复性；首输出后禁止重试，禁止静默换 Provider/模型。 |
| **Conditional** | **确定性文件账本**：只从成功的结构化 `Read/Write/Edit/Delete` 提取路径，做注入清洗、预算和跨 checkpoint 合并。[实现](https://github.com/Stack-Cairn/LiveAgent/blob/7de95a20bf93cfe026a57f6367c453e74a50acef/crates/agent-gui/src/lib/chat/compaction/fileLedger.ts#L3-L15)、[提取与合并](https://github.com/Stack-Cairn/LiveAgent/blob/7de95a20bf93cfe026a57f6367c453e74a50acef/crates/agent-gui/src/lib/chat/compaction/fileLedger.ts#L135-L200)、[测试](https://github.com/Stack-Cairn/LiveAgent/blob/7de95a20bf93cfe026a57f6367c453e74a50acef/crates/agent-gui/test/chat/compaction-file-ledger.test.mjs#L136-L156) | 从现有 Receipt / Artifact 派生机器事实，和模型 prose 分开；`execute` 等不可完全观察的副作用明确标为 unknown。 | 只有 compaction eval 证明存在文件遗忘；不猜 Shell 副作用，不新增事实表。 |
| **已覆盖** | **Runtime-owned 附件**：Gateway 只传 bytes，桌面决定暂存、授权与读取根。[上传](https://github.com/Stack-Cairn/LiveAgent/blob/7de95a20bf93cfe026a57f6367c453e74a50acef/crates/agent-gateway/internal/handler/upload.go#L14-L97)、[授权](https://github.com/Stack-Cairn/LiveAgent/blob/7de95a20bf93cfe026a57f6367c453e74a50acef/crates/agent-gui/src-tauri/src/commands/app/system.rs#L577-L638) | SheJane 已在 Run 接纳时导入不可变内容寻址存储，并冻结 `/attachments/...`。 | 无动作；继续用现有 Runtime 协议。 |
| **已覆盖** | **单一 wire schema 与 settlement**：LiveAgent 用 Proto 生成和 breaking/drift CI；审批使用 deadline、conversation binding 和 first settlement wins。[schema CI](https://github.com/Stack-Cairn/LiveAgent/blob/7de95a20bf93cfe026a57f6367c453e74a50acef/.github/workflows/ci.yml#L54-L70)、[审批](https://github.com/Stack-Cairn/LiveAgent/blob/7de95a20bf93cfe026a57f6367c453e74a50acef/crates/agent-gui/src/lib/tools/toolApproval.ts#L114-L191) | SheJane 已有 Pydantic/OpenAPI/SDK drift gate，并有更严格的持久 HITL / Receipt。 | 无动作；不换 Proto，不弱化 fail-closed 默认值。 |
| **Explore：Remote Client** | **可恢复远程流**：单调 `seq`、终态 first-wins、有界 10 分钟/4096 事件/8 MiB 缓存、epoch reset 与 snapshot reconciliation。[不变量](https://github.com/Stack-Cairn/LiveAgent/blob/7de95a20bf93cfe026a57f6367c453e74a50acef/crates/agent-gateway/internal/session/conversation_stream.go#L12-L41)、[恢复](https://github.com/Stack-Cairn/LiveAgent/blob/7de95a20bf93cfe026a57f6367c453e74a50acef/crates/agent-gateway/internal/session/conversation_stream.go#L383-L500) | 未来独立 Gateway 复用“设备身份/撤销 + epoch/replay/reset-to-snapshot”失败模式表；Runtime 仍只监听 loopback。 | 有真实远程需求后再设计；LiveAgent 的 command dedupe 仅在当前进程，不能替代 SheJane 的 durable command owner。 |
| **Later：多文件安装** | **Skill stage-then-swap**：进程级写锁，隐藏 staging 完整构建、验证后 atomic rename。[说明](https://github.com/Stack-Cairn/LiveAgent/blob/7de95a20bf93cfe026a57f6367c453e74a50acef/docs/features/skills-and-mcp.md#L5-L12)、[约束](https://github.com/Stack-Cairn/LiveAgent/blob/7de95a20bf93cfe026a57f6367c453e74a50acef/crates/agent-gui/src-tauri/src/services/skills/install.rs#L1-L17) | 只补未来完整目录安装的原子性与单一写路径。 | 不能替代固定插件 `(plugin_id, version)`、digest 与不可变字节纪律。 |
| **Explore：完整团队拓扑** | **Subagent readonly/worktree、apply policy、消息与恢复**。[文档](https://github.com/Stack-Cairn/LiveAgent/blob/7de95a20bf93cfe026a57f6367c453e74a50acef/docs/features/tools.md#L67-L89) | 只把并发上限、workspace policy、失败/取消 taxonomy 当检查表。 | 当前同步 fan-out/fan-in 足够；父 Agent 仍 await 批次，不要包装成 durable background Agent。 |

### 若落地 Subagent lifecycle，阶段归属

```text
主要阶段：P4（公开 snapshot / SSE / Client projection 合同）
直接相邻阶段：P3 / P5
受影响的事件生产阶段：P10、P12
状态所有者：Runtime 持久事件日志；底层执行事实继续由 P10 Tool Receipt 拥有
替换的当前路径：AIMessageChunk 猜 spawned → ToolMessage 一律翻译为 completed → Client default raw event
```

如果只改后端的 `Receipt → lifecycle` 投影，局部主阶段是 P10；完整产品改动按“从 P1 扫描后选择最早受影响阶段”的规则归为 P4。[阶段规则](../harness-runtime-stages.md#L49-L69)

## 不建议照搬

- **不复制整体技术栈或 Gateway**：上述机制与 Go、Tauri、WebSocket/Protobuf 并不绑定；SheJane 若已有 Client ↔ Runtime 边界，应在现有所有权模型内实现。
- **不复制 GUI/WebUI 双份源码**：LiveAgent 用 manifest 强制 119 个文件逐字节镜像，[manifest](https://github.com/Stack-Cairn/LiveAgent/blob/7de95a20bf93cfe026a57f6367c453e74a50acef/scripts/mirror-manifest.json#L1-L125)并在 CI 检查 byte-identical，[检查脚本](https://github.com/Stack-Cairn/LiveAgent/blob/7de95a20bf93cfe026a57f6367c453e74a50acef/scripts/check-mirror.mjs#L20-L60)。这是对既有重复的负责治理，不是值得主动制造的结构。
- **不复制默认放行策略**：审批机制本身扎实，但未配置工具默认 allow；这只能视为兼容性选择，不能视为安全默认值。
- **不复制明文凭据与宿主 Shell 边界**：Provider / SSH secret 落 SQLite 文本列，Bash 直接以当前用户权限启动；这都弱于 SheJane 的 credential store 与 Sandbox/HITL 纪律。
- **不一次性引入 ClawHub、多 registry、Memory organizer、Cron、Tunnel、SFTP、Subagent 全套能力**：它们显著扩大权限面和测试矩阵，只有明确产品需求才能抵消复杂度。
- **不把 LLM 摘要当审计记录**：LiveAgent 的文件账本本身也明确只是结构化 FS 操作的“下界”，Shell 等间接副作用不会猜测；正确借鉴是“机器事实与模型叙述并存”。

## 安全与文档可信度提醒

1. README 声称 Gateway “never stores any credentials”，[FAQ](https://github.com/Stack-Cairn/LiveAgent/blob/7de95a20bf93cfe026a57f6367c453e74a50acef/README.md#L347-L359)；实际代码会持久化每台 Agent 的 `token_sha256`，明文仅签发一次，[token store](https://github.com/Stack-Cairn/LiveAgent/blob/7de95a20bf93cfe026a57f6367c453e74a50acef/crates/agent-gateway/internal/auth/agenttoken/store.go#L1-L2)、[schema](https://github.com/Stack-Cairn/LiveAgent/blob/7de95a20bf93cfe026a57f6367c453e74a50acef/crates/agent-gateway/internal/auth/agenttoken/store.go#L65-L83)。更准确的表述是“不保存 provider API key 与 Agent token 明文”，而不是“不保存任何凭证材料”。
2. Gateway 不直接执行工具，但已认证浏览器可直通历史、设置、Skill、Memory、Cron 和多类文件写操作；只有部分 Git/Terminal/SFTP/Tunnel 再受功能开关控制。[请求白名单](https://github.com/Stack-Cairn/LiveAgent/blob/7de95a20bf93cfe026a57f6367c453e74a50acef/crates/agent-gateway/internal/protocol/pbws/guard.go#L12-L106)因此 Gateway browser token 应被视为高影响控制凭证，而非普通浏览令牌。
3. Provider 配置整体序列化进 SQLite 的 `payload_json`，SSH 密码、私钥和 passphrase 也有明文文本列。[schema](https://github.com/Stack-Cairn/LiveAgent/blob/7de95a20bf93cfe026a57f6367c453e74a50acef/crates/agent-gui/src-tauri/src/commands/config/settings/db.rs#L25-L72)、[写入](https://github.com/Stack-Cairn/LiveAgent/blob/7de95a20bf93cfe026a57f6367c453e74a50acef/crates/agent-gui/src-tauri/src/commands/config/settings/providers.rs#L88-L120)SheJane 必须继续把 API key 留在 OS credential store。
4. Bash 最终直接 `Command::new(...).spawn()`，而工具策略测试锁定 Bash、MCP 和未知工具默认 allow。[Shell](https://github.com/Stack-Cairn/LiveAgent/blob/7de95a20bf93cfe026a57f6367c453e74a50acef/crates/agent-gui/src-tauri/src/runtime/shell_runner.rs#L607-L641)、[策略测试](https://github.com/Stack-Cairn/LiveAgent/blob/7de95a20bf93cfe026a57f6367c453e74a50acef/crates/agent-gui/test/tools/tool-policy.test.mjs#L20-L41)其 worktree/readonly 是协作策略，不是 SheJane 所需的 OS 隔离边界。
5. 远程 WebUI 新录入的 Provider / SSH secret 会作为明文 sidecar 经 Gateway 转发；“API key never leaves my machine”只适用于普通脱敏同步，不适用于该远程录入路径。[WebUI 文档](https://github.com/Stack-Cairn/LiveAgent/blob/7de95a20bf93cfe026a57f6367c453e74a50acef/docs/architecture/webui.md#L70-L78)、[同步实现](https://github.com/Stack-Cairn/LiveAgent/blob/7de95a20bf93cfe026a57f6367c453e74a50acef/crates/agent-gateway/web/src/lib/settings/sync.ts#L1057-L1105)
6. 主窗口配置为 `withGlobalTauri: true` 且 `csp: null`，[Tauri 配置](https://github.com/Stack-Cairn/LiveAgent/blob/7de95a20bf93cfe026a57f6367c453e74a50acef/crates/agent-gui/src-tauri/tauri.conf.json#L12-L26)。这不是漏洞证明，但对高权限桌面 WebView 来说会放大任何渲染层注入问题，不能照搬。
7. 文档存在漂移：README 的 LLM 依赖表仍列出 `@openai/codex-sdk` 和 `claude-agent-sdk`，[README](https://github.com/Stack-Cairn/LiveAgent/blob/7de95a20bf93cfe026a57f6367c453e74a50acef/README.md#L273-L286)，当前 manifest 与源码使用的是 `@earendil-works/pi-*`。评估能力时应以实现、测试和 manifest 为准。

## 成熟度、发布与许可证

- 仓库于 2026-05-24 创建；截至固定提交约 70 天，已有 1,112 个 commits、43 个 tags。GitHub API 在调研时显示 1,592 stars、172 forks、`open_issues_count=61`（该字段合并 issue 与 PR），[官方 API 快照](https://api.github.com/repos/Stack-Cairn/LiveAgent)。这说明迭代和关注度高，也说明历史尚短。
- 调研时共有 42 个 GitHub Releases；最新稳定版为 [`v1.2.3`](https://github.com/Stack-Cairn/LiveAgent/releases/tag/v1.2.3)，发布于 2026-07-27；main manifest 已进入 `1.3.0-dev.0`。发布节奏很快，不能仅凭版本号推断长期稳定性。
- 固定提交对应的核心 [CI run 30649507260](https://github.com/Stack-Cairn/LiveAgent/actions/runs/30649507260) 中 Gateway、Docker smoke、WebUI、GUI、Rust check、mirror 与 diff hygiene 均成功。CI 会跑 Go 全量测试、前端测试和 Rust `cargo check --tests` 加若干重点测试；它不是 Rust 全量 `cargo test`。[完整 CI 定义](https://github.com/Stack-Cairn/LiveAgent/blob/7de95a20bf93cfe026a57f6367c453e74a50acef/.github/workflows/ci.yml#L16-L238)
- 发布链覆盖 macOS Intel/Apple Silicon 签名与 notarization、Windows、Linux 和 updater 资产，[Desktop Release workflow](https://github.com/Stack-Cairn/LiveAgent/blob/7de95a20bf93cfe026a57f6367c453e74a50acef/.github/workflows/desktop-release.yml#L40-L71)。但 Gateway workflow 同时记录：`v0.1.0` 到 `v1.1.8` 曾把 x86-64 binary 放进 arm64 image，后来才增加逐架构二进制验证。[修复门禁](https://github.com/Stack-Cairn/LiveAgent/blob/7de95a20bf93cfe026a57f6367c453e74a50acef/.github/workflows/gateway-docker.yml#L51-L92)这正好说明“发布成功”不能替代安装包/镜像内容验证。
- 仓库采用 [MIT License](https://github.com/Stack-Cairn/LiveAgent/blob/7de95a20bf93cfe026a57f6367c453e74a50acef/LICENSE#L1-L20)，允许使用、修改和再分发，但需保留版权与许可声明且不提供担保。若复制代码，还需逐项审查第三方依赖、模型 API、registry 内容和资产各自的许可证/条款；MIT 仓库许可证不会自动覆盖它们。

## 分阶段建议

- **Now**：只收敛现有 Subagent lifecycle——稳定 ID、正确失败/取消状态、durable replay、SDK 类型和 Client 投影；复用 Tool Receipt，不新建表或消息总线。
- **有模型瞬时失败数据后**：验证“同模型、首输出前、每 attempt 独立记账”的有限重试；没有失败率与恢复率证据就保持现状。
- **有 compaction 失败证据后**：做一个从 Receipt / Artifact 派生文件账本的 eval 实验；证明收益前不进入产品主链。
- **出现真实远程需求后**：按“设备身份与撤销 → durable command owner → 单调 cursor → 终态 → epoch/replay/snapshot”的顺序设计独立 Gateway。
- **开放多文件第三方安装后**：把 stage-validate-swap 与单一写路径纳入现有 Skill / 插件纪律；不引入 marketplace 复杂度。
- **继续留在 Explore**：稳定 Subagent 人格、worktree 自动合并、Agent 间消息、后台 Agent 和 Swarm。

## 本次验证边界

本次在固定提交上逐文件核对了实现、测试和 workflow，并本地运行 `node scripts/check-mirror.mjs`，结果为 `mirror check passed (119 file(s))`。未安装全部依赖，也未在本地执行 GUI、Gateway、Rust 的完整 build/test；构建状态引用的是上述官方 GitHub Actions 结果。GitHub 数量类数据会继续变化，架构判断以固定 SHA 为准。
