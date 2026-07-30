# NewAPI 模型用途识别与能力元数据调研

核对日期：2026-07-30

上游基线：`QuantumNous/new-api` `main`，提交 [`66ee6b8f9889050ffef1f863a4314ce4a0516fb9`](https://github.com/QuantumNous/new-api/commit/66ee6b8f9889050ffef1f863a4314ce4a0516fb9)

来源限制：仅使用 NewAPI 官方文档、官方 GitHub 仓库源码和仓库内 OpenAPI 契约。

## 结论

NewAPI **不会从供应商的 `/v1/models` 响应中可靠地自动识别模型用途**。它的实际机制是：

1. 从供应商模型接口获取模型 ID；
2. 根据渠道类型决定一组默认端点；
3. 用内置模型名列表、包含/前缀规则补充少量特殊类型；
4. 用管理员维护的模型 `endpoints` 继续补充；
5. 管理员可选择具体端点执行真实测试。

因此，NewAPI 的“自动分辨”更准确地说是**规则和配置驱动的用途归类**，不是协议级能力发现，也不是对每个模型自动执行聊天、生图、Embedding 等请求后得出的结论。

这套机制对已知标准模型体验不错，但不能覆盖任意中转站别名。例如当前内置图片规则包含 `gpt-image-1`，却没有 `gpt-image-2`；`gpt-image-2-vip` 也不会命中。仅照搬这张规则表，仍会重现 SheJane 当前的小白配置问题。[图片模型名规则](https://github.com/QuantumNous/new-api/blob/66ee6b8f9889050ffef1f863a4314ce4a0516fb9/common/model.go#L5-L48)

官方仓库中一项已关闭且未合并的修复 PR 也记录了同一现象：`gpt-image-2` 会被渠道自动测试错误发送到 `/v1/chat/completions`，而不是 `/v1/images/generations`。这可以作为当前限制的交叉证据，不代表官方已经接受或发布该修复。[NewAPI PR #5140](https://github.com/QuantumNous/new-api/pull/5140)

## 1. NewAPI 如何得到模型用途

### 1.1 供应商模型接口只提供候选 ID

NewAPI 按渠道类型请求供应商的模型列表。普通 OpenAI 兼容渠道请求 `/v1/models`，然后只读取每项的 `id`；上游返回的 `metadata` 虽然可以被反序列化，但没有进入用途识别。Ollama、Gemini、Codex 有各自的模型列表适配器，最终同样归一化成字符串 ID 列表。[上游模型列表抓取](https://github.com/QuantumNous/new-api/blob/66ee6b8f9889050ffef1f863a4314ce4a0516fb9/controller/channel_upstream_update.go#L335-L425) [上游模型响应结构](https://github.com/QuantumNous/new-api/blob/66ee6b8f9889050ffef1f863a4314ce4a0516fb9/controller/channel.go#L27-L54)

也就是说，供应商 `/models` 负责回答“有哪些模型”，不负责回答“这些模型用于什么”。

### 1.2 渠道类型先决定默认端点

NewAPI 为渠道类型分配默认端点：

- Jina 默认是 `jina-rerank`；
- Anthropic/AWS 默认是 `anthropic` 和 `openai`；
- Gemini/Vertex AI 默认是 `gemini` 和 `openai`；
- Sora 默认是 `openai-video`；
- NewAPI/Sub2API 渠道默认声明多种聊天协议；
- 普通渠道默认是 `openai` 聊天端点。

随后才根据模型名判断是否把 `image-generation` 插到端点列表首位。[渠道到端点的映射](https://github.com/QuantumNous/new-api/blob/66ee6b8f9889050ffef1f863a4314ce4a0516fb9/common/endpoint_type.go#L5-L59)

因此，归类首先来自管理员选择的渠道类型，不是模型自描述。

### 1.3 少量用途由模型名启发式识别

当前主分支的图片模型识别表包含：

- 精确/包含匹配：`dall-e-3`、`dall-e-2`、`gpt-image-1`、`flux-`、`flux.1-`；
- 前缀匹配：`imagen-`。

实现使用 `strings.Contains` 和一个特殊的 `prefix:` 约定；它不是正则库，也没有读取供应商返回的结构化能力。[图片模型判断源码](https://github.com/QuantumNous/new-api/blob/66ee6b8f9889050ffef1f863a4314ce4a0516fb9/common/model.go#L12-L48)

渠道测试的“自动检测”也使用名称规则：包含 `rerank` 走重排，包含 `embedding`/`embed` 或 `m3e`、`bge-` 走向量，火山 `seedream` 走生图，包含 `codex` 走 Responses。[测试端点自动选择](https://github.com/QuantumNous/new-api/blob/66ee6b8f9889050ffef1f863a4314ce4a0516fb9/controller/channel-test.go#L114-L153) [测试请求自动构造](https://github.com/QuantumNous/new-api/blob/66ee6b8f9889050ffef1f863a4314ce4a0516fb9/controller/channel-test.go#L695-L815)

这些规则解决常见命名，但无法可靠判断自定义别名、版本变化或一个模型同时具备多种能力。

### 1.4 模型元数据支持人工补充端点，中心同步当前不会应用端点字段

NewAPI 有独立的模型元数据表。核心字段包括 `model_name`、`tags`、`vendor_id`、`endpoints`、`name_rule` 和 `sync_official`；`name_rule` 支持精确、前缀、包含、后缀四种匹配。[模型元数据结构](https://github.com/QuantumNous/new-api/blob/66ee6b8f9889050ffef1f863a4314ce4a0516fb9/model/model_meta.go#L12-L45)

“同步上游模型”不是直接把供应商 `/models` 变成能力，而是从 NewAPI 配置的中心元数据源拉取描述、标签、供应商和名称规则。上游数据结构虽然声明了 `endpoints`，但当前创建模型时没有写入它，可选覆盖也没有 `endpoints` 分支；因此这条同步链路当前不会自动补齐模型用途。默认只创建缺失模型，已有记录只有在管理员选择覆盖字段且 `sync_official` 未关闭时才更新。[中心同步数据结构与来源](https://github.com/QuantumNous/new-api/blob/66ee6b8f9889050ffef1f863a4314ce4a0516fb9/controller/model_sync.go#L23-L90) [创建字段与选择性覆盖](https://github.com/QuantumNous/new-api/blob/66ee6b8f9889050ffef1f863a4314ce4a0516fb9/controller/model_sync.go#L344-L449) [官方模型管理说明](https://docs.newapi.pro/zh/docs/guide/feature-guide/admin/model)

运行时会把以下来源合并成 `supported_endpoint_types`：

1. 已启用渠道能力对应的默认端点；
2. Advanced Custom 渠道显式配置的路由；
3. 模型元数据 `endpoints` 中的自定义端点。

同一模型可以得到多个端点，顺序还表示优先级。[端点集合构建](https://github.com/QuantumNous/new-api/blob/66ee6b8f9889050ffef1f863a4314ce4a0516fb9/model/pricing.go#L272-L316) [默认与自定义端点合并](https://github.com/QuantumNous/new-api/blob/66ee6b8f9889050ffef1f863a4314ce4a0516fb9/model/pricing.go#L318-L355)

### 1.5 测试是人工触发的验证，不是后台自动分类

渠道测试界面允许管理员选择 `Auto detect`，也可以明确选择 Chat、Responses、Anthropic、Gemini、Rerank、Image Generation 或 Embeddings。不同端点会构造不同的最小请求，例如生图会发送 `a cute cat`，Embedding 会发送 `hello world`。[测试端点选项](https://github.com/QuantumNous/new-api/blob/66ee6b8f9889050ffef1f863a4314ce4a0516fb9/web/src/features/channels/components/dialogs/channel-test-dialog.tsx#L181-L211) [端点专用测试请求](https://github.com/QuantumNous/new-api/blob/66ee6b8f9889050ffef1f863a4314ce4a0516fb9/controller/channel-test.go#L695-L756)

测试成功证明当前渠道、Key、模型和端点组合可以工作，但源码没有把测试结果持久化为完整的模型 capability 声明。因此它不能代替模型目录的结构化能力来源。

## 2. NewAPI 支持的模型类型和能力边界

### 2.1 可结构化展示的端点类型

当前 `EndpointType` 枚举包含：

| 值 | 主要用途 |
| --- | --- |
| `openai` | Chat Completions / 文本聊天 |
| `openai-response` | OpenAI Responses |
| `openai-response-compact` | Responses Compact |
| `openai-alpha-search` | 独立搜索端点 |
| `anthropic` | Claude Messages |
| `gemini` | Gemini Generate Content |
| `jina-rerank` | 文档重排 |
| `image-generation` | 图片生成 |
| `embeddings` | 向量嵌入 |
| `openai-video` | OpenAI 风格视频生成 |

枚举和字符串契约见 [EndpointType 源码](https://github.com/QuantumNous/new-api/blob/66ee6b8f9889050ffef1f863a4314ce4a0516fb9/relaykit/types/endpoint_type.go#L3-L19)。

### 2.2 网关实际支持的接口比端点元数据更广

官方 API 文档列出的能力包括聊天、Responses、Embedding、Rerank、Moderation、音频、实时语音、图像和视频。[官方 API 总览](https://docs.newapi.pro/zh/docs/api)

网关路由还明确实现了：

- 图片生成与图片编辑；
- 语音转文字、音频翻译、文字转语音；
- Embedding 与 Rerank；
- Realtime；
- Moderation；
- 独立的 Midjourney、Suno 等任务接口。

源码入口见 [Relay 路由](https://github.com/QuantumNous/new-api/blob/66ee6b8f9889050ffef1f863a4314ce4a0516fb9/router/relay-router.go#L69-L171)，官方用户文档也列出了主要端点：[使用 API](https://docs.newapi.pro/zh/docs/guide/feature-guide/user/api)。

但“网关有这个路由”不等于“每个模型都有这个能力”。特别是图片编辑、音频转录、TTS、Realtime、Moderation 目前没有对应的细粒度 `EndpointType` 模型元数据，无法只靠 `supported_endpoint_types` 完整表达。

### 2.3 工具调用、视觉、推理等不是稳定的结构化下游能力

模型元数据中的 `tags` 是逗号分隔字符串，可以展示 Vision、Tools 等标签，但它不是版本化的 capability schema。前端类型预留了 `input_modalities`、`output_modalities`、`capabilities`，包含 vision、tools、reasoning、streaming 等概念；当前后端 `Pricing` 结构却没有返回这些字段，只返回 `tags` 和 `supported_endpoint_types` 等现有字段。[前端预留字段](https://github.com/QuantumNous/new-api/blob/66ee6b8f9889050ffef1f863a4314ce4a0516fb9/web/src/features/pricing/types.ts#L30-L89) [后端 Pricing 结构](https://github.com/QuantumNous/new-api/blob/66ee6b8f9889050ffef1f863a4314ce4a0516fb9/model/pricing.go#L18-L39)

因此，NewAPI 当前能较好表达“该模型可走哪些 API 端点”，不能稳定表达“是否支持工具调用、图片输入、结构化输出、图片编辑”等完整语义能力。

## 3. API、数据结构与更新机制

### 3.1 `GET /v1/models`

当前 Go 实现给 OpenAI 风格的每个模型附加 `supported_endpoint_types`。[响应 DTO](https://github.com/QuantumNous/new-api/blob/66ee6b8f9889050ffef1f863a4314ce4a0516fb9/relaykit/dto/pricing.go#L5-L12) [模型响应组装](https://github.com/QuantumNous/new-api/blob/66ee6b8f9889050ffef1f863a4314ce4a0516fb9/controller/model.go#L153-L169) [列表返回](https://github.com/QuantumNous/new-api/blob/66ee6b8f9889050ffef1f863a4314ce4a0516fb9/controller/model.go#L259-L303)

示意：

```json
{
  "id": "gpt-image-1",
  "object": "model",
  "owned_by": "openai",
  "supported_endpoint_types": ["image-generation", "openai"]
}
```

注意：Anthropic 和 Gemini 格式的模型列表转换只保留各自标准字段，不带这个扩展字段。[多协议模型列表转换](https://github.com/QuantumNous/new-api/blob/66ee6b8f9889050ffef1f863a4314ce4a0516fb9/controller/model.go#L268-L303)

### 3.2 `GET /api/pricing`

NewAPI 自己的定价接口返回每个模型的 `tags`、`supported_endpoint_types`、价格、分组和供应商信息，同时返回全局 `supported_endpoint` 路径映射。[定价响应](https://github.com/QuantumNous/new-api/blob/66ee6b8f9889050ffef1f863a4314ce4a0516fb9/controller/pricing.go#L36-L76) [定价数据构建](https://github.com/QuantumNous/new-api/blob/66ee6b8f9889050ffef1f863a4314ce4a0516fb9/model/pricing.go#L357-L409)

这个接口适合 NewAPI 自己的模型中心页面按端点筛选和展示。[前端按端点筛选](https://github.com/QuantumNous/new-api/blob/66ee6b8f9889050ffef1f863a4314ce4a0516fb9/web/src/features/pricing/components/pricing-sidebar.tsx#L230-L245) [端点标签展示](https://github.com/QuantumNous/new-api/blob/66ee6b8f9889050ffef1f863a4314ce4a0516fb9/web/src/features/pricing/components/pricing-columns.tsx#L370-L392)

### 3.3 两种更新链路

| 链路 | 获得什么 | 是否自动得到用途 |
| --- | --- | --- |
| 渠道上游模型更新 | 供应商当前模型 ID | 否，只更新渠道模型候选 |
| 中心模型元数据同步 | 描述、标签、供应商、名称规则 | 当前不会应用上游 `endpoints`，不能自动补充用途 |

渠道更新可以记录新增/删除模型，并在开启自动同步时把新增 ID 合入渠道。[渠道模型变更与自动应用](https://github.com/QuantumNous/new-api/blob/66ee6b8f9889050ffef1f863a4314ce4a0516fb9/controller/channel_upstream_update.go#L474-L520)

中心元数据同步提供预览和显式应用，路由是 `/api/models/sync_upstream/preview` 与 `/api/models/sync_upstream`。[管理路由](https://github.com/QuantumNous/new-api/blob/66ee6b8f9889050ffef1f863a4314ce4a0516fb9/router/api-router.go#L343-L354)

模型元数据新增、修改、删除后会主动刷新定价/端点缓存；普通读取的缓存最长约一分钟，渠道缓存重建也会使其失效。[模型变更刷新](https://github.com/QuantumNous/new-api/blob/66ee6b8f9889050ffef1f863a4314ce4a0516fb9/controller/model_meta.go#L89-L169) [一分钟缓存](https://github.com/QuantumNous/new-api/blob/66ee6b8f9889050ffef1f863a4314ce4a0516fb9/model/pricing.go#L66-L87) [渠道缓存失效联动](https://github.com/QuantumNous/new-api/blob/66ee6b8f9889050ffef1f863a4314ce4a0516fb9/model/channel_cache.go#L95-L103)

## 4. 下游客户端能否拿到结构化 capability

答案是：**能拿到一部分实现级元数据，但不能把它当成完整、稳定的 capability 契约。**

### 可以拿到

- OpenAI 风格 `GET /v1/models` 当前实现会返回 `supported_endpoint_types`；
- NewAPI 专用 `GET /api/pricing` 会返回 `supported_endpoint_types`、`tags` 和端点路径映射；
- 一个模型可以有多个端点，因此可以表达“既能聊天又能生图”这类组合。

### 不能直接得到

- 图片生成与图片编辑的精确区分；
- ASR、TTS、音频输入、Realtime 等细分用途；
- 工具调用、视觉理解、流式、结构化输出、推理等可靠布尔能力；
- 能力的验证状态、验证时间、失败原因和来源可信度。

还有一个契约风险：官方 `GET /v1/models` OpenAPI schema 只声明 `id`、`object`、`created`、`owned_by`，没有声明 `supported_endpoint_types`。[官方 OpenAPI Model schema](https://github.com/QuantumNous/new-api/blob/66ee6b8f9889050ffef1f863a4314ce4a0516fb9/docs/openapi/relay.json#L2388-L2425) [官方模型列表文档](https://docs.newapi.pro/zh/docs/api/ai-model/models/list/listmodels)

所以该字段在当前源码中可用，但属于未进入官方文档契约的扩展。第三方客户端应容忍字段缺失，SheJane 如果依赖它，应在自己的 shejane-cloud 契约中明确固定。

## 5. 对 SheJane 的建议

可以借鉴 NewAPI 的“自动优先”，但不应照搬它的纯模型名规则。推荐把普通用户流程改成：

```text
登录官方服务
→ 自动拉取模型及结构化用途
→ Runtime 自动分组并选择默认模型
→ 用户直接使用
→ 仅在识别失败或高级设置中允许人工修正
```

### 官方服务

shejane-cloud 是可控上游，应该成为模型用途的权威来源：

1. 先复用 NewAPI 当前的 `supported_endpoint_types`，覆盖 Chat、Responses、Embedding、Rerank、Image Generation 和 Video；
2. 在 shejane-cloud 的稳定契约中补充 `capabilities`、`input_modalities`、`output_modalities`，用于图片编辑、ASR、TTS、视觉、工具调用等细分能力；
3. 每个能力带 `source` 和 `verification`，区分平台声明、名称推断和真实请求验证；
4. Runtime 接收并保存，Client 只按用途展示，不让小白选择“这个模型是什么类型”。

### BYOK 和任意中转服务

无法要求所有 `/v1/models` 都返回能力，因此保留分层回退：

1. 优先读取服务端结构化 `supported_endpoint_types`/`capabilities`；
2. 再用 SheJane 维护的已知模型规则做低成本推断；
3. 对未知模型提供按用途的验证；聊天等无副作用能力可主动探测，生图、视频等会计费或产生产物的能力只在首次真实使用或用户主动验证时确认；
4. 只有识别冲突或全部失败时才要求用户手动选择。

### UI

默认界面只显示“对话”“图片”“语音”等人类用途和已自动选中的模型，不显示协议名。高级设置再展示来源与状态，例如：

```text
图片生成：gpt-image-2-vip
由官方服务识别 · 已验证
```

这样保留 NewAPI 的无脑体验，同时避免其模型名映射落后时把新模型误判成聊天模型。
