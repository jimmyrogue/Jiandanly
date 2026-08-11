# Agent 执行过程展示设计

> 状态：Proposed
> 日期：2026-08-04
> 前置调研：[Agent 思考与活动可见性调研](../ui/agent-thinking-visibility-research.md)

## 决策

把一次 Assistant Run 展示为 Runtime 管理的有序 `RunPresentationItem` 流。Client 不再分别拼接 `content`、`reasoning`、`agentEvents` 和 `subagents`，只负责折叠、分组和逐字动画。

不新增 presentation 账本表，也不把整份展示 JSON 写回 `local_thread_items`。新的深 Module `RunPresentationProjection` 在读取时，从现有 Run、持久事件、Tool Receipt、等待候选、child Run、Artifact、验证结果和最终 Assistant item 生成强类型快照；只有目前确实没有持久所有者的“完整模型回合叙述 / 可展示推理摘要”新增一种 durable source event。

这是一个刻意的混合方案：

- 顺序复用 `local_events.seq`；
- Tool、审批、SubAgent、验证和终态继续引用各自权威事实；
- 模型逐 token 文本继续是可丢失的临时反馈；
- 完整的用户可见模型回合在 P8 结束时持久化，断线后不再消失；
- P4 输出统一 presentation，Client 不再理解底层事件如何归并。

## Runtime 阶段边界

| 项目 | 决策 |
| --- | --- |
| `primary_stage` | P4：客户端读取快照并订阅变化 |
| 直接上游 | P8：完整模型回合；P10：Tool Receipt 与等待候选；P12：终态提交 |
| 直接下游 | SDK 强类型 Adapter；Client Assistant message renderer |
| canonical state owner | Runtime；具体事实仍由 Run、Receipt、wait、child Run、Artifact、Assistant item 各自拥有 |
| 替换的旧路径 | `ChatMessage.content + reasoning + agentEvents + subagents` 四条 Client 投影，以及 `AgentProgress` 的完成态隐藏规则 |

P4 是主阶段，因为本设计改变的是“Runtime 如何向 Client 提供可恢复的展示快照与变化”。P8、P10、P12 只补齐或提供投影来源，不建立第二条执行链。

## 问题定义

当前 Runtime 已经拥有丰富的执行事实，但展示弱的根因是数据形状而不是视觉样式：

1. `llm.round.started`、Tool 请求或提问会清空 Client 临时正文，旧回合叙述丢失。
2. `reasoning` 只有一个槽位，新回合会覆盖旧回合。
3. Tool、审批、验证和 SubAgent 由 Client 从通用事件推断并分散保存。
4. `AgentProgress` 在 Run 完成或正文开始出现时隐藏过程。
5. `llm.delta` 和 `tool.progress` 是临时事件；`llm.phase.changed` 已持久化并可从 SSE 游标回放，模型阶段还可由 Run 快照恢复，完整逐字历史仍不能依赖临时事件。

因此目标不是公开原始 chain-of-thought，也不是增加一个更醒目的“思考中”动画，而是让用户可靠地看到：

```text
模型说明准备做什么
→ 执行了什么
→ 结果或等待是什么
→ 下一轮为何继续
→ 最终回答
```

## 设计目标与非目标

### 目标

- 按真实发生顺序保留 `叙述 → Tool → 结果 → 下一轮叙述 → 最终回答`。
- Run 完成后仍能展开查看过程，最终回答保持最高视觉权重。
- 没有 provider reasoning 的模型也能显示完整活动过程。
- 断线后恢复 Item 的顺序、摘要、状态和关键引用；不要求恢复逐 token 动画。
- 审批、问题、计划、失败、结果未知和验证不能被普通 Tool 分组隐藏。
- Pydantic → OpenAPI → Runtime SDK → Client 保持 discriminated union 强类型。

### 非目标

- 不展示或持久化原始 chain-of-thought。
- 不把 Tool stdout、完整返回值或敏感参数复制到展示记录。
- 不改变 Run、Thread、checkpoint、Receipt 或 Artifact 的状态所有权。
- 不增加第二条 SSE 连接或第二套 cursor。
- 不在本设计中实现远程 Gateway 或跨设备同步。

## Design It Twice：三个 Interface 方案

### 方案 A：读取时 `RunPresentationProjection`（选择）

Runtime 用现有 durable facts 和事件顺序即时生成 presentation。标准快照嵌入现有 Thread snapshot；现有 Run SSE 在源事件上附带可选 `presentation_change`。

优点：没有第二份事实、迁移可加性发布、历史 Run 可尽可能投影、删除 Client 归并逻辑后 Locality 最好。缺点：快照需要跨表读取，投影成本为 `O(events + receipts + waits)`。

### 方案 B：新增 `local_run_presentation_items` 账本

为每个 Item 保存固定位置、source reference 和 authored text，状态仍从 source resolver 读取。

优点：Item 生命周期和分页最灵活，长远可支持独立回放。缺点：每个 P8/P10/P12 写事务都要维护引用和 revision；最容易逐步复制 Tool 状态、终态和回答正文，形成第二真相。

### 方案 C：在 Assistant `local_thread_items` 保存 `presentation_json`

每条 Assistant item 直接保存完整 blocks，Client 的读取 Interface 最简单。

优点：普通调用者只读一行，渲染成本最低。缺点：活动 Run 会反复重写一个增长的 JSON；它会和 Receipt、wait、child Run、终态漂移，也不适合作为 child Run 的独立展示来源。

### 选择理由

方案 A 最符合现有架构：`local_events.seq` 已经提供顺序，P4 已经拥有一致快照和游标恢复，Tool Receipt、wait、child Run 和 Assistant item 已经拥有状态。重新保存这些信息不会增加真实能力。

唯一缺口是完整模型回合叙述目前只存在于临时流或 checkpoint。因此只持久化这一类新事实，其余一律读取时解析。这个 Module 有足够 Depth：它隐藏排序、source 解析、状态归一化、安全摘要、历史兼容和重连规则；Client 只看到稳定 Items，得到高 Leverage，而协议知识集中在 Runtime，保持 Locality。

SQLite 是 local-substitutable 依赖，直接在当前 Store 事务内读取，不创建抽象 port。Runtime SDK 是 remote-but-owned Adapter，负责把 OpenAPI 快照和 SSE change 转成同一组 Item；暂不为一个实现增加额外 Transport Interface。

## 强类型领域模型

以下是协议形状，不是要求手写一份 Client 镜像类型。正式实现由 Pydantic schema 生成 TypeScript 类型。

```ts
type PresentationStatus =
  | 'pending'
  | 'in_progress'
  | 'waiting'
  | 'completed'
  | 'failed'
  | 'canceled'
  | 'unknown'

type PresentationOrder = {
  event_seq: number
  slot: number
}

type PresentationSource =
  | { kind: 'assistant_round'; id: string }
  | { kind: 'tool_receipt'; id: string }
  | { kind: 'wait_candidate'; id: string }
  | { kind: 'child_run'; id: string }
  | { kind: 'artifact'; id: string }
  | { kind: 'assistant_item'; id: string }
  | { kind: 'run_event'; id: string }

type RunPresentationItem =
  | NarrativeItem
  | ActivityItem
  | DecisionItem
  | NoticeItem
  | FinalAnswerItem

type NarrativeItem = {
  id: string
  kind: 'progress' | 'reasoning_summary'
  status: 'in_progress' | 'completed'
  order: PresentationOrder
  revision: number
  round_id: string
  text: string
  source: Extract<PresentationSource, { kind: 'assistant_round' }>
  started_at: string
  completed_at?: string
}

type ActivityItem = {
  id: string
  kind: 'tool' | 'subagent' | 'verification' | 'artifact'
  status: PresentationStatus
  order: PresentationOrder
  revision: number
  summary: string
  source: Extract<PresentationSource,
    { kind: 'tool_receipt' | 'child_run' | 'artifact' }>
  detail: ToolDetail | SubagentDetail | VerificationDetail | ArtifactDetail
  started_at: string
  updated_at: string
  completed_at?: string
}

type DecisionItem = {
  id: string
  kind: 'approval' | 'question' | 'plan' | 'reconciliation'
  status: 'waiting' | 'completed' | 'canceled'
  order: PresentationOrder
  revision: number
  summary: string
  source: Extract<PresentationSource, { kind: 'wait_candidate' }>
  detail: ApprovalDetail | QuestionDetail | PlanDetail | ReconciliationDetail
  started_at: string
  completed_at?: string
}

type NoticeItem = {
  id: string
  kind: 'notice'
  status: 'in_progress' | 'completed' | 'failed' | 'canceled' | 'unknown'
  order: PresentationOrder
  revision: number
  code: string
  summary: string
  source: Extract<PresentationSource, { kind: 'run_event' }>
  started_at: string
  completed_at?: string
}

type FinalAnswerItem = {
  id: string
  kind: 'final_answer'
  status: 'completed'
  order: PresentationOrder
  revision: number
  content: string
  source: Extract<PresentationSource, { kind: 'assistant_item' }>
  completed_at: string
}

type RunPresentationSnapshot = {
  schema_version: 1
  run_id: string
  items: RunPresentationItem[]
  event_high_watermark: number
}
```

`order` 使用首次来源事件的 `(event_seq, slot)`，不会因并行 Tool 的完成顺序改变。一个事件可以生成多个 Item，例如一个完整模型回合可按 `reasoning_summary → progress` 分配两个 slot。`revision` 是最后一次影响该 Item 的 durable event seq；Client 只接受更高 revision 的 upsert。

稳定 ID 由 source identity 派生：

- `round:{round_id}:reasoning`
- `round:{round_id}:progress`
- `tool-call:{tool_call_id}`（标准过程只投影 `execution_namespace=main`）
- `wait:{candidate_id}`
- `child:{child_run_id}`
- `artifact:{artifact_id}`
- `answer:{assistant_item_id}`

来源缺失或外部结果不明时必须输出 `unknown`，不能猜测成功，也不能静默删除 Item。

## 唯一新增的 durable source

P8 在拿到一个完整、顶层 AI message 后，按需追加：

```json
{
  "event_type": "assistant.round.committed",
  "payload": {
    "round_id": "modelcall_...",
    "role": "progress",
    "text": "我先核对事件协议，再检查现有投影。",
    "display_reasoning_summary": null,
    "tool_call_ids": ["call_1", "call_2"]
  }
}
```

规则：

1. 只有带 Tool calls 的完整模型消息正文作为 `role=progress` 持久化；最终无 Tool 回合由 P12 的 Assistant item 拥有，不复制到该事件。
2. `display_reasoning_summary` 只接受 provider 明确标记为可向用户展示的 summary。
3. DeepSeek `reasoning_content` 等原始推理默认不视为 summary，不持久化到 presentation。
4. 空文本不制造“正在分析”之类的伪叙述；Tool Item 本身足以说明动作。
5. `round_id` 复用模型调用账本身份；不能用 Client 临时编号。

Agent 默认指令应要求在开始有意义的外部工作前给出一句简短、用户可见的进度叙述。它只是普通 Assistant 内容，不是 chain-of-thought。模型未提供时，Runtime 不替它编造原因。

## Module 与 Interface

```text
P8 assistant.round.committed ─┐
P10 Receipt / wait / child ───┼─> RunPresentationProjection
P12 Assistant item / terminal ┘             │
                                             ├─> P4 Thread snapshot
                                             ├─> existing Run SSE change
                                             └─> on-demand detailed snapshot
```

`RunPresentationProjection` 是唯一理解 source event → user item 映射的 Module。Store 负责一致读取原始记录；Module 负责：

- source 归一化和稳定 ID；
- 顺序与 revision；
- 状态解析和 `unknown` 降级；
- 标准摘要与安全 detail；
- 历史协议 Adapter；
- 标准视图所需的原子 Item，不负责视觉分组。

对外最多三个入口：

1. `GET /v1/threads/{thread_id}`：在现有 `LocalThreadSnapshot` 增加 `presentations: Record<run_id, RunPresentationSnapshot>`，这是普通 Client 的首帧。
2. `GET /v1/runs/{run_id}/stream?after=`：现有 `AgentRunEvent` 增加可选的强类型 `presentation_change`；不增加第二条 stream。持久 change 复用源事件的 id/seq，临时 change 不参与恢复。
3. `GET /v1/runs/{run_id}/presentation?view=detail`：用户展开“详细过程”时按需返回已脱敏的参数、结果摘要、时间、Artifact 和验证引用。

SSE change 只有三种：

```ts
type RunPresentationChange =
  | { kind: 'item.upsert'; item: RunPresentationItem }
  | { kind: 'draft.delta'; round_id: string; content: string }
  | { kind: 'draft.closed'; round_id: string; committed_item_ids: string[] }
```

`draft.*` 是临时动画，可以丢。`item.upsert` 必须可从对应 durable source event 重算。一个 durable event 生成多个 Item 时使用 `presentation_changes` 数组；兼容期内同时保留单数 `presentation_change`。旧 Client 会忽略未知字段，继续消费当前通用事件。

## LangSmith 与可观测性边界

LangSmith 适合作为可选的外部观测 Adapter，但不能成为 `RunPresentationProjection` 的 source，也不能参与 P4 恢复。

```text
Runtime authoritative facts ─┬─> RunPresentationProjection ─> Client UI
                             ├─> local diagnostics trace ────> export/support
                             └─> optional LangSmith Adapter ─> remote trace/eval
```

三条路径职责不同：

- Presentation 回答“用户应该看到什么”，必须可由本地 Runtime 恢复。
- Local diagnostics 回答“本机发生了什么”，是脱敏、持久、可导出的诊断事实。
- LangSmith 回答“模型、Tool 和 Agent 调用树在哪一步变慢或失败”，并提供远程筛选、人工反馈、dataset 和 evaluator；它是可丢失的外部副本。

LangSmith 接入的 `primary_stage` 是 P7（启动或恢复 LangGraph），不是 P4。推荐用一个受配置控制的根 Trace 包住一次 SheJane execution attempt，让 LangGraph 的模型与 Tool spans 成为子节点，并补充少量 SheJane 自有 spans：

| 阶段 | LangSmith 记录 | 仍以本地事实为准的内容 |
| --- | --- | --- |
| P7 | Run/Attempt 根 Trace，release、platform、Runtime Run ID | 租约、Job、checkpoint branch |
| P8 | 模型回合、延迟、usage、错误、round ID | 模型调用账本与 Assistant draft |
| P10 | Tool/SubAgent 调用树、耗时、错误、operation ID | Tool Receipt、等待候选、外部结果 unknown |
| P11/P12 | cleanup/settlement span 与 terminal classification | 原子提交、最终回答、Artifact、验证和终态 |

P4 不从 LangSmith 拉数据。LangSmith 网络、鉴权、限流或后台 flush 失败不得延迟、失败或改变 Run；最多影响远端诊断完整性。

### 建议的三种模式

1. `off`：公开安装默认值；当前本地 diagnostics 继续可用。
2. `support_metadata`：授权官方服务后默认开启、可随时关闭；只发送工具名、状态、耗时、usage、版本和脱敏错误分类，隐藏 inputs、outputs 和非白名单 metadata。
3. `support_content_once`：只针对用户明确选择的单个复现 Run，带清晰告知和自动到期；允许发送经过脱敏的必要内容。开发者自己的测试 workspace 可以使用相同模式，但不能继承到普通用户运行。

成功 Run 的常规采样可以降低成本；失败复现必须明确开启，不能依赖随机 sampling。公共发行版不携带 LangSmith service key，也不让桌面 Runtime 直接连接 LangSmith。当前产品选择 SheJane Cloud relay：Runtime 只提交已授权的 metadata-only 终态诊断，Cloud 才能把它映射到 LangSmith trace。

`RuntimeObserver` 继续阻止开发环境中的 LangSmith 变量意外继承。桌面 Runtime 不增加 LangSmith 直接依赖、第二套 BYOK 凭据或离线重试队列；现有 `/v1/shejane/diagnostics`、独立系统凭据和 terminal callback 是唯一外部诊断入口。若未来提供开发者自有 workspace，再以独立产品决策增加 programmatic Client，而不是复用环境变量。

验收以现有 Cloud relay 合约为边界：禁止字段不能离开本机，中继网络或鉴权失败不能影响 Run，关闭诊断后不再提交。Cloud 侧负责 LangSmith ingestion 的 Trace、采样和 flush 测试。LangSmith 不覆盖 Electron、Launcher、Updater 或 native crash，这些仍属于独立 crash-reporting 路径。

## P4 / P8 / P10 / P12 流程

### P8：完整模型回合

1. 模型调用账本分配稳定 `round_id`。
2. `llm.round.started` 和 `llm.delta` 携带 `round_id`，仅驱动临时 draft。
3. 完整 AI message 到达后，Runtime 区分：
   - 带 Tool calls：提交 `assistant.round.committed(role=progress)`；
   - 无 Tool calls：形成最终回答候选，等待 P9/P12；
   - 有明确 display-safe reasoning summary：附加 summary；
   - 只有 raw reasoning：不进入 presentation。
4. 新回合不会清空已经 committed 的旧回合 Item。

### P10：Tool 或等待

1. Receipt `prepared` 时按模型原始调用顺序创建 Tool Item。
2. `prepared → running → paused/completed/failed/outcome_unknown` 原位更新同一稳定 ID；position 不变。
3. `task` Receipt 映射为 SubAgent；durable child 引用 child Run；`task.verify` 映射为 verification。
4. permission、question、plan 和 reconciliation 映射为顶层 Decision Item，禁止进入普通 Tool 分组。
5. 并行 Tool 只改变各自 revision，不改变展示顺序。
6. `tool.progress` 可更新临时 detail，断线后以 Receipt 当前状态为准。

### P12：原子终态

1. 最终回答只引用已提交的 Assistant item。
2. Run、Job、Attempt、Assistant item、用量、Receipt、Artifact、验证和 terminal event 仍在现有事务内结算。
3. `RunPresentationProjection` 不在事务内写第二份状态；提交后第一次快照必然从同一组事实得到一致终态。
4. 未结束的来源解析为 `unknown` 或 terminal notice，不能让 completed Run 继续显示 running。

### P4：快照与恢复

1. 在现有一致读取边界查询 source facts 与事件高水位。
2. Module 生成标准 presentation snapshot。
3. Client 保存 disposable projection 和 cursor。
4. SSE 用 `id + revision` 幂等 upsert。
5. 断线后从 `after` 继续；游标过期时重新读完整 Thread snapshot。
6. 临时 draft 丢失时直接回到最后一个 committed Item，不拼接半截正文。

## Client 信息架构

### 运行中

```text
正在处理
  我先核对事件协议，再检查现有投影。       ← progress

  › 检查 4 个相关文件                     已完成
  › 对照 Runtime 快照与事件恢复            进行中

  思考摘要                                可展开
```

- 当前 progress 与当前活动默认展开。
- Tool、SubAgent 和验证使用稳定行，状态在原位变化。
- 审批、问题、计划、失败和 unknown 始终单独显示。
- reasoning summary 使用“思考摘要”，不称为完整思考过程。

### 完成后

```text
最终回答正文……

过程 · 6 步 · 2 个 Tool 组 · 已验证        ›
```

- 最终回答是视觉主角。
- 过程默认折叠为一行，但永远不删除。
- 展开后的“标准”视图显示叙述、动作摘要、异常和验证。
- “详细”视图按需读取脱敏参数、结果摘要、时间、Artifact 与诊断引用。
- 连续普通成功 Tool 可在 Client 视觉分组；分组只影响渲染，不改变 Runtime Item。

分组中断条件：Decision、SubAgent、失败、unknown、验证、Artifact、文件 Diff 和 Notice。这样普通成功调用可以紧凑，但高风险或需要用户理解的状态不会被吞掉。

## 状态所有权

| 信息 | 唯一所有者 | Presentation 保存或返回什么 |
| --- | --- | --- |
| Run 状态 | `local_runs` | 读取时映射状态，不复制 |
| 最终回答 | P12 后的 Assistant `local_thread_items` | `final_answer` source reference 与正文 |
| 完整中间模型叙述 | `assistant.round.committed` durable event | 该事件是此内容的唯一所有者 |
| 可展示 reasoning summary | 同一 round event | 仅 display-safe summary |
| raw reasoning | provider / LangGraph 内部 | 不进入 Client SSE、不持久化、不展示 |
| Tool / task SubAgent / verification | `local_tool_receipts` | source reference、脱敏摘要和解析状态 |
| durable child | child `local_runs` | child source reference 与投影状态 |
| 审批 / 问题 / 计划 / 对账 | wait candidate 与决定记录 | source reference 与当前决定 |
| Artifact | `local_artifacts` | metadata/ref，不复制正文 |
| 顺序 | `local_events.seq` + event 内 slot | 稳定 `order` |
| 折叠、动画、连续成功分组 | Client | disposable UI state |

## 安全与隐私

- Provider Adapter 必须区分 `raw_reasoning` 与 `display_reasoning_summary`；默认拒绝未知字段进入 summary。
- progress 是模型主动发给用户的普通文本，仍经过当前内容策略；不从隐藏 prompt 或 checkpoint 私有字段提取。
- Tool detail 在 Runtime 统一脱敏：凭据、Authorization、cookie、环境变量值和本地敏感路径不能因“详细视图”绕过现有保护。
- Tool 完整结果继续留在 Receipt / Artifact；presentation 只提供有界摘要和引用。
- source record 不属于请求 principal 或 Run 时，整个 Item 拒绝输出并记录诊断。

## 迁移顺序

### PR 1：协议与 Runtime Projection（2–3 天）

- 定义 Pydantic discriminated unions 和 OpenAPI contract tests。
- 增加 `assistant.round.committed`、稳定 `round_id` 和读取时 Module。
- Thread snapshot 加可选 `presentations`；旧字段不变。
- 用现有历史 fixtures 验证无第二份状态。

### PR 2：SSE 与 SDK Adapter（1–2 天）

- 为现有 SSE envelope 增加可选 `presentation_change`。
- 生成 SDK snapshot 类型；为 hand-written SSE union 增加类型守卫。
- 验证 snapshot → replay → live 无重复、无倒序。

### PR 3：Client 单一展示模型（3–4 天）

- `ChatMessage` 改为消费 `presentation`。
- 新建统一 renderer，完成态保留折叠过程。
- 旧 Runtime 使用现有 `timelineItem()` 作为 Compatibility Adapter。
- 旧 Client 继续消费现有 content 和通用 events。

### PR 4：Provider summary 与删除旧路径（2–3 天）

- 接入明确支持 display-safe summary 的 Provider Adapter。
- 删除 `message.reasoning` 清空、`projectTransientAssistantText()` 的跨轮职责和 Client 主路径的大型 event switch。
- 保留通用 durable events 作为审计、诊断和一个版本周期的兼容来源。

单人完整实现预计 8–12 个开发日。四个 PR 都有独立回滚边界，不要求一次切换全部 Provider。

## 兼容策略

- 新 Runtime + 旧 Client：继续提供 `content`、通用 events 和当前 SSE 字段；未知 `presentation_change` 被忽略。
- 新 Client + 旧 Runtime：Compatibility Adapter 从当前 durable events 生成粗粒度 Tool/Decision/terminal Items；不伪造已丢失的 progress 或 reasoning。
- 历史 Run：读取时投影现有 Receipt、wait、SubAgent、Artifact、terminal 和最终回答；旧回合缺少的叙述保持缺失，不回填猜测文本。
- 数据库：方案 A 不需要 presentation 表迁移；只增加新的 durable event payload。
- 兼容期结束后先删除 Client fallback，再评估哪些旧事件仅为 UI 存在；审计事件不因 UI 迁移删除。

## 验收标准

1. `progress → Tool → progress → Tool → final answer` 五段按真实顺序保留，Run 完成后仍可展开。
2. 并行 Tool 按模型原始顺序展示，不按完成时间重排；生命周期在稳定 Item 上原位更新。
3. 丢失全部临时 delta 后重连，durable snapshot 仍恢复 Item 顺序、摘要、状态和最终回答。
4. 快照、事件补页和 live SSE 组合后，与一次全新快照产生相同 presentation。
5. 没有 reasoning 的 Provider 仍显示 progress 和执行事实；只有 raw reasoning 的 Provider 不泄露原始内容。
6. approval、question、plan、reconciliation、failed、unknown 和 verification 不进入普通成功 Tool 分组。
7. P12 提交后的 completed Run 不包含 `in_progress` Item；无法确认的外部操作显示 `unknown`。
8. 新旧 Runtime / Client 两个兼容方向都有 contract test。
9. 标准视图不包含完整参数或 Tool 返回；详细视图通过脱敏测试。
10. 200 个 Turn、5000 个 durable events 的本地 fixture 上，presentation 快照投影 p95 低于 50 ms。

## 性能上限与升级点

读取时投影是有意的简单实现，成本为 `O(events + receipts + waits)`。先用验收 fixture 测量，不预先维护另一份索引。

只有以下任一条件真实发生时，才在相同 Interface 后增加物化 `local_run_presentation_items` Adapter：

- 上述 fixture 的 p95 持续超过 50 ms；
- 开始压缩或删除 source events，导致稳定顺序无法重建；
- 需要独立于 Run 事实分页、搜索或远程增量同步 presentation items。

即使升级，物化表也只能保存 source reference、稳定 order 和 authored narrative；Tool 状态、最终回答和外部结果仍不能复制。

## 删除点

迁移完成后删除：

- `App.tsx` 中每轮清空并累加 `message.reasoning` 的逻辑；
- `projectTransientAssistantText()` 在 Tool / question 时清空正文的展示职责；
- `ToolArgsByCallId` 的 Client 关联职责；
- `ChatMessage.agentEvents`、`subagents`、`reasoning` 作为主展示模型；
- `timelineItem()` 大型 switch 的产品主路径；
- `AgentProgress` 在正文开始或完成态时隐藏过程的特殊规则；
- MessageBubble、pending approval/question/plan 各自重复扫描事件的路径。

继续保留：

- 临时 token delta，用于低延迟动画；
- durable source events，用于审计、诊断和恢复；
- Run、Thread、checkpoint、Receipt、wait、Artifact 的现有所有权；
- Compatibility Adapter，直到最低支持版本不再需要。

## 开放问题

1. 详细视图第一版是否直接复用 diagnostics 的脱敏器，还是只显示 source metadata 与 Artifact 引用？推荐复用同一个脱敏函数，不复制规则。
2. display-safe reasoning summary 当前由 Runtime admission 静态声明，只允许官方 OpenAI preset 的 Responses 协议；后续 Provider 接入时再提升为 capability catalog 字段，Client 不参与猜测。
3. 完成态默认摘要文案是“过程”还是“活动记录”？推荐“过程”；“思考摘要”只用于确实存在的 reasoning summary。
