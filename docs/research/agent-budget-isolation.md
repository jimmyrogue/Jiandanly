# Agent 子任务预算隔离调研

> 调研日期：2026-07-20
> 结论记录，不是运行时提示词。目标是修复子 Agent 耗尽全局预算、导致主 Agent 无法汇总的问题。

## 当前故障事实

- Run 冻结的 `max_model_calls` 为 20；主 Agent 与两个 `researcher` 共用同一 `agent` 模型调用账本。
- 两个 `researcher` 共触发 33 次 `web.fetch`，其中 27 次失败；最终 20 个模型调用额度全部耗尽。
- `RESEARCHER_PROMPT` 虽写有“最多 5 次工具调用”，但 `researcher` 当前只挂载 `ToolReviewMiddleware`、`ToolExecutionMiddleware` 和 `FileWriteConflictMiddleware`，没有代码级工具或模型回合限制。
- `task` 属于 Runtime 内部控制流工具；子任务抛出的确定性 `ModelCallBudgetExceeded` 被外层工具回执写成 `outcome_unknown`，错误触发了用户对账。
- 主要阶段是 P8（模型预算）；相邻阶段是 P9（失败路由）和 P10（`task` 工具回执）。预算与回执的权威状态所有者均为 Runtime。

## 一手资料比较

| 框架 | 官方机制 | 对 SheJane 的启示 |
| --- | --- | --- |
| Deep Agents | 自定义子 Agent 的 `middleware` **不继承**主 Agent；官方把子 Agent middleware 明确作为日志、限流等行为的独立配置。[Subagents](https://docs.langchain.com/oss/python/deepagents/subagents) | 主 Agent 已有的限制不会自动保护 `researcher`；限制必须显式加入每个子 Agent。 |
| LangChain | 已内置 `ModelCallLimitMiddleware` 和 `ToolCallLimitMiddleware`，分别限制单次运行的模型调用和工具调用，并支持达到上限后结束、报错或阻止后继续。[Prebuilt middleware](https://docs.langchain.com/oss/python/langchain/middleware/built-in) | 当前锁定的 LangChain 1.3.12 已包含这两个 middleware，应直接复用，不再写一套计数器。 |
| OpenAI Agents SDK | “agents as tools”让 manager 保持对用户输出和最终汇总的控制；`Agent.as_tool(..., max_turns=...)` 为每个嵌套 Agent 设置独立回合上限。[Agent orchestration](https://openai.github.io/openai-agents-python/multi_agent/) [Agent.as_tool](https://openai.github.io/openai-agents-python/ref/agent/) | 子 Agent 应是有界工具调用，主 Agent 必须保留最终回答权和可用回合。 |
| Claude Agent SDK | Agent loop 提供 `max_turns`、总成本预算，并用明确的错误 subtype 表示达到回合上限，而不是把确定性预算终止当成外部结果未知。[Agent loop](https://code.claude.com/docs/en/agent-sdk/agent-loop) | 总预算和子 Agent 回合预算应并存；预算耗尽必须作为确定性失败传播。 |

## 建议方案

采用现有组件完成最小修复，不增加调度器或新的预算服务：

1. Runtime 持久化的全局 `max_model_calls` 默认提高到 100，继续作为最终硬上限。
2. 给每个子 Agent 显式挂载已安装的 `ModelCallLimitMiddleware(run_limit=50, exit_behavior="end")`，达到上限时返回明确的有界结果，不再抛到外层对账。
3. 给 `researcher` 显式挂载 `ToolCallLimitMiddleware(tool_name="web.fetch", run_limit=10)`，把提示词限制变成代码约束。不要新增自定义计数 middleware。
4. 主 Agent 不设置累计 `task` 次数。调度偏好并发 3 个，Runtime 强制单批最多 5 个；完成后可在共享预算允许时滚动补位。每个子 Agent 仍受自己的 12 次模型调用上限约束，父 Run 的持久模型账本保留最终汇总额度。
5. `task` 自身不执行外部副作用；其子工具已有各自的持久化回执。因此 `ModelCallBudgetExceeded`、`ModelCallLimitExceededError` 等确定性子任务失败应把外层 `task` 回执结算为 `failed`，返回模型可见的失败结果，不得进入 `outcome_unknown` 对账。真实副作用工具是否需要对账仍由其自己的回执决定。

建议从 `researcher` 的 5 次 `web.fetch` 与有界模型回合开始；不先做按 Token、美元或动态任务复杂度分配。这些能力只有在现有固定上限的评测证明不足时才需要。

## 最小验收

1. 五个 `researcher` 并行且所有 `web.fetch` 都失败：每个子 Agent 最多执行 10 次 `web.fetch`，主 Agent 仍能产生最终回答。
2. 子 Agent 达到模型回合上限：`task` 返回有界失败结果，Run 不出现 `tool_reconciliation_required`。
3. 子 Agent 内真实副作用工具出现不确定结果：仍只为该具体工具生成对账，不能被新的 `task` 失败分类吞掉。
4. 正常研究任务：子 Agent 的简短总结仍回传主 Agent，现有全局模型调用账本与用量统计保持完整。
