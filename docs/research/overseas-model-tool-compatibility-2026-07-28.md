# 海外模型工具调用兼容性核对（2026-07-28）

> 主要阶段：P8（模型回合）
> 直接上游：P7 恢复的消息与检查点
> 直接下游：P9 工具调用校验、P10 工具结果回写
> 状态所有者：P8 模型调用账本和助手草稿
> 当前路径：`LedgerChatModel` → LangChain Provider adapter

本文只核对官方原生协议。OpenAI-compatible 中转服务仍应按它实际暴露的协议验证，不能从底层模型品牌推断兼容性。

## 落地状态（2026-07-28）

- P8 共享边界现已对工具定义、`tool_choice`、历史 assistant tool call、Responses `tool_call` content block 和 `ToolMessage.name` 使用同一可逆 alias；回到 Runtime 时恢复内部名称。
- 名称改写的契约测试覆盖 call ID、并行顺序、OpenAI Responses item metadata、Anthropic thinking block 和 Gemini thought signature 保真。
- Runtime 已增加 OpenAI Responses 与 Google GenerateContent 明确协议；Anthropic 继续使用原生 Messages。Run 接纳会冻结协议，OpenAI Responses 不把 Provider `previous_response_id` 当作唯一恢复状态。
- 兼容性探针改用 `shejane.ping`，复用生产 alias 并执行两轮闭环；Gemini 协议级失败 finish reason 会在 P9 fail closed。
- “实现了 adapter”不等于“所有具体模型已经实测兼容”。每个连接/模型仍须使用用户自己的凭证通过真实探针后才会标记 `verified`。

## 调研时 Runtime 基线（修复前）

- Runtime 目前只有 `openai_chat` 和 `anthropic_messages` 两个 adapter；没有 Google Gemini 原生 adapter（[`model_services.py`](../../runtime/src/shejane_runtime/model_services.py#L9)、[`builder.py`](../../runtime/src/shejane_runtime/agent/builder.py#L647-L684)）。
- 共享边界会把内部工具名编码为 `^[a-zA-Z0-9_-]{1,64}$` wire name，并在工具定义、`tool_choice`、历史 assistant/tool 消息和返回解码中复用同一映射。
- 当前兼容性探针执行“请求工具 → 返回工具结果 → 最终文本”的两轮闭环，并使用带点号的 `shejane.ping` 覆盖生产名称重放路径。

## OpenAI API

### 官方契约

| 项目 | OpenAI 官方要求 | 对 SheJane 的含义 |
|---|---|---|
| 工具名称 | Chat Completions 的 function name 只允许字母、数字、下划线和短横线，最长 64；工具结果必须用原调用的 `tool_call_id` 关联。[Chat Completions API](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create) | `image.generate` 不能直接进入 Chat wire；定义、`tool_choice`、历史 `assistant.tool_calls` 必须使用同一个 alias。 |
| Chat 工具循环 | 应保留模型返回的完整 assistant 工具调用消息，再追加对应的 `role: "tool"` 结果；每个调用由 ID 配对，模型随后返回最终文本或继续调用工具。[Function calling](https://developers.openai.com/api/docs/guides/function-calling) | 当前第二轮历史工具名未重新编码，正好破坏这一完整回放契约。 |
| Responses 工具循环 | Responses 把调用和结果建模为独立 Item：`function_call` 与 `function_call_output` 用 `call_id` 关联；手动管理上下文时要把原始 `response.output` 一并带入下一轮，也可使用 `previous_response_id`。[Function calling](https://developers.openai.com/api/docs/guides/function-calling) | 不能把 Responses 压平为 Chat messages 后丢失 Item 类型、call ID 或原始输出。 |
| Reasoning 回放 | 对 GPT-5/o 系列等 reasoning 模型，工具调用响应中的 reasoning Items 也必须与工具结果一起回传；无状态或 ZDR 场景需要保留并回放加密 reasoning 内容。[Migrate to Responses](https://developers.openai.com/api/docs/guides/migrate-to-responses) | 新增 Responses 支持时，Runtime 检查点必须保存完整 Provider Item，而不只是展示用 reasoning 文本。 |
| 流式事件 | Chat 使用 `choices[].delta`；Responses 使用类型化 SSE，包括 `response.function_call_arguments.delta/done`、`response.completed` 和 `error`。[Migrate to Responses](https://developers.openai.com/api/docs/guides/migrate-to-responses#update-streaming-consumers) | Chat 和 Responses 不能共用一套假定 `choices[].delta` 的解析器；任何流内 `error` 都必须覆盖 HTTP 200。 |
| 并行调用 | 响应可能包含零个、一个或多个 function call；应用应逐个执行，并以各自 `call_id` 返回结果。[Function calling](https://developers.openai.com/api/docs/guides/function-calling#handling-function-calls) | P10 可以按风险策略调度，但 P8/P9 必须保留全部调用、ID 和原始顺序。 |

### 调研判断（修复前）

1. **OpenAI Chat 确认受当前历史名称问题影响。** 首轮定义使用合法 alias，返回内部后恢复成点号名称；第二轮 `assistant.tool_calls` 又把点号名称序列化到 Provider 请求。
2. **OpenAI Responses 尚未成为 Runtime 的可执行协议。** 模型能力已经保存 `protocol`，但 admission 只把 `openai_chat_completions` 和 `anthropic_messages` 映射为 Adapter；`builder.py` 也只构造 `ChatOpenAI` Chat 或 `ChatAnthropic`。
3. **不需要引入新模型框架。** 当前 `langchain-openai` 已提供 Responses 模式；最小落地是增加明确的 `openai_responses` 协议选择、完整 Item 回放和契约测试，而不是重写 Agent 循环。
4. **Runtime 应继续拥有权威状态。** 默认使用检查点中的完整 Item 手动续接；如果启用 `previous_response_id`，也必须把它作为冻结协议状态持久化，不能让 Provider 会话成为唯一恢复点。

## Anthropic Messages API

### 官方契约

| 项目 | Anthropic 原生要求 | 对 SheJane 的含义 |
|---|---|---|
| 工具名称 | `name` 必须匹配 `^[a-zA-Z0-9_-]{1,64}$`；参数结构使用 `input_schema` JSON Schema。[Define tools](https://platform.claude.com/docs/en/agents-and-tools/tool-use/define-tools) | `image.generate` 不能直接上行；定义、`tool_choice`、历史 `tool_use.name` 必须使用同一个 alias。 |
| 工具请求 | assistant 内容中返回一个或多个 `tool_use` block：`id`、`name`、对象类型的 `input`；正常停止原因是 `tool_use`。[Handle tool calls](https://platform.claude.com/docs/en/agents-and-tools/tool-use/handle-tool-calls) | 必须保存 Provider 返回的 call ID，并把对象参数映射到 Runtime 的统一工具调用。 |
| 工具结果 | 下一条必须是 `user` 消息，内容为 `tool_result`；`tool_use_id` 必须指向原 `tool_use.id`。结果 block 必须紧邻对应 assistant 消息，并排在该 user 内容数组的任何 text 前面；工具失败使用 `is_error: true`。[Handle tool calls](https://platform.claude.com/docs/en/agents-and-tools/tool-use/handle-tool-calls) | 不能在 assistant 工具请求与结果之间插入普通消息；不能把工具错误只变成普通文本。 |
| 并行调用 | 一个 assistant 回合可包含多个 `tool_use`。下一条 user 消息必须一次返回每个调用的 `tool_result`，即使某个调用未执行也要返回 `is_error: true` 的结果；用 `tool_use_id` 配对。[Parallel tool use](https://platform.claude.com/docs/en/agents-and-tools/tool-use/parallel-tool-use) | P10 可以按风险决定并行或串行执行，但 Provider 回写必须保持一个完整批次，且每个 ID 恰好有一个结果。 |
| 流式工具参数 | `tool_use` 的 `input` 以 `input_json_delta.partial_json` 字符串分片发送；只有收到 `content_block_stop` 后才能把完整输入当对象解析。[Streaming Messages](https://platform.claude.com/docs/en/build-with-claude/streaming) | P8/P9 只能把分片用于临时显示；P10 只能接收完成并成功解析的参数对象。 |
| 流内错误 | SSE 建连成功后仍可能收到 `event: error`（如 `overloaded_error`）；未来还可能增加事件类型，客户端应忽略未知事件而不是崩溃。[Streaming Messages](https://platform.claude.com/docs/en/build-with-claude/streaming) | 不能只按 HTTP 状态判断成功；流内 error 必须进入统一模型失败结算。 |
| Thinking 回放 | 工具循环被视为同一 assistant turn。返回工具结果时，必须把 assistant 的 `thinking`、`redacted_thinking`、`signature` 等内容完整且不修改地回传；重建或过滤会触发 400。手动 extended thinking 只兼容 `tool_choice: auto/none`，adaptive thinking 才支持强制工具。[Thinking in tool and multi-turn workflows](https://platform.claude.com/docs/en/build-with-claude/thinking-tool-workflows) | 若启用 Anthropic thinking，alias 编码必须只改工具名，不能丢弃或改写原始内容 blocks/signature；thinking 配置不能在同一工具循环中变化。 |

### 调研判断（修复前）

1. **确认缺口：历史工具名没有重新编码。** 首轮定义会发出 `image_generate`，返回 Runtime 后恢复为 `image.generate`；第二轮历史消息又会把这个内部名交给 Anthropic adapter。Anthropic 明确禁止点号，因此严格端点会拒绝该请求。
2. **当前可复用：Anthropic 原生 adapter。** `ChatAnthropic` 已经负责把统一的 `AIMessage` / `ToolMessage` 转成 `tool_use` / `tool_result`；不需要在 Runtime 再实现一套 Anthropic 消息序列化器。
3. **需要契约测试：批次完整性。** 覆盖一次 assistant 返回两个 call、P10 返回两个对应结果、第二轮成功收尾，并验证中间没有插入普通消息。
4. **Thinking 目前不是已发生缺陷。** Runtime 构造 `ChatAnthropic` 时尚未启用 thinking 参数；如果未来启用，必须增加“原样保留 thinking/redacted/signature”的两轮测试，不能仅测试展示用 reasoning 文本。

## Google Gemini API

### 官方契约

| 项目 | Google 原生要求 | 对 SheJane 的含义 |
|---|---|---|
| 工具名称 | 原生 `FunctionDeclaration.name` 允许字母、数字、下划线、冒号、点和短横线，最长 128；因此 `image.generate` 在原生 Gemini 中合法。[GenerateContent API reference](https://ai.google.dev/api/generate-content) | Google 原生不需要为点号改名，但继续使用更严格的 64 字符共享 alias 可减少跨 Provider 差异。不能由此推断 Google 的 OpenAI-compatible 入口也接受原生格式。 |
| 调用与结果 | GenerateContent 返回 `functionCall`（`name`、对象类型 `args`、`id`）。Gemini 3 总是返回唯一 `id`，下一轮 `functionResponse.id` 必须原样匹配；历史顺序是 user 内容 → 完整 model `Content` → user `functionResponse`。[Function calling](https://ai.google.dev/gemini-api/docs/generate-content/function-calling) | Runtime 不能重建 ID，也不能只保存函数名和参数后丢弃完整 Provider 内容。 |
| Thought signature | Gemini 3 工具调用的 `thoughtSignature` 必须留在收到它的原始 `Part` 并在下一轮原样回传，否则 400。单调用签名在该调用上；并行调用通常只在第一个调用上；顺序调用的每一步都要保留签名。[Thought Signatures](https://ai.google.dev/gemini-api/docs/generate-content/thought-signatures) | 通用消息归一化必须保留 Provider 扩展字段及其位置，不能只保留统一的 `tool_calls[{id,name,args}]`。 |
| OpenAI-compatible 签名 | Google 官方示例把签名放在 `assistant.tool_calls[].extra_content.google.thought_signature`；下一轮仍使用 `role: tool`、`name`、`tool_call_id` 和 `content`。[Thought Signatures](https://ai.google.dev/gemini-api/docs/generate-content/thought-signatures) | SheJane 当前若通过 `openai_chat` 接 Gemini，必须确认 LangChain 和 alias 改写不会删除 `extra_content`。只修工具名不足以兼容 Gemini 3。 |
| 并行调用 | 并行历史必须保持“全部 functionCall 后跟全部 functionResponse”的顺序；`FC1+signature, FR1, FC2, FR2` 的交错形式会返回 400。[Thought Signatures](https://ai.google.dev/gemini-api/docs/generate-content/thought-signatures) | P10 可以改变执行调度，但不能改变 Provider 历史中调用批次和结果批次的相对顺序。 |
| 流式调用 | 新 Interactions API 通过 `step.start` 给出调用 name/ID，通过 `step.delta` 分片发送 JSON arguments；执行前必须聚合为完整调用。[Function calling](https://ai.google.dev/gemini-api/docs/function-calling) | 只有完整 ID、名称和参数才能进入 P9/P10；分片仅用于 P8 临时状态。 |
| 语义终止错误 | HTTP 响应中的 `finishReason` 还能表示 `MALFORMED_FUNCTION_CALL`、`UNEXPECTED_TOOL_CALL`、`TOO_MANY_TOOL_CALLS`、`MISSING_THOUGHT_SIGNATURE`、`MALFORMED_RESPONSE` 等失败。[GenerateContent API reference](https://ai.google.dev/api/generate-content) | 不能把 HTTP 2xx 或流正常结束直接视为成功；P9 必须检查 Provider 的终止语义。 |
| Schema 与参数 | 原生声明使用 OpenAPI/JSON Schema 风格的对象 schema，原生 `args` 是 JSON 对象；Google OpenAI-compatible 形式的 `function.arguments` 是 JSON 字符串。[Function calling](https://ai.google.dev/gemini-api/docs/generate-content/function-calling) | Provider adapter 应负责 schema 和参数 wire shape；Runtime 内部继续只保留对象参数，不增加两套工具实现。 |

### 调研判断（修复前）

1. **Google 原生当前尚未接入。** Runtime 的 adapter 枚举和模型构建器都没有 Gemini/Google 类型，因此不能把现状描述为“支持 Google 原生 API”。
2. **通过 OpenAI-compatible 入口仍有高风险缺口。** 当前统一消息只显式处理标准 `tool_calls` 名称；尚无测试证明 `extra_content.google.thought_signature` 在模型返回、内部名称恢复、第二轮重新编码后仍原样存在。
3. **Google 的核心风险不是点号，而是状态回放。** 原生协议允许 `image.generate`，但 Gemini 3 对 call ID、Part 顺序和 thought signature 的要求比普通 OpenAI 工具循环更严格。
4. **最小验证应是 Gemini 3 两轮真实回环。** 使用一个带点号的工具，断言 ID 不变、签名不变、第二轮最终文本成功；再补一个双并行调用，断言调用在前、结果成组在后。通过前不能标记 Google 兼容。

## 跨 Provider 的最小修改边界

1. 在共享 Provider 出站边界对工具定义、`tool_choice`、历史 assistant tool calls 使用同一份可逆 alias；返回内部前再解码。
2. 名称改写只能修改 name，必须完整保留 call ID、原始内容 block、Anthropic thinking/signature、Google `extra_content`/thought signature 和调用顺序。
3. P9 统一拒绝未完成参数、语义错误 finish reason 和不完整 call/result 批次；P10 仍按 Runtime 风险策略执行，但按 Provider 契约成组回写。
4. 兼容性探针复用生产映射，至少覆盖单调用两轮、并行两调用和 Provider 状态签名回放；不要按厂商复制 Agent 循环。
