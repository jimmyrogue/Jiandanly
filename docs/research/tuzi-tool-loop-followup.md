# Tuzi 工具调用第二回合与图片模型协议调研

日期：2026-07-28

范围：只核对 Tuzi 已发布的 Apifox 文档，以及必要的 OpenAI 官方协议。本文不把中转站的实际行为等同于 OpenAI 官方实现；凡是文档没有明确说明的部分，均标记为推断。

> 落地状态（2026-07-28）：Runtime 已在共享 Provider 边界修复工具定义、历史 assistant tool call 和 `ToolMessage.name` 的可逆别名，并把兼容性探针改为带点号的 `shejane.ping` 两轮闭环；OpenAI Responses 也已成为可冻结的明确协议。Tuzi 的具体模型仍须用对应连接真实验证，不能由本次代码修复直接标记兼容。

## 结论

1. **Tuzi 没有公开 `gpt-5.6-luna` 专属的工具调用第二回合格式。**它的 Chat Completions 文档说明了首轮的 `tools` 和 `tool_choice`，但没有完整写出第二轮所需的 `assistant.tool_calls`、`role: "tool"` 和 `tool_call_id`。
2. **如果 Tuzi 的 `/v1/responses` 能调用该模型，Agent 工具循环优先选择 Responses API。**Tuzi 在 Responses 文档中明确提供了 `function_call_output`、`call_id` 和 `previous_response_id`，第二回合契约比它的 Chat 文档完整。这是针对 Tuzi 当前公开文档完整度的建议，不代表 Chat Completions 一定不支持工具回传。
3. **HTTP 200 只表示流式连接成功建立，不保证整次生成成功。**流打开后，上游仍可能失败，错误只能作为 SSE 事件或中转站自定义错误体返回。因此检测逻辑必须读取完整流，不能只看 HTTP 状态码。
4. **图片生成与主对话模型的收尾应解耦。**图片模型负责产生并保存图片制品；主对话模型负责消费工具结果并生成最终文本。图片成功、文本收尾失败时，应保留图片并只重试收尾，避免再次产生费用。
5. **当前失败还有一个很具体的协议风险：真实工具名 `image.generate` 含点号。**OpenAI Chat 的 Function Definition 只允许字母、数字、下划线和短横线；兼容性探针使用的 `ping` 合法，不能证明真实工具名也能通过上游校验。

## 1. Chat Completions 的工具调用第二回合

### Tuzi 公开文档覆盖了什么

Tuzi 的 [Chat 模型接口](https://tuzi-api.apifox.cn/490351156e0) 使用 `POST /v1/chat/completions`，请求定义包含 `tools` 和 `tool_choice`。它另有一个 [ToolCall 数据结构](https://tuzi-api.apifox.cn/271066784d0)，包含 `id`、`type: "function"`、函数名和 JSON 字符串参数。

但截至本次核对，Tuzi 已发布文档中没有找到以下内容：

- `gpt-5.6-luna` 的专属页面或专属工具回传规则；
- 第二回合 `assistant.tool_calls` 的完整消息示例；
- `role: "tool"` 与 `tool_call_id` 的请求字段说明；
- 要求 Chat 工具循环改用某种 Tuzi 私有格式的说明。

因此，不能从 Tuzi 官方文档得出“Chat 第二回合不受支持”的结论；只能确认其公开 Chat 文档对这一回合的描述不完整。

### 如果继续使用 Chat，应发送的标准格式

OpenAI 官方 [Chat API 参考](https://developers.openai.com/api/reference/resources/chat) 规定：助手请求工具后，下一次请求必须保留原始的 assistant 工具调用消息，并为每个调用追加一个 `tool` 消息；`tool_call_id` 必须对应原调用的 `id`。

最小结构如下：

```json
{
  "model": "gpt-5.6-luna",
  "stream": true,
  "messages": [
    {
      "role": "user",
      "content": "生成一张水墨山水图"
    },
    {
      "role": "assistant",
      "tool_calls": [
        {
          "id": "call_abc",
          "type": "function",
          "function": {
            "name": "generate_image",
            "arguments": "{\"prompt\":\"水墨山水\"}"
          }
        }
      ]
    },
    {
      "role": "tool",
      "tool_call_id": "call_abc",
      "content": "{\"ok\":true,\"image_url\":\"https://example.invalid/image.png\"}"
    }
  ]
}
```

需要特别保证：

- 不要只发送工具结果而丢掉前一条 `assistant.tool_calls`；
- 不要重新生成或改写调用 ID；
- 一个工具调用对应一条 `role: "tool"` 消息；
- `function.arguments` 和工具结果 `content` 都是字符串，其中可以承载序列化后的 JSON；
- 流式 `tool_calls` 可能分散在多个增量中，应拼接完成后再执行工具。

此外，OpenAI 官方 Chat Function Definition 对函数名的约束是 `a-z`、`A-Z`、`0-9`、下划线和短横线，最长 64 个字符。因此：

- `ping`、`image_generate`、`image-generate` 合法；
- `image.generate` 的点号不在允许范围内。

如果首轮为了适配上游把工具名改写成合法别名，后续所有回合都必须使用同一个别名，并在 Runtime 内部映射回真实工具；不能只在兼容性探针中使用合法的 `ping`，实际请求仍发送 `image.generate`。

### 为什么更建议 Tuzi 的 Responses API

Tuzi 的 [Responses 文本接口](https://tuzi-api.apifox.cn/490349069e0) 和 [Responses 多模态接口](https://tuzi-api.apifox.cn/463707786e0) 明确把工具调用、流式输出和会话续接列为支持能力。它还公开了 [FunctionCallOutput](https://tuzi-api.apifox.cn/278115062d0) 的第二回合结构：

```json
{
  "type": "function_call_output",
  "call_id": "call_abc",
  "output": "{\"ok\":true,\"image_url\":\"https://example.invalid/image.png\"}"
}
```

同时，Responses 文档定义了 `previous_response_id` 用于续接上一轮响应。OpenAI 官方 Chat 参考也建议新项目优先尝试 Responses API，以使用较新的能力。

所以更稳妥的策略是：

- 先验证 `gpt-5.6-luna` 是否真的可通过 Tuzi `/v1/responses` 调用；
- 若可用，Agent 工具循环采用 Responses；
- 若不可用，继续使用标准 Chat 格式，但兼容性测试必须覆盖完整两回合，而不是只验证模型能返回 `tool_calls`。

## 2. 为什么 HTTP 200 的流里仍会出现错误

Tuzi 的 [接口说明](https://tuzi-api.apifox.cn/) 规定，`stream: true` 时响应为 `text/event-stream`，内容通过连续的 `data:` 行传输，并以 `[DONE]` 结束。

HTTP 响应头在流开始前就已经发送。因此可能出现以下时序：

1. 中转站接受请求，返回 HTTP 200 并打开 SSE；
2. 中转站随后请求真实上游，或继续解析上游增量；
3. 上游在生成过程中失败；
4. HTTP 状态已无法改写，中转站只能在流内返回错误。

OpenAI 官方 [Responses 流式事件参考](https://platform.openai.com/docs/api-reference/responses-streaming/response/refusal) 也定义了流内 `error` 事件和 `response.failed` 事件。这直接说明“HTTP 200”和“生成成功”是两个不同层次的状态。

### 对 `bad_response_status_code / openai_error` 的判断

Tuzi 已发布文档中没有定义这两个错误码。以下只能作为最可能的解释，而不是官方结论：

- 中转站已经向客户端打开 200 SSE，但它访问的真实上游随后返回了非 2xx；
- 第一轮 `tool_calls` 成功，第二轮请求没有被某个上游适配器接受，例如丢失 `assistant.tool_calls`、`tool_call_id` 不匹配、消息角色或内容形态不受支持；
- 中转站把上游错误统一包装成 `openai_error`，再写入已经打开的流。

本次现场日志提供了更明确的线索：2026-07-28 00:09:48，Tuzi 记录的真实上游结果为 `status_code=400`，请求走 `/v1/chat/completions`；Request ID 为 `202607271609472544755658268d9d6rnfx4kbr`，上游标识为 `c28660177fc452f4e68c315cf1a96f85`，价格优先采样为 `734/750`，同组和换组重试均为 0 次。这与“客户端先收到已建立的流，Tuzi 随后把上游 400 包装成流内错误”的解释一致。

结合真实工具名 `image.generate` 不符合 OpenAI Function Definition 的命名约束，应优先排查上游是否因非法函数名返回 400。它不是唯一可能原因，但比泛化地归因于图片模型更具体，也能解释为什么使用合法工具名 `ping` 的兼容性探针通过、真实图片调用失败。

要确定具体原因，需要保留并提交给 Tuzi 支持：发生时间、完整 endpoint、Tuzi 返回的 `request_id`、脱敏后的第二回合请求体，以及从第一个 `data:` 到错误事件的原始流。Tuzi [接口说明](https://tuzi-api.apifox.cn/) 也要求反馈时提供时间、`request_id`、接口地址和脱敏后的请求/响应信息。

### 兼容性检测应该如何判定

不能把“HTTP 200”或“首轮产生了 `tool_calls`”当作通过。一次可靠的 Agent 工具兼容测试应完成：

1. 首轮流正常产生一个完整工具调用；
2. 客户端正确拼接参数并执行测试工具；
3. 第二轮按所选协议回传工具结果；
4. 第二轮流没有 `error` / `response.failed` / 中转站错误对象；
5. 模型产生最终 assistant 文本，并正常结束为协议规定的完成事件或 `[DONE]`。

任何流内错误都应覆盖 HTTP 200，判定为失败，并向用户显示服务端的错误码、消息和 `request_id`。

兼容性测试还必须使用与生产相同的工具名编码规则。只测试 `ping` 会漏掉非法字符、长度、别名映射等真实差异。

## 3. 图片生成与 Chat 收尾应解耦

Tuzi 官方文档本身已经体现出协议分离：

- [Images 生成接口](https://tuzi-api.apifox.cn/448333922e0) 使用 `POST /v1/images/generations`，结果是图片 URL 数据；
- [Chat 兼容的图片生成接口](https://tuzi-api.apifox.cn/343646951e0) 使用 `POST /v1/chat/completions`，但其已发布成功响应 schema 为空，返回契约并不完整；
- [Responses 接口](https://tuzi-api.apifox.cn/463707786e0) 是另一套带状态续接与工具调用的协议。

合理的运行边界是：

```text
主对话模型提出 generate_image 工具调用
        ↓
图片适配器调用 Images 或已验证的 Chat 图片协议
        ↓
先持久化图片 URL / base64 / 元数据与计费结果
        ↓
把轻量工具结果回传给主对话模型
        ↓
主对话模型生成最终文本回复
```

由此得到几个明确规则：

- 图片模型不是 Agent 的主对话模型，不负责完成通用工具循环；
- 图片生成成功后立即保存制品，不等待 Chat 收尾成功才保存；
- Chat 收尾失败时，保留并展示已经生成的图片，允许只重试文本收尾；
- 不要因为收尾失败自动重新调用付费图片生成；
- 用户直接调用图片能力时，可以在图片生成完成后直接结束，不必额外调用主对话模型；
- 若要支持 Tuzi 的 Chat 图片协议，应为它建立独立适配器，并先用真实成功响应固化返回格式，不能依据文档中的空 schema 猜测。

## 对 SheJane 的实现含义

以下是基于上述协议差异得出的产品建议，不是 Tuzi 文档原文：

- 模型能力配置应同时记录“用途”和“调用协议”，例如 Agent 对话 + Responses、Agent 对话 + Chat、图片生成 + Images、图片生成 + Chat wrapper；
- 对外发送的 Function Definition 使用符合协议的稳定名称，例如 `image_generate`；Runtime 内部维护它与 `image.generate` 的双向映射；
- 兼容性测试按协议分别执行，Agent 对话必须跑完整两回合；
- 兼容性测试应包含至少一个经过生产映射后的真实工具名，不能只使用 `ping`；
- SSE 读取器应把流内错误视为最终失败，即使 HTTP 状态为 200；
- 图片制品状态与对话收尾状态分别持久化，支持仅重试收尾；
- 对于 Tuzi 当前文档未说明的 Chat 图片返回格式，UI 应提示“需要真实响应验证”，而不是假定兼容 OpenAI Images。

## 来源

- Tuzi：[接口说明与流式约定](https://tuzi-api.apifox.cn/)
- Tuzi：[Chat 模型接口](https://tuzi-api.apifox.cn/490351156e0)
- Tuzi：[ToolCall](https://tuzi-api.apifox.cn/271066784d0)
- Tuzi：[Responses 文本接口](https://tuzi-api.apifox.cn/490349069e0)
- Tuzi：[Responses 多模态接口](https://tuzi-api.apifox.cn/463707786e0)
- Tuzi：[FunctionCallOutput](https://tuzi-api.apifox.cn/278115062d0)
- Tuzi：[Images 生成接口](https://tuzi-api.apifox.cn/448333922e0)
- Tuzi：[Chat 兼容图片生成接口](https://tuzi-api.apifox.cn/343646951e0)
- OpenAI：[Chat API 参考](https://developers.openai.com/api/reference/resources/chat)
- OpenAI：[Responses 流式事件参考](https://platform.openai.com/docs/api-reference/responses-streaming/response/refusal)
