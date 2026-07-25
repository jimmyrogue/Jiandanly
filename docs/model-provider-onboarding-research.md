# 面向非技术用户的模型 Provider 接入方案调研

> 核验日期：2026-07-24
>
> 范围：首次选择 Provider、授权或 API Key、模型发现、连接验证、错误修复、默认模型、费用与隐私提示
>
> 来源约束：产品事实只引用官方文档、官方站点或官方源码；“对 SheJane 的建议”均为产品推断，不表示外部产品已经这样实现

## 结论

对 SheJane 最合适的不是继续扩充“供应商配置表单”，而是提供三条清晰路径：

1. **在线模型（推荐）：使用 OpenRouter 登录授权。** 用户不接触 API 地址、模型 ID 和 API Key；SheJane 通过 OAuth PKCE 获得一把用户控制的 Key，保存到现有系统凭据库。
2. **本地模型：自动检测 Ollama 和 LM Studio。** 检测到服务后直接列出兼容模型；未检测到时才显示安装或启动指引。
3. **其他供应商：保留直接 API Key 接入作为高级选项。** 内置供应商只显示“去官网获取 Key”和“粘贴 Key”，隐藏固定 API 地址，并在继续时自动验证、发现模型。

接入成功后只给普通用户三个经过兼容性过滤的选择：**推荐、速度优先、能力优先**。完整模型列表、Base URL、手动模型 ID、Token 上限放进高级设置。

这保留了 SheJane 的现有原则：Runtime 持有 Provider 配置和系统凭据、每次 Run 提交具体 `local:<provider>:<model>`、不做静默供应商回退，也不把秘密交给 Client。

## 一、已验证的代表性方案

### 1. OpenRouter：把复制 API Key 变成一次登录授权

#### 已验证事实

- OpenRouter 官方为第三方应用提供 OAuth PKCE，用户可“一键连接”；推荐使用 `S256` challenge。授权后应用用 code 和 verifier 换取一把用户控制的 API Key。[OpenRouter OAuth PKCE](https://openrouter.ai/docs/guides/overview/auth/oauth)
- OAuth 官方文档明确支持任意端口的 localhost callback，适合绑定临时回调端口的本地优先应用。[OpenRouter OAuth PKCE: Localhost Apps](https://openrouter.ai/docs/guides/overview/auth/oauth#localhost-apps)
- `GET /api/v1/key` 可以验证当前 Key，并返回是否为免费层、使用量、额度上限、剩余额度、重置周期和过期时间；不需要发送一次付费模型请求。[OpenRouter Get current API key](https://openrouter.ai/docs/api/api-reference/api-keys/get-current-key)
- OpenRouter 的 Models API 提供统一模型目录，可按工具参数支持、输出模态、价格、吞吐、延迟和热度筛选或排序，并返回定价等标准化元数据。[OpenRouter Models](https://openrouter.ai/docs/guides/overview/models)
- OpenRouter 对常见错误提供稳定分类：`401` 凭据、`402` 余额、`403` 权限、`429` 限流、`502/503` 上游不可用，并在适用时返回 `Retry-After`。[OpenRouter Errors and Debugging](https://openrouter.ai/docs/api/reference/errors-and-debugging)
- OpenRouter 说明：默认不保存 prompt/response，除非用户主动开启日志或数据使用；请求元数据仍会保存。模型请求还会经过实际模型 Provider，因此不能只展示 OpenRouter 一层隐私说明。[OpenRouter Data Collection](https://openrouter.ai/docs/guides/privacy/data-collection)
- OpenRouter 采用预付 credits，不同模型价格不同；其 FAQ 说明购入 credits 有费用，使用自己的上游 BYOK 也有单独收费规则。[OpenRouter FAQ](https://openrouter.ai/docs/faq)

#### 对 SheJane 的判断

这是当前最接近“普通用户无需理解 Provider”的云端方案。它的优势不是 Provider 数量，而是：

- 用户只理解“登录并连接”，不需要理解 Base URL、API 协议和模型 ID；
- 一次授权即可访问多个厂商模型；
- 可以在保存前无费用验证 Key，并显示剩余额度；
- 模型 API 已包含做“推荐 / 快速 / 强大”筛选所需的大部分数据。

代价也必须明确：

- 用户仍需注册 OpenRouter，并可能充值；
- 请求经过聚合服务和实际模型 Provider；
- SheJane 不能把 OpenRouter 表述成“自己的免费模型”；
- OAuth 返回的仍是长期秘密，必须由 Runtime 直接写入系统凭据库，不能进入 Renderer、localStorage、日志或诊断包。

### 2. Cherry Studio：直接供应商接入里最成熟的低门槛表单

#### 已验证事实

- Cherry Studio 对内置 Provider 默认隐藏 API 地址，普通情况下只填写 Key；自定义 Provider 才要求处理地址。[Cherry Studio Model Service Settings](https://docs.cherry-ai.com/docs/en-us/pre-basic/settings/providers)
- API Key 输入框旁有连通性检查；模型管理可以从 Provider 自动拉取列表，再由用户添加需要的模型。[Cherry Studio Model Service Settings](https://docs.cherry-ai.com/docs/en-us/pre-basic/settings/providers)
- 各 Provider 有单独的图文教程，包含“去哪里创建 Key—复制—回到应用粘贴—添加模型”的完整路径。例如 Gemini 指引直接链接官方 Key 页面。[Cherry Studio Gemini setup](https://docs.cherry-ai.com/docs/en-us/pre-basic/providers/google-gemini)
- Cherry Studio 的隐私说明把应用本地处理与第三方 Provider 处理分开：软件不收集 API Key 和对话，但用户仍需查看所选 Provider 的政策。[Cherry Studio Privacy Policy](https://docs.cherry-ai.com/about/privacypolicy)

#### 对 SheJane 的判断

值得复制的不是大量 Provider 卡片，而是四个细节：

1. 内置 Provider 不暴露固定地址；
2. 每个 Provider 都有直接的“获取 Key”入口；
3. Key 验证与模型发现是同一流程；
4. 自定义兼容接口才进入高级模式。

Cherry Studio 仍要求“检查—管理模型—添加—启用”多个动作。SheJane 可以再少一步：用户点击“继续”时自动验证并发现，推荐模型默认勾选但要求用户确认。

### 3. Open WebUI：自动发现优先，手动模型 ID 只作回退

#### 已验证事实

- Open WebUI 将通用接入收敛成 URL + API Key，并从大多数 Provider 自动发现模型，模型随后直接出现在选择器中。[Open WebUI Connect a Provider](https://docs.openwebui.com/getting-started/quick-start/connect-a-provider/)
- URL 输入会提示常见 Provider endpoint；只有 Provider 不支持 `/models` 时才要求填写 Model ID allowlist。[Open WebUI OpenAI-Compatible](https://docs.openwebui.com/getting-started/quick-start/connect-a-provider/starting-with-openai-compatible/)
- 官方文档也指出聚合服务可能返回数量过大的模型列表，例如 OpenRouter，因此建议过滤，而不是把全部模型直接塞进普通选择器。[Open WebUI OpenAI-Compatible](https://docs.openwebui.com/getting-started/quick-start/connect-a-provider/starting-with-openai-compatible/)

#### 对 SheJane 的判断

“自动发现是正常路径，手动 ID 是兼容性回退”应成为 SheJane 的固定规则。当前 SheJane 已有自动发现 API，但 UI 仍让用户主动点击“获取模型”，且初始状态默认显示手动模型 ID；这会把高级回退误呈现为正常流程。

Open WebUI 的 URL + Key 方案本身仍偏管理员工具。SheJane 应只在“自定义服务”中展示 URL；OpenAI、Anthropic、DeepSeek、OpenRouter 等内置模板不让普通用户编辑固定 endpoint。

### 4. Dify：验证成功后再启用，预定义模型自动可用

#### 已验证事实

- Dify 的 Provider 卡片在保存前检查 Key，凭据有效后 Provider 才可用。[Dify Model Providers](https://docs.dify.ai/en/cloud/use-dify/workspace/model-providers)
- 对预定义模型，连接 Provider 后模型立即可用；只有新模型、微调模型或目录中不存在的模型才手动添加。[Dify Model Providers](https://docs.dify.ai/en/cloud/use-dify/workspace/model-providers)
- Dify Cloud 还能先提供平台 credits、免 Key 使用，再允许用户添加 BYOK；默认模型是单独的明确选择。[Dify Quick Start](https://docs.dify.ai/en/quick-start)

#### 对 SheJane 的判断

“无效配置不能进入已连接状态”和“手动模型只服务未知模型”值得采用。Dify 的平台 credits 路径只有在 SheJane 将来愿意运营账户、额度、计费和客服体系时才适用；现在不应为了省一次粘贴 Key 引入自己的模型网关。

### 5. AnythingLLM：本地模型真正可以不需要 Key

#### 已验证事实

- AnythingLLM Desktop 的公开流程是：下载应用，选择模型或让应用根据硬件自动推荐，然后直接使用；本地路径不要求账户或 API Key。[AnythingLLM official site](https://anythingllm.com/)
- 官方明确把“模型可由用户选，也可由产品依据硬件推荐”作为 onboarding 的第二步。[AnythingLLM official site](https://anythingllm.com/)

#### 对 SheJane 的判断

这是普通用户最容易理解的本地路径：不先讲 Ollama、量化、参数规模和端口，而是先回答“这台电脑能跑什么”。

SheJane 当前没有自带模型 Runtime，因此不应假装可以做到相同的一体化体验。近期可以实现“检测已安装的 Ollama / LM Studio”；只有当用户需求证明安装仍是主要阻力时，再考虑托管下载和硬件推荐。

### 6. Ollama 与 LM Studio：本地服务可以被可靠检测

#### 已验证事实

- Ollama 安装后默认在 `http://localhost:11434/api` 提供 API；`GET /api/tags` 返回本机已有模型，`POST /api/show` 返回模型能力和上下文等详情。[Ollama API introduction](https://docs.ollama.com/api/introduction) [Ollama list models](https://docs.ollama.com/api/tags) [Ollama show model details](https://docs.ollama.com/api-reference/show-model-details)
- LM Studio 默认本地服务地址为 `http://localhost:1234`；用户可在 Developer 页面用开关启动服务。[LM Studio server](https://lmstudio.ai/docs/developer/core/server) [LM Studio quickstart](https://lmstudio.ai/docs/developer/rest/quickstart)
- LM Studio 的本地模型 API 能列出模型、可读名称、大小、上下文、Vision 和 Tool Use 能力，并提供加载与下载 endpoint；同时也提供 OpenAI-compatible `/v1/models`。[LM Studio list models](https://lmstudio.ai/docs/developer/rest/list) [LM Studio load model](https://lmstudio.ai/docs/developer/rest/load) [LM Studio download model](https://lmstudio.ai/docs/developer/rest/download) [LM Studio OpenAI-compatible models](https://lmstudio.ai/docs/developer/openai-compat/models)

#### 对 SheJane 的判断

本地模型不应使用“添加自定义 OpenAI Provider”的文案。应直接探测两个固定 loopback 地址，并给出状态：

- `已发现 Ollama · 3 个兼容模型`
- `已发现 LM Studio，但服务未启动 · 打开 LM Studio`
- `未安装本地模型服务 · 了解本地模型`

模型进入 SheJane 前仍需检查 `streaming` 和 `tool_calling`，因为 SheJane 是 Agent 产品，不是任何能聊天的模型都能正常工作。

## 二、SheJane 当前状态

以下来自当前仓库，不是互联网结论：

- [`ModelProvidersSettings.tsx`](../client/src/features/settings/ModelProvidersSettings.tsx) 已内置 OpenAI、OpenRouter、DeepSeek、Anthropic 和两个自定义模板。
- 内置模板已经有固定 Base URL，但 UI 仍把 URL 作为必填可编辑字段展示。
- 新建 Provider 时先显示手动模型 ID；用户填 Key 后还要主动点击“获取模型”、勾选模型、再保存。
- Runtime 已有 `POST /v1/model-providers/discover-models`，会用 Key 调 Provider 的模型目录；Provider 已保存时还可从系统凭据库读取 Key。
- 对已知 Provider，Runtime 已使用 [Models.dev](https://github.com/anomalyco/models.dev) 补全能力和 Token 限制。Models.dev 官方数据还包含价格、输入模态、Tool Use、推理、上下文和状态，可继续服务模型筛选。[Models.dev API and schema](https://github.com/anomalyco/models.dev#api)
- Provider Key 已进入操作系统凭据库；Client 只看到是否配置，不看到秘密。
- Chat 只提交具体 Runtime model spec；当前 catalog 变化后，Client 以第一个可用模型替换失效选择，但没有独立的“接入完成后确认默认模型”步骤。

因此，基础能力大部分已经存在。主要问题是**交互顺序仍以配置字段为中心，而不是以“连接成功并开始对话”为中心**。

## 三、推荐的产品方案

以下全部是对 SheJane 的产品建议。

### 1. 首次进入：只问“你想怎样使用模型？”

```text
连接模型

[ 在线模型（推荐） ]
登录一次，可选择多家公司的模型
连接 OpenRouter

[ 本地模型 ]
数据在本机处理，不需要 API Key
检测本地模型

其他服务
OpenAI · Anthropic · DeepSeek · 自定义服务
```

不要在第一屏出现 Provider 类型、Base URL、模型 ID、上下文 Token 或最大输出 Token。

### 2. OpenRouter 主路径

1. 点击“连接 OpenRouter”。
2. Runtime 创建高熵 verifier、`S256` challenge 和单次 pending state。
3. 系统浏览器打开 OpenRouter 授权页。
4. loopback 临时端口或经过验证的 app deep link 收到 callback。
5. Runtime 校验 state，交换 code，把 Key 直接写进现有系统凭据库。
6. 调 `/api/v1/key` 验证身份、有效期和额度；再拉模型目录。
7. 只显示符合 SheJane 强约束的模型，用户明确选择一个默认模型。
8. 显示“已连接、默认模型、费用入口、隐私入口”，然后进入对话。

OAuth transaction、verifier 和 callback 只能属于 Runtime；Renderer 只接收有限状态：`waiting_for_browser | validating | choosing_model | connected | failed`。

### 3. 直接 Provider 路径

内置 Provider 表单只保留：

```text
OpenAI
需要在 OpenAI 官网创建 API Key，费用由 OpenAI 收取。

[ 去 OpenAI 获取 Key ]
[ 粘贴 API Key                  ]

[ 继续 ]
```

点击继续后自动完成：

1. 格式化但不记录 Key；
2. 调用无费用的凭据或模型目录 endpoint；
3. 发现可用模型；
4. 过滤不支持 Streaming / Tool Use 的模型；
5. 进入默认模型确认。

Base URL、Provider 协议、手动模型 ID 和 Token 上限只在“自定义服务 / 高级设置”出现。

### 4. 模型选择

不要展示未经筛选的数百或数千个模型。普通视图只提供：

| 选择 | 排序依据 | 显示信息 |
| --- | --- | --- |
| 推荐 | SheJane 维护的兼容清单；必须支持 streaming + tool calling | 一句话用途、图片能力、费用等级 |
| 速度优先 | 兼容清单中低延迟模型 | “回复更快，复杂任务能力较弱” |
| 能力优先 | 兼容清单中高能力模型 | “复杂任务更稳，通常更慢、更贵” |

“全部模型”放到二级页面。推荐不是运行时自动路由：用户确认后仍保存一个具体 `local:<provider>:<model>`；运行失败时不得静默切换供应商。

模型数据优先级建议为：

1. 实际 Provider 模型目录决定“用户能否访问”；
2. SheJane 的兼容性清单决定“能否作为 Agent 主模型”；
3. Provider 元数据或 Models.dev 决定价格、上下文、图片和工具能力展示；
4. 未知模型只进入高级列表，不默认声称支持工具。

### 5. 连接测试与错误修复

测试结果不能只显示原始 HTTP 文本：

| 分类 | 用户文案 | 主操作 |
| --- | --- | --- |
| 凭据无效 | `这个 Key 无效或已被撤销` | `重新填写` / `去官网创建 Key` |
| 余额不足 | `账户余额不足，Provider 暂时不会响应` | `前往充值` |
| 权限不足 | `Key 有效，但没有使用该模型的权限` | `选择其他模型` |
| 限流 | `请求过于频繁，可在 42 秒后重试` | 倒计时后 `重试` |
| Provider 暂时不可用 | `模型服务暂时不可用` | `重试`，不静默切换 |
| 模型不兼容 | `这个模型不能使用 SheJane 的工具` | `选择推荐模型` |
| 本地服务未运行 | `已安装 LM Studio，但服务没有启动` | `打开 LM Studio` |
| 无法连接自定义地址 | `无法连接该地址` | 展示 DNS/TLS/代理的高级详情 |

详情区可以保留 request ID、HTTP status 和原始 Provider message，供诊断使用，但不能把它作为普通用户唯一可见的答案。

### 6. 费用与隐私

连接前至少说清楚四件事：

- **谁收费**：OpenRouter 或用户直接选择的 Provider；
- **何时收费**：发送模型请求时，而不是仅保存配置时；
- **数据发到哪里**：云端路径会发送给聚合服务和/或实际模型 Provider；本地路径只应在确实使用 loopback 时称为本地；
- **Key 存在哪里**：操作系统凭据库，不进入对话、插件或诊断导出。

OpenRouter 接入完成后可显示 Key 的剩余额度和重置周期；模型选择显示“低 / 中 / 高”费用等级，详情再显示官方的精确定价与更新时间。不要把可能变化的价格硬编码进 Client。

## 四、实施优先级

### P0：先改现有流程，不增加新的 Provider 平台

1. Provider 空状态改成三条路径。
2. 内置 Provider 隐藏 Base URL。
3. 粘贴 Key 后“继续”自动执行验证与模型发现，删除普通路径中的“获取模型”步骤。
4. 手动模型 ID 移入高级设置。
5. 添加显式默认模型确认和结构化错误修复。

这些变化复用现有 Provider CRUD、discover endpoint、Models.dev 目录和系统凭据库。

### P1：OpenRouter OAuth PKCE

新增的核心能力只有：

- Runtime-owned PKCE transaction；
- 安全 callback；
- code exchange；
- `/api/v1/key` 验证；
- Key 写入现有 credential store。

不需要新的通用 OAuth 框架，也不需要 SheJane 自建模型代理。

### P2：本地自动检测

先只探测 Ollama 和 LM Studio 的 loopback 默认端口，读取已安装模型并过滤 Agent 兼容性。不要在没有数据证明前同时建设模型下载器、硬件评分器和自带推理 Runtime。

### 暂不做

- 不先扩充几十个 Provider 卡片；
- 不让普通用户配置 Base URL、模型 ID 和 Token 上限；
- 不建立 SheJane 自有 credits、计费和模型网关；
- 不做跨 Provider 自动路由或静默 fallback；
- 不把第三方模型目录中的 `tool_calling=true` 当成无需验证的质量保证；
- 不把“本地应用”误写成“数据不出本机”，云端模型仍会收到内容。

## 五、成功标准

面向普通用户的首个版本可以用以下结果验收：

- 新用户无需理解 Base URL 或模型 ID；
- OpenRouter 路径从点击到可对话只需要一次外部授权和一次默认模型确认；
- 直接 Provider 路径只需要获取并粘贴 Key；
- 本地服务运行时能自动出现，不要求用户手抄端口；
- 无效 Key、余额不足、模型不兼容和本地服务未启动都有明确下一步；
- API Key 从未进入 Renderer 持久化、日志、插件输入或诊断包；
- 每个 Run 仍绑定一个用户明确确认的具体模型，不产生静默供应商切换。
