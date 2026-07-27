# 第三方模型列表与能力处理方式调研

核对日期：2026-07-27  
范围：Open WebUI、LobeHub、Dify、Cherry Studio、LibreChat  
来源限制：仅使用官方文档和官方 GitHub 源码。

## 结论

主流 Agent/AI 客户端普遍把以下事情分开处理：

1. 服务地址和 API Key 能否连接；
2. 第三方服务暴露了哪些模型 ID；
3. 用户实际想启用哪些模型；
4. 模型被声明为什么类型、具备哪些能力；
5. 该能力在真实请求中是否可用。

没有发现这些产品会把第三方 `/models` 返回的所有条目统一执行一次“流式工具调用完整闭环”，再决定模型是否能保存。图片生成通常也不会被当作 Agent 主聊天模型，而是独立的图片模型、图片服务或 Agent 工具。

## 对比表

| 产品 | 模型发现与人工选择 | 能力处理 | 检测方式 | 图片生成边界 |
| --- | --- | --- | --- | --- |
| Open WebUI | 默认请求 `/models`；也可用 `model_ids` 直接建立人工白名单 | Workspace Model 可手动设置 Vision、Image Generation、Builtin Tools 等能力 | 连接验证主要请求 `/models`，不执行工具闭环 | 独立配置图片引擎、URL、Key 和图片模型 |
| LobeHub | 支持远程拉取；NewAPI 默认没有静态列表；用户可以增加、隐藏和启用模型 | 用户可选择模型类型并勾选 Function Call、Vision、Image Output 等能力 | 从聊天模型中选一个发送普通 `hello`；能力开关不等于验证成功 | 有独立 `image` 模型类型，也支持聊天模型的 `imageOutput` 能力 |
| Dify | 支持预置、远程拉取和用户逐模型配置三种方式 | 模型类型与 `vision`、`tool-call`、`stream-tool-call` 等能力是显式元数据 | Provider 凭证验证与逐模型凭证验证分开；兼容插件让用户明确选择能力 | 生图通常作为 Tool Plugin，由 Agent 的 LLM 调用 |
| Cherry Studio | 可自动拉取模型，但只有用户点击 `+` 添加的模型才进入选择器；也支持手填模型 ID | 可人工修正视觉、函数调用、Embedding 等模型属性 | 连接检查使用模型列表中最后加入的聊天模型，不逐能力验证 | AI 绘画是独立功能；文档明确不同模型类型的请求和返回结构不同 |
| LibreChat | `models.fetch` 可选且默认关闭；管理员可提供人工 `models.default` 列表 | 能力被视为具体模型的属性，Model Specs 用于策展和限制选择器 | 没有统一的逐模型能力探测；是否可用由具体模型和实际运行决定 | 生图/改图是挂到 Agent 上的独立工具，拥有自己的模型、Key 和 Base URL |

## 1. Open WebUI

### 模型发现与人工列表

Open WebUI 的 OpenAI-compatible 连接在没有人工 `model_ids` 时请求 `/models`；配置了 `model_ids` 后则直接用这份列表构造可用模型，不再依赖远端模型发现。连接的 `/verify` 也只是请求 `/models`。这说明它把“连接/列表可读”当作连接检查，而不是能力证明。

- [模型发现与人工 model_ids 源码](https://github.com/open-webui/open-webui/blob/01f4282f1ffe0d6212f58d3afbeae21fffd0c4be/backend/open_webui/routers/openai.py#L528-L570)
- [连接验证源码](https://github.com/open-webui/open-webui/blob/01f4282f1ffe0d6212f58d3afbeae21fffd0c4be/backend/open_webui/routers/openai.py#L795-L870)
- [OpenAI-compatible 接入文档](https://docs.openwebui.com/getting-started/quick-start/connect-a-provider/starting-with-openai-compatible/)

### 能力标注

Workspace Model 的能力是可编辑元数据，界面提供 Vision、File Upload、Image Generation、Builtin Tools 等复选项。源码没有在保存这些复选项时执行相应能力探测。

- [能力配置界面源码](https://github.com/open-webui/open-webui/blob/01f4282f1ffe0d6212f58d3afbeae21fffd0c4be/src/lib/components/workspace/Models/Capabilities.svelte#L9-L98)
- [模型元数据结构](https://github.com/open-webui/open-webui/blob/01f4282f1ffe0d6212f58d3afbeae21fffd0c4be/backend/open_webui/models/models.py#L67-L75)

### 图片生成

Open WebUI 把图片生成作为独立设置：用户选择图片引擎，并单独配置图片 API Base URL、Key 和模型。它没有要求图片模型先通过聊天工具调用测试。

- [OpenAI 图片生成配置](https://docs.openwebui.com/features/chat-conversations/image-generation-and-editing/openai/)
- [图片生成故障排查与独立配置项](https://docs.openwebui.com/troubleshooting/image-generation/)

## 2. LobeHub

LobeHub 是与 SheJane 当前问题最接近的实现。

### NewAPI 与模型发现

它内置了 NewAPI Provider，但没有写死聊天模型列表，而是打开远程模型拉取能力。也就是说，中转站的 `/models` 被视为候选目录。

- [NewAPI Provider 源码](https://github.com/lobehub/lobehub/blob/5b4cef6e5f57080747906af1dc6d5e38e5cb432b/packages/model-bank/src/modelProviders/newapi.ts#L3-L18)

### 用户选择类型和能力

用户添加模型时可以选择 `chat`、`embedding`、`tts`、`asr`、`image`、`video` 等类型，并勾选 Function Call、Vision、Reasoning、Search、Image Output 等能力。

- [自定义模型表单源码](https://github.com/lobehub/lobehub/blob/5b4cef6e5f57080747906af1dc6d5e38e5cb432b/src/routes/%28main%29/settings/provider/features/ModelList/CreateNewModelModal/Form.tsx#L41-L193)
- [模型类型和能力结构](https://github.com/lobehub/lobehub/blob/5b4cef6e5f57080747906af1dc6d5e38e5cb432b/packages/model-bank/src/types/aiModel.ts#L15-L84)

LobeHub 还会根据官方模型库和模型名关键字推测能力，但允许用户覆盖。其界面文案明确提醒：打开能力开关只会启用对应功能，模型是否真正支持仍需用户自行确认。

- [能力推测源码](https://github.com/lobehub/lobehub/blob/5b4cef6e5f57080747906af1dc6d5e38e5cb432b/packages/model-runtime/src/utils/modelParse.ts#L14-L219)
- [能力开关警告文案](https://github.com/lobehub/lobehub/blob/5b4cef6e5f57080747906af1dc6d5e38e5cb432b/locales/zh-CN/modelProvider.json#L267-L296)

### 检测和选择器

连接检测只从 `chat` 类型中选择一个模型，发送普通 `hello` 并判断是否收到结果，不检测 Function Call、Vision 或 Image Output。模型选择器则显示视觉、图片输出、工具调用等能力标签。

- [普通聊天连接检测源码](https://github.com/lobehub/lobehub/blob/5b4cef6e5f57080747906af1dc6d5e38e5cb432b/src/routes/%28main%29/settings/provider/features/ProviderConfig/Checker.tsx#L84-L169)
- [模型选择器能力标签源码](https://github.com/lobehub/lobehub/blob/5b4cef6e5f57080747906af1dc6d5e38e5cb432b/src/components/ModelSelect/index.tsx#L56-L176)
- [人工增加、隐藏和标注能力的部署文档](https://github.com/lobehub/lobehub/blob/5b4cef6e5f57080747906af1dc6d5e38e5cb432b/docs/self-hosting/advanced/model-list.zh-CN.mdx#L12-L69)

LobeHub 的优点是灵活，缺点是能力开关属于用户声明，并不证明中转站真的能透传相应协议。

## 3. Dify

### 模型类型与配置方式

Dify 将模型 Provider 分为三种配置方式：官方预置、从远端拉取、用户逐模型配置。用户自定义模型需要明确模型名称、Base URL 和模型类型，而不是把一个 `/models` 列表中的所有 ID 都按 LLM 处理。

- [Dify Model Specs](https://docs.dify.ai/en/develop-plugin/features-and-specs/plugin-types/model-designing-rules)
- [自定义模型接入方式](https://docs.dify.ai/en/develop-plugin/features-and-specs/advanced-development/customizable-model)

Dify 的 OpenAI-compatible 官方插件要求用户为具体模型选择 LLM、Embedding、Rerank、Speech-to-text 或 TTS 等类型。LLM 还可以分别选择函数调用格式、是否支持流式函数调用以及是否支持视觉输入。

- [OpenAI-compatible 模型类型配置源码](https://github.com/langgenius/dify-official-plugins/blob/7e980f012773c4914a35c78e016923713818d65c/models/openai_api_compatible/provider/openai_api_compatible.yaml#L1-L25)
- [函数调用、流式工具和视觉能力配置源码](https://github.com/langgenius/dify-official-plugins/blob/7e980f012773c4914a35c78e016923713818d65c/models/openai_api_compatible/provider/openai_api_compatible.yaml#L321-L386)

### 验证边界

Dify 把 Provider 凭证验证和用户自定义模型的逐模型验证分开。规范建议使用轻量请求验证凭证和模型是否可访问，并不要求每种模型都完成 Agent 工具闭环。

- [Model API 验证规范](https://docs.dify.ai/en/develop-plugin/features-and-specs/plugin-types/model-schema)

### 图片生成

Dify 的 Model Provider 类型不包含图片生成模型；图片生成属于 Tool Plugin 场景，由普通 Agent LLM 选择并调用图片工具。

- [Tool Plugin 规范](https://docs.dify.ai/en/develop-plugin/dev-guides-and-walkthroughs/tool-plugin)
- [官方 OpenAI 图片工具](https://marketplace.dify.ai/plugin/langgenius/openai_tool)

## 4. Cherry Studio

Cherry Studio 支持自动获取服务商模型列表，但不会把列表中的所有模型自动启用。用户需要在模型管理中点击 `+`，加入后的模型才出现在模型选择器；自定义服务商也允许手工填写模型 ID。

- [模型服务设置](https://docs.cherry-ai.com/docs/en-us/pre-basic/settings/providers)
- [自定义服务商](https://docs.cherry-ai.com/cherry-studio-wen-dang/en-us/pre-basic/providers/zi-ding-yi-fu-wu-shang)

它的连接检查默认使用最后加入的聊天模型，因此仍是聊天连通性检查，不是图片、Embedding、Vision 和工具调用的统一验证。FAQ 允许用户在模型设置中手动修正视觉、Embedding 等分类，并明确提醒聊天、Embedding、绘图模型的请求方式和返回结构不同，不能互相强制套用。

- [模型能力分类 FAQ](https://docs.cherry-ai.com/cherry-studio-wen-dang/en-us/questions-and-feedback/faq)

Cherry Studio 还提供独立的 AI 绘画面板，这与聊天模型选择器是不同的产品入口。

## 5. LibreChat

LibreChat 的 Custom Endpoint 可以选择是否调用远端模型列表。`models.fetch` 默认关闭，管理员可以直接提供 `models.default`；拉取失败时也使用人工列表作为回退。

- [Custom Endpoint 模型配置](https://www.librechat.ai/en/docs/configuration/librechat_yaml/object_structure/custom_endpoint)

Model Specs 允许管理员维护一份面向用户的模型清单，设置默认模型、排序、分组以及是否强制只显示这份清单。官方兼容矩阵明确说明 Vision 和工具调用取决于具体模型与 Provider 的兼容程度。

- [Model Specs](https://www.librechat.ai/docs/configuration/librechat_yaml/object_structure/model_specs)
- [兼容性矩阵](https://www.librechat.ai/docs/compatibility)

LibreChat 将图片生成和编辑做成 Agent Tool。图片工具拥有自己的模型、API Key 和 Base URL，聊天模型只负责决定何时调用它。

- [图片生成与编辑工具](https://www.librechat.ai/docs/features/image_gen)

## 共同模式

五个产品的共同做法可以概括为：

```text
连接服务
→ 获取或手填候选模型
→ 用户选择要启用的模型
→ 声明模型类型和能力
→ 在对应使用场景中调用
```

它们普遍接受两件事：

- `/models` 只能证明有哪些模型 ID，不能证明工具、视觉或生图能力；
- 用户声明能力可能不准确，真正可用性最终仍由实际请求决定。

LobeHub 最接近“让用户手动选择用途”的方案；Dify 的类型和能力结构最清晰；LibreChat 和 Dify 对图片生成的边界最稳妥。

## 对 SheJane 的建议

不建议完全照搬任何一个产品。SheJane 是 Agent 产品，主模型一旦错误声明工具能力，会直接导致运行失败，因此可以在上述模式上多保留一道验证。

推荐流程（已在 SheJane 实现）：

1. **连接检查**：只验证 Base URL、Key 和模型列表，失败时仍允许手填模型 ID。
2. **候选模型**：第三方 `/models` 返回的条目默认只标记为“已发现”，不自动进入对话选择器。
3. **用户选择能力**：同一模型可分别声明“Agent 对话”“图片理解”“图片生成”“图片编辑”，不再强制单选用途。
4. **按能力验证**：Agent 对话继续使用现有流式工具闭环；图片理解发送最小图片输入；图片生成和编辑分别调用对应图片接口。
5. **按用途展示**：只有通过 Agent 验证的模型进入主模型选择器；图片生成模型进入工具/媒体配置，不与 Agent 主模型混列。

失败模型可以保存为“已配置、未验证”，但不能作为对应能力投入运行。这样既保留 LobeHub/Cherry Studio 的中转站灵活性，也保留 SheJane 比纯聊天客户端更需要的运行安全。

当前实现支持 OpenAI-compatible 图片生成和编辑、显式默认能力绑定、Run 快照、Artifact 持久化与对话内展示。视频、供应商异步任务轮询和通用媒体工作流仍留到出现真实需求后再扩展。

## 核对版本

| 项目 | 核对点 |
| --- | --- |
| Open WebUI | `main`：`01f4282f1ffe0d6212f58d3afbeae21fffd0c4be`，提交时间 2026-07-27 |
| LobeHub | `main`：`5b4cef6e5f57080747906af1dc6d5e38e5cb432b`，提交时间 2026-07-23 |
| Dify Official Plugins | `main`：`7e980f012773c4914a35c78e016923713818d65c`，提交时间 2026-07-25 |
| Cherry Studio | `main`：`d2b1e7ebf094df07f3c89e6893149792451d0839`，提交时间 2026-07-27 |
| LibreChat | `main`：`a53936d27351e798d320df8f717be3f2272fc49d`，提交时间 2026-07-27 |
