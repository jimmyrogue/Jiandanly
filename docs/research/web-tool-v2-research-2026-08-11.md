# Runtime Web 工具 v2 调研（2026-08-11）

> 范围：只比较 OpenAI Responses Web Search、Anthropic Web Search / Web Fetch、Google Gemini Google Search / URL Context，以及 HTTP、SSRF、robots 相关一手规范。v2 全量迁移仍是建议；下方“仓库当前行为”记录 2026-08-11 已实现部分。

## 结论先行

SheJane 不应该用一个更大的 HTTP 客户端替换旧 `web.fetch`，也不应该把所有联网能力都交给某一家模型供应商。成熟方案的共同形态已经很清楚：

1. **搜索（discovery）与定向读取（explicit URL retrieval）是两种工具。** 搜索负责发现、排序和引用来源；读取只处理用户明确给出的 URL，或搜索/前一次读取已经返回的 URL。
2. **优先使用供应商托管搜索，但保留本地 `web.fetch`。** OpenAI、Anthropic、Google 的托管搜索都能把搜索、页面读取与引用放在同一次模型回合内；本地工具仍是自定义中转、本地模型、MCP 搜索结果和用户指定 URL 的必要后备。
3. **第一期不改名，只收紧现有 `web.fetch`。** 把它变为 GET-only 的“网页读取器”：HTTP 非 2xx 是结构化失败；抽取正文而不是返回原始 HTML；明确 `retryable`、最终 URL、重定向链、来源、截断原因和抓取时间。工具名改动会波及提示词、限额、回执、历史 checkpoint 和诊断，当前收益不抵迁移成本。
4. **删除 POST 能力。** RFC 9110 只把语义上只读的方法定义为 safe；POST 不能继续挂在始终标为 `read_only` 的 `web.fetch` 下。若将来出现真实的 HTTP API 调用需求，应通过 MCP/连接器，或另设需审批的 `web.request`，而不是为假设需求保留 POST。
5. **动态过滤是优化，不是真相层。** 先做 HTML 主体抽取和内容预算；供应商支持时使用托管动态过滤。不要为了模仿 Anthropic 在本地再造一套“生成代码过滤网页”的执行层。无论正文是否被过滤，都必须保留完整、可审计的来源元数据。

这是一项 **P10 工具执行** 改造：

```text
主要阶段：P10
上游输入：P9 已校验的 web.fetch 调用；P8 供应商托管搜索动作
下游输出：P11 可结算的结构化 Tool Receipt 与来源记录
状态所有者：Runtime Tool Receipt；供应商托管搜索的原始动作由 P8 模型调用账本保存
替换的当前路径：tools/web.py 返回 raw HTML + 通用 tool_result_codec 二次截断
```

直接相邻阶段是 P9（工具参数/风险判断）和 P11（回执结算与资源释放）。供应商托管搜索仍在 P8 内完成，不应伪装成本地 P10 ToolNode 调用。

## 当前实现的问题

当前 [`runtime/src/shejane_runtime/tools/web.py`](../../runtime/src/shejane_runtime/tools/web.py) 已有值得保留的安全基础：只允许 HTTP(S)，拒绝 URL 凭据，解析并固定公网 IP，TLS/SNI 仍使用原主机名，每次重定向重新解析目标，限制 5 次重定向、15 秒和约 2 MB。现有问题位于契约层：

| 当前行为 | 后果 |
|---|---|
| GET/POST 共用 `web.fetch`，风险固定为 `read_only` | POST 可能产生远端副作用，却绕过参数相关的风险判断 |
| 任意 HTTP 响应都返回 `ok: "true"` | 404/403/500 被模型当成成功证据，`tool.completed` 与真实结果矛盾 |
| 网络异常只有 `error` 字符串，没有 `retryable` | 已存在的 ToolResultRetryMiddleware 无法重试结构化超时/连接失败 |
| 返回最多 2 MB 原始 HTML | 导航、脚本、样式和 cookie banner 浪费上下文；正文可能反而被通用截断丢掉 |
| 通用 codec 超限后给模型约 32 KiB 的 head/tail preview | 对 HTML 的截头截尾不是相关性过滤，且可能把结束脚本/页脚当作主要内容 |
| 没有标题、最终 URL、抓取时间、正文 hash、截断原因 | 无法稳定引用、去重、缓存或判断来源是否改变 |
| URL 可由模型任意构造 | SSRF 已防住私网访问，但仍有通过 URL 查询参数外泄上下文的风险 |
| HTTPS 重定向到 HTTP 时若改写为 HTTPS | 客户端请求了服务端没有返回的另一个 URL，来源链不再忠实；应明确拒绝降级或原样、安全地处理 |

RFC 9110 明确说明 404 表示目标资源没有当前表示或服务端不愿披露其存在，因此“HTTP 请求拿到响应”不能等同于“页面读取成功”。同一规范还区分 safe/idempotent 方法，并要求客户端不要自动重试非幂等请求。[RFC 9110 §15.5.5](https://www.rfc-editor.org/rfc/rfc9110.html#name-404-not-found)、[§9.2.1](https://www.rfc-editor.org/rfc/rfc9110.html#name-safe-methods)、[§9.2.2](https://www.rfc-editor.org/rfc/rfc9110.html#name-idempotent-methods)

## 成熟方案比较

### 1. OpenAI Responses Web Search

OpenAI 把 `web_search` 定义为 Responses 内的托管工具。模型可以选择 `search`、推理模型可继续 `open_page` 和 `find_in_page`；输出同时包含 `web_search_call` 动作与带 `url_citation` 的回答。应用还可请求完整 `sources`，它与“正文实际引用的少数 URL”是两层不同数据。[Web Search：动作与引用](https://developers.openai.com/api/docs/guides/tools-web-search#output-and-citations)、[Sources](https://developers.openai.com/api/docs/guides/tools-web-search#sources)

可用控制包括：

- `allowed_domains` / `blocked_domains`，各最多 100 个，包含子域名；当前 Responses 示例可同时设置两者；
- `user_location` 近似位置；
- `search_context_size=low|medium|high`；
- `external_web_access=false` 使用缓存/索引，不访问实时网页；
- GPT-5+ 推理搜索可选默认或 `unlimited` 的 `return_token_budget`。

来源：[domain filtering](https://developers.openai.com/api/docs/guides/tools-web-search#domain-filtering)、[search context size](https://developers.openai.com/api/docs/guides/tools-web-search#search-context-size)、[live internet access](https://developers.openai.com/api/docs/guides/tools-web-search#live-internet-access)、[longer web research](https://developers.openai.com/api/docs/guides/tools-web-search#run-longer-web-research)。

对 SheJane 的含义：OpenAI 路径适合“查什么并回答”，但它不是一个由应用传入 URL、确定性返回页面正文的独立 Fetch API。`open_page` 是模型在托管搜索内部选择的动作。现有 `include: ["web_search_call.action.sources"]` 应保留；还要明确区分“consulted sources”与“inline citations”。OpenAI 文档未为单次搜索动作给出像 Anthropic 那样稳定的细粒度错误码，因此 Runtime 只能保存 call status、Responses 顶层错误和 incomplete 原因，不能伪造跨供应商统一的错误细节。

### 2. Anthropic Web Search 与 Web Fetch

Anthropic 的边界最接近 SheJane 需要的目标：

- `web_search` 负责实时搜索并始终产生引用；成功但无结果是空列表，不是错误。
- `web_fetch` 负责读取 URL；引用默认关闭但可启用，返回最终 URL、document 内容与 `retrieved_at`。
- 两者都支持 `max_uses`、域名 allow/block；失败调用也计入 Fetch 限额。
- `web_fetch` 有近似的 `max_content_tokens`；新版本还有缓存绕过和 response inclusion 控制。
- 2026-02 及以后版本可以在结果进入模型上下文前通过代码执行做动态过滤；2026-03 版本还能不把已经消费的嵌套原始结果重复放进 API 响应。

来源：[Anthropic Web Search](https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-search-tool)、[Anthropic Web Fetch](https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-fetch-tool)、[Server tools / domain filtering](https://platform.claude.com/docs/en/agents-and-tools/tool-use/server-tools)。

Anthropic 还给出了最值得直接借鉴的安全和错误契约：

- Fetch 只能读取用户消息、客户端工具结果、前序搜索或 Fetch 结果中已经出现过的 URL；模型不能凭空拼接 URL。
- `url_not_allowed` 同时覆盖域名策略、私网和 `robots.txt` 拒绝；`url_not_in_prior_context` 单独表示来源链不合法。
- `url_not_accessible` 表示 HTTP 获取失败，`unsupported_content_type` 单独分类。
- 工具失败仍可能位于 API 的 HTTP 200 响应内，但内容块明确为 error；模型看到错误后可继续当前回合。
- Search 的 rate limit、参数错误、限额、查询过长、请求过大和 unavailable 都有稳定错误码；成功但无结果为空结果。

来源：[Web Fetch URL validation 与错误码](https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-fetch-tool#url-validation)、[Web Search errors](https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-search-tool#errors)。

这说明 SheJane 的“Runtime HTTP 200 / 工具函数正常 return / `ok` 成功”也必须分层：工具调用可以顺利结算，但业务结果仍是明确失败。

### 3. Google Gemini Google Search 与 URL Context

Google 同样把发现和定向读取拆开：`google_search` 由模型生成一个或多个查询，处理结果并输出带区间的 `url_citation`；响应还保留 `google_search_call` 的查询和 `google_search_result` 的搜索建议。[Grounding with Google Search](https://ai.google.dev/gemini-api/docs/google-search)

`url_context` 接收明确 URL，先查内部索引缓存，未命中再实时抓取；可与 Google Search 一起使用，让模型先广搜再深入读取选中的页面。它为每个 URL 返回 `url_context_result`，状态至少区分 `success`、`error`、`paywall` 和 `unsafe`，并在回答中提供 `url_citation`。限制为每个请求最多 20 个 URL、单 URL 最多 34 MB，仅允许公开互联网地址；支持常见文本、图片和 PDF，不支持付费墙、Google Workspace 文档、视频和音频。[URL Context](https://ai.google.dev/gemini-api/docs/url-context)、[Interactions API `UrlContextResult`](https://ai.google.dev/api/interactions-api-v1)

Gemini Developer API / Interactions 的当前公开工具 schema 没有 Anthropic/OpenAI 那样的请求级域名 allow/block，也没有 `max_uses`；Google Search 对 Gemini 3 按模型实际生成的每个查询计费。Vertex AI / Gemini Enterprise 是另一套产品契约，不能把那里的控制项当成 Developer API 已支持。因而 Google 的托管工具适合供应商内建 grounding，但不能单独承担 SheJane 的统一域名策略和硬调用预算。这个判断来自当前公开 schema，后续实现前需再次核对最新文档。

### 横向对照

| 能力 | OpenAI Responses | Anthropic | Gemini | SheJane 应取的交集 |
|---|---|---|---|---|
| 搜索/指定 URL 分离 | 搜索内部含 open/find，无独立 URL 工具 | 明确分离 Search/Fetch | 明确分离 Search/URL Context | 保留 `web.search` 与本地 `web.fetch` 两类意图 |
| 引用 | 行内 URL 注解 + 完整 sources | Search 必有；Fetch 可开；含 cited text | 行内 URL 区间；保留 search/url steps | durable source 元数据 + 回答行内可点击引用 |
| 失败表达 | call status + Responses 错误/incomplete | HTTP 200 内结构化 tool error；无结果非错误 | URL status: success/error/paywall/unsafe | 工具传输成功与业务读取成功分开 |
| 内容预算 | context size；default/unlimited 返回预算 | 数值 `max_content_tokens` | 20 URL、34 MB；计入 input/tool tokens | 下载字节上限 + 抽取后上下文上限分开 |
| 调用预算 | 无通用 `max_uses` | `max_uses`，失败也计数 | 搜索按实际 query 计费，无 `max_uses` | Runtime 统一计数；标明 hard/best-effort |
| 域名控制 | allow/block 各 100 | allow 或 block；组织策略叠加 | 当前公开工具 schema 无 | Runtime policy 为底线，供应商能力可下推 |
| 动态过滤 | 模型托管搜索内部完成 | 明确支持代码过滤、可省略原始块 | 未公开等价开关 | 能用托管能力就用；本地先做正文抽取 |
| URL 来源约束 | 未公开等价契约 | 只准先前上下文出现的 URL | Prompt 给定 URL；公共地址限制 | 采用 Anthropic 的 prior-context 规则 |

## 增量：GPT、Luna/中转与 DeepSeek 的 hosted `web_search` 边界

结论不能简化成“所有 GPT 都直接使用原生搜索”：

1. **直连 OpenAI 时，也只能给官方明确声明支持的模型启用。** Responses endpoint 可用不等于 Responses 的 hosted `web_search` 可用。当前模型页明确写明 GPT-5.6 Luna、Terra、Sol 在 Responses API 中支持 Web search；但 GPT-5 mini、GPT-4o mini 的当前模型页只证明 Responses endpoint 可用，没有给出同样的 hosted tool 能力声明。OpenAI 的 Web Search 指南另明确列出 GPT-4.1、GPT-4.1 mini、o4-mini 等支持或限制示例。因此不能用 `model.startswith("gpt-")` 代表能力，应该维护来自官方模型目录/指南的逐模型 capability。[GPT-5.6 Luna](https://developers.openai.com/api/docs/models/gpt-5.6-luna)、[GPT-5.6 Terra](https://developers.openai.com/api/docs/models/gpt-5.6-terra)、[GPT-5.6 Sol](https://developers.openai.com/api/docs/models/gpt-5.6-sol)、[GPT-5 mini](https://developers.openai.com/api/docs/models/gpt-5-mini)、[GPT-4o mini](https://developers.openai.com/api/docs/models/gpt-4o-mini)、[Web Search guide](https://developers.openai.com/api/docs/guides/tools-web-search)
2. **SheJane 的 Luna 或任意第三方 OpenAI-compatible relay 不能因为模型名相同就视为可透传。** OpenAI 官方资料只描述 OpenAI 自己的 endpoint，不承诺第三方中转会接受 hosted tool、在供应商侧真正执行搜索，或完整保留 `web_search_call`、URL citation、sources 和错误状态。中转声明支持 `/v1/responses` 也只证明协议入口，不证明 hosted tool 语义。必须由中转的可信模型目录显式声明，并用真实调用探测；否则维持禁用。
3. **DeepSeek 只为 V4 Flash 启用。** DeepSeek 当前 Responses 指南只声明 `deepseek-v4-flash` 支持 Responses；`tools` 部分支持 `web_search` / `web_search_2025_08_26`，并明确由服务端执行。SheJane 因此只把 Flash 冻结为 `openai_responses` 并声明托管搜索；Pro 继续使用 Chat Completions。`search_context_size`、`user_location`、`max_tool_calls` 会被忽略，`include` 不支持，所以 Flash 也不冒充 OpenAI hosted search 的完整等价实现。[DeepSeek Responses API](https://api-docs.deepseek.com/guides/responses_api/)

### 仓库当前行为

[`model_runtime.py`](../../runtime/src/shejane_runtime/agent/model_runtime.py) 当前只在 `protocol == "openai_responses"` 时注入 hosted tool；[`model_services/__init__.py`](../../runtime/src/shejane_runtime/model_services/__init__.py) 则决定正常目录冻结的协议。两者合起来得到：

- `api.openai.com` 官方连接：只对 Runtime 精确核准表中的 Responses 模型声明能力；
- `api.deepseek.com` 官方连接：只对 `deepseek-v4-flash` 使用 Responses 并声明能力；
- SheJane Cloud：只对 Cloud 模型目录明确发布为已验证的 Responses 模型启用；
- 自定义中转：即使名称相同也不继承能力。

Runtime 不再从模型名前缀、品牌或 Responses endpoint 推断能力；Run 接纳时冻结 Cloud/官方目录给出的独立能力。Cloud 的 Responses relay 原样保留 `web_search`、引用和 sources；DeepSeek adaptor 使用原生 `/responses`，并移除不受支持的 OpenAI 扩展语义。第三方中转继续 fail-closed。

### 已实现的 capability gate

冻结到 Run 的模型绑定至少增加一个独立语义能力 `hosted_web_search`；不能复用 `supported_endpoint_types`，也不能从模型名推导：

```text
enable_hosted_web_search =
  protocol == "openai_responses"
  and frozen_model.capabilities.hosted_web_search == "verified"
```

能力来源分两类：

- OpenAI/DeepSeek 官方直连由 Runtime 的逐模型表根据官方文档声明；DeepSeek Flash 同时冻结为 `openai_responses`；
- SheJane Cloud/Luna 只接受受信 Cloud 目录的显式声明；第三方 relay 的同名字段会被清除。

发布验证应查询一项可验证的近期事实，并同时检查终态成功、至少一个 `web_search_call`，以及 URL citation 或等价来源记录。若 relay 静默忽略工具、只回普通文本、丢失来源或把工具转成本地未声明实现，发布验证不通过；正常启动和目录刷新不主动产生搜索费用。

第一期实现一个是否可用的 gate 和 `full_sources` 差异；`domain_filters`、`user_location`、`search_context_size` 仍不虚报。发布验证用真实产品调用检查终态、`web_search_call` 与引用；它是 release gate，不在每次启动或目录刷新时产生搜索费用。

## 建议的 v2 契约

### 搜索

`web.search` 继续表达“发现来源”，但执行位置按冻结的模型能力决定：

- OpenAI / Anthropic / Gemini 原生能力存在时，在 P8 使用供应商托管工具；
- SheJane 托管的 Luna / 自定义中转只有在冻结模型目录明确声明并通过探测的 `hosted_web_search` 能力时，才能走 P8 托管搜索；不能再根据模型名或“OpenAI 兼容”推测；
- 没有托管能力时，只在 Runtime 实际配置了结构化搜索后端（标准 MCP 或明确的 SheJane Cloud Search capability）时暴露；
- 不用 `web.fetch` 假装搜索，也不静默切换到另一个供应商；
- 模型目录和 Runtime 都没有绑定搜索能力时，`web.search` 必须不可见，Agent 不得通过抓取 Google/Bing 搜索结果 HTML 绕过这个边界；
- 各供应商原始动作、查询、完整来源与行内引用都进入模型调用账本/来源投影，不能只保存最后一段答案。

对输入控制做“能力下推”而非伪统一：有域名过滤就下推；没有则通过提示、结果审查和本地后续 Fetch 约束，但 UI/诊断必须标注 `budget_enforcement` / `domain_enforcement` 是 `hard` 还是 `best_effort`。

### 定向读取

第一期保留名称 `web.fetch`，但 schema 只需要 URL 与可选的正文预算：

```json
{
  "url": "https://example.com/article",
  "max_content_chars": 32000
}
```

`max_content_chars` 只能在 Runtime 固定的安全上下限内取值，调用方不能借它突破全局上下文预算。

成功结果示例：

```json
{
  "ok": true,
  "url": "https://example.com/article",
  "final_url": "https://www.example.com/article",
  "http_status": 200,
  "media_type": "text/html",
  "title": "Example article",
  "retrieved_at": "2026-08-11T08:30:00Z",
  "source_origin": "user",
  "redirect_chain": [
    {"status": 301, "url": "https://example.com/article"}
  ],
  "content": "Extracted main text or Markdown...",
  "content_chars": 18420,
  "content_sha256": "...",
  "truncated": false,
  "truncation_reason": null
}
```

失败结果示例：

```json
{
  "ok": false,
  "error_code": "http_not_found",
  "message": "The requested page was not found.",
  "url": "https://example.com/missing",
  "final_url": "https://example.com/missing",
  "http_status": 404,
  "retryable": false,
  "retry_after_ms": null
}
```

最低错误分类：

| `error_code` | 典型来源 | `retryable` |
|---|---|---|
| `invalid_url` / `unsupported_scheme` | 参数校验 | false |
| `url_not_in_prior_context` | 非用户/搜索/前序 Fetch 来源 | false |
| `private_address` / `blocked_domain` | SSRF/策略 | false |
| `robots_disallowed` | robots 规则 | false |
| `redirect_blocked` / `too_many_redirects` | 降级、目标不安全、超过上限 | false |
| `dns_error` / `connect_error` / `timeout` | 传输失败 | true（有界重试） |
| `tls_error` | 证书或握手失败 | false，除非能证明是临时网络故障 |
| `http_rate_limited` | 429 | true，并解析 `Retry-After` |
| `http_not_found` | 404/410 | false |
| `http_server_error` | 500/502/503/504 | true（仅 GET、有界重试） |
| `unsupported_content_type` | 非允许 MIME | false |
| `download_too_large` / `extraction_failed` | 内容处理 | false |

RFC 9110 允许服务端用 `Retry-After` 给出 HTTP-date 或秒数，503 也可据此建议重试时间；Runtime 应把它规范化到 `retry_after_ms`，而不是把原始响应头交给模型自行猜测。[RFC 9110 §10.2.3](https://www.rfc-editor.org/rfc/rfc9110.html#name-retry-after)、[§15.6.4](https://www.rfc-editor.org/rfc/rfc9110.html#name-503-service-unavailable)

布尔值和数字应使用 JSON 原生类型，不再使用 `"true"`、`"404"` 字符串。错误页正文最多保留很短的诊断摘要，不得当作研究证据进入模型上下文。

## 内容抽取、截断与动态过滤

下载边界和模型上下文边界必须分开：

1. **网络层**继续流式读取并在字节上限处停止，避免内存/带宽失控。
2. **媒体层**只接受明确支持的文本/HTML（PDF 已有 Runtime 文档工具，不应偷偷在网页工具里再实现一套 PDF 解析）。
3. **抽取层**删除 script/style/navigation 等噪声，保留标题、正文、标题层级、链接文本和 canonical URL；正文转换为纯文本或轻量 Markdown。
4. **上下文层**按抽取后的字符/token 预算截断，返回明确的原始长度、实际长度与原因。
5. **来源层**始终独立保存 URL、最终 URL、抓取时间与 hash；截断正文不能截掉来源信息。

第一期到此为止。只有真实指标证明“抽取后的 32 KiB 正文仍经常不够”时，再增加可选 `focus`，按段落做确定性的相关性选择。不要先增加向量库、缓存服务、代码执行过滤器或另一套 Artifact 系统；现有 Tool Receipt 与 Artifact 能覆盖大结果留存。

供应商动态过滤可直接使用，但要遵守两条边界：过滤后的内容用于模型上下文；完整来源列表和过滤动作摘要用于审计。Anthropic 的 `response_inclusion: "excluded"` 可以省 token，却不能成为丢失来源 provenance 的理由。

## SSRF、重定向、URL 来源与 robots

SSRF 没有一份等价于 HTTP Semantics 的单一 IETF 协议标准。CWE-918 定义了服务端替攻击者访问内部/受限资源的风险；OWASP 建议使用协议/目标 allowlist、校验所有 A/AAAA 地址、防 DNS pinning，并避免自动跟随未经重验的重定向。[CWE-918](https://cwe.mitre.org/data/definitions/918.html)、[OWASP SSRF Prevention Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Server_Side_Request_Forgery_Prevention_Cheat_Sheet.html)

因此现有 pinned DNS 与逐跳重验必须保留，并补充：

- 只允许 GET，不转发 Runtime/模型服务凭据、Cookie 或环境 header；
- URL 必须来自用户、可信客户端工具结果、搜索结果或前序 Fetch 结果；
- 每个重定向目标重新做 scheme、凭据、主机、全部 A/AAAA 地址和域名策略检查；
- HTTPS → HTTP 不改写为另一个 HTTPS URL，直接返回 `redirect_blocked`；
- 日志和诊断默认不展开可能含敏感值的 query，持久回执仍保存经脱敏的可审计 URL；
- 域名规则只接受 ASCII/punycode 规范化后的 hostname，避免同形异义绕过。

RFC 9309 是 Robots Exclusion Protocol 的 Standards Track 文档：成功取得 robots 文件时必须遵守可解析规则；robots 的 5xx/网络不可达应视为 complete disallow；缓存通常不应超过 24 小时。[RFC 9309 §2.3.1](https://www.rfc-editor.org/rfc/rfc9309.html#name-access-results)、[§2.4](https://www.rfc-editor.org/rfc/rfc9309.html#name-caching)

建议对所有 **Agent 自动发现后读取** 的 URL 遵守 robots；用户明确要求读取某个公开 URL 时也默认遵守，以得到单一、可解释的策略。实现时缓存每个 authority 的 robots 结果（最长 24 小时并尊重 Cache-Control），失败按 RFC 9309 fail closed。Anthropic 已把 robots 拒绝归入 `url_not_allowed`，说明这一行为在成熟 Agent Fetch 中已有产品先例。

robots 不是鉴权机制，也不能替代 SSRF、付费墙、登录态和内容安全检查。

## 预算与可观测性

Web 预算至少有三个维度：

- 搜索 query/action 次数；
- 页面打开/Fetch 次数（失败也计数）；
- 进入模型的抽取后内容量。

现有主 Agent 搜索小限额与 researcher 的较高限额方向正确，不建议在本次迁移中调大。先把所有执行路径计入同一预算：P8 的 OpenAI/Anthropic/Gemini 托管动作、P10 的本地 `web.fetch` 和 MCP `web.search` 不能各自拥有一套看不见彼此的计数器。

硬度需要诚实标注：Anthropic `max_uses` 可在单请求内硬限制；OpenAI 没有通用次数上限，只有上下文/返回预算；Gemini Search 可能在一次模型调用内生成多个计费 query。无法由供应商硬限制时，Runtime 只能在本轮后记账并阻止后续联网回合，因此是 `best_effort`，不是 hard cap。

每个来源/读取回执至少记录：执行位置（P8 hosted / P10 local / MCP）、provider、query 或脱敏 URL、最终 URL、HTTP/供应商状态、耗时、下载字节、抽取字符、截断、缓存/live、错误码、retryable、来源 origin、robots/domain 决策。正文和 query 中的用户敏感内容不进入常规 diagnostics。

近似位置只在用户任务确实需要本地结果且用户允许时传给供应商；不要从系统地址静默推断并发送精确位置。

## 迁移顺序

### M1：修正 `web.fetch` 契约（先做）

- 保留名称与 pinned-DNS/逐跳验证；移除 POST schema 或至少拒绝非 GET。
- HTTP 非 2xx 改成 `ok:false`；增加稳定 `error_code`、`retryable`、`retry_after_ms`。
- 拒绝 HTTPS 降级，不再改写 Location。
- 返回最终 URL、重定向链、媒体类型、抓取时间和 truncation 元数据。
- 从 raw HTML 改为抽取正文后再做上下文预算。
- 让结构化超时/连接错误真正进入现有 ToolResultRetryMiddleware；只重试 GET。

验收重点：404 必须成为 failed Tool Receipt；超时可有界重试；错误页不得成为 completion evidence；redirect 每一跳继续 SSRF 校验。

### M2：统一来源投影

- 定义供应商无关的 source/citation 内部形状，但原始 provider payload 仍留在模型调用账本。
- OpenAI 保留 inline annotations 与完整 `sources`；Anthropic/Gemini 投影各自 citation range、查询和 retrieval status。
- Client 只展示可点击引用、来源与必要失败，不展示 raw provider block。

不要把“被查询/打开过的 source”和“支持某句回答的 citation”合并成一张表。

### M3：扩展原生托管能力

- 按冻结的模型 capability 为 Anthropic 添加 Web Search/Web Fetch，为 Gemini 添加 Google Search/URL Context。
- 这些仍属于 P8 provider adapter；本地 `web.fetch` 不删除。
- 域名/位置/预算只在供应商明确支持时下推；不支持时标注 best-effort，不静默声称已经执行硬限制。

### M4：有数据再加相关性过滤

只有 telemetry 证明正文抽取后的内容预算持续造成失败，才添加 `focus` 段落过滤或启用供应商动态过滤的更多控制。不要把“更聪明的过滤”放在 M1 前面。

## 不建议做的事

- 不用浏览器自动化替代普通 Search/Fetch；动态页面或登录站点才需要 Browser QA。
- 不新增一个平行 Agent loop 来做网页研究；继续走现有 P8/P10/Receipt 链。
- 不把 2 MB 调到更大来“解决截断”；问题是没有抽取和双层预算。
- 不把所有 provider 错误压成 `fetch_failed`，也不假造供应商没有提供的细粒度错误。
- 不为兼容旧 POST 保留错误的 `read_only` 分类。
- 不在第一期引入新数据库、向量索引、缓存服务或网页抓取 SaaS。

## 一手资料

- [OpenAI Web Search](https://developers.openai.com/api/docs/guides/tools-web-search)
- [Anthropic Web Search](https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-search-tool)
- [Anthropic Web Fetch](https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-fetch-tool)
- [Anthropic Server tools](https://platform.claude.com/docs/en/agents-and-tools/tool-use/server-tools)
- [Google Grounding with Google Search](https://ai.google.dev/gemini-api/docs/google-search)
- [Google URL Context](https://ai.google.dev/gemini-api/docs/url-context)
- [Google Interactions API](https://ai.google.dev/api/interactions-api-v1)
- [RFC 9110: HTTP Semantics](https://www.rfc-editor.org/rfc/rfc9110.html)
- [RFC 9309: Robots Exclusion Protocol](https://www.rfc-editor.org/rfc/rfc9309.html)
- [CWE-918: Server-Side Request Forgery](https://cwe.mitre.org/data/definitions/918.html)
- [OWASP SSRF Prevention Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Server_Side_Request_Forgery_Prevention_Cheat_Sheet.html)
