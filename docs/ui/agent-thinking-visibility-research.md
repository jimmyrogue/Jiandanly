# Agent 思考与活动可见性调研

> 初次调研：2026-07-19；最新复核：2026-08-04
>
> 范围：LiveAgent、Codex、Claude Code、OpenHands、Cline，以及初次调研覆盖的 ChatGPT、Gemini CLI、Cursor、GitHub Copilot。只采用官方文档、官方帮助、官方博客或官方源码仓库。

## 2026-08-04 复核结论

SheJane 当前的问题已经不是 7 月时的“Tool 卡片太多”，而是矫枉过正后的**过程过弱**：Runtime 实际拥有相当丰富的工具、审批、验证、修复和 SubAgent 事件，但 Client 没有保留一个完整、按发生顺序组织的“工作过程”。

若本节与下方 2026-07-19 的产品记录冲突，以本次复核为准。

成熟 Agent 的共同做法不是把原始思维链直接铺出来，而是同时保留三种不同信息：

1. **工作叙述（progress narrative）**：面向用户的短句，例如“我先核对事件协议，再检查现有投影逻辑”。这是 Agent 主动说明接下来做什么，不是原始 CoT。
2. **思考摘要（reasoning summary）**：供应商可选提供的可展示摘要。并非每个模型都有，也不应成为过程可见性的唯一来源。
3. **执行轨迹（action trace）**：Tool、命令、文件改动、搜索、审批、SubAgent、验证和错误等确定性事件。

LiveAgent 和 Codex 的优势就在于把这三类内容放进同一条有序的 Turn / Round 记录中；Claude Code 则进一步用普通视图与详细 transcript 控制信息密度。SheJane 现在把最终文本、单个 `reasoning` 字符串和 `agentEvents` 分开投影，顺序与上下文被削弱，所以即使底层事件很多，用户仍只感到“正在思考……然后突然给结果”。

### 当前实现的根因

1. **模型每轮的中间叙述被主动清空。** [`projectTransientAssistantText`](../../client/src/features/chat/chatStore.ts) 在 `llm.round.started`、`tool.requested` 或 `question.asked` 时返回空字符串；对应测试也明确要求“只保留当前模型回合文本”。因此模型在调用 Tool 前说的“准备检查附件”等内容不会进入最终可见过程。
2. **思考内容只有一个槽位。** [`appendLocalRunEvent`](../../client/src/App.tsx) 在每个 `llm.round.started` 时清空 `message.reasoning`，旧回合的思考无法与随后发生的 Tool 保持顺序关系。
3. **完成态活动被完全隐藏。** [`AgentProgress`](../../client/src/features/chat/components/AgentProgress.tsx) 在 `tone === 'done'` 时直接返回；一旦开始输出正文，运行中的活动也会消失。用户无法在完成后展开查看这次任务做过什么。
4. **Raw reasoning 已退出 Client 协议。** [`event_translator.py`](../../runtime/src/shejane_runtime/event_translator.py) 不再把 `reasoning_content` 转成 SSE；Runtime 只在自身模型能力明确允许时，将 Provider 标记为 display-safe 的 summary 归一化为 `reasoning_summary`。
5. **实时文本与 reasoning 不可可靠回放。** [`runtime-protocol.md`](../runtime-protocol.md) 明确把 `llm.delta`、`llm.reasoning`、`llm.tool_call_chunk` 和 `tool.progress` 视为可丢失的临时事件。仅在 Client 改样式可以改善当前运行中的体验，但无法让断线重连或历史会话恢复出完整过程。

所以根因不是缺一个更好看的“思考中”动画，而是缺少**有序、分块、可完成、可回放的 Turn 展示模型**。

## 最新产品与开源实现对比

### LiveAgent：按真实 Round 顺序交错展示

本次复核固定在 `Stack-Cairn/LiveAgent main@00a2c6fc43754f40022b0703459824559bee73ea`（2026-08-04）。它最值得 SheJane 参考的不是视觉样式，而是数据形状：

- [`uiMessages.ts`](https://github.com/Stack-Cairn/LiveAgent/blob/00a2c6fc43754f40022b0703459824559bee73ea/crates/agent-gui/src/lib/chat/messages/uiMessages.ts) 把一个 Assistant turn 保存为多个 `UiRound`，每个 Round 又是按原顺序排列的 `thinking`、`text`、`tool` 和 hosted search block。它不会为了得到最终回答而覆盖前一轮内容。
- [`RoundContent.tsx`](https://github.com/Stack-Cairn/LiveAgent/blob/00a2c6fc43754f40022b0703459824559bee73ea/crates/agent-gui/src/pages/chat/components/assistant-bubble/RoundContent.tsx) 在 thinking 流式生成时自动展开，完成后仍保留为可折叠区；同一条 Assistant 记录中继续渲染 Tool 和最终文本。
- [`assistantBubbleUtils.ts`](https://github.com/Stack-Cairn/LiveAgent/blob/00a2c6fc43754f40022b0703459824559bee73ea/crates/agent-gui/src/pages/chat/components/assistant-bubble/assistantBubbleUtils.ts) 只合并连续的普通 Tool；Todo、AskUserQuestion、Image 和 SubAgent 等重要 Tool 保持独立卡片，避免一刀切压缩。
- [`ToolTraceGroup.tsx`](https://github.com/Stack-Cairn/LiveAgent/blob/00a2c6fc43754f40022b0703459824559bee73ea/crates/agent-gui/src/pages/chat/components/assistant-bubble/ToolTraceGroup.tsx) 把连续调用汇总成一行，显示总数、构成和 running / failed / success，展开后才显示逐条调用。
- [`ToolCallItem.tsx`](https://github.com/Stack-Cairn/LiveAgent/blob/00a2c6fc43754f40022b0703459824559bee73ea/crates/agent-gui/src/pages/chat/components/assistant-bubble/ToolCallItem.tsx) 的默认行只放 Tool 名、关键参数摘要、状态和文件增删数；参数、命令、结果和错误进入第二层。运行中的 Todo / 提问保持展开，完成后自动收起。

LiveAgent 给出的关键答案是：**不要只做“一个思考框 + 一个活动框”；要保留 Round 内真实的 `思考/说明 → 动作 → 结果 → 下一轮说明` 顺序。**

### Claude Code：普通视图与详细 transcript 分层

- Claude Code 默认会压缩低价值细节，例如 MCP 连续调用可以折叠成 “Called slack 3 times”；`Ctrl+O` 打开的 transcript viewer 才显示详细 Tool 使用、执行、时间戳与模型信息。[Interactive mode](https://code.claude.com/docs/en/interactive-mode#keyboard-shortcuts)
- 计划与后台工作不是混在对话日志里：`Ctrl+T` 显示最多五项任务清单，`/tasks` 单独查看正在运行的 shell 与 SubAgent。[Task list](https://code.claude.com/docs/en/interactive-mode#task-list) · [Run agents in parallel](https://code.claude.com/docs/en/agents#check-on-running-work)
- 前台 SubAgent 会把权限请求带回主会话；后台 SubAgent 继续并行，但需要额外权限的调用会被拒绝，状态仍可通过任务入口查看。[Subagents](https://code.claude.com/docs/en/sub-agents#run-subagents-in-foreground-or-background)

Claude Code 的启发是：**主对话需要足够强的当前动作和结果摘要，但完整审计日志应该有稳定的详细入口；计划、后台任务和对话 transcript 是不同的信息架构。**

### Codex：先定义可渲染 Item，再谈 UI

- Codex app-server 把一次 Turn 表示成一组有生命周期的 `ThreadItem`：`reasoning`、`plan`、`commandExecution`、`fileChange`、`mcpToolCall`、`collabToolCall`、`webSearch`、`imageView`、`sleep`、`contextCompaction` 等，而不是只有一个不断变化的 assistant 字符串。[app-server protocol](https://github.com/openai/codex/blob/6d4d9442c7142c08ac5c5098dfd6e82d8cd9f65a/codex-rs/app-server/README.md#L1447-L1506)
- 所有 Item 都有 `item/started` 与 `item/completed`；命令还可流式发送 output delta，并在完成项中携带退出码和耗时。Reasoning summary 与只对部分开源模型适用的 raw reasoning text 也是两个不同字段。[app-server events](https://github.com/openai/codex/blob/6d4d9442c7142c08ac5c5098dfd6e82d8cd9f65a/codex-rs/app-server/README.md#L1458-L1506)
- Codex 的基础指令要求在 Tool 调用前给用户简短 preamble，长任务中持续给出进度更新；Plan 是独立可渲染状态，而不是让用户从 Tool 日志猜进度。[Codex default instructions](https://github.com/openai/codex/blob/6d4d9442c7142c08ac5c5098dfd6e82d8cd9f65a/codex-rs/protocol/src/prompts/base_instructions/default.md#L264-L296)
- Codex TUI 会从 reasoning summary 的标题提取当前动作，完成后再生成弱化的摘要块；普通 Agent 命令输出默认只显示少量行，完整内容留给 transcript。[Reasoning streaming](https://github.com/openai/codex/blob/6d4d9442c7142c08ac5c5098dfd6e82d8cd9f65a/codex-rs/tui/src/chatwidget/streaming.rs#L229-L297) · [Command output rendering](https://github.com/openai/codex/blob/6d4d9442c7142c08ac5c5098dfd6e82d8cd9f65a/codex-rs/tui/src/exec_cell/render.rs#L431-L496)

Codex 的关键启发是：**用户感知到的“思考过程”主要来自模型主动写出的工作叙述，加上宿主渲染的结构化执行 Item，而不是依赖供应商恰好返回 raw thinking。**

### OpenHands 与 Cline：事件事实和展示投影解耦

- OpenHands 用不可变、append-only 的 typed event log 保存 `MessageEvent`、`ActionEvent`、`ObservationEvent`、错误和状态变化；Visualizer 只是逐事件读取并决定如何展示。[OpenHands events](https://docs.openhands.dev/sdk/arch/events) · [Custom visualizer](https://docs.openhands.dev/sdk/guides/convo-custom-visualizer)
- Cline SDK 把 `content_start/update/end`、`iteration_start/end`、usage、notice、done/error 分成不同事件，并提供 snapshot 给 UI 恢复当前状态。[Cline events](https://docs.cline.bot/sdk/events)
- OpenHands 的 Web UI 会把连续 Tool 自动成组：活动组显示最近动作和完成数，历史组压缩为“已完成 N 个动作”；Plan、TaskTracker、错误和 SubAgent 会打断普通分组，避免关键状态被吞掉。[Event grouping](https://github.com/OpenHands/OpenHands/blob/main/src/components/conversation-events/chat/group-events.ts) · [Grouped event UI](https://github.com/OpenHands/OpenHands/blob/main/src/components/conversation-events/chat/event-message-components/event-group.tsx)
- Cline 为 thinking、命令、Diff、浏览器、错误、checkpoint 和 SubAgent 提供专用 Row；thinking 在流式期间突出、完成后折叠。它的优点是语义清楚，代价是长任务容易逐行过密，因此仍需自动分组。[Cline chat UI](https://github.com/cline/cline/tree/main/webview-ui/src/components/chat)

这两者共同说明：**Runtime 事件应是事实，Client 的紧凑/详细视图是投影策略；不能为了 UI 简洁而销毁事件之间的顺序和回合边界。**

## 建议的 SheJane 目标形态

### 用户看到的默认结构

```text
正在处理
  我先检查现有事件流，确认哪些信息已经由 Runtime 提供。

  › 检查代码结构                         已完成
  › 搜索 18 处相关实现                   已完成
  › 对照 LiveAgent / Codex                进行中

  思考摘要                               可展开

最终回答正文……

过程 · 6 步 · 2 个工具组 · 已验证         ›
```

规则：

1. 运行中保留当前工作叙述，并按真实发生顺序插入 Tool / SubAgent / 审批 / 验证项。
2. 新一轮模型开始时，结束前一轮 block，不清空它。
3. 连续普通成功 Tool 可折叠为组；审批、提问、失败、验证失败、SubAgent 和文件 Diff 保持独立。
4. 最终回答仍是视觉主角；完成后过程变成一条可展开摘要，但不消失。
5. “标准”视图显示叙述、动作摘要和异常；“详细”视图显示参数、stdout、Tool 返回、时间戳和模型信息。先不增加第三档。
6. Provider 没有 reasoning summary 时，工作叙述和结构化事件仍能形成完整过程；不能退化成只有“正在思考”。

### 推荐的数据边界

```text
主要阶段：P4 客户端读取快照并订阅变化
来源阶段：P8 模型回合、P10 工具/等待、P12 终态提交
状态所有者：Runtime 拥有有序展示项与完成状态；Client 只拥有折叠状态和临时动画
替换的当前路径：ChatMessage.content + reasoning + agentEvents 三条割裂投影
```

建议将每个用户可见 Turn 投影成有序的 presentation items，最小字段为：

- `item_id`
- `round_id`
- `kind`: `progress_message | reasoning_summary | tool | plan | subagent | verification | notice | final_answer`
- `status`: `in_progress | completed | failed | waiting | canceled`
- `summary`
- `detail_ref` 或结构化 detail
- `started_at` / `completed_at`

逐 token delta 可以继续走临时通道，但 Item 的存在、顺序、摘要和终态需要持久化；P12 负责最终化。这样断线恢复时即使没有每个 token，也能恢复“做了什么、为什么这样做、结果如何”。

### 分阶段落地边界

1. **Client 快速修复**：不再清空旧 Round；将现有 reasoning、文本和 `agentEvents` 按 Round 组织；完成后保留一条折叠过程摘要。这个版本改善当前会话，但承认断线后可能缺失临时文本。
2. **Runtime 正式协议**：增加 provider-neutral `reasoning.summary` / `progress.message`，并持久化 presentation item 的顺序、摘要和终态；继续让 token delta 保持临时。
3. **Provider 归一化**：分别适配 OpenAI reasoning summary、Anthropic thinking summary/block、Gemini thought summary 与 DeepSeek `reasoning_content`；只有明确允许展示的内容才进入 `reasoning_summary`。

### 验收标准

- 一次 `文本 → Tool → 文本 → Tool → 最终回答` 的运行，五段内容按真实顺序保留，不发生覆盖。
- Run 完成后仍能通过一条紧凑摘要展开完整过程；普通成功项默认收起，失败与等待默认展开。
- 没有 reasoning 输出的模型仍能显示 Agent 工作叙述与执行轨迹。
- 断线重连后，至少恢复每个 Item 的顺序、摘要、状态和关键结果；不要求恢复可丢失的逐 token 动画。
- 详细视图能看到 Tool 参数、结果、错误、文件 Diff、SubAgent 状态与验证证据；标准视图不被这些内容淹没。

## 原始结论仍成立

## 结论先行

成熟 Agent 产品并不把“每一次内部事件”都做成同等醒目的永久卡片。共同方向是：

1. **不展示原始思维链**，最多展示模型生成的 reasoning / thinking 摘要。
2. **运行中突出当前状态**，允许用户中断、纠偏或处理审批，而不是要求用户阅读完整日志。
3. **完成后默认收起成功活动**，保留 Tool、命令、Diff 与错误作为可展开、可回看的审计证据。
4. **重复、低风险的成功 Tool 活动采用紧凑或汇总视图**；审批、失败、等待用户输入保持显眼。
5. **最终结果和执行轨迹分层**：结果面向多数用户，轨迹面向核查与调试。

截图中的主要问题不是“展示了 Tool”，而是**十余条成功 Tool 事件被渲染成同等视觉权重的独立大卡片**，并且出现在最终答复之后。它们包含多次针对同一文件的写入、读取和列目录，审计价值存在，但默认展示价值很低。

## 先统一三个概念

| 概念 | 内容 | 是否应默认展示 |
| --- | --- | --- |
| 原始思维链（raw chain of thought） | 模型内部逐 token 推理、犹豫、尝试和安全推断 | 否。不是可靠的产品解释，也可能包含敏感内容 |
| 思考摘要 / 状态（reasoning summary / status） | “正在检查冲突”“准备验证生成文件”这类模型生成摘要或宿主状态 | 运行中显示短状态；完成后收起 |
| Tool 活动（tool activity） | 读写文件、执行命令、搜索、浏览器操作、审批、返回值与错误 | 保留；成功项紧凑汇总，异常项展开 |

OpenAI 明确说明不会向用户展示原始思维链，o1 系列展示的是模型生成摘要；原因包括安全、用户体验及保留对未受约束 CoT 的监控能力。[OpenAI：Hiding the Chains of Thought](https://openai.com/index/learning-to-reason-with-llms/#hiding-the-chains-of-thought) Anthropic 当前 API 同样把面向用户的内容定义为 summarized thinking，并允许省略；Google 也明确区分 raw thoughts、加密 thought signature 与输出的 thought summary。[Anthropic：Extended thinking](https://platform.claude.com/docs/en/docs/build-with-claude/extended-thinking#summarized-thinking) · [Google：Gemini thinking](https://ai.google.dev/gemini-api/docs/thinking#thought-summaries)

因此，SheJane 不应把 UI 中的 Tool 记录称为“思考过程”。它们是**活动 / 执行记录**；真正可展示的 thinking 也应标为**思考摘要**。

## 产品对比

| 产品 | Reasoning / Thinking | Tool 与进度 | 完成后、错误与历史 | 对 SheJane 最有用的模式 |
| --- | --- | --- | --- | --- |
| ChatGPT | 手动选择 Thinking 时会显示 Thinking trace；自动路由且推理很短时可能不显示。开始前可先给短 preamble。trace 不是原始 CoT。[官方帮助](https://help.openai.com/en/articles/11909943-gpt-53-and-54-in-) | Deep research 先给可编辑计划，运行时可实时查看进度、打断并调整方向。[Deep research 帮助](https://help.openai.com/en/articles/10500283-deep-research-in-chatgpt) | 完成后进入面向阅读的全屏报告；活动历史和来源仍可回看，但不与报告正文争夺注意力。[同上](https://help.openai.com/en/articles/10500283-deep-research-in-chatgpt#download-and-review-your-results) | “当前进度”与“完成报告”分层；活动历史作为次级入口 |
| Codex | OpenAI 对 raw CoT 采取隐藏、摘要化原则；Codex 对复杂工作使用可见的进度计划，而不是把原始 CoT 当日志。[CoT 原则](https://openai.com/index/learning-to-reason-with-llms/#hiding-the-chains-of-thought) | 复杂任务用 todo 跟踪进度；工具调用和 Diff 在终端中被专门格式化，便于跟随。[Codex 更新](https://openai.com/index/introducing-upgrades-to-codex/#updates-to-codex) | App 以 thread 保存任务，支持切换后继续、在线查看改动和 Diff；Automation 完成后进入 review queue。[Codex App](https://openai.com/index/introducing-the-codex-app/) | 进度用计划表达；证据通过 Diff / Tool 明细按需查看；完成态进入 review |
| Claude / Claude Code | Claude Chat 显示带计时器的 Thinking 指示器，正文上方有可展开 Thinking 区域；官方称其内容为 thought process summary。高风险内容可能只显示“不提供剩余过程”。[Claude 帮助](https://support.anthropic.com/en/articles/10574485-using-extended-thinking) Claude Code 交互模式默认不显示摘要，只显示折叠 stub；`showThinkingSummaries` 才打开摘要。[Settings](https://code.claude.com/docs/en/settings#available-settings) | Claude Code Agent View 用 waiting / working / done 三种高层状态管理并行 Agent。[Agent View](https://claude.com/blog/agent-view-in-claude-code) | Client 的 Normal 把 Tool 折叠为摘要，Verbose 显示每个 Tool / 读文件 / 中间步骤，Summary 只显示最终回复和改动。[Desktop view modes](https://code.claude.com/docs/en/desktop#switch-view-modes) VS Code 中 thinking block 也默认折叠，可单个或全部展开；会话历史可搜索并恢复完整消息。[VS Code](https://code.claude.com/docs/en/ide-integrations#use-the-prompt-box) | 三档密度是最直接参考；正常模式不必逐条铺满 Tool |
| Gemini / Gemini CLI | Gemini API 默认只返回最终结果；thinking summary 需显式开启，也可设为 `none`，summary 可能为空。它不是 raw thought。[Gemini thinking](https://ai.google.dev/gemini-api/docs/thinking#thought-summaries) CLI 的 inline thinking 默认 `off`，窗口标题可只显示 Ready / Action Required / Working 高层状态。[CLI Settings](https://geminicli.com/docs/cli/settings/#ui) | CLI 自动调用 Tool；修改文件或执行命令前展示 Diff / 精确命令并请求确认。[Tools reference](https://github.com/google-gemini/gemini-cli/blob/main/docs/reference/tools.md#automatic-execution-and-security) `ui.compactToolOutput` 默认开启，目录列表和文件读取等输出以紧凑结构展示。[CLI Settings](https://geminicli.com/docs/cli/settings/#ui) | 可恢复错误默认以低详细度隐藏，`ui.errorVerbosity=full` 才完整显示。历史自动保存全部 Tool 输入输出和可用 reasoning summary，`/resume` 可搜索恢复。[CLI Settings](https://geminicli.com/docs/cli/settings/#ui) · [Session management](https://geminicli.com/docs/cli/session-management/) | Tool 数据仍在，但默认压缩输出；历史回看与当前对话分开 |
| Cursor | Thinking block 支持流式期间展开 / 折叠；官方变更记录把该能力视为正常交互。[Cursor 3](https://cursor.com/changelog/3-0) | Tool call 可折叠；Compact chat 会隐藏 Tool 图标、默认折叠 Diff、空闲时隐藏输入框。[Changelog 1.0](https://www.cursor.com/en/changelog?v=1.0) · [Compact mode](https://cursor.com/changelog/1-4#compact-chat-mode) | Agent 历史可打开完整对话、重命名、删除、导出 Markdown；Background Agent 另有独立入口。[History](https://docs.cursor.com/en/agent/chat/history) | 对长会话提供全局“紧凑模式”，而不是让每张 Tool 卡自己抢占空间 |
| GitHub Copilot | VS Code 可调 reasoning effort；可单独复制 final response，明确跳过 thinking steps 与 Tool calls。[VS Code 更新](https://github.blog/changelog/2026-04-08-github-copilot-in-visual-studio-code-march-releases/) | 连续 Tool 默认折叠，折叠区有摘要和 AI 标题；`collapsedTools` 可选分离、仅随 thinking 分组或始终分组，默认 `always`。[VS Code 1.107](https://code.visualstudio.com/updates/v1_107/#_collapsible-reasoning-and-tools-output-experimental) · [AI settings](https://code.visualstudio.com/docs/agents/reference/ai-settings#_chat-settings) | Subagent 活动默认收起，只显示正在做什么的 HUD；需要时展开完整输出。Coding Agent session log 保存 reasoning、Tool、进度和 setup 输出，便于历史审计与错误诊断。[Session visibility](https://github.blog/changelog/2026-03-19-more-visibility-into-copilot-coding-agent-sessions/) · [Session logs](https://docs.github.com/en/copilot/how-tos/copilot-on-github/use-copilot-agents/manage-and-track-agents) | “默认收起 + 当前工作 HUD + 可展开完整日志”最贴近截图问题 |

### 可确认的行业共识

- **摘要优于原始 CoT**：OpenAI、Anthropic、Google 都把用户可见内容定义为摘要、trace 或状态，不等于原始推理。
- **运行时和完成后采用不同密度**：运行时显示当前动作、时间或计划；完成后转为报告、Diff、review 或折叠日志。
- **Tool 透明度可调**：Claude 有三档视图，Cursor 有 Compact mode，Gemini CLI 默认 compact output，Copilot 将 subagent activity 默认收起。
- **审计能力不等于默认展开**：Copilot session log、Cursor chat history、Gemini resume / rewind 都保留回看能力，但不会把完整历史永久铺在主答案上。
- **例外优先于常规成功**：官方产品普遍把确认、权限、等待输入与失败作为需要用户注意的交互；普通成功调用则可以折叠或紧凑化。

## 针对当前截图的建议

### 建议的默认层级

```text
执行中
● 正在生成并验证游戏…                 8 / 11
  └─ 展开活动

完成后
✓ 已完成 · 创建并打开 贪吃蛇.html       12 项活动  ›

展开后
  发现文件名冲突                       1
  写入文件 · snake.html                6
  读取文件                             3
  列出目录                             1
  打开文件 · 贪吃蛇.html               1
```

这不是删除事件，而是把同一 Run 的事件从“卡片列表”变成“一条活动摘要 + 按需展开的审计明细”。

### 默认规则

1. **最终答复是主内容**。Tool 活动在时间线上应位于最终答复之前；Run 完成后只保留一条紧凑摘要，不在答复下方追加十几张大卡。
2. **运行中只突出一个当前动作**。旧的成功动作收进同一个 Activity disclosure；不要一边 streaming 一边无限增加全宽卡片。
3. **连续相同 Tool + 相同目标合并计数**。截图中多次 `写入文件 · snake.html` 应显示为 `写入文件 · snake.html × 6`。底层事件、参数、时间和返回值仍逐条保留。
4. **按语义阶段分组，而非只按 Tool 名分组**。例如“检查现有文件”“生成游戏”“验证并打开”，比“read / write / list”更适合普通用户；展开第二层后再显示具体 Tool。
5. **成功默认收起，异常默认展开**：
   - 普通成功：收起；
   - 正在运行：显示当前项；
   - 等待审批 / 用户输入：置顶并展开；
   - 失败 / 部分失败：展开错误摘要和重试入口；
   - 涉及外部发送、支付、删除等高风险动作：即使成功也保留醒目标记。
6. **思考摘要与 Tool 活动分成两个 disclosure**。`思考摘要`回答“为何采用这个方向”；`活动`回答“实际执行了什么”。不要把 Tool 行为包装成“思考”。
7. **提供三档密度，但先只实现必要两档**：默认“标准”（摘要 + 异常），可切换“详细”（逐条事件）。Claude 的 Summary / Normal / Verbose 证明三档有效，但 SheJane 当前不需要同时实现三套；等用户确实需要极简模式再加“仅结果”。
8. **历史可回看**。折叠只是展示策略，不应删掉 Runtime 拥有的事件，也不应改变导出、重放、诊断和审批证据。

### 视觉处理

遵循现有 SheJane 设计系统：

- Activity 容器使用一层 `--sj-paper-sunken` 或 hairline，不再给每个成功事件独立白色浮层和阴影。
- 当前执行用 seal red 小点；完成使用 moss；普通历史使用 `--sj-ink-faint`。
- 单行摘要包含：状态、语义动作、关键对象、数量、展开箭头。完整路径、参数和 stdout 放入展开层。
- 相同状态不要重复使用图标、圆点、阴影、边框四种提示；保留圆点和文字即可。

## 不建议做的事

- 不显示或保存所谓“完整原始思维链”作为用户功能；它不等于可信解释。
- 不用 LLM 在前端临时重新总结每一批 Tool 事件。优先根据已有事件类型、Tool 名、目标和状态做确定性分组，避免增加延迟、成本和新的不稳定层。
- 不在第一版引入复杂的可配置规则引擎。两种展示密度、确定性合并和异常展开已经能解决截图中的主要问题。
- 不隐藏审批、错误或需要用户接管的信息。压缩常规噪声不能牺牲可控性。

## 推荐决策

SheJane 可以采用 **“标准模式默认 + 详细模式可选”**：

- 运行中：一个当前状态行，下面是收起的累计活动数；
- 完成后：一条完成摘要，默认收起所有成功 Tool；
- 展开：先按语义阶段分组，再查看逐条 Tool；
- 审批、等待输入、错误：自动展开；
- 思考：展示 Agent 主动给用户的工作叙述；provider 提供的 summary / trace 作为单独的可选折叠层，永远不把 Tool activity 命名为 thinking。

Client 可以先基于现有事件恢复 Round 顺序并保留完成态摘要；完整的历史回放则需要 Runtime 持久化 presentation item 的顺序、摘要和终态。两步都不应删除现有审计数据。
