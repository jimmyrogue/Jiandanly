# Provider、AI Gateway 与一键连接调研

> 核验日期：2026-07-24
>
> 范围：模型官方 API、聚合服务、自建 AI Gateway，以及 SheJane 如何在不暴露协议细节的情况下完成连接。
>
> 资料只使用官方文档、官方仓库和项目维护者自己的 issue。

## 结论

SheJane 不是在建设另一个 AI Gateway，也不是逐个实现所有 Provider。我们要建设的是：

1. 一个面向用户的 **连接系统**；
2. 两到三个稳定的 **协议 Adapter**；
3. 一个针对实际 endpoint 行为生成的 **Effective Compatibility Profile**；
4. 一套判断该连接能否稳定运行 Agent 的契约测试。

Provider 数量可以很多，Gateway 数量也会继续增加，但大多数最终暴露的是 OpenAI Chat、OpenAI Responses 或 Anthropic Messages。SheJane 不应为每个品牌新增执行代码。

Compatibility Profile 确实比 Provider 枚举更重要。不过它不能只绑定 Provider，因为 Gateway 可能改写请求、重命名模型、路由到不同上游，甚至隐藏实际模型。

Profile 最终应绑定：

```text
connection revision + endpoint fingerprint + exposed model id
```

运行时只使用一个已经解析完成的 Effective Profile，不在每次调用时动态猜测。

## 一、五个概念

| 概念 | 回答的问题 | 用户是否需要看到 |
| --- | --- | --- |
| Provider | 模型或服务由谁提供，例如 DeepSeek、OpenAI | 只显示友好名称 |
| Gateway | 请求是否经过 Cloudflare、OpenRouter、New API、Sub2API 等中转 | 必要时显示 |
| Connection | 用户实际连接的 endpoint、账户和凭证 | 显示连接名称与状态 |
| Adapter | endpoint 使用 OpenAI Chat、Responses 还是 Anthropic Messages | 不显示 |
| Compatibility Profile | 这个 endpoint/model 的工具、推理、流、usage 和错误到底怎么工作 | 不显示，只显示认证结果 |

一次连接可能是：

```text
Kimi 模型
  → New API 自建网关
  → 对外暴露 OpenAI Chat
  → SheJane Connection
```

SheJane 不能只看“Kimi”决定请求格式，也不能因为 URL 看起来像 New API 就假设所有模型都支持工具调用。

## 二、Effective Compatibility Profile

### 解析来源

连接成功时，Runtime 将以下证据保守合并一次：

```text
协议默认规则
  + 已知 Gateway recipe
  + 已知上游 Provider/model 规则
  + 当前 endpoint 的模型目录
  + 实际 capability probe 结果
  = Effective Compatibility Profile
```

保存键：

```text
connection_id
connection_revision
endpoint_fingerprint
exposed_model_id
profile_revision
```

Connection、endpoint、Gateway 版本或模型路由变化后，旧认证失效。

### 合并规则

- `unsupported` 优先于 `supported`；
- 没有证据时保持 `unknown`；
- 探测可以把 `unknown` 提升为 `supported`，不能覆盖明确的安全限制；
- Gateway 动态路由必须使用所有候选目标的能力交集；
- 无法知道实际上游时，不宣称精确 Provider、价格或上下文长度；
- Effective Profile 随 Run 冻结，执行期间不重新猜测。

### 最小字段

```text
protocol:
  adapter_id
  endpoint_paths
  request_headers

catalog:
  discovery_mode
  model_id_namespace

messages:
  supported_roles
  reasoning_input_policy
  reasoning_output_format

tools:
  capability
  parallel_capability
  argument_format
  tool_choice_policy

stream:
  completion_sentinel
  usage_policy
  tool_delta_policy

errors:
  auth
  insufficient_credit
  rate_limit
  safety
  retryable

identity:
  upstream_visibility
  dynamic_routing
  request_id_headers
```

这不是开放给第三方执行代码的插件。Profile 只能从 Runtime 固定 schema 和白名单策略生成。

## 三、不同 Gateway 对 SheJane 的意义

### Cloudflare AI Gateway

Cloudflare 当前提供 OpenAI Chat、OpenAI Responses 和 Anthropic Messages 兼容 endpoint，也提供统一 `/ai/run`。第三方模型可以使用 Cloudflare Unified Billing，无需用户再向每个上游提供 API Key；也可以通过 BYOK 保存上游 Key。[REST API](https://developers.cloudflare.com/ai-gateway/usage/rest-api/) [BYOK](https://developers.cloudflare.com/ai-gateway/configuration/bring-your-own-keys/)

它还支持缓存、限流、日志、预算和动态路由。动态路由可以按条件选择模型并 fallback，因此 SheJane 不能把一个 route alias 假装成固定模型；必须标记 `dynamic_routing=true`，使用能力交集，并说明上游可能变化。[Dynamic Routing](https://developers.cloudflare.com/ai-gateway/features/dynamic-routing/)

Cloudflare 已支持第三方应用 OAuth。用户授权时可以选择账户并查看、撤销权限，因此值得验证“使用 Cloudflare 登录”的一键连接 POC。[Cloudflare OAuth](https://developers.cloudflare.com/fundamentals/oauth/) [授权应用](https://developers.cloudflare.com/fundamentals/oauth/authorizing-an-application/)

### OpenRouter

OpenRouter 官方提供 OAuth PKCE，支持 localhost 任意端口回调，并把授权码换成用户控制的 API Key。这是当前最明确的桌面端一键连接路径。[OpenRouter OAuth PKCE](https://openrouter.ai/docs/guides/overview/auth/oauth)

它仍然是 Gateway：同一个模型 ID 可能有路由和上游差异。SheJane 应认证 OpenRouter endpoint 暴露的行为，而不是套用官方 Provider Profile。

### LiteLLM Proxy

LiteLLM Proxy 将大量 Provider 统一成 OpenAI Chat，并提供 `/models`、虚拟 Key、预算、模型别名、负载均衡和 fallback。[LiteLLM Proxy](https://docs.litellm.ai/docs/proxy/quick_start) [Virtual Keys](https://docs.litellm.ai/docs/proxy/virtual_keys)

对 SheJane 来说它只是一个自定义 Gateway Connection，不应把 LiteLLM SDK嵌入 Runtime。

### One API / New API

One API 的目标是将多个上游统一为 OpenAI API，并管理 Key、额度和渠道；New API 在其基础上继续扩展模型管理与资产管理。[One API](https://github.com/songquanpeng/one-api) [New API](https://github.com/QuantumNous/new-api)

自建实例域名、模型别名和渠道配置都不固定。SheJane 可以提供已知 recipe，但仍需要用户输入实例 URL 与 Key，并对每个暴露模型生成 Effective Profile。

### Sub2API

Sub2API 不只是普通 BYOK 网关，它会把 Claude、OpenAI、Gemini 等产品订阅或 OAuth 账户转换成平台 API Key，并负责计费、粘性会话、并发和调度。[Sub2API](https://github.com/Wei-Shaw/sub2api)

这证明“响应长得像 OpenAI”并不足够。它的官方仓库明确提示部分 Claude 来源不能混用同一会话，项目 issue 也记录了 Responses 多轮 reasoning replay 等兼容问题。SheJane 应将 Sub2API 视为独立 Gateway recipe，能力默认 `unknown`，通过实际 Agent probe 后再启用。

## 四、一键连接应该是什么

真正的一键连接不是把 Base URL 和 API Key 预填到表单，而是用户完成一次授权后，SheJane 自动完成：

```text
获得可撤销凭证
→ 创建 Connection
→ 识别 Gateway/协议
→ 获取模型
→ 生成 Effective Profile
→ 运行轻量连接测试
→ 选择推荐模型
→ 完成
```

### A. 真正的一键连接

适用于提供 OAuth 或 delegated authorization 的服务：

- OpenRouter OAuth PKCE；
- Cloudflare OAuth POC；
- 未来明确开放第三方 OAuth 的 Provider/Gateway。

用户只看到“连接 OpenRouter”或“使用 Cloudflare 登录”。

### B. 辅助连接

多数中国官方 Provider 暂时仍是 API Key：

1. 点击“连接 DeepSeek / 千问 / Kimi”；
2. SheJane 打开官方创建 Key 页面；
3. 用户只粘贴一次 Key；
4. endpoint、region、协议、模型发现和 Profile 自动完成。

普通用户不填写 Base URL、协议或模型 ID。

### C. 自建/中转站快速连接

入口名称应是“连接已有服务”，不是“添加 Provider”：

1. 选择 Cloudflare、LiteLLM、New API、One API、Sub2API，或“其他兼容服务”；
2. 只输入实例地址和 Key；
3. Runtime 规范化 URL、探测协议、读取 `/models`；
4. 能力未知时执行一次用户可见的 Agent 兼容测试；
5. 成功后自动推荐可用模型。

不能把 secret 放进 `shejane://` URL、二维码或普通剪贴板日志。未来如支持深链接，只允许携带无密钥的 endpoint 与 recipe id。

## 五、对当前 SheJane 的直接调整

当前设置页把 OpenAI、OpenRouter、DeepSeek、Anthropic 和两个自定义协议放在同一个下拉框，并要求用户理解 Base URL、API Key、获取模型和手工模型 ID。

建议改为两个入口：

```text
连接模型
├── 推荐连接
│   ├── 使用 OpenRouter 登录
│   ├── 使用 Cloudflare 登录（POC 验证后）
│   ├── DeepSeek
│   ├── 千问
│   ├── Kimi
│   ├── GLM
│   ├── MiniMax
│   └── 豆包
└── 连接已有服务
    ├── Cloudflare AI Gateway
    ├── LiteLLM
    ├── New API / One API
    ├── Sub2API
    └── 其他兼容服务
```

完成后只显示：

- 连接名称；
- 已连接/需要处理；
- 推荐模型；
- “经过中转”或“动态路由”提示；
- 重新连接与移除。

Base URL、Adapter、Profile 和 capability probe 只进入高级详情。

## 六、最小落地顺序

1. 把现有 `kind` 明确为 `adapter_id`；
2. 增加 Runtime-owned Connection Recipe；
3. 增加 Effective Profile 解析与冻结；
4. 先实现 OpenRouter OAuth PKCE；
5. 为中国官方 Provider 实现辅助连接；
6. 为自建 Gateway 实现 URL + Key 自动探测；
7. 单独验证 Cloudflare OAuth 和 Unified Billing POC。

暂不做：

- 不建设 SheJane 自有模型网关和计费系统；
- 不嵌入 LiteLLM；
- 不为每个 Gateway 建一套执行 Adapter；
- 不定义未经生态采用的通用连接 manifest；
- 不允许 Runtime 自己静默 fallback；
- 不把 Gateway alias 宣称为固定上游模型。

最终边界是：

> 用户连接的是服务；Runtime 调用的是协议；Compatibility Profile 保证实际 endpoint 的行为能满足 SheJane Agent 契约。
