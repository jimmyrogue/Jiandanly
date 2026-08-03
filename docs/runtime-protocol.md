# Runtime HTTP 与 SSE 协议

> 本文记录当前公开协议。线程快照返回每个 Run 的安全事件高水位，客户端通过 `?after=<seq>` 恢复持久状态；游标超出保留窗口时重新读取权威快照。逐字文本、推理、临时用量和未完成调用片段只通过有界实时通道发送，断线后不重放。P4 的阶段边界见 [`harness-runtime-stages.md`](harness-runtime-stages.md)。

适用于 `GET /v1/runs/{run_id}/stream`（`Content-Type: text/event-stream`）。

> **文档版本**：对应 `@shejane/runtime-sdk` 的 SSE 解析、Runtime `RunCoordinator.stream` 与 `event_translator.translate`。
>
---

## 外部 A2A 边界

A2A 不是 `/v1` Runtime 协议的一部分。独立 `shejane-a2a-gateway` 进程对外提供 A2A 1.0 JSON-RPC，并通过本页记录的 Runtime HTTP/SSE 创建、查询和取消权威 Run：

- Runtime 仍只监听 loopback；A2A TLS、mTLS、OIDC、peer token、租户限域和速率限制由 Gateway 拥有。
- Gateway 的 SQLite 只保存外部身份、不可枚举的 A2A ID 映射、push outbox/config 和审计；Run、Event、Artifact 正文与执行终态仍由 Runtime SQLite 拥有。
- A2A Task 快照和订阅由 Runtime 快照/持久事件投影，断线后先重新取得完整 Task，再订阅后续状态；A2A 1.0 没有 SheJane `after=<seq>` 的跨服务 cursor 承诺。
- 当前 Card 只声明经过官方 TCK/ITK 验证的 `JSONRPC` 1.0、streaming、push 和 extended Card；HTTP+JSON 与 gRPC 未声明。
- 手机/桌面 Client 不使用 A2A。远程 Client 仍需要独立的设备配对、撤销和 Runtime gateway，不能把 Agent peer token 当用户设备凭证。

固定版本、偏差和复现命令见 [`a2a-conformance.md`](a2a-conformance.md)；部署见 [`operations.md`](operations.md#独立-a2a-gateway)。

## Wire 格式

每条事件如下：

```
event: <event_type>
data: <JSON object — AgentRunEvent envelope>

```

**关键点**：
- `event:` 行只是装饰（给 `curl -N` 看的）。**客户端只读 `data:` 里 JSON 的 `event_type` 字段** —— `parseAgentSSEChunk` 在 `sse.ts:58-72` 完全不解析 `event:` 行。
- 帧之间用 **LF** 双换行分隔（`\n\n`）。Runtime 使用 sse-starlette 的 `sep="\n"` 覆盖默认的 `\r\n`，因为客户端的 `split(/\n\n/)` 不匹配 CRLF。
- 终止信号是单独一行 `data: [DONE]`（没有 `event:`）。客户端识别到 `[DONE]` 后才会 resolve stream Promise。`event: stream.end` 已**废弃**。

## AgentRunEvent envelope

```ts
interface AgentRunEvent {
  event_type: string                       // 必填 — UI switch 入口
  payload?: Record<string, unknown>        // 事件特定 payload
  id?: string                              // dedupe 用，evt_<hex>
  run_id?: string
  seq?: number                             // 仅持久事件；同 run 内单调递增
  created_at?: string                      // ISO8601
}
```

状态变化持久化在 `local_events` 表，每条都有 `seq`；replay 路径只返回这些持久事件。临时事件仍有唯一 `id`，但没有 `seq`，不会写入数据库或在重连后重放。Runtime 升级时会清理旧版本曾错误持久化的临时事件，序号空洞不影响后续游标。

完整 TS 类型见 `runtime/sdk/src/sse.ts`。

---

## 事件类型

### 生命周期

| event_type | 触发时机 | payload 关键字段 |
|---|---|---|
| `run.started` | run 进入 `running` 状态 | `goal` |
| `run.resumed` | resume_run 后第一个 frame | `payload`（resume 时传入的 dict） |
| `run.waiting` | 卡在 HITL interrupt（**通常伴随 `permission.required` 或 `question.asked`**，UI 优先听后者） | `next`, `interrupts`, `handoff` |
| `run.completed` | 终态 completed | `final_text`, `input_tokens`, `output_tokens`, `model_calls`, `unmetered_calls`, `outcome_unknown_calls` |
| `run.failed` | 终态 failed | `error`, `type`, `category?`, `recoverable?`, `retryable?`, `action_kind?`, `suggested_action?` |
| `run.cleanup_required` | 清理尚未确认，执行代次已隔离 | `error`, `type`, `category`, `retryable=false`, `cleanup` |
| `run.canceled` | 终态 canceled | _(空)_ |
| `repair.workflow` | 用户触发的 repair run 进入/结束/失败/被上限拒绝/取消 | `status`, `attempt`, `max_attempts`, `source_run_id?`, `source_message_id?`, `failure_category?`, `reason?` |

`run.waiting.handoff` 是暂停点的轻量交接快照，包含
`ledger_state`（`not_required` / `fresh` / `missing` / `stale`）、
`ledger_message` 和最新 `feature_ledger` 摘要。它不包含 artifact 正文或
checkpoint messages。`permission.required` / `question.asked` / `run.waiting`
这类被动等待信号不会单独让 ledger 变脏；真正的工具结果、权限决策、
run 失败/取消等状态变化才会触发 `missing` 或 `stale`。

### LLM 流

| event_type | 触发时机 | payload |
|---|---|---|
| `llm.delta` | 每个 streamed token（assistant content） | `content: string` |
| `llm.reasoning` | DeepSeek-style thinking-mode chunk | `content: string` |
| `llm.tool_call_chunk` | 工具调用 args 的部分 JSON 流 | `id, name, args_delta, index` |
| `llm.usage` | 供应商返回的临时用量，只用于实时显示 | `input_tokens`, `output_tokens` |
| `llm.error` | 流中报错（非致命） | `message` |

`llm.delta`、`llm.reasoning`、`llm.tool_call_chunk` 和 `llm.usage` 是临时事件，断线或慢客户端背压时可以丢失。`llm.usage` 不是结算事实来源；`run.completed` 中的用量由
Runtime 持久模型调用账本聚合；重复 SSE 事件不会改变该结果。

### 工具

| event_type | 触发时机 | payload |
|---|---|---|
| `tool.completed` | 一次工具调用完成 | `tool_call_id, name, tool, content, status: "ok"` |
| `tool.failed` | 工具完成但 `ToolMessage.status == "error"`，或工具结果 envelope 明确 `ok:false` | `tool_call_id, name, tool, content, status: "error", error_code?, recoverable?, retryable?` |

`task` 仍会产生通用的 `tool.completed` / `tool.failed`，供旧 Client 降级显示；Subagent 生命周期不从这些 ToolMessage 或流式参数猜测，而由同一事务内的持久 Tool Receipt 转换投影：

| event_type | Receipt 转换 | 投影状态 |
|---|---|---|
| `subagent.spawned` | 新建 `prepared` | `queued` |
| `subagent.started` | `prepared/paused → running` | `running` |
| `subagent.waiting` | `running → paused` | `waiting` |
| `subagent.completed` | `running/outcome_unknown → completed` | `completed` |
| `subagent.failed` | `running/prepared/outcome_unknown → failed/rejected` | `failed` |
| `subagent.canceled` | `running/prepared/paused → canceled` | `canceled` |
| `subagent.outcome_unknown` | 执行租约丢失且无法证明结果 | `unknown` |

这些事件都有持久 `seq`，公共 payload 使用同一结构：`operation_id, parent_run_id, parent_operation_id, tool_call_id, subagent_type, description, status, receipt_status, attempt_count, usage, error_type, created_at, started_at, completed_at, updated_at`。其中 `parent_operation_id`、`error_type`、`started_at`、`completed_at` 始终存在，无值时为 `null`；`usage` 包含 `model_calls, input_tokens, output_tokens, unmetered_calls, outcome_unknown_calls`。

这里的 `operation_id` 只代表一次同步 `task` 调用，不是可寻址、可追问的 Agent 或 child Run。`LocalRun.subagent_invocations` 是同一 Receipt 的当前快照投影；即使线程事件因 `event_limit` 截断，Client 仍从该字段重建当前 Subagent 状态。`event_high_watermarks` 表示各 Run **实际包含在本次快照中的最高 seq**（未包含事件时为 `0`），Client 从这里续订，不能越过因截断而未返回的权限、提问或计划事件。

#### Runtime-owned durable child Run

需要后台执行、独立恢复/取消或稍后查询时，父 Agent 使用 `child.spawn` 创建真正的 child Run；短时同步委派仍使用上面的 `task`。两者不能互换：

- `child.spawn` 在父 Run 当前 Attempt 的租约和 Tool Receipt 下，原子写入 child Run、待执行 Job 与父 Run 的 `child.spawned` 事件；同一 `spawn_operation_id` 重放只返回原 child。
- child 拥有独立的 Run/Job/Attempt/checkpoint、冻结的 Agent 定义版本、工具权限子集和用量；当前拓扑限制为一层、每个父 Run 最多八个 child。
- `child.list/check/wait/cancel` 只操作调用者直接拥有的 child。`wait` 支持 `all` / `any`，单次最长 30 秒；超时不取消任务。
- `child.spawn` 同时冻结 `completion_mode=required|best_effort|quorum`、已存在 sibling 的 `depends_on`、精确 workspace 文件 `resource_claims`，以及 quorum 的 group/required。依赖未完成前 Job 不可领取；依赖失败后 dependent 原子取消。
- 父 Run 成功结算前，P11 自动等待所有 required child 和尚未满足的 quorum，不依赖模型记得调用 `child.wait`；达到 quorum 后取消多余成员，best-effort 未完成成员也在父终态前取消。父失败或取消向整棵 child 树传播，不支持隐式 detach。
- 同一 collaboration root 的精确 workspace 文件只能有一个 owner。已声明文件的其他成员写入会在 Tool Receipt 边界失败；存在他人 claim 时禁用无法静态证明写集的 `execute`，必须改用可检查路径的文件工具。
- Client 断线不影响 child。Runtime 重启后从 SQLite 的 Run/Job/Attempt 恢复；旧租约丢失后先进入隔离，迟到 Attempt 不能写入新事实。
- `child.spawn` 若在 child 已提交、工具回执未结算的窗口崩溃，Runtime 根据 `spawn_operation_id` 自动确认已发生的内部副作用，不要求用户手工对账，也不会重复创建 child。

父 Run 的持久事件流投影以下 child 生命周期：

| event_type | child 事实 | 父投影状态 |
|---|---|---|
| `child.spawned` | child Run + pending Job 已原子创建 | `queued` |
| `child.started` | child Job 被领取或恢复 | `running` |
| `child.waiting` | child 持久进入权限或输入等待 | `waiting_permission` / `waiting_input` |
| `child.completed` | child 结果与用量已提交 | `completed` |
| `child.failed` | child 明确失败 | `failed` |
| `child.canceled` | child 取消已提交 | `canceled` |
| `child.cleanup_required` | 旧 Attempt 的资源静止性无法证明 | `cleanup_required` |

`LocalRun.child_runs` 返回当前直接 child 的权威快照；`GET /v1/runs/{parent_run_id}/children` 返回同一列表，`GET /v1/runs/{child_run_id}` 可单独查询 child。child 不出现在顶层 `GET /v1/runs` 对话列表中，Client 不能自行创建、推断或结算 child。

`GET /v1/runs/{root_run_id}/collaboration` 在同一个 SQLite 读取事务中返回 root、child、pending waits、mailbox messages、artifact 元数据、dependency edges、resource owners，以及每个 Run **实际包含的事件高水位**。SDK 对应 `getCollaborationSnapshot()`。桌面端和未来手机端断线后用这份完整快照重建，再分别从各 Run 的高水位续订；手机仍通过现有 child `/inject`、HITL 命令和 `/cancel` 操作，不成为状态所有者。该接口只接受 collaboration root；它不放宽 Runtime 的 loopback 监听，远程手机仍需要独立 TLS/设备授权网关。

#### Durable Agent mailbox

同一个 collaboration root 内的根 Agent 与 durable child、或两个同级 durable child，可以使用 Runtime-owned mailbox 交换消息。它不是任意群聊，也不允许跨 root、跨 principal 或向自己发送：

- `mailbox.send` 发送 `request / question / update / result / cancel` 类型消息；发送者 Run 和 Tool Receipt operation id 只由 Runtime 注入，模型不能伪造。
- `mailbox.reply` 只能回复发给当前 Run 的 `request / question / update`，Runtime 自动绑定原发送者、`correlation_id`、递增序号和最多 8 跳的循环上限。
- `mailbox.inbox` 返回当前 Run 的持久收件箱；`mailbox.ack` 只确认已投递消息。未确认消息会在检查点丢失时再次注入，语义是 at-least-once，不虚构 exactly-once。
- 每个收件箱最多 32 条待处理消息，每个 root 最多 512 条消息；TTL 为 60 秒到 24 小时。超限明确返回 backpressure，不静默丢消息。
- 消息内容是同级 Agent 输入，不是 system 指令，不能改变安全、权限、工具或用户要求。等待用户许可/输入的 Run 不会因收到消息而绕过等待。
- `mailbox.send/reply/ack` 使用现有 Tool Receipt 和执行租约；进程丢失后的同 operation id 重放是事务幂等的，不会重复发信或重复确认。

Runtime 在收件 Run 下一次模型调用前，把 `queued` 原子变为 `delivered` 并以稳定 `agent_message_id` 写入 checkpoint 消息。若数据库已标记投递、但 checkpoint 尚未提交，下一 Attempt 会再次注入同一 message id；checkpoint 已包含该 id 时不会重复注入。

| event_type | mailbox 事实 |
|---|---|
| `agent.message.sent` | 消息已持久化到发送者 outbox 与收件者 inbox |
| `agent.message.received` | 收件 Run 已取得消息，状态变为 `delivered` |
| `agent.message.acknowledged` | 收件 Run 已显式处理并确认消息 |
| `agent.message.expired` | TTL 到期前未确认，消息不再投递 |

这些事件与消息状态由同一个 Runtime SQLite 事实源产生。`GET /v1/runs/{run_id}/mailbox?box=inbox|outbox` 返回当前权威消息列表，SDK 对应 `listAgentMessages()`；HTTP 只提供查询，发送与确认仍必须经过 Agent 工具、租约和 Receipt。

### 人在回路（HITL）

| event_type | 触发时机 | payload |
|---|---|---|
| `permission.required` | 参数化工具确认在整批执行前暂停；同一批可连续多条 | `request_id, tool, tool_name, tool_call_id, operation_id, arguments_hash, arguments, risk, description, review_source?, review_reason?, allowed_decisions, allow_run_scope` |
| `permission.resolved` | `permission.resolve` 命令成功后持久化，并在恢复流中先于 `run.resumed` 出现 | `request_id, tool, tool_name, decision, scope` |
| `permission.auto_approved` | 规则或模型在 P10 自动允许操作；该实时反馈不替代 Tool Receipt 审计 | `request_id, operation_id, tool, tool_name, risk, source, reason, scope: "run"`；source 为 `rule` / `llm` / `run_grant` |
| `question.asked` | `user.ask` 工具触发 interrupt | `request_id, questions: [{question, options, id}]` |
| `question.answered` | `question.answer` 命令成功后持久化，并在恢复流中先于 `run.resumed` 出现 | `request_id, answers` |
| `plan.approval_required` | 计划模式生成待确认计划 | `request_id, tool_call_id, todos, summary` |
| `plan.approval_resolved` | `plan.resolve` 命令成功后持久化，并在恢复流中先于 `run.resumed` 出现 | `request_id, decision, instructions` |

### 中间件 / 框架内部

| event_type | 触发时机 | payload |
|---|---|---|
| `agent.custom` | middleware 通过 `get_stream_writer()` 推送 | _(任意)_ |

LangGraph 原始节点更新不进入产品 SSE；它们保留在 checkpoint 和 tracing 诊断层。

---

## 客户端消费骨架

```ts
import { parseAgentSSEBuffer } from '@shejane/runtime-sdk'

const resp = await fetch(`/v1/runs/${runID}/stream`, { signal })
const reader = resp.body!.getReader()
const decoder = new TextDecoder()
let buffer = ''
let assistantText = ''

while (true) {
  const { value, done } = await reader.read()
  if (done) break
  buffer += decoder.decode(value, { stream: true })
  const { events, rest } = parseAgentSSEBuffer(buffer)
  buffer = rest
  for (const ev of events) {
    if (ev.type === 'done') return            // ← data: [DONE]
    if (ev.type !== 'agent') continue
    const { event_type, payload = {} } = ev.event
    switch (event_type) {
      case 'llm.delta':
        assistantText += String(payload.content ?? '')
        render(assistantText)
        break
      case 'llm.reasoning':
        appendReasoningPanel(String(payload.content ?? ''))
        break
      case 'tool.completed':
      case 'tool.failed':
        renderToolCard(payload)
        break
      case 'permission.required':
        showApprovalCard({
          requestId: payload.request_id,
          tool: payload.tool,
          args: payload.arguments,
        })
        break
      case 'permission.resolved':
      case 'tool.reconciliation_resolved':
        clearApprovalCard(payload.request_id)
        break
      case 'question.asked':
        showQuestionPrompt(payload)
        break
      case 'run.completed':
        finalize({
          text: payload.final_text,
          inputTokens: payload.input_tokens,
          outputTokens: payload.output_tokens,
        })
        break
    }
  }
}
```

EventSource API 也能用，但不能传 Authorization 头；fetch + ReadableStream 是 Electron 渲染进程的标准做法。

---

## 控制面端点

事件流是只读的；状态变更靠这些 HTTP 端点：

| 方法 + 路径 | body | 触发的 SSE |
|---|---|---|
| `POST /v1/runs` | `{command_id, client_message_id, goal, permission_mode?, attachment_paths?, history?, settings?, ...}` | `permission_mode` 为 `ask`、`auto` 或 `full_access`，省略时使用 `ask`；附件必须是本机现有文件，最多 10 个，接纳时流式导入 Runtime 的不可变内容寻址输入存储（单个及单 Run 合计上限 200 MiB）；后续执行不再依赖原始主机路径。任务附件和 PDF 文件的模型读取上限为 200 MiB，其他 workspace、Skill、Memory 与子任务文件读取上限为 20 MiB；更大的文件必须由兼容插件流式处理；创建后开 stream → `run.started` |
| `POST /v1/runs/:id/fork` | `{command_id, client_message_id, assistant_message_id, thread_id, protocol_version, required_capabilities, checkpoint_id, ...}` | 创建分支后开 stream → `run.started` |
| `GET /v1/runs/:id/events?after=<seq>&limit=<n>` | — | 有限分页返回已持久化事件；用于快照截断后的缓存重建，不等待未来事件 |
| `GET /v1/runs/:id` | — | 返回一个普通或 child Run 的权威快照；child 包含 parent/root、Agent 定义版本、状态、结果/错误和用量 |
| `GET /v1/runs/:id/children` | — | 返回该 Run 直接拥有的 durable child 快照；未知或越权父 Run 返回 404 |
| `GET /v1/runs/:id/collaboration` | — | 仅以 root Run ID 返回同一读取边界的成员、pending waits、消息、artifact 元数据、依赖、resource owners、完成策略摘要和逐 Run 事件高水位；child ID 请求返回 409 |
| `GET /v1/runs/:id/mailbox?box=inbox\|outbox` | — | 返回该 Run 的持久收件箱或发件箱；未知或越权 Run 返回 404 |
| `GET /v1/runs/:id/stream` | — | （本协议） |
| `POST /v1/commands` | Run/HITL 命令，以及 `plugin.install`、`plugin.runtime_asset.install`、`plugin.enable/disable/update/rollback/remove`、`plugin.model.bind`、`plugin.setup.advance` 的严格联合类型 | Run 命令产生对应状态事件；插件命令写入幂等 Command 日志并返回收据。`plugin.setup.advance` 只接受固定 Computer Use 能力和当前 revision，且不会由 Client 后台自动重试系统授权动作 |
| `GET /v1/plugins` / `GET /v1/plugins/:id` | — | 返回当前 principal 可见的安装、版本、Action、Command、签名、能力与安全模型绑定摘要，不返回密钥、credential ref 或模型服务地址 |
| `GET/PUT/DELETE /v1/plugins/:id/runtime-asset` | — | 仅对固定 Browser QA/RapidOCR 返回是否已下载、幂等准备或删除 Runtime 解析的精确平台资产；GET 在下载期间附带 `downloading` 与可用时的 `download_progress`（0–100），正在被 Run 租用时拒绝删除，不向 Client 暴露下载 URL、文件名或 digest |
| `GET /v1/plugins/:id/readiness` | — | 返回固定 Computer Use 能力的只读准备状态、当前单步动作和 revision；读取不会触发系统授权 |
| `GET /v1/artifacts/:id` | — | 返回授权后的 Artifact 元数据；旧的小型文本可内联，文件 Artifact 只返回 `storage_kind=blob`、大小和摘要 |
| `GET /v1/artifacts/:id/content` | — | 按所属 Run 授权并流式返回正文；支持 HTTP Range，不暴露内部存储路径 |

`plugin.model.bind` 只接受具体的 `local:<connection>:<model>`，并要求目标 Managed Worker 已声明 `model.vision.invoke`、模型已启用且声明 `image_inputs`。绑定时冻结 connection version；Run 接纳时再复制为 Run-owned binding，后续重绑只影响新 Run。Worker 调用时 Runtime 再核验该版本并从系统凭据库读取密钥，公开收据和插件详情只返回 `binding_id`、请求模型、connection/model ID 与 connection version。

权限批准的 happy path：

```
[user types] → POST /runs (id=R)
   GET /runs/R/stream
   → run.started
   → llm.reasoning / llm.delta...
   → permission.required {request_id: P, tool: "write_file", args: {...}}
   → run.waiting {handoff: {ledger_state, ledger_message, feature_ledger}}
   → [DONE]   ← stream 暂时关闭

[user clicks "don't ask again"] → POST /commands {type: "permission.resolve", permission_id: P, decision: "approve", scope: "run"}
   (Runtime：幂等保存决定；仅合格普通工具可按同工具、风险和稳定图定义复用，并有时限与次数上限)
   GET /runs/R/stream?after=<last_seq>  ← 客户端从快照高水位继续订阅
   → permission.resolved {request_id: P, decision: "approve", scope: "run"}
   → run.resumed
   → tool.completed {tool: "write_file", content: "..."}
   → llm.delta...
   → run.completed
   → [DONE]
```

如果一次 HITL 暂停同时包含多个 `action_requests`，Runtime 会在同一个
`run.waiting` 前发多条 `permission.required`。每次提交 `permission.resolve`
只 resolve 对应的一张卡；只要当前批次还有 `pending` permission，响应为
`resumed:false`，不会发 `run.resumed`。最后一个同批权限 resolved 后，
Runtime 按 LangGraph `interrupt_id` 和动作顺序构造恢复映射并继续执行。

权限模式在 Run 接纳时冻结进 `settings_json`，由 Runtime 的工具审查层执行：

- `ask`：工作区写入、沙箱命令、剪贴板读取和外部或未知工具需要确认。
- `auto`：工作区操作、无网络且工作区只读的沙箱命令和受限插件由确定性规则自动允许；外部或未知灰区由当前 Run 冻结的模型审查。模型只能选择 `allow` 或 `ask`，超时、异常、非法或不完整结果统一回退确认；剪贴板读取等受保护 Runtime 状态仍直接询问。
- `full_access`：不产生普通工具确认，但删除等不可恢复工具仍需逐次确认；所有操作仍受工作区授权、路径校验、操作系统沙箱、工具参数校验和回执审计约束。

权限模式决定“何时询问”；`permission.resolve.scope` 决定一次明确批准可以复用多久，两者互不替代。模型自动决定按 `operation_id` 写入 Tool Receipt，恢复时不会再次调用模型。分支任务继承源 Run 冻结的权限模式；定时任务在创建时同样冻结该字段。

后续 turn 再触发具备 `allow_run_scope=true` 的同一个工具，且风险和图定义不变时，可消耗有界运行级授权直接执行；新参数仍重新做 schema、能力和工作区校验。删除、外部未知及其他不合格操作不能取得该授权。若副作用工具结果不确定，则进入显式核对：

```
   → llm.tool_call_chunk
   → tool.reconciliation_required {operation_id, tool_name}
   ← POST /v1/commands {type: "tool.reconcile", command_id, operation_id, decision}
   → tool.reconciliation_resolved {decision}
```

---

## 设计原则

Runtime 的线程快照是界面事实来源：

- `GET /v1/threads` 使用稳定游标分页列出对话，并返回全局变化高水位。
- `GET /v1/threads/{thread_id}` 按消息位置分页；后续页携带线程版本，版本变化返回冲突并由客户端重读。
- 助手消息投影在写入正文时原子记录它已覆盖的事件序号；线程快照返回这个安全高水位，客户端把该序号保存到可丢弃缓存。
- `GET /v1/threads/changes?after=<cursor>` 用于发现其他客户端或后台任务提交的变化。
- 线程快照标记 `events_truncated` 时，客户端对仍在等待用户动作的 Run 调用 `GET /v1/runs/{run_id}/events?after=<seq>`，有限分页补齐缺失事件后再保存缓存；它不会像 SSE 一样等待 Run 终态。
- `GET /v1/runs/{run_id}/stream?after=<seq>` 只回放更大的事件序号，并在 SSE `id` 字段携带序号。SSE 提供低延迟增量，不承担最终一致性。
- 如果 `after` 大于最新序号，或落在已删除事件形成的缺口之前，Runtime 返回 `409 event_cursor_reset_required`。客户端重新读取完整线程快照，再从快照高水位与首个保留序号前一位中的较大值继续订阅。当前版本尚未主动裁剪事件，但该检查同时覆盖数据库恢复或未来保留策略造成的窗口变化。
- 正文消息可以完整分页；过程事件只是辅助时间线，达到上限时返回截断标记。

1. **加性兼容**：新增 event_type 不破坏老客户端。Switch 用 fall-through default 忽略未知 type。
2. **窄 schema**：不暴露 LangGraph 内部类（`AIMessageChunk` / `StateGraph` / ...）；payload 字段都是普通 JSON 标量 + 字典。
3. **Persist + stream 同源**：每条业务 SSE 事件同时写 `local_events` 表，重连可重放完整事件序列；等待和终态事件与运行状态在同一事务提交，提交后才通知实时订阅者；`[DONE]` 是传输层结束标记，不写库。
4. **失败可观测**：`run.failed` 一定带 `error` + `type`，并尽量附带 `category` / `recoverable` / `retryable` / `action_kind` / `suggested_action`，让事件流消费者无需再请求 diagnostics 也能先判断是重试、用户处理、修复、运维处理还是继续排查；客户端普通失败文案会保留原始错误并追加本地化的短策略标签；`tool.failed` 一定带 `content` + `status="error"`，结构化工具 envelope 失败还会尽量带 `error_code` / `recoverable` / `retryable`；用户触发的 repair run 另有 `repair.workflow`，避免 UI 把修复尝试误读成普通 retry 或裸露内部事件名。
5. **HITL 双轨**：`run.waiting` 是兜底（curl 友好），`permission.required` / `question.asked` 是窄信号 — UI 永远听窄的；同一 pause 批次内的多张 permission 卡必须全部 resolved 后才 resume。
