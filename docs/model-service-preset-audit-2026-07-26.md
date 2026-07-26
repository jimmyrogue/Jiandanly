# 官方模型服务预置审计（2026-07-26）

本审计只采用厂商官方模型页、API 文档与更新公告，检查 `runtime/src/shejane_runtime/model_services.py` 中的六个预置。官方声明支持某项能力，只能证明“文档兼容”；SheJane 的 `verified` 还必须由当前区域、账户、端点和 Runtime 适配器完成真实流式工具调用测试。

## 结论

| 服务 | 当前预置 | 最新官方状态 | 建议 |
| --- | --- | --- | --- |
| DeepSeek | `deepseek-v4-flash`、`deepseek-v4-pro` | 两个 ID 都正确；V4 仍标为 Preview | 保留模型；Base URL 改为官方当前建议的 `https://api.deepseek.com` |
| Kimi | `kimi-k2.6` | 仍可用，但当前旗舰已是 `kimi-k3` | K3 作为新推荐候选；K2.6 可保留为兼容备选 |
| Qwen | `qwen3.7-plus` | 当前稳定 Plus 型号，能力/成本平衡定位未变 | 保留 |
| GLM | `glm-5` | 仍可用，但最新旗舰已是 `glm-5.2` | 更新候选为 5.2，并补齐实际请求的 `tool_stream` 配置 |
| MiniMax | `MiniMax-M2.7` | 仍可用，但最新语言模型已是 `MiniMax-M3` | M3 作为新推荐候选；M2.7 可保留为兼容备选 |
| SiliconFlow | `Pro/zai-org/GLM-5` | 已下线并重定向，当前 ID 过期 | 移除旧 ID；两区共享候选用 `zai-org/GLM-5.2` |

## 平台核对

### DeepSeek

- [官方模型与价格页](https://api-docs.deepseek.com/quick_start/pricing/)当前列出 `deepseek-v4-flash` 与 `deepseek-v4-pro`，均为 1M 上下文并支持工具调用；文本输入、无图片输入。
- [V4 发布说明](https://api-docs.deepseek.com/news/news260424/)将 Pro 定位为高性能版本，Flash 定位为更快、更经济的版本，因此 Flash 默认推荐、Pro 高性能备选是合理的产品选择。
- [官方 Agent 集成文档](https://api-docs.deepseek.com/quick_start/agent_integrations/oh_my_pi/)当前要求 Base URL 使用 `https://api.deepseek.com`，不要追加 `/v1`；同时要求保留 `reasoning_content`，且不支持强制 `tool_choice`。当前统一 OpenAI 配置不能仅凭 ID 宣称完全兼容。

### Kimi

- [Kimi 模型总览](https://platform.kimi.ai/docs/api/models-overview)与 [K3 Quickstart](https://platform.kimi.ai/docs/guide/kimi-k3-quickstart)已将 `kimi-k3` 列为当前旗舰：1M 上下文、原生视觉、推理和工具调用。
- `kimi-k2.6` 仍是有效模型，但不再是最新旗舰；可作为现有用户的兼容备选。
- 当前端点仍正确：中国区 `https://api.moonshot.cn/v1`（[中国区 API 概览](https://platform.kimi.com/docs/api/overview)），国际区 `https://api.moonshot.ai/v1`（[国际区 API 概览](https://platform.kimi.ai/docs/api/overview)）。
- K3 的思考与工具调用有专用约束；回传工具结果时必须保留完整 assistant 消息和 `reasoning_content`。两区分别通过真实探针前，不应直接写成当前连接已验证。

### Qwen

- [阿里云模型表](https://help.aliyun.com/zh/model-studio/models)和[文本模型文档](https://help.aliyun.com/zh/model-studio/text-generation-model)仍将 `qwen3.7-plus` 定位为兼顾效果、速度和成本的稳定模型，支持 1M 上下文、视觉输入和 Function Calling。
- 当前共享端点仍有效：中国区 `https://dashscope.aliyuncs.com/compatible-mode/v1`，新加坡区 `https://dashscope-intl.aliyuncs.com/compatible-mode/v1`（[官方 Base URL 文档](https://help.aliyun.com/zh/model-studio/base-url)）。UI 中的“国际站”更准确地应显示为“新加坡”。
- 官方现在更推荐工作空间专属域名，但它需要用户自己的 Workspace ID，不能硬编码进通用预置。

### GLM

- [GLM-5.2 官方文档](https://docs.bigmodel.cn/cn/guide/models/text/glm-5.2)将 `glm-5.2` 列为最新旗舰：1M 上下文、文本输入、流式输出和工具调用。
- 当前中国区 `https://open.bigmodel.cn/api/paas/v4`、国际区 `https://api.z.ai/api/paas/v4` 均为官方 OpenAI 兼容端点（[Z.AI HTTP API](https://docs.z.ai/guides/develop/http/introduction)）。
- [GLM Function Calling 文档](https://docs.bigmodel.cn/cn/guide/capabilities/function-calling)要求流式工具调用设置 `tool_stream: true`。SheJane 目前只在兼容性探针中加入它，正式模型请求没有同等配置；只更新模型 ID 仍可能在真实对话中失败。

### MiniMax

- [MiniMax API 总览](https://platform.minimaxi.com/docs/api-reference/api-overview)和 [M3 Function Call 指南](https://platform.minimaxi.com/docs/guides/text-m3-function-call)已列出 `MiniMax-M3`，支持 OpenAI Chat Completions、流式输出和工具调用；M3 是当前最新语言模型。
- `MiniMax-M2.7` 仍支持已有工作流，但已不是最新型号，可保留为兼容备选。
- 当前端点正确：中国区 `https://api.minimaxi.com/v1`，国际区 `https://api.minimax.io/v1`。
- M3 多轮工具调用需要保留完整 assistant/思考内容；在 SheJane 的消息回传链路完成实测前，应标记为待验证候选。

### SiliconFlow

- [中国区更新公告](https://docs.siliconflow.cn/cn/release-notes/overview)说明 `Pro/zai-org/GLM-5` 已于 2026-06-11 下线，请求会转到 `Pro/zai-org/GLM-5.1`；继续保留旧 ID 会掩盖真实模型变化。
- 5.1 的 ID 存在区域差异：中国区为 `Pro/zai-org/GLM-5.1`，国际区为 `zai-org/GLM-5.1`。当前预置结构只有一个共享 `model_id`，不适合用 5.1 表达两个区域。
- 两区官方模型中心均列出共享 ID `zai-org/GLM-5.2`，支持流式工具调用、不支持图片输入；它是更合适的共享替代（[中国区模型中心](https://www.siliconflow.cn/models)、[国际区 GLM-5.2](https://www.siliconflow.com/models/glm-5-2)）。
- 当前端点正确：中国区 `https://api.siliconflow.cn/v1`，国际区 `https://api.siliconflow.com/v1`。

## 检测方式的问题

当前把“预置模型”直接等同于 `verification: verified`，但连接即使改成第三方代理或自定义网关，Runtime 仍会补回并信任同一批预置。这会把厂商官方能力错误地当成当前连接已经通过测试。

最小正确规则：

1. `bundled` 表示官方文档中已知的候选模型；`recommended` 表示产品默认推荐；两者都不等于实测通过。
2. 只有 Base URL 精确匹配该区域官方端点，并完成普通流式文本、真实工具调用、SSE 参数重组、工具结果回传和最终答复，才能将“当前连接”标为 `verified`。
3. 自定义地址、代理和聚合平台发现的模型一律保持未验证，必须点击兼容性测试。
4. 探针按厂商设置请求参数：DeepSeek/Kimi 保留推理内容，GLM 使用 `tool_stream: true`，MiniMax 保留完整 assistant 消息；不能用一个统一请求体推断所有平台兼容。

## 推荐的下一步预置矩阵

```text
DeepSeek:     recommended deepseek-v4-flash; bundled deepseek-v4-pro
Kimi:         candidate recommended kimi-k3; fallback kimi-k2.6
Qwen:         recommended qwen3.7-plus
GLM:          candidate recommended glm-5.2
MiniMax:      candidate recommended MiniMax-M3; fallback MiniMax-M2.7
SiliconFlow:  candidate recommended zai-org/GLM-5.2; remove Pro/zai-org/GLM-5
```

“candidate”表示官方资料已确认，但仍需要 SheJane 在中国区和国际区各做一次真实鉴权与流式工具调用回归，才能变成连接级 `verified`。
