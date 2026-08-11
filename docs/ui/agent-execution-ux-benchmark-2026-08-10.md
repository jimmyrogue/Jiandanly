# Agent 执行过程 UX Benchmark（2026-08-10）

> 目的：回答“成熟 Agent 如何展示 Tool、进度、思考摘要与完成结果”，并定位 SheJane 当前 `web.fetch · success/failed` 与长段过程文本的展示回归。
>
> 资料范围：Codex、Claude Code、Cursor、GitHub Copilot；只使用官方文档、官方博客与官方开源代码。已有完整背景见 [`agent-thinking-visibility-research.md`](agent-thinking-visibility-research.md)，本篇只记录 2026-08-10 的增量结论与当前代码落点。

## 结论

成熟 Agent 的默认界面不是“原始 Tool 名 + success/failed”，也不是持续铺开全部中间文本，而是四层信息：

1. **当前动作**：一条可立即理解的短句，例如“正在读取网页 · bochk.com”。
2. **任务进度**：Todo、计划或 SubAgent 状态，回答“做到哪一步”。
3. **可展开过程**：按语义分组的 Tool、命令、Diff、来源和错误，回答“实际做了什么”。
4. **最终结果**：完成后成为视觉主角；成功过程默认折叠，失败、等待和审批保持显眼。

这不是新的产品方向。SheJane 现有设计已经规定“运行中显示当前 progress 与活动、完成后折叠为一行、思考摘要单独展开”；当前实现只是没有完整落到新的 `RunProcess` 上。

## 对比总表

| 产品 | 默认 Tool 密度 | 进度表达 | 思考内容 | 完成后 |
| --- | --- | --- | --- | --- |
| Codex | 把连续读取/搜索聚合成 `Explored`，命令显示 `Running` / `Ran`，MCP 显示 `Calling` / `Called`；输出只留预览，完整 transcript 另开 | 独立 Plan/Todo；运行中还有短 preamble / commentary | reasoning summary 与 raw reasoning 分开；raw 不是默认主界面信息 | 最终回答为主，Tool、Diff、测试输出保留为可核查证据 |
| Claude Code Desktop | Normal 把 Tool 调用折叠成摘要；Verbose 才显示每次调用、读文件和中间步骤 | SubAgent 面板与可定制 status line 展示状态、时长、成本和进度 | 通过视图密度控制中间步骤，不强迫所有用户阅读 | Summary 模式只显示最终回复和改动 |
| Cursor | Compact / Balanced / Detailed 三档控制 Tool trace 密度 | 复杂任务自动生成 Todo，实时勾选；运行中可在 Tool 边界插入纠偏消息 | CLI print 模式不输出 thinking；结构化流仍保留 Assistant、Tool started/completed 与 terminal result 的边界 | 最终结果、Diff 和 Review 独立于 Tool 事件流 |
| GitHub Copilot | 同类 Tool 自动分组，输出显示 inline preview，文件改动使用可展开 Diff | Session 列表显示运行状态；SubAgent 默认折叠，但 HUD 持续显示正在做什么 | 完整 session log 可追踪 reasoning 与 Tool，不在默认摘要层全部铺开 | 完成后进入 PR / review，session log 继续作为审计证据 |

## 逐项证据：已确认事实

### Codex

- OpenAI 官方说明：复杂工作由 Todo 跟踪进度；新版终端 UI 专门格式化 Tool call 与 Diff，使过程更容易跟随。[Introducing upgrades to Codex](https://openai.com/index/introducing-upgrades-to-codex/#updates-to-codex)
- Codex app-server 不把一次 Run 压成一个字符串：Turn 内是 reasoning、command execution、file change、MCP、collaboration、web search 等 typed Items，并通过 started / delta / completed 生命周期增量更新。[App-server protocol](https://github.com/openai/codex/blob/1c042dd4d823b451ae44029abaf0e13b7cef8904/codex-rs/app-server/README.md#L1444-L1500)
- Codex TUI 的官方源码把“已提交历史”和“运行中 active cell”分开；active cell 可以是合并后的 exec/tool group，完整 transcript 仍能即时看到 in-flight 调用。[`chatwidget.rs`](https://github.com/openai/codex/blob/1c042dd4d823b451ae44029abaf0e13b7cef8904/codex-rs/tui/src/chatwidget.rs#L1-L29)
- Codex 的默认指令要求把连续相关 Tool 合并描述；长任务用一到两句 plain-language update 说明“完成了什么、下一步是什么”，Plan 则保持为独立 checklist。[Default instructions](https://github.com/openai/codex/blob/1c042dd4d823b451ae44029abaf0e13b7cef8904/codex-rs/protocol/src/prompts/base_instructions/default.md#L224-L246)
- 官方实现使用面向人的动词，而不是后端事件名：MCP 运行中/完成后分别显示 `Calling` / `Called`；Plan 是独立的 `Updated Plan` Item。[`mcp.rs`](https://github.com/openai/codex/blob/1c042dd4d823b451ae44029abaf0e13b7cef8904/codex-rs/tui/src/history_cell/mcp.rs#L126-L145) · [`plans.rs`](https://github.com/openai/codex/blob/1c042dd4d823b451ae44029abaf0e13b7cef8904/codex-rs/tui/src/history_cell/plans.rs#L192-L234)
- Codex 把 reasoning summary、隐藏 reasoning 与 raw reasoning 的显式开关分开，说明“思考摘要”和“原始推理”不是同一展示层。[`config_toml.rs`](https://github.com/openai/codex/blob/1c042dd4d823b451ae44029abaf0e13b7cef8904/codex-rs/config/src/config_toml.rs)
- 多 Agent 状态按身份和任务展示，并区分 Running、Completed、Error；等待多个 Agent 与截断后的结果预览也有独立 UI，而不是多个匿名的 `task success`。[`multi_agents.rs`](https://github.com/openai/codex/blob/1c042dd4d823b451ae44029abaf0e13b7cef8904/codex-rs/tui/src/multi_agents.rs#L203-L279)
- Codex Cloud 完成后提供终端日志与测试输出的引用，用户可以核查动作证据，而不需要把所有日志放进最终正文。[Introducing Codex](https://openai.com/index/introducing-codex/#how-codex-works)

### Claude Code

- Claude Code Desktop 有三档 transcript：Normal 将 Tool 调用折叠成摘要并保留完整文本回复；Verbose 显示每次 Tool、文件读取与中间步骤；Summary 只显示最终回复和改动。[Desktop view modes](https://code.claude.com/docs/en/desktop#switch-view-modes)
- Claude Code Terminal 默认把重复 MCP 调用压成类似 `Called slack 3 times` 的单行；`Ctrl+O` 才展开详细 Tool 使用、执行、时间戳与模型信息。Todo checklist 与运行中的 shell/SubAgent 也有不同入口。[Interactive mode](https://code.claude.com/docs/en/interactive-mode#keyboard-shortcuts)
- Claude Code 的 status line 可持续显示模型、上下文、成本、持续时间等信息；SubAgent 行有独立 `status`、`description`、`startTime` 与 token 数据，不需要从聊天长文中猜任务是否仍在运行。[Status line](https://code.claude.com/docs/en/statusline#subagent-status-lines)

### Cursor

- Cursor 的复杂任务会生成结构化 Todo，列表实时更新并自动标记完成；默认消息通常在一次 Tool call 结束后立刻注入，使用户可以中途纠偏。[Planning](https://docs.cursor.com/en/agent/planning)
- Cursor 提供 Compact、Balanced、Detailed 三档 Tool call density：分别显示最少 Tool trace、重要中间步骤和接近完整的逐步上下文；同一版本也加强了后台/恢复任务的状态文案。[Compact chat responses](https://cursor.com/changelog/3-4#compact-chat-responses)
- Cursor 把 Agent 创建的终端放到后台，需要时可 `Focus` 查看命令或接管；这将“当前进度”和“完整命令输出”分成两层。[Shared terminal](https://cursor.com/changelog/1-3#share-terminal-with-agent)
- Cursor CLI 的默认 text 输出只给最终 Assistant message；需要过程时，structured stream 才提供 Assistant message、`tool_call started/completed` 与 terminal result，并用稳定 call ID 关联生命周期。Print 模式明确 suppress thinking events。[CLI output format](https://cursor.com/docs/cli/reference/output-format)

### GitHub Copilot

- GitHub Agents session log 会把相似 Tool 调用分组、显示 inline output preview、把文件改动渲染成可展开 Diff，并保留 Bash 命令透明度。[Redesigned session logs](https://github.blog/changelog/2026-01-26-introducing-the-agents-tab-in-your-repository/)
- SubAgent 活动默认折叠，但 HUD 显示当前任务；用户需要时再展开完整输出。[More visibility into agent sessions](https://github.blog/changelog/2026-03-19-more-visibility-into-copilot-coding-agent-sessions/)
- Copilot CLI Agent 在运行中同时显示 live progress 与 Tool calls，结束时单独给出 changes 和 updated files summary；session 列表则显示标题、Agent 类型、耗时与状态。[Copilot CLI agent and unified sessions](https://github.blog/changelog/2026-05-13-introducing-copilot-cli-agent-and-unified-sessions-view-in-github-copilot-for-jetbrains-ides/)
- 完成后的 PR/commit 可以追溯到 session log；日志保存 reasoning、Tool 与验证动作，用于 review 和审计，而不是与最终结果争夺默认视觉权重。[Managing agent sessions](https://docs.github.com/en/copilot/how-tos/copilot-on-github/use-copilot-agents/manage-and-track-agents)

## 从证据得出的推断

以下是产品设计推断，不是各厂商逐字声明：

1. **Tool 状态是事实层，不是默认文案。** `web.fetch`、`completed` 适合协议和诊断；用户层应翻译成动作、对象与必要结果，例如“已读取网页 · bochk.com”。
2. **运行中只需要一个强当前状态。** 旧成功动作应滚入同一个可展开过程；否则调用越多，主界面越像日志控制台。
3. **进度叙述要短，审计文本可以完整。** 默认层限制为一到两行；完整原文进入展开层，既防止长段“思考”淹没界面，也不销毁证据。
4. **完成不是给每个 Tool 加 success。** 完成态应先显示结果，再显示“过程 · N 步 · M 次工具调用”；只有失败、等待、unknown、审批与高风险动作需要默认展开。
5. **思考摘要不能承担进度职责。** Provider 没有 reasoning summary 时，Todo、当前动作和结构化 Tool 事实仍应组成完整过程。

## SheJane 当前实现差距

```text
主要阶段：P4 客户端读取快照并订阅变化
上游来源：P8 模型回合；P10 Tool / SubAgent / 等待；P12 终态提交
下游输出：Client Assistant message renderer
状态所有者：Runtime 拥有有序 presentation 与终态；Client 只拥有折叠、分组和动画
替换中的旧路径：agentEvents / AgentProgress 的 Client 本地展示投影
```

### 已确认的当前事实

- Runtime 已经生成稳定、有序、可恢复的 `progress`、`reasoning_summary`、`tool`、`subagent`、`verification`、`artifact`、decision、notice 与 `final_answer` Items；本次无需重新设计第二套 timeline。[`presentation.py`](../../runtime/src/shejane_runtime/presentation.py) · [`threads.py`](../../runtime/src/shejane_runtime/api_models/threads.py)
- 新 [`RunProcess.tsx`](../../client/src/features/chat/components/RunProcess.tsx) 对 Tool 直接输出 `item.tool_name` 与通用状态，所以 `web.fetch ... success/failed` 是确定的渲染结果，不是 Runtime 丢失状态。
- 同一文件对 `progress` 与实时 draft 逐条完整渲染，没有标准视图的行数上限或摘要层，因此模型在 Tool 前写得较长时，用户会看到一整段过程文本。
- 旧 [`AgentProgress`](../../client/src/features/chat/components/progress/AgentProgress.tsx) 已经具备人类动作文案、当前目标、连续成功合并、并行 SubAgent 状态与异常展开；但 [`ChatThread.tsx`](../../client/src/features/chat/components/ChatThread.tsx) 在存在 presentation 时优先走 `RunProcess`，这套成熟展示逻辑因此被绕过。
- [`chatToolPresentation.ts`](../../client/src/features/chat/projection/chatToolPresentation.ts) 已有 Tool 名本地化与 URL host、文件 basename、命令、查询等确定性摘要。第一步没有必要再写一套 mapper 或引入 LLM 总结。
- 当前 `RunPresentationToolItem` 只含 `tool_name`、状态、风险与稳定 ID，不含 display target 或结果摘要；只靠该 Item 无法在断线历史中恢复“读取 bochk.com”这类对象详情。[`threads.py`](../../runtime/src/shejane_runtime/api_models/threads.py)

### 与现有设计的偏差

现有 [`agent-execution-presentation-design.md`](../plans/agent-execution-presentation-design.md#client-信息架构) 已经要求：

- 当前 progress 与当前活动默认展开；
- Tool、SubAgent、验证使用稳定行并原位更新；
- 完成后过程折叠为一行；
- 标准视图显示叙述、动作摘要、异常和验证；
- 连续普通成功 Tool 视觉分组；
- reasoning summary 单独标为“思考摘要”。

所以这次应修复 renderer 的信息密度和语义映射，不应再建新的事件协议或第二个过程组件。

## 推荐的 SheJane 默认形态

### 运行中

```text
正在查找资料 · bochk.com
  已查阅 2 个网页 · 1 个来源不可访问                 ›

当前说明：正在核对开户要求与官方地址                 1–2 行
思考摘要                                               ›
```

- `web.fetch` → “读取网页”；`completed` 不单独显示为英文状态。
- 同类普通成功调用按语义与目标合并，例如“查阅网页 × 2”。
- 当前 Tool 原位更新，不为 started/completed 各加一行。
- progress/draft 的标准视图最多一到两行；完整文本进入展开层。
- reasoning summary 默认折叠，与 progress 分开。

### 完成后

```text
最终回答正文……

过程 · 6 步 · 2 次网页访问 · 1 个失败                 ›
```

- 最终回答先出现，成功过程默认折叠。
- 展开后按真实顺序显示进度叙述与动作组。
- 失败行显示“动作 + 对象 + 原因”，例如“无法读取 bochk.com · 404”；未知结果不渲染成成功。
- SubAgent 使用“描述 + running/completed/failed”，默认只突出仍在运行或失败的项。

## 最小落地顺序

1. **先修 Client renderer**：`RunProcess` 复用现有 `toolActionLabel` / Tool detail 规则，合并普通成功 Tool，并限制标准视图中的 progress/draft 高度。
2. **保留一个过程组件**：把 `AgentProgress` 已验证的语义规则迁入 presentation renderer 后删除重复路径，不让两套 UI 继续漂移。
3. **只在历史目标确实需要时补协议**：若要在不依赖兼容事件的重连/历史里显示 host、文件名或查询，再给 presentation 增加 Runtime 生成、脱敏后的可选 display detail；不要传完整 Tool 参数或返回值。

## 验收示例

输入事件：

```text
web.fetch requested https://www.bochk.com/a
web.fetch completed
web.fetch requested https://www.bochk.com/b
web.fetch failed 404
```

标准视图应是：

```text
运行中：正在读取网页 · bochk.com
完成后：过程 · 2 次网页访问 · 1 个失败              ›
```

展开后才显示两次调用的 URL、状态和 404。任何默认层都不应出现裸 `web.fetch`，也不应把完整 Tool 返回或原始 reasoning 铺在聊天正文中。
