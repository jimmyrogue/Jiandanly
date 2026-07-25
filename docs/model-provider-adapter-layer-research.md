# 模型 Provider 适配层调研

> 核验日期：2026-07-24
>
> 研究对象：SheJane Runtime 如何通过不同协议、凭证来源和服务形态接入模型。
>
> 本文不讨论 Provider 设置页的具体交互。外部产品事实只引用官方文档或官方源码；“对 SheJane 的建议”是基于当前代码的架构判断。

## 结论

SheJane 不需要再引入一套“万能模型框架”，也不应按 OpenAI、DeepSeek、OpenRouter 等品牌不断增加分支。

更合适的方向是：

1. **连接（Connection）**描述模型服务在哪里、用户选择了什么凭证以及配置版本；
2. **适配器（Adapter）**只描述调用协议，例如 OpenAI Chat Completions、Anthropic Messages；
3. **模型资料（Profile）**描述具体模型的能力、限制和请求差异；
4. **凭证来源（Credential Source）**独立处理 API Key、OAuth、无凭证、本机账户或未来的云身份；
5. 上层继续只面对 LangChain `BaseChatModel`，并由现有 `LedgerChatModel` 统一负责调用预算、用量和结果不确定性。

最值得参考的现有设计是：

- Pydantic AI 对 **Model / Provider / Profile** 的明确拆分；
- Open WebUI 的 **协议优先**策略；
- Vercel AI SDK 的 Provider Specification、Registry、能力警告和 namespaced provider options；
- LiteLLM 的广泛协议转换能力，但更适合作为 SheJane 可连接的外部网关，不适合嵌入 Runtime；
- Dify 的 Provider 插件适合开放生态，不适合 SheJane 当前固定能力、少供应商的阶段。

## 一、先把四个概念拆开

### 1. 服务连接

连接是用户实际配置的一处模型服务，例如：

- OpenAI 官方 API；
- OpenRouter；
- 企业自建 LiteLLM；
- AWS 某个 Region 的 Bedrock；
- Google Cloud 某个 Project 的 Vertex AI。

连接拥有名称、endpoint、适配器编号、凭证引用、模型列表和修订版本。它是运行时冻结到 Run 中的配置对象。

### 2. 调用协议

多个不同服务可以使用同一种协议：

- OpenAI、DeepSeek、OpenRouter 和 LiteLLM 都可以提供 OpenAI-compatible Chat Completions；
- Amazon Bedrock 同时提供 Converse、Messages、Chat Completions 和 Responses 等多种 API；

因此“OpenRouter”不是一种调用适配器，“OpenAI Chat Completions”才是。

Open WebUI 也明确采用协议优先设计，以 OpenAI-compatible API 组织不同服务连接，避免永久维护大量品牌适配模块。[Open WebUI OpenAI-Compatible](https://docs.openwebui.com/getting-started/quick-start/connect-a-provider/starting-with-openai-compatible/)

### 3. 凭证来源

调用同一种协议，也可能使用完全不同的凭证方式：

- Keychain 中的 API Key；
- OAuth 换取并保存在 Keychain 中的 token 或 API Key；
- Google Application Default Credentials；
- AWS 身份、短期凭证或 Bedrock API Key。

Google ADC 会从标准凭据文件、附加的服务账号等位置解析身份；Vertex AI 同时支持 API Key 和 ADC。[Google ADC](https://docs.cloud.google.com/docs/authentication/application-default-credentials) [Vertex AI Quickstart](https://docs.cloud.google.com/vertex-ai/generative-ai/docs/start/quickstart)

凭证取得和模型请求转换不是同一职责。OpenRouter OAuth 只改变凭证如何进入 Runtime，不应产生一个新的模型调用协议。

### 4. 模型资料

同一协议下的模型仍然有不同约束：

- 是否支持工具；
- 是否支持图片；
- 是否支持流式工具参数；
- JSON Schema 限制；
- reasoning 参数和值域；
- 上下文和最大输出；
- 是否需要保留签名或 provider metadata；
- token usage 是否在流末尾出现。

这些属于具体模型或模型族，而不是连接或认证。

Pydantic AI 将这三层定义得最清楚：

- `Model`：某类模型 API 的统一调用实现；
- `Provider`：认证、endpoint 和已认证 client；
- `Profile`：具体模型/模型族的能力及请求处理差异。

它还允许多个 Provider 共用一个接口，例如 OpenAI-compatible 服务共用 OpenAI 模型接口，同时由 Profile 修正 Gemini、DeepSeek 等模型的特殊行为。[Pydantic AI Models and Providers](https://pydantic.dev/docs/ai/models/overview/) [Pydantic AI Providers](https://pydantic.dev/docs/ai/api/pydantic-ai/providers/) [Pydantic AI Profiles](https://pydantic.dev/docs/ai/api/pydantic-ai/profiles/)

SheJane 应借用这个概念划分，但不需要迁移到 Pydantic AI。

## 二、成熟方案对比

### LangChain：SheJane 已经使用的执行接口

LangChain 的 Chat Model 为不同供应商提供一致的 `invoke`、`stream`、`bind_tools` 等接口，也支持通过统一初始化方法选择不同 Provider。[LangChain Providers and Models](https://docs.langchain.com/oss/python/concepts/providers-and-models)

对 SheJane 的意义：

- `BaseChatModel` 已经是 P8 模型调用的有效接口；
- `ChatOpenAI` 和 `ChatAnthropic` 已经完成主要消息、流和工具格式转换；
- 没有必要在它上面再定义一套完整的 Message/Chunk 类型；
- SheJane 需要的是创建、发现、验证和错误归一化的薄适配层。

LangChain 不会消除所有 Provider 差异。是否支持工具、结构化输出、图片和特殊参数仍需 Profile 和测试约束。

### Pydantic AI：最好的概念参考

Pydantic AI 的价值不在于“支持很多模型”，而在于它没有把品牌、API interface、endpoint/auth 和模型能力混成一个 Provider 枚举。

适合直接借鉴：

- Adapter 对应它的 Model interface；
- Connection + Credential Source 对应它的 Provider；
- SheJane Model Profile 对应它的 Profile；
- 模型字符串只负责选择，不暗含凭证和自动回退。

不建议为此替换 LangChain/Deep Agents。SheJane 已经在 LangChain 上拥有模型账本、中间件、检查点和工具循环，迁移成本远大于收益。

### Vercel AI SDK：Provider Specification 和 Registry

Vercel AI SDK 通过统一 Language Model Specification 规范普通生成、流式输出、工具调用、finish reason、usage、warnings 和错误；Provider Registry 负责由带命名空间的 ID 解析模型。Provider-specific 参数保留在 namespaced `providerOptions` 中，而不是强行塞进通用参数。[AI SDK Provider Management](https://ai-sdk.dev/docs/ai-sdk-core/provider-management) [Writing a Custom Provider](https://ai-sdk.dev/providers/community-providers/custom-providers)

值得借鉴：

- 统一核心能力，但保留 Provider-specific escape hatch；
- 不支持的设置返回 warning 或在严格模式失败；
- Registry 只解析明确的模型 ID，不承担隐式路由；
- Adapter 自己处理请求、响应和 stream chunk 的转换。

它是 TypeScript SDK，而 SheJane 的模型执行在 Python Runtime，因此只适合作为接口设计参考。

### LiteLLM：覆盖很广，但不适合嵌入 SheJane Runtime

LiteLLM Python SDK 将许多 Provider 映射到 OpenAI 输入、输出、流式 chunk 和异常类型；LiteLLM Proxy 额外提供认证、限流、成本、路由和日志。[LiteLLM Documentation](https://docs.litellm.ai/)

优势：

- Provider 覆盖广；
- OpenAI 格式输出和异常归一化；
- 有模型元数据、成本和 Router；
- 能作为独立网关接入企业模型。

不适合直接嵌入 SheJane 的原因：

- 与 LangChain Provider packages 形成第二套适配层；
- 自带重试、fallback、路由、预算和回调，容易与 `LedgerChatModel` 的唯一账本边界冲突；
- Provider 行为和 usage 完整度仍需逐个验证；
- 依赖面和升级风险明显扩大。

OpenAI Agents SDK 也把 LiteLLM/Any-LLM 定位为需要额外 Provider 覆盖或路由时才使用的第三方 beta 适配器，并提醒工具、结构化输出、usage 和 Responses 行为需要按实际后端验证。[OpenAI Agents SDK Models](https://openai.github.io/openai-agents-python/models/)

建议：把 LiteLLM Proxy 当成一个普通 OpenAI-compatible Connection；不要把 LiteLLM SDK塞进 Runtime。

### Open WebUI：协议优先

Open WebUI 主要支持广泛采用的协议；不符合协议的服务通过外部 proxy/pipe 连接。[Open WebUI FAQ](https://docs.openwebui.com/faq/)

这是最适合 SheJane 当前规模的扩展策略：

1. 先支持覆盖面最大的协议；
2. 对确实不能通过兼容协议获得所需能力的服务，才增加原生 Adapter；
3. 不为了 Provider logo 建一个新 Adapter。

### Dify：完整 Provider 插件平台

Dify Provider 插件包含声明式凭据 schema、模型 schema、凭据验证和 Provider 代码。[Dify Model Provider Plugin](https://docs.dify.ai/en/develop-plugin/dev-guides-and-walkthroughs/creating-new-model-provider) [Dify Model Schema](https://docs.dify.ai/en/develop-plugin/features-and-specs/plugin-types/model-schema)

这种方案适合第三方开发者发布 Provider、平台动态安装代码的产品。SheJane 当前模型能力由自己维护，不开放 Provider 插件生态；引入安装、签名、隔离、升级和兼容治理没有必要。

## 三、不同接入方式应该如何落位

| 接入方式 | 示例 | Adapter | Credential Source |
| --- | --- | --- | --- |
| 官方 OpenAI-compatible API | OpenAI、DeepSeek | `openai_chat` | Keychain API Key |
| 聚合/网关 | OpenRouter、LiteLLM、Vercel AI Gateway | `openai_chat` | API Key 或 OAuth 取得的 Key |
| 官方原生 API | Anthropic Messages | `anthropic_messages` | Keychain API Key |
| 云平台统一 API | Bedrock Converse | 未来 `bedrock_converse` | AWS 专用凭据解析 |
| 云平台原生 API | Vertex AI | 未来 `google_genai` | Keychain API Key 或受控 ADC |
| 新一代统一协议 | Open Responses | 实验性 `open_responses` | 由 Connection 决定 |

Amazon Bedrock 自己就同时提供 Converse、Invoke、Messages、Chat Completions 和 Responses 等接口，说明品牌不能唯一决定 Adapter。[Amazon Bedrock APIs](https://docs.aws.amazon.com/bedrock/latest/userguide/apis.html)

## 四、SheJane 当前已经具备的部分

当前代码已经存在一个相当完整的模型执行边界：

- `ChatOpenAI` 和 `ChatAnthropic` 把 Provider 响应转换成 LangChain `BaseChatModel`；
- `RuntimeModelProxy` 将可复用图定义绑定到当前 execution 的唯一模型；
- `LedgerChatModel` 在调用前预留账本、限制上下文、处理工具名、结算 usage 和记录未知结果；
- Run admission 会冻结 Provider 版本、credential ref、model id 和 profile；
- `ModelProviderError` 已经是 Provider-neutral 错误；
- API Key 已由 Runtime credential store 持有。

因此这不是一次模型运行链重写。主要阶段仍是 **P8 模型回合**：

```text
主要阶段：P8
上游输入：P6/P7 冻结并绑定的 model connection、credential ref 和 model profile
下游输出：P9 可验证的完整文本、推理、工具调用、finish reason、usage 或结构化错误
状态所有者：模型调用账本和助手草稿
替换的当前路径：builder.py 与 server.py 中按 kind 分散的协议分支
```

## 五、当前结构的实际问题

### 1. `Provider kind` 实际是协议

`openai_compatible` 和 `anthropic` 描述的是调用接口，却被命名为 Provider kind。与此同时，`provider_id` 才是真正的用户连接实例。

### 2. 配置实体混合了太多职责

`LocalModelProvider` 同时包含：

- 服务身份；
- 协议；
- endpoint；
- 是否需要 API Key；
- 模型目录；
- 模型能力；
- 开关和版本。

而且“Local”既代表本机 Runtime 中保存的配置，又容易被理解为本地推理服务。

### 3. 创建模型和发现模型各自维护协议分支

- `agent/builder.py::_build_chat_model()` 决定如何创建模型 client；
- `server.py::discover_model_provider_models()` 另外决定 models URL 和 auth header；
- Run binding validation 再次枚举支持的 kind。

增加一个新协议会要求同步修改多个位置。

### 4. 凭证能力只有布尔值

`requires_api_key` 无法表达无凭证、OAuth token、不同 header、官方云身份等来源。

### 5. 未知模型默认能力过于乐观

当前模型发现默认 `tool_calling=True` 和 `streaming=True`。对 Agent 产品来说，未知应当是 `unknown`，而不是在没有证据时声称支持。

### 6. `/models` 被当作通用发现方式

很多兼容服务实现 `/chat/completions`，但不实现标准 `/models`；发现失败不等于调用协议不可用。Open WebUI 也为这种情况保留手动 model allowlist。[Open WebUI OpenAI-Compatible](https://docs.openwebui.com/getting-started/quick-start/connect-a-provider/starting-with-openai-compatible/)

## 六、推荐的最小边界

不要先设计一个完整插件 SDK。当前只需要一个 Runtime-owned Adapter Registry。

### Connection

持久化的用户配置：

```text
id
name
adapter_id
endpoint
credential_source
adapter_config
enabled
revision
```

`adapter_config` 必须由具体 Adapter 的 Pydantic schema 校验，不能成为任意透传字典。

### Model Profile

独立于 Connection：

```text
model_id
display_name
capability confidence
tool calling / image / streaming
context / output limits
structured output / reasoning / tool-choice constraints
source and revision
```

能力最好使用 `supported | unsupported | unknown`，避免未知被当作支持。

### Resolved Model Binding

Run admission 冻结：

```text
requested_model
connection_id + connection_revision
adapter_id
model_id
credential_ref or credential_source snapshot
resolved profile + profile revision
required capabilities
```

它不能冻结明文凭证，也不能保存一个可在执行中自动路由到其他 Provider 的别名。

### Adapter

首版只需要四个职责：

```text
validate_connection()
discover_models()
build_chat_model()
normalize_error()
```

`build_chat_model()` 返回 LangChain `BaseChatModel`；模型请求、stream 和 tool call 不再定义第二套类型。

适配器创建的 client 必须由 execution 的 `AsyncExitStack` 管理。SDK 内部重试应继续设为 0，避免越过 SheJane 模型账本重复请求。

## 七、统一什么，不统一什么

### 必须统一

- 文本、图片和工具消息进入 P8 的标准表示；
- 完整工具调用和稳定 tool call id；
- stream 生命周期、取消和资源关闭；
- finish reason；
- input/output/cache usage，缺失时明确为 unknown；
- Provider request id；
- 稳定错误分类和是否可重试；
- capability admission；
- 调用前预留、调用后结算和 outcome unknown。

### 不应强行统一

- Provider-specific reasoning、cache、service tier 等参数；
- `/models` 是否存在；
- OAuth、API Key、AWS、ADC 的取得流程；
- Provider 服务器端会话和 `previous_response_id`；
- 精确 token 计算；
- 模型价格和动态可用性；
- 网关内部路由；
- Provider 的原生托管工具。

Provider-specific 选项应采用命名空间配置，并在不支持时 fail-fast 或产生明确 warning，不能静默删除。OpenAI Agents SDK 的 strict feature validation 也体现了这个原则：不同模型 shape 支持的功能并不相同。[OpenAI Agents SDK Models](https://openai.github.io/openai-agents-python/models/)

## 八、推荐的演进顺序

### P0：收拢现有两个协议

1. 把 `ChatOpenAI`、`ChatAnthropic` 创建和 model discovery 移入两个 Adapter；
2. 用小型静态 registry 替换 builder/server/runs 中重复的 kind 判断；
3. 保持现有数据库、HTTP schema 和 model spec 暂时不变；
4. 建立一套 Adapter contract test，覆盖 stream、tool loop、usage、错误和资源关闭。

这一阶段不新增 Provider，也不改变用户界面。

### P1：修正配置模型

1. 将概念上的 Provider record 迁移为 Connection；
2. 将 `kind` 明确为 `adapter_id`；
3. 将 `requires_api_key` 演进为受控 credential source；
4. capability 从 bool 演进为带来源和 confidence 的 profile。

迁移必须保留已保存的 Provider、Keychain credential ref 和冻结 Run。

### P2：用一个真正不同的接口验证边界

不要通过再加一个 OpenAI-compatible 品牌证明扩展性。选择确有不同协议/认证的一个目标，例如：

- Google Gen AI / Vertex AI；或
- Bedrock Converse；或
- Open Responses。

只有真实需求出现时再选，不同时实现三种。

### 暂不做

- 不嵌入 LiteLLM SDK；
- 不建设 Provider 插件 Marketplace；
- 不允许 Runtime 自动 fallback；
- 不把 Adapter 与 OAuth UI 绑在一起；
- 不为每个 Provider 建一个类；
- 不重写 LangChain 的消息、工具和 stream 抽象；
- 不把所有 Provider-specific 功能压成一个最低公分母接口。

## 九、验收标准

- 增加新协议时，主要修改只发生在一个 Adapter 模块及其 contract test；
- OpenRouter、DeepSeek、LiteLLM 等兼容服务不需要品牌专用执行代码；
- OAuth、API Key 和无凭证连接能复用同一调用 Adapter；
- P8 仍只调用一个明确冻结的模型；
- 所有请求继续经过 `LedgerChatModel`；
- unsupported 与 unknown capability 不再被误判为 supported；
- 不支持的参数明确失败或告警，不静默丢弃；
- Provider SDK 的重试和路由不会绕过 Runtime 账本；
- Adapter 资源在 P11 由 execution stack 完整释放。
