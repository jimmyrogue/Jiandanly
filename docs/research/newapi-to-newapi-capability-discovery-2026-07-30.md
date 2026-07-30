# NewAPI 对接 NewAPI 时的模型能力发现链路

核对日期：2026-07-30

官方上游基线：`QuantumNous/new-api` `main`，提交 [`66ee6b8f9889050ffef1f863a4314ce4a0516fb9`](https://github.com/QuantumNous/new-api/commit/66ee6b8f9889050ffef1f863a4314ce4a0516fb9)

本地核对范围：`/Users/MediaStorm/Desktop/ColdFlame/shejane-cloud` 当前源码。来源只使用 NewAPI 官方 GitHub 源码、官方文档和本地同源实现。

## 结论

“上游也是 NewAPI 时会自动分辨模型能力”这个观察有依据，但准确说法是：**`ChannelTypeNewAPI` 会启用 NewAPI 专用的多协议渠道适配和本地端点推断；它目前不会从上游逐模型继承 `supported_endpoint_types`、`capabilities` 或 `recommended_for`。**

NewAPI → NewAPI 的现有链路是：

```text
管理员选择 New API 渠道类型
→ GET 上游 /v1/models
→ 只保留 data[].id
→ 写入本地渠道 models / abilities
→ 本地根据 ChannelTypeNewAPI、模型名规则和模型 endpoints 重新计算能力
→ 本地 /v1/models 与 /api/pricing 输出重新计算的 supported_endpoint_types
```

因此自动体验确实存在，但能力来源是**下游 NewAPI 自己重新推断**，不是透传上游 NewAPI 的模型能力目录。[New API 渠道初始实现提交](https://github.com/QuantumNous/new-api/commit/398cdafecf29f5211edd93cbb0525152299a6893)

## 1. 渠道拉取实际读取哪个接口

### 1.1 普通 OpenAI 兼容渠道和 New API 渠道都读取 `/v1/models`

除少数厂商专用分支外，渠道模型抓取默认请求：

```http
GET {base_url}/v1/models
Authorization: Bearer {channel_key}
```

`ChannelTypeNewAPI` 没有切换到 `/api/pricing`，所以也走这个默认分支。[模型抓取 URL 与认证](https://github.com/QuantumNous/new-api/blob/66ee6b8f9889050ffef1f863a4314ce4a0516fb9/controller/channel_upstream_update.go#L335-L425) 官方 NewAPI 渠道的 `ModelList` 为空，源码注释也明确说明模型由上游 `/v1/models` 动态取得。[NewAPI 渠道常量](https://github.com/QuantumNous/new-api/blob/66ee6b8f9889050ffef1f863a4314ce4a0516fb9/relay/channel/newapi/constants.go#L1-L6)

### 1.2 响应只保留模型 ID

上游解析结构只声明了 `id`、`object`、`created`、`owned_by`、`metadata` 等 OpenAI 字段，没有 `supported_endpoint_types`、`capabilities` 或 `recommended_for`。[上游模型响应结构](https://github.com/QuantumNous/new-api/blob/66ee6b8f9889050ffef1f863a4314ce4a0516fb9/controller/channel.go#L27-L54)

抓取函数最终只映射 `item.ID`，再去空、去重；即使上游 NewAPI 在 `/v1/models` 中返回额外字段，Go JSON 解码也会忽略它们。[只提取 ID 的实现](https://github.com/QuantumNous/new-api/blob/66ee6b8f9889050ffef1f863a4314ce4a0516fb9/controller/channel_upstream_update.go#L265-L280) [默认抓取分支](https://github.com/QuantumNous/new-api/blob/66ee6b8f9889050ffef1f863a4314ce4a0516fb9/controller/channel_upstream_update.go#L395-L425)

渠道的定期上游同步复用同一函数，所以后台自动新增/删除模型也只同步名称，不同步逐模型端点元数据。[官方环境变量文档：Channel Upstream Model Synchronization](https://docs.newapi.ai/en/docs/installation/config-maintenance/environment-variables#channel-upstream-model-synchronization)

## 2. New API 渠道为什么看起来会自动识别能力

### 2.1 它有独立的多协议适配器

普通 OpenAI 渠道使用 OpenAI 适配器；`ChannelTypeNewAPI` 被注册为独立 `APITypeNewAPI`，使用 NewAPI 适配器。该适配器按原请求路径转发，并分别处理 OpenAI、Responses、Claude、Gemini 和图片请求；Claude/Gemini 还会补各自认证头。[NewAPI 适配器](https://github.com/QuantumNous/new-api/blob/66ee6b8f9889050ffef1f863a4314ce4a0516fb9/relay/channel/newapi/adaptor.go#L20-L113)

这是真正的 NewAPI → NewAPI 专用能力，不只是把它当作普通 OpenAI 兼容服务。

### 2.2 它按渠道类型给每个模型声明一组多协议端点

本地定价/模型目录从 `abilities` 读取模型与渠道类型，再调用 `GetEndpointTypesByChannelType` 生成端点集合。[定价能力入口](https://github.com/QuantumNous/new-api/blob/66ee6b8f9889050ffef1f863a4314ce4a0516fb9/model/pricing.go#L98-L118) [端点集合构建](https://github.com/QuantumNous/new-api/blob/66ee6b8f9889050ffef1f863a4314ce4a0516fb9/model/pricing.go#L272-L316)

对于 `ChannelTypeNewAPI`，每个模型默认得到：

- `openai`
- `openai-response`
- `openai-response-compact`
- `anthropic`
- `gemini`
- `openai-alpha-search`

这是渠道级的统一声明，不是从每个模型的上游响应中读取的差异化结果。[渠道到端点的映射](https://github.com/QuantumNous/new-api/blob/66ee6b8f9889050ffef1f863a4314ce4a0516fb9/common/endpoint_type.go#L33-L41)

### 2.3 图片生成仍由模型名规则补充

所有渠道类型在完成默认端点推断后，都会调用 `IsImageGenerationModel`；命中后把 `image-generation` 插到首位。[图片端点补充](https://github.com/QuantumNous/new-api/blob/66ee6b8f9889050ffef1f863a4314ce4a0516fb9/common/endpoint_type.go#L48-L59)

当前官方主分支的内置规则只有 `dall-e-3`、`dall-e-2`、`gpt-image-1`、`imagen-`、`flux-` 和 `flux.1-`。`gpt-image-2` 与 `gpt-image-2-vip` 不会命中，所以仅仅把上游渠道类型设为 New API，并不能让这两个名称自动得到 `image-generation`。[图片模型名规则](https://github.com/QuantumNous/new-api/blob/66ee6b8f9889050ffef1f863a4314ce4a0516fb9/common/model.go#L12-L48)

## 3. `supported_endpoint_types` 从哪里来、如何返回

NewAPI 自己的 OpenAI 风格 `GET /v1/models` 确实包含 `supported_endpoint_types`。[响应 DTO](https://github.com/QuantumNous/new-api/blob/66ee6b8f9889050ffef1f863a4314ce4a0516fb9/relaykit/dto/pricing.go#L5-L12)

但这个字段由当前实例的 `model.GetModelSupportEndpointTypes(modelName)` 填充，是本地定价缓存的结果，不是上游抓取响应的透传。[模型列表组装](https://github.com/QuantumNous/new-api/blob/66ee6b8f9889050ffef1f863a4314ce4a0516fb9/controller/model.go#L153-L169)

本地缓存合并三个来源：

1. 渠道类型推断出的端点；
2. Advanced Custom 渠道的显式路由；
3. 模型元数据表 `endpoints` 中的管理员配置。

合并逻辑只追加有效端点，不会用模型配置裁剪渠道推断出来的端点。[端点来源合并](https://github.com/QuantumNous/new-api/blob/66ee6b8f9889050ffef1f863a4314ce4a0516fb9/model/pricing.go#L272-L316)

因此，两个 NewAPI 实例串联时会出现：

```text
上游 NewAPI 计算 supported_endpoint_types
→ 上游 /v1/models 返回该字段
→ 下游渠道抓取忽略该字段，仅留下 id
→ 下游 NewAPI 再按自己的渠道类型与规则计算 supported_endpoint_types
```

## 4. `/api/pricing` 是否参与渠道能力发现

不参与。

`GET /api/pricing` 是当前实例的价格与目录展示接口，响应包含模型的 `supported_endpoint_types` 和全局 `supported_endpoint` 路径映射。[定价响应](https://github.com/QuantumNous/new-api/blob/66ee6b8f9889050ffef1f863a4314ce4a0516fb9/controller/pricing.go#L36-L76)

另有一套独立的“上游倍率同步”工具，默认端点才是 `/api/pricing`；它用于同步模型倍率、固定价格、缓存倍率等计费字段，不是创建/编辑渠道时的模型能力发现。[倍率同步默认端点](https://github.com/QuantumNous/new-api/blob/66ee6b8f9889050ffef1f863a4314ce4a0516fb9/controller/ratio_sync.go#L31-L44) [两种倍率响应格式解析](https://github.com/QuantumNous/new-api/blob/66ee6b8f9889050ffef1f863a4314ce4a0516fb9/controller/ratio_sync.go#L341-L390)

所以：

| 操作 | 读取接口 | 保留内容 |
| --- | --- | --- |
| 渠道“获取模型”/定期模型同步 | `/v1/models` | 仅模型 ID |
| 上游倍率同步 | 默认 `/api/pricing` | 价格与倍率字段 |
| 当前实例模型目录 | `/v1/models` | 本地重算的 `supported_endpoint_types` |
| 当前实例定价目录 | `/api/pricing` | 本地模型、价格、标签、分组、`supported_endpoint_types` |

## 5. NewAPI 自己有哪些可配置项

NewAPI 有原生配置，但没有独立的通用 `capabilities` 或 `recommended_for` 数据列：

1. **渠道类型**：选择 `New API` 后自动启用专用适配器和多协议默认端点。
2. **渠道模型列表**：可从上游 `/v1/models` 获取 ID，也可自动追踪新增/删除。
3. **模型管理 → Endpoint Configuration**：模型表的 `endpoints` 字段可以为某个模型或名称规则补充 `image-generation`、`embeddings` 等端点。[模型元数据结构](https://github.com/QuantumNous/new-api/blob/66ee6b8f9889050ffef1f863a4314ce4a0516fb9/model/model_meta.go#L12-L45)
4. **模型名内置规则**：源码中的图片、Responses-only 等规则自动补充端点。

官方管理文档说明渠道页面可获取/选择模型，模型管理页面可同步和编辑模型元数据，但没有把 `capabilities`、`recommended_for` 定义为管理协议。[官方渠道管理](https://docs.newapi.pro/zh/docs/guide/feature-guide/admin/channel) [官方模型管理](https://docs.newapi.pro/zh/docs/guide/feature-guide/admin/model)

## 6. 对 SheJane 当前实现的直接影响

### 6.1 应复用 NewAPI 的原生目录字段

SheJane Cloud 的图片用途不应该继续按具体模型 ID硬编码。更稳定的来源是当前实例最终算出的 `supported_endpoint_types`：

```text
image-generation → capability: image_generation
                 → recommended_for: image_generation
```

这样以后只要 NewAPI 通过渠道规则或模型 `endpoints` 识别了新图片模型，SheJane 官方 `/v1/models` 就能自动声明用途，无需普通用户选择，也无需再为每个模型修改 SheJane 代码。

### 6.2 不能把所有 New API 渠道的 `openai` 当成已验证聊天能力

`ChannelTypeNewAPI` 会给每个模型统一添加 `openai` 等六类协议，因此它表达的是“该渠道适配器能够转发这些协议”，不一定证明每个上游模型都真实支持每种协议。SheJane 可以可靠地用明确的 `image-generation` 做图片用途分类，但不应仅凭 New API 渠道的宽泛 `openai` 声明跳过现有聊天能力验证。

### 6.3 `gpt-image-2` 仍需要在 NewAPI 层获得原生图片端点

当前官方名称规则不包含 `gpt-image-2`。要让整个链路无硬编码模型目录，可在 SheJane Cloud 的 NewAPI 模型管理中为 `gpt-image-2` / `gpt-image-2-vip` 配置 `image-generation`，或在 fork 的通用图片名称规则中增加 `gpt-image-2`。随后 SheJane 只从最终的 `supported_endpoint_types` 派生语义字段。

如果目标是严格继承另一个 NewAPI 实例的逐模型能力，则需要另做协议增强：让 `ChannelTypeNewAPI` 的抓取结构读取上游 `supported_endpoint_types`，并设计可持久化、失效和冲突处理。**当前官方实现没有这条透传链路。**

## 最终判断

- 用户观察正确的部分：NewAPI 上游应选择专用 `New API` 渠道类型；该类型会自动启用多协议转发和本地能力目录计算。
- 需要纠正的部分：自动能力不是从上游 `/v1/models` 的逐模型字段继承，而是下游按渠道类型、模型名和本地 `endpoints` 重算。
- 渠道获取模型读 `/v1/models`，不是 `/api/pricing`。
- SheJane 下一步应从 NewAPI 原生 `supported_endpoint_types` 派生 `capabilities` / `recommended_for`，删除按 `gpt-image-2` 名称写死的目录逻辑。
