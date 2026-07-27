# 模型服务流式工具调用探测调研

> 核验日期：2026-07-26  
> 来源范围：仅厂商官方 API 文档、官方集成指南。模型和兼容层会持续变化，结论应由 Runtime 的 Provider Profile 承载，不按“OpenAI-compatible”名称永久推断。

```text
主要阶段：P6 绑定资源并取得 Agent 定义
上游输入：P5 准备的 Runtime Connection、具体 Model ID 与凭据引用
下游输出：经过正式 Provider 适配器验证的模型能力；通过后可进入 P7/P8
状态所有者：Runtime 模型服务记录与操作系统凭据库
替换的旧路径：server.py 中只检查首轮 SSE 工具事件、并对所有 OpenAI-compatible 模型统一发送强制具名 tool_choice 的探测
```

## 结论

修复前的探测方式有系统性问题：它实际同时测试了“流式工具调用”和“强制指定某个工具”两种能力，却把后者失败统一解释为前者失败。**强制具名 `tool_choice` 不能作为跨平台通用探测。**

- DeepSeek V4 thinking 明确拒绝 `tool_choice`；阿里云百炼的思考模式也不支持强制工具。
- Kimi 的具名 function 在 thinking 开启时会 400；K3 只能用“一个工具 + `required`”获得确定性调用。
- Z.AI / GLM 当前只声明 `tool_choice: "auto"`，流式函数调用还需要 `tool_stream: true`。
- MiniMax 当前 Chat Completions schema 不声明 `tool_choice`，已废弃原生端点也只声明 `auto/none`；SiliconFlow 的 Chat Completions schema 同样没有声明该字段。
- Anthropic 的具名工具形态本身正确，但手动 extended thinking 只允许 `auto/none`，个别模型也不支持强制工具。
- 修复前的 `max_tokens: 64` 会让先思考后调用工具的模型在产生 `tool_calls` 前被截断；Kimi 的 thinking 工具调用文档明确建议更大的输出预算。当前实现用 4096 作为有费用上限的通用探测值，后续 Profile 可按模型放宽。
- 修复前的 `httpx.post()` 会先缓冲完整响应再解析 `response.text`，只能验证“最终响应采用 SSE 格式”，不能验证增量到达。

当前实现已经采用正式 Agent 共用的 LangChain Provider 适配器：通用基线省略 `tool_choice`，只声明一个 `ping` 工具，先取得流式工具调用，再由 Runtime 执行工具并把原始 AssistantMessage（含 reasoning 内容）和 ToolMessage 回传模型，要求第二轮返回精确成功标记。只有完整两轮闭环成功才写入 `verified`；GLM 的 `tool_stream: true` 也由共用适配器同时应用于探测和正式 Agent。

## 官方兼容矩阵

| 平台 | `tool_choice` 与 thinking / reasoning | 流式工具事件与额外要求 | 对当前统一探测的判断 |
| --- | --- | --- | --- |
| DeepSeek | Chat Completions 参考列出 `none/auto/required` 和具名 function；但官方 V4 集成指南明确写明 thinking mode 拒绝整个 `tool_choice` 参数。V4 默认 thinking 开启；thinking 工具循环还必须回传完整 `reasoning_content`。 | OpenAI SSE：`choices[].delta.tool_calls`，结束可含 `data: [DONE]`；`stream: true` 即可。探测 thinking 路径时应省略 `tool_choice`；若要单独验证强制选择，需显式 `thinking: {"type":"disabled"}`。 | **当前必然误判 V4 默认 thinking。** |
| Moonshot / Kimi | API 总体接受 `auto/none/required` 及具名 function，但约束属于模型与 thinking 组合：thinking 开启时具名 function 返回 400；K3 始终 thinking，只支持 `auto/none/required`；K2.6、K2.7 Code 不支持 `required`。K3 可用“只声明一个工具 + `required`”确定性探测；K2.6 只有关闭 thinking 后才适合具名强制。 | OpenAI SSE：工具片段位于 `choices[].delta.tool_calls`，按 `index` 拼接 name/arguments，最终 `finish_reason` 为 `tool_calls`，传输以 `data: [DONE]` 结束。Thinking 工具循环还需保留 `reasoning_content`；K2 thinking 文档建议 `max_tokens >= 16000`。 | **修复前 K2.6 默认 thinking + 具名对象会误判；64 token 上限也可能截断。** 当前 4096 是受控折中，Profile 仍需区分 K3、K2.6 与 K2.7 Code。 |
| 阿里云百炼 / Qwen | OpenAI-compatible 接口支持 `auto/none` 和具名 function，但官方明确：**思考模式不支持强制指定工具**。`enable_thinking` 是顶层扩展字段（SDK 中放 `extra_body`）；不同模型的默认和可切换性不同。 | OpenAI SSE：`choices[].delta.tool_calls`；thinking 模型要求流式调用。`stream_options.include_usage` 只影响 usage，不是工具流必需字段。 | 发现模型的思考状态未知时发送具名选择会误判；通用探测应省略/使用 `auto`，或在 Profile 明确支持时关闭 thinking 后另测强制选择。 |
| 智谱 GLM / Z.AI | 当前 Chat Completions 参考中 `tool_choice` 唯一可用值是字符串 `auto`；thinking 默认开启，`thinking.type` 可为 `enabled/disabled`。 | `stream: true` 返回标准 Event Stream 并以 `data: [DONE]` 结束；**GLM-4.6 及以上要流式返回 Function Calls 还需 `tool_stream: true`**。工具片段仍在 OpenAI 风格 `choices[].delta.tool_calls`。 | **当前具名对象无效，且缺少 `tool_stream: true`，会双重误判。** |
| MiniMax | 当前 Chat Completions schema 声明 `tools`，但不声明 `tool_choice`；已废弃原生端点也只声明 `auto/none`。MiniMax-M3 的 OpenAI-compatible thinking 默认开启，M2.x thinking 无法关闭。 | 当前官方流式 schema 没有完整声明 `delta.tool_calls`；旧官方示例会在流末返回 `object: "chat.completion"` 与 `choices[].message.tool_calls`。可选 `reasoning_split: true` 只改变思考位置；工具循环需保留完整 assistant 思考内容。 | 不能发送未被官方契约保证的具名对象；解析器还要同时接受 `delta.tool_calls` 与流内最终 `message.tool_calls`。官方契约不足时应保持 `unverified`，不能武断标记不兼容。 |
| 硅基流动 | 官方 Chat Completions schema 声明 `tools`，但没有声明 `tool_choice`；官方示例只使用 `auto`。能力和 thinking 约束属于具体托管模型；官方还注明部分 DeepSeek 模型只有 `enable_thinking: false` 才能 Function Call。 | `stream: true` 返回 SSE，结束为 `data: [DONE]`。官方只完整说明非流式 `message.tool_calls`，没有给出工具 arguments 的权威流式分片示例；解析应兼容 `delta.tool_calls` 与流内 `message.tool_calls`。`enable_thinking`、`thinking_budget` 是模型级扩展。 | 聚合平台必须按 Model Profile 探测；当前统一具名选择是未文档化输入，且只识别 `delta.tool_calls` 也可能误判。 |
| Anthropic Messages | 正常工具选择形态为 `auto/any/tool/none`，具名形态是 `{"type":"tool","name":"ping"}`。手动 extended thinking (`type: enabled`) 只允许 `auto/none`；自适应 thinking 可强制工具，但官方另有不支持强制工具的模型。 | 请求需要 `anthropic-version`、`x-api-key`、`max_tokens` 与 `stream: true`。SSE 先以 `content_block_start` 开启 `tool_use`，参数随后由 `content_block_delta.input_json_delta.partial_json` 分片，最后 `content_block_stop`；解析器必须忽略未知事件类型。 | 当前事件识别方向正确，但“所有 Anthropic 模型都可具名强制”的前提不成立；通用探测仍应 `auto`/省略。 |

## 建议的探测契约

### 1. 基线请求只测试生产必需能力

所有平台都发送一个无参数 `ping` 工具和一条明确指令，例如“必须调用 `ping`，不要输出文本”。默认省略 `tool_choice`；只在某平台要求显式值时发其官方支持的 `auto`。不要在通用层主动开启 thinking，也不要发送未被具体 Profile 允许的扩展字段。

Provider Profile 只需保留当前已经有真实差异的窄配置：

| Profile | 探测附加项 |
| --- | --- |
| `deepseek` | 默认 thinking 探测省略 `tool_choice`；可选第二项独立测试在 `thinking.disabled` 下的具名选择 |
| `dashscope` | thinking 状态未知时省略/`auto`；不要强制具名 |
| `glm` | `tool_choice: "auto"`、`tool_stream: true` |
| `kimi` | K3：一个工具 + `required`；K2.6：默认 thinking 下省略/`auto`，只有关闭 thinking 才具名；K2.7 Code：省略/`auto`；不要回退到 64 token 硬上限 |
| `minimax` | `tool_choice: "auto"` 或省略；保留默认 thinking |
| `siliconflow` | 省略未文档化的 `tool_choice`；按具体 Model Profile 决定是否发送 `enable_thinking: false` |
| `anthropic_messages` | 默认省略/`auto`；只在确认 thinking/model 组合支持时使用具名 `tool` |

### 2. 使用正式适配器增量读取 SSE

由正式 Provider 适配器聚合流式事件，不再在 `server.py` 维护第二套 SSE 解析器。首轮工具事件只是中间证据，探测还必须执行工具并完成第二轮最终回答：

- OpenAI-compatible：优先识别任一 `choices[].delta.tool_calls`；MiniMax、SiliconFlow 还要接受 SSE 中最终 `choices[].message.tool_calls`，并按 `index` 拼接参数片段；
- Anthropic Messages：`content_block_start.content_block.type == "tool_use"`，随后仍应允许 `input_json_delta`；
- 忽略 comment、`event:`、空行、未知事件和 `[DONE]`；
- 设置“首事件超时”和“总超时”，不要让 20 秒完整响应缓冲把慢 thinking 模型误判为协议不兼容。

### 3. 错误分类不能吞掉上游原因

- `401/403`：凭证或权限；
- `402/余额不足`：账单；
- `408/429/5xx`：临时不可用，可重试；
- `400/422` 且指出 `tool_choice`、thinking 或未知字段：**探测策略不匹配**，不是模型不支持工具；
- HTTP 成功但没有工具调用：先记为 `unverified`，允许一次 Profile 受控重试；只有官方明确不支持或重复的正确探测仍无工具事件时才标记 `incompatible`。

返回给 Client 的文案应保留安全清洗后的厂商错误原因，避免所有失败都收敛成“模型未通过兼容性测试”。

### 4. 探测上限

当前探测能证明“该 Connection + Model + 当前 Profile”可以完成一次流式工具闭环，包括工具结果回传与 thinking/reasoning 保留。它仍不是完整 Agent Eval：连续多工具、取消、断流恢复、usage 精度、复杂工作区任务和资源关闭由 Agent Evals 与 Runtime E2E 覆盖。

## 官方来源

- DeepSeek：[Chat Completions API](https://api-docs.deepseek.com/api/create-chat-completion/) · [Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode/) · [Oh My Pi 集成的 V4 兼容字段](https://api-docs.deepseek.com/quick_start/agent_integrations/oh_my_pi/)
- Moonshot / Kimi：[Tool Choice](https://platform.kimi.ai/docs/guide/use-tool-choice.md) · [模型参数与限制](https://platform.kimi.ai/docs/api/models-overview.md) · [Thinking 模型](https://platform.kimi.ai/docs/guide/use-thinking-models.md) · [Chat API 与 SSE](https://platform.kimi.ai/docs/api/chat.md)
- 阿里云百炼 / Qwen：[OpenAI-compatible Chat Completions](https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions) · [Function Calling](https://help.aliyun.com/zh/model-studio/qwen-function-calling)
- 智谱 GLM / Z.AI：[Chat Completions API](https://docs.z.ai/api-reference/llm/chat-completion)
- MiniMax：[当前 Chat Completions API](https://platform.minimaxi.com/docs/api-reference/text-chat-openai.md) · [OpenAI SDK 兼容接口](https://platform.minimaxi.com/docs/api-reference/text-openai-api.md) · [已废弃原生端点](https://platform.minimaxi.com/docs/api-reference/text-post) · [Anthropic Messages 兼容接口](https://platform.minimaxi.com/docs/api-reference/text-chat-anthropic)
- 硅基流动：[Chat Completions API](https://docs.siliconflow.cn/cn/api-reference/chat-completions/chat-completions.md) · [Function Calling](https://docs.siliconflow.cn/cn/userguide/guides/function-calling.md) · [Stream Mode](https://docs.siliconflow.cn/cn/faqs/stream-mode.md)
- Anthropic：[定义工具与 tool_choice](https://platform.claude.com/docs/en/agents-and-tools/tool-use/define-tools) · [Thinking 与工具约束](https://platform.claude.com/docs/en/build-with-claude/thinking) · [Streaming Messages](https://platform.claude.com/docs/en/build-with-claude/streaming) · [Messages API](https://platform.claude.com/docs/en/api/messages)
