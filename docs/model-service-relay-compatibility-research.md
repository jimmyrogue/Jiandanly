# 中转模型服务兼容性调研

更新时间：2026-07-27

## 结论

SheJane 当前的兼容性测试不是通用的“模型能否调用”测试，而是“该模型能否作为 Agent 主模型”测试：模型必须完成一次 `模型 -> 函数工具 -> 工具结果 -> 最终回答` 闭环。因此，纯图片生成、图片编辑、视频生成和其他任务型模型不应参加这项测试，也不应出现在 Agent 主模型选择器中。

问题不在于保留了工具调用测试，而在于当前模型目录没有先区分模型用途，导致 `/v1/models` 返回的所有 ID 都被当成 Agent 聊天模型候选。

## Tuzi 的协议边界

- `https://tuzi-api.apifox.cn/` 是文档站，不是 API Base URL。Tuzi 文档给出的示例网关是 `https://api.tu-zi.com`，版本通过 `/v1/...` 路径表示；在 SheJane 当前会自行追加 `/models` 的实现下，应填写包含版本的 `https://api.tu-zi.com/v1`，或账户实际提供的同结构网关。
- `GET /v1/models` 只表示当前 Key 可见的模型 ID。Tuzi 页面没有提供能力字段的响应结构；New API 的官方格式也只有 `id`、`object`、`created`、`owned_by`，不能据此判断工具调用、图片输入或图片输出能力。
- Tuzi 将聊天、Responses 和图片生成分成不同接口：`POST /v1/chat/completions`、`POST /v1/responses`、`POST /v1/images/generations`。即使某些图片模型额外提供 Chat 包装，也不能推断它支持函数工具闭环。

| 模型用途 | 常见接口 | 是否适合作为 SheJane Agent 主模型 | 应验证的能力 |
| --- | --- | --- | --- |
| 文本/多模态聊天 Agent | `/v1/chat/completions` 或 `/v1/responses` | 只有同时支持流式函数调用时适合 | 流式输出、工具调用、工具结果回传、最终回答 |
| 图片理解 | Chat 或 Responses 图片输入 | 只有同时支持 Agent 工具闭环时适合 | 上述 Agent 验证，再增加最小图片理解请求 |
| 图片生成/编辑 | `/v1/images/generations`、`/v1/images/edits` 或供应商任务接口 | 不适合 | 生成/编辑请求、图片结果、错误与计费；异步接口还需轮询终态 |
| 视频/音乐等异步任务 | 供应商任务创建与查询接口 | 不适合 | 创建任务、查询状态、终态资源和幂等性 |

## NewAPI / OneAPI 的额外风险

- New API 的 Chat Completions 接口格式允许 `tools`，但这只是接口字段，不证明 `/v1/models` 中每个模型都支持工具调用。
- One API 明确说明模型映射会重构请求体，尚未正式支持的字段可能无法透传；这会影响 `tools`、工具结果消息或流式扩展字段。
- 中转站的模型映射、渠道负载均衡和失败重试可能让同一个公开模型名落到能力不同的上游渠道。因此能力必须按“连接 + 模型别名 + 接口协议”实测，不能只按模型名称推断。

## SheJane 当前行为

主阶段为 P6（绑定模型资源并取得 Agent 定义），相邻阶段为 P5（冻结具体模型配置）和 P7/P8（启动图并执行模型回合）。Runtime 的模型服务目录与凭据库是状态所有者。

当前链路有四个关键事实：

1. Runtime 调用 `<base_url>/models`，只提取返回列表中的模型 ID。
2. 未提供能力元数据时，发现模型默认标记为 `tool_calling=true`、`streaming=true`、`image_inputs=false`。
3. 所有手工模型都使用同一个聊天模型工具闭环测试。
4. 只有通过该测试且标记为工具调用、流式输出的模型才会成为可用 Agent 模型。

这套门槛对 Agent 主模型是必要的，但不能用于判断图片生成模型是否可用。当前 Runtime 也没有通用的 `/images/generations` 模型服务适配层；已有 `image_inputs` 与 `model.vision.invoke` 是图片理解能力，不是图片生成能力。

## 不改代码时的使用方式

1. Base URL 填账户实际网关并包含 `/v1`；不要填写 Apifox 文档网址。
2. 只把明确支持 `/v1/chat/completions` 流式函数调用的模型添加为 Agent 主模型。
3. 不要让 `gpt-image-*`、Nano Banana 图片版、Flux、Midjourney 等纯生成模型通过或绕过当前兼容性测试。即使强制标记成功，Agent 运行到工具回合时仍会失败。
4. 图片生成暂时通过 Tuzi 对应接口或支持该接口的客户端使用。SheJane 若要正式支持，应把它作为独立媒体工具/模型能力接入，而不是降低 Agent 主模型的验证门槛。
5. 若聊天模型也失败，应向中转站提供脱敏后的请求时间、模型别名、接口、HTTP 状态和 `request_id`，请其确认该别名对应的上游渠道是否透传 `tools` 与工具结果消息。

## 后续最小设计

保留现有 Agent 工具闭环验证，同时只增加两层区分：

1. 连接验证：只验证网关、Key 与模型目录是否可访问。
2. 按用途验证：Agent Chat、图片理解、图片生成/编辑和异步媒体任务分别调用自己的接口。

`/v1/models` 只作为 ID 目录，不作为能力目录。图片生成模型进入独立的媒体工具列表，不进入对话主模型选择器。暂不需要为每个中转站写一套供应商专用探测器；先支持显式选择用途和接口协议即可。

## 一手资料

- [Tuzi API 总览](https://tuzi-api.apifox.cn/)
- [Tuzi 获取模型](https://tuzi-api.apifox.cn/460547168e0)
- [Tuzi Chat Completions](https://tuzi-api.apifox.cn/343647063e0)
- [Tuzi Responses 图片输入](https://tuzi-api.apifox.cn/463707786e0)
- [New API 模型列表](https://docs.newapi.pro/en/docs/api/ai-model/models/list/listmodels)
- [New API Chat Completions](https://docs.newapi.pro/en/docs/api/ai-model/chat/openai/createchatcompletion)
- [New API 图片生成](https://docs.newapi.pro/en/docs/api/ai-model/images/openai/post-v1-images-generations)
- [One API 官方仓库](https://github.com/songquanpeng/one-api)
