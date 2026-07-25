# 中国大模型优先的 Provider 适配层调研

> 核验日期：2026-07-24
>
> 范围：DeepSeek、阿里云百炼/通义千问、Moonshot/Kimi、智谱 GLM、MiniMax、火山方舟/豆包、百度千帆、腾讯混元/TokenHub、硅基流动。
>
> 本文只引用厂商官方文档或官方源码。模型名称变化很快，因此架构和测试结论不依赖某个长期硬编码的模型名。

## 结论

加入“中国大模型优先”这个前提后，上一份调研的基本分层仍然成立，但 **Compatibility Profile 不能只是展示用的能力元数据**，而必须成为可执行的兼容策略。

中国主流平台目前几乎都提供 OpenAI-compatible Chat Completions。首批支持不需要九个原生 Adapter，也不需要引入 LiteLLM：

1. 继续使用现有 `ChatOpenAI -> BaseChatModel -> LedgerChatModel -> RuntimeModelProxy` 主链；
2. 第一阶段只保留 `openai_chat`、现有 `anthropic_messages` 和测试用 `fake` 三个 Adapter；
3. 为每个 Connection 绑定一个数据驱动的 `CompatibilityProfile`，处理 endpoint、模型发现、额外请求字段、推理内容续传、usage、安全字段和错误差异；
4. “支持某平台”必须表示通过 SheJane Agent 契约测试，而不只是 `/chat/completions` 能返回文本；
5. 模型能力默认为 `unknown`，不得因为接口长得像 OpenAI 就默认支持工具、图片或稳定的流式工具调用；
6. 厂商原生 Adapter 只在 OpenAI-compatible 接口无法承载 SheJane 所需能力时增加。就当前 Agent 核心能力而言，首批中国平台 **没有一家必须新增原生 Adapter**。

真正的兼容差异是推理可能位于 `reasoning_content`、`reasoning_details` 或 `<think>`；工具循环可能要求续传 assistant 推理状态；tool arguments 可能是字符串或对象；usage 可能只在 SSE 末帧出现；能力属于模型而非平台；国内/国际 Key 通常不可混用；缺少 `/models` 也不代表不能调用。

## 一、适合 SheJane 的四层结构

### Connection：一次真实连接

最小字段：`id`、`display_name`、`adapter_id`、`provider_profile_id`、`site`、`region`、`base_url`、`credential_ref`、`selected_model_id`、`revision`。

Connection 表达“用户连接到哪个站点、区域和账户”。国内站与国际站应是不同 Connection preset，不能只用一个可随意修改的 URL：

- 阿里云百炼的 Key 按 Region 隔离；
- 腾讯 TokenHub 广州与新加坡不支持跨区域调用；
- Moonshot、MiniMax、智谱、硅基流动分别提供中国站和国际站域名。

### Adapter：稳定协议

第一阶段只有 `openai_chat`、`anthropic_messages`、`fake`。Adapter 只负责 `validate_connection()`、`discover_models()`、`build_chat_model() -> BaseChatModel` 和 `normalize_transport_error()`。

它不按 DeepSeek、Kimi、豆包分别复制 `ChatOpenAI`。

### CompatibilityProfile：可执行兼容策略

Profile 包含 `discovery_mode`、白名单 `request_options`、`reasoning_input_policy`、`reasoning_output_format`、`tool_argument_format`、`usage_stream_policy`、`safety_policy` 和 `capability_source`。

Profile 是 Adapter 内部的窄策略，不是任意 Python 插件。配置内容必须由 Runtime 固定发布，不能让外部输入注册代码、header 或解析器。

### ModelProfile：模型级能力

`tool_calling`、`streaming_tools`、`image_inputs`、`reasoning` 和 `structured_output` 至少使用 `supported | unsupported | unknown` 三态。

`/models` 返回了模型 ID，不代表它支持 Agent。只有官方模型目录明确声明、SheJane 维护目录命中，或能力探测通过，才可以进入默认 Agent 模型选择器。

## 二、中国平台兼容矩阵

以下的“可复用”表示核心 Agent 对话走 `openai_chat`；厂商特殊字段由 Compatibility Profile 处理。

| 平台 | 中国 / 国际 endpoint | 鉴权与模型发现 | Agent 能力与主要差异 | 结论 |
| --- | --- | --- | --- | --- |
| [DeepSeek](https://api-docs.deepseek.com/) | `https://api.deepseek.com`；官方同时提供 `/anthropic` | Bearer API Key；官方 `GET /models` | OpenAI/Anthropic 兼容，支持流式、工具和 thinking；思考 + 工具时必须续传 `reasoning_content`，否则会 400；官方错误包含余额不足和过载 | `openai_chat` + `deepseek` profile |
| [阿里云百炼 / Qwen](https://help.aliyun.com/en/model-studio/base-url) | 北京 `dashscope.aliyuncs.com/compatible-mode/v1`；新加坡 `dashscope-intl.aliyuncs.com/compatible-mode/v1`；另有 workspace/美日德区域地址 | Bearer、Key 与 region 绑定；官方文档指向模型目录，未找到稳定的兼容 `/models` 契约 | Chat、Responses、Anthropic 和 DashScope；工具、视觉、推理均为模型级；`enable_thinking`、`thinking_budget`、`reasoning_effort` 为扩展字段；部分托管 GLM 工具调用要求 `tool_stream=true` | `openai_chat` + `dashscope` profile；发现采用维护目录/手填回退 |
| [Moonshot / Kimi](https://platform.kimi.com/docs/api/overview) | 中国 `https://api.moonshot.cn/v1`；国际 `https://api.moonshot.ai/v1` | Bearer；`GET /models` 还返回 context、图片、视频、推理能力 | OpenAI Chat 兼容；流式、并行工具、多模态和推理；`thinking` 走 `extra_body`；多步工具必须保留完整 assistant 和 `reasoning_content` | `openai_chat` + `kimi` profile |
| [智谱 GLM](https://docs.bigmodel.cn/cn/guide/develop/openai/introduction) | 中国 `https://open.bigmodel.cn/api/paas/v4`；国际 `https://api.z.ai/api/paas/v4` | Bearer API Key；也支持由 Key 生成 JWT；未找到稳定的公开兼容 `/models` 契约 | 流式、工具、多模态、thinking；`clear_thinking` 决定历史推理是否保留；官方响应 schema 将 function arguments 定义为对象，不能只接受 OpenAI 的 JSON 字符串 | `openai_chat` + `glm` profile；arguments 接受 string/object |
| [MiniMax](https://platform.minimaxi.com/docs/api-reference/text-openai-api) | 中国 `https://api.minimaxi.com/v1`；国际 `https://api.minimax.io/v1` | Bearer；官方 `GET /models` | OpenAI 兼容，也提供 Anthropic 兼容；工具为模型级；可通过 `reasoning_split=true` 把思考放到 `reasoning_details`，否则部分模型把 `<think>` 留在正文；多步工具要求保留完整 assistant | `openai_chat` + `minimax` profile |
| [火山方舟 / 豆包](https://www.volcengine.com/docs/82379/1795150) | 中国 `https://ark.cn-beijing.volces.com/api/v3`；国际 BytePlus `https://ark.ap-southeast.bytepluses.com/api/v3` | Bearer；模型/推理服务在控制台开通，未找到稳定的兼容 `/models` 契约 | Chat 和 Responses；支持工具、多模态、thinking；平台还存在不同计费的 Coding Plan endpoint，不能与按量 endpoint 混用 | `openai_chat` + `ark` profile；Responses 暂不另建 Adapter |
| [百度千帆](https://cloud.baidu.com/doc/qianfan/s/qmh4sv5vi) | `https://qianfan.baidubce.com/v2` | Bearer `bce-v3/...`；官方 `GET /models` 返回类型、上下文、模态和价格 | V2 声明完全兼容 OpenAI，支持流式、工具、视觉、`reasoning_content`；额外返回安全 `flag` / `ban_round`，必须在上屏前处理 | `openai_chat` + `qianfan` profile |
| [腾讯混元 / TokenHub](https://cloud.tencent.com/document/product/1823/130078) | 广州 `https://tokenhub.tencentmaas.com/v1`；新加坡 `https://tokenhub-intl.tencentmaas.com/v1` | Bearer；官方 `GET /models` 返回上线/即将下线状态 | OpenAI Chat、Responses、Anthropic；流式、工具、图像、推理；交错式思考 + 工具需要回填 `reasoning_content`；原混元平台正在下线迁移 | 新连接只支持 TokenHub：`openai_chat` + `tokenhub` profile |
| [硅基流动](https://docs.siliconflow.com/en/api-reference/chat-completions/chat-completions) | 中国 `https://api.siliconflow.cn/v1`；国际 `https://api.siliconflow.com/v1`（另有亚太域名） | Bearer；官方 `GET /models` 可按 chat/vision 等过滤 | OpenAI 兼容；流式、工具、视觉、`reasoning_content`；`enable_thinking` / `thinking_budget` 为扩展；某些模型只有关闭 thinking 才能工具调用 | `openai_chat` + `siliconflow` profile |

## 三、哪些差异只需要数据，哪些需要代码

### 纯 Connection preset

只需要声明，不进入请求解析逻辑：

- 厂商名称与帮助链接；
- 国内/国际站；
- region 与固定 base URL；
- Bearer Key 是否必填；
- 模型发现模式；
- Coding Plan 与按量接口是否是独立连接。

不要让用户在普通路径手填这些厂商的 base URL。自定义兼容服务仍保留高级入口。

### 数据驱动 Compatibility Profile

以下差异都不值得产生一个新 Adapter：

- `enable_thinking`、`thinking`、`thinking_budget`、`reasoning_effort`；
- `tool_stream`；
- 是否在 SSE 最后一帧请求 usage；
- 是否保留同一轮或跨轮 `reasoning_content`；
- `reasoning_details` / `<think>` / `reasoning_content` 的读取策略；
- tool arguments 接受 JSON 字符串还是对象；
- 模型发现使用 `/models`、维护目录或手填；
- Provider/model family 的工具、视觉、推理能力三态；
- 安全字段如千帆 `flag`、智谱 `sensitive` finish reason；
- 可重试错误、余额不足、限流、过载的归类。

### 需要窄代码 hook，但仍共用 `openai_chat`

三个 hook 足够：

```text
prepare_messages(messages, profile)
prepare_request_options(options, profile)
normalize_generation(generation, profile)
```

用途：

- 将上轮推理载荷放回 assistant message；
- 把 object 形式的 tool arguments 规范为 JSON 字符串；
- 从 `reasoning_details` 或 tagged content 分离推理；
- 在进入 `LedgerChatModel` 结算前归一 usage、finish reason 和安全拒绝。

不要建立 Provider 子类树。Profile ID + 三个有限 hook 比九个类更容易测试。

### 真正需要原生 Adapter 的条件

只有出现下面需求才新增：

- 必须使用 DashScope 原生接口的 Qwen Audio 或特殊精调模型；
- 必须使用火山方舟 Responses 的平台内置工具、服务端状态或文件对象；
- 必须使用腾讯云旧 TC3-HMAC 接口；

这些都不是当前 Agent 文本/视觉/工具调用的必要条件，因此第一阶段不做。

## 四、中国优先的 Tier A 认证矩阵

“Tier A”不是厂商 logo 出现在设置页，而是至少一个当前可用模型通过以下契约。模型应在测试时从官方 `/models` 或 Runtime 维护目录中按能力选择，不在测试代码里永久固定营销名称。

| 契约 | Tier A 必须通过 |
| --- | --- |
| 连接 | 正确站点/region；Key 不出 Runtime；401、余额不足、限流、过载可区分 |
| 基础调用 | 非流式文本、SSE 文本、取消、超时、连接关闭 |
| 工具循环 | 单工具、连续两轮工具、并行工具（声明支持时）、UTF-8/中文参数、无效 JSON 参数拒绝 |
| 思考 + 工具 | 能提取推理；工具后续请求正确续传厂商要求的推理载荷；推理不混入最终正文 |
| Usage | 非流式和流式均能结算 prompt/completion/reasoning；缺失 usage 时保持 outcome unknown，不能估算后假装精确 |
| 多模态 | 仅当 Profile 声明支持时，测试 URL 与 base64 图片；不支持时在 admission 阶段拒绝 |
| 安全 | 内容过滤、敏感 finish reason、千帆 flag 等不绕过终端状态和 UI 上屏规则 |
| 能力变化 | `/models` 下线、模型变更、Connection revision 变化后重新 admission |
| 资源 | SDK retry 关闭；HTTP client 由 execution `AsyncExitStack` 关闭；取消后不遗留流或重复结算 |

### 首批认证范围

#### Tier A1：直接厂商

- DeepSeek
- 阿里云百炼 / Qwen
- Moonshot / Kimi
- 智谱 GLM
- MiniMax
- 火山方舟 / 豆包

#### Tier A2：国内聚合与云平台

- 百度千帆
- 腾讯 TokenHub
- 硅基流动

A1/A2 都使用相同 Adapter contract；分组只决定实施顺序，不表示能力等级。A1 先覆盖中国用户最常见的原厂账户，A2 随后覆盖一把 Key 调多模型、企业 region 和更复杂的安全字段。

## 五、落地顺序

### 第一步：修正抽象，不改执行框架

将当前 `kind=openai_compatible|anthropic` 明确迁移为 `adapter_id`，增加固定的 `provider_profile_id`。保留 `BaseChatModel`、`RuntimeModelProxy`、`LedgerChatModel` 和当前 Run 冻结规则。

### 第二步：先完成一个 OpenAI Chat 中国兼容测试夹具

建立录制响应/fake server 契约，覆盖：

- `reasoning_content`；
- `reasoning_details`；
- tagged thinking；
- string/object tool arguments；
- final-chunk usage；
- 安全 flag；
- 400/401/402/429/5xx；
- 断流和取消。

这一个夹具比先写九个 Provider 类更有价值。

### 第三步：接入 Tier A1 preset 与 profile

Connection preset 固定官方 endpoint，模型发现按三种模式运行：

```text
openai_models -> 调用官方 /models
catalog       -> Runtime 维护的可更新目录
manual        -> 高级用户手填，能力默认为 unknown
```

只有通过 Agent capability admission 的模型出现在默认列表。

### 第四步：接入 Tier A2 和安全差异

重点补齐千帆安全字段、TokenHub region/下线状态、硅基流动的模型级 thinking/tool 组合约束。

## 六、明确不做

- 不按厂商创建九个 `BaseChatModel` 实现；
- 不嵌入 LiteLLM；
- 不把平台支持误认为每个模型都支持工具；
- 不以硬编码“最新模型名”作为长期能力判断；
- 不默认打开厂商 thinking 参数；
- 不把推理内容直接拼进用户可见正文；
- 不把国内 Key 自动发送到国际 endpoint，或反向发送；
- 不支持即将下线的腾讯旧混元 endpoint 作为新 preset；
- 不因 `/models` 缺失而判定一个兼容服务不可用；
- 不绕开 `LedgerChatModel` 做 Provider 自己的重试、fallback 或 usage 结算。

最终建议是：**一个 `openai_chat` Adapter，九组受 Runtime 控制的 Compatibility Profile，一套以真实 Agent 工具循环为核心的 Tier A 认证测试。** 这是覆盖中国大模型的最小可靠边界。
