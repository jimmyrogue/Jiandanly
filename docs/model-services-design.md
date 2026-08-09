# 模型服务连接设计

> 状态：已实施
>
> 更新时间：2026-07-25

## Runtime 阶段

```text
主要阶段：P3.4 接纳检查
上游输入：P2 提交的具体 Connection + Model 选择
下游输出：P6 可绑定的冻结模型配置与凭据引用
状态所有者：Runtime SQLite（连接、模型目录、兼容结果）+ 操作系统凭据库（API Key）
替换的当前路径：/v1/model-providers、local_model_providers、ModelProvidersSettings
```

P3.4 只验证并冻结已经存在的连接、模型和能力，不在接纳 Run 时联网探测。
P6 临时读取凭据并建立本次模型连接；API Key 不进入 Run 快照、Client、日志或诊断导出。

## 要解决的问题

用户要完成的是“连接一个可以使用的模型服务”，而不是配置 Provider、协议、Base URL
和 Token 上限。现有实现虽然已经具备凭据库、模型发现和两种模型协议，但把这些实现细节
作为普通表单字段暴露给了用户。

新模块在一个小接口后负责：

- 官方服务入口、国内站与国际站；
- API Key 保存和重新连接；
- OpenAI Chat / Anthropic Messages 协议选择；
- 自定义服务协议探测；
- 模型发现、本地缓存、内置目录兜底和手动模型；
- 推荐与未验证状态；
- 厂商错误归一；
- Run 创建前的能力接纳。

Client 只展示连接流程和 Runtime 返回的状态，不复制厂商规则。

## 产品范围

首批官方服务：

1. DeepSeek
2. Kimi
3. 千问（阿里云百炼）
4. GLM
5. MiniMax
6. 硅基流动
7. 连接已有服务

第一版执行 Adapter：

- `openai_chat`
- `anthropic_messages`

`openai_responses` 是下一优先级，但第一版不提供空 Adapter、隐藏入口或兼容别名。
第一版不做动态路由、本地模型、SheJane 自有额度、计费或模型网关。

## 核心模型

### Connection

代码使用 `ModelConnection`，用户界面统一称“模型服务”。

```text
id                    Runtime 生成的稳定 ID
preset_id             shejane-official | deepseek | kimi | qwen | glm | minimax | siliconflow | custom
name                  用户可识别名称
region                cn | intl | custom | official
adapter_id            openai_chat | anthropic_messages
base_url              Runtime 保存；官方 preset 固定，自定义服务才由用户输入
connection_method     api_key | browser_authorization；Runtime preset 决定
credential_ref        只保存在 Runtime；引用操作系统凭据库
credential_configured 只返回布尔值
catalog_status        ready | stale | unavailable
version               凭据或执行连接配置有效修改时递增
```

同一家模型服务可以有多个 Connection。国内站和国际站是独立 Connection，不共享
API Key，也不自动切换。

### Compatibility Profile

兼容性不是 Provider 品牌的属性，而是下面这组键的结果：

```text
connection_id + connection_version + exposed_model_id
```

第一版 Profile 只保存当前执行真正需要的字段：

```text
model_id
display_name
source                bundled | discovered | manual
verification          verified | unverified
recommended
recommended_for       agent_chat | image_understanding | image_generation | image_editing 的用途列表
streaming
tool_calling
image_inputs
max_input_tokens
max_output_tokens
```

`recommended` 只能来自随应用发布并通过 SheJane 测试的内置目录。厂商 `/models`
新发现的模型进入“更多模型”，不会自动成为默认模型。手动模型在兼容性测试前保持
`unverified`，不进入默认推荐。

后续确有两个以上 Adapter 需要共享厂商差异时，再在模型调用 seam 内增加有限的
请求/响应 hook；第一版不建立 Provider 子类树。

## Runtime 接口

旧 `/v1/model-providers*` 接口全部删除，不保留别名。

```text
GET    /v1/model-services/presets
GET    /v1/model-services
POST   /v1/model-services
POST   /v1/model-services/shejane/authorization
GET    /v1/model-services/shejane/authorization/{authorization_id}
POST   /v1/model-services/import
PUT    /v1/model-services/{connection_id}/credential
DELETE /v1/model-services/{connection_id}
POST   /v1/model-services/{connection_id}/refresh
POST   /v1/model-services/{connection_id}/models
POST   /v1/model-services/{connection_id}/models/{model_id}/verify
GET    /v1/models
```

创建官方 Connection 只接收 `preset_id`、`region` 和 `api_key`。官方 URL、协议、
帮助链接和目录策略由 Runtime preset 决定，Client 无权覆盖。

更新凭据只接收新的 `api_key`。Runtime 先用新 Key 检查当前连接，再原位替换系统
凭据；检查或保存失败时保留旧 Key。

创建自定义 Connection 接收名称、地址和 API Key。Runtime 先做非计费的目录探测，
自动选择 Adapter；只有探测失败时，Client 才显示高级协议选择。两个目录接口都通过时，
第一版固定选择生态覆盖更广的 OpenAI Chat，用户仍可在高级设置中明确选择。

## 连接流程

### SheJane 官方服务

1. Runtime 在 `127.0.0.1` 动态端口监听固定路径 `/shejane/auth/callback`，生成一次性
   `state` 和 PKCE S256 verifier/challenge。
2. Client 只接收 Runtime 返回的授权 URL，并通过 Electron 打开系统浏览器；Client
   不生成安全参数，也不能提交或覆盖 Cloud 地址。
3. Runtime 严格校验 callback、一次性消费 `state`，再用 `code`、原 redirect URI 和
   verifier 向固定 Cloud origin 交换 inference token。
4. inference token 只写入操作系统凭据库。SQLite 和 Client 只得到普通
   `ModelServiceConnection` 与 `credential_configured`。
5. Runtime 从同一固定 origin 拉取 `/v1/models`，信任 Cloud 返回的 `capabilities` 和
   `recommended_for`，自动按用途归类和推荐模型；这些声明只对固定官方 origin 生效，
   BYOK 或自定义服务不能借此绕过现有验证。
6. 授权和目录同步不运行兼容性探针。Agent 对话模型同步后即可使用；若用户尚未选择图片
   能力模型，Runtime 分别为图片生成和图片编辑绑定官方目录中的推荐模型，没有推荐项时
   使用对应能力的第一个官方模型。已有用户绑定不会被覆盖。
7. 拒绝、超时、state 不匹配、交换失败和响应丢失均为终态；不回退到 BYOK Key，也不
   接受浏览器返回的服务地址。

SheJane 官方服务不能通过普通 `/v1/model-services` API Key 接口创建、导入或替换凭据。
导出文件保留它的非秘密连接元数据，但导入时跳过该连接并要求重新完成浏览器授权。本地
删除连接只删除本机凭据；Cloud 设备撤销由 Cloud 设备页完成。

### 厂商官方服务（BYOK）

1. Client 显示厂商说明和“获取 API Key”官方链接。
2. 系统浏览器完成注册、登录、实名认证、充值和创建 Key。
3. 用户主动粘贴 API Key；SheJane 不自动读取剪贴板。
4. Runtime 保存 Key 到操作系统凭据库。
5. Runtime 尝试刷新模型目录；失败时使用内置目录并仍然完成连接。
6. Client 展示 1–3 个推荐模型和“更多模型”。
7. 用户明确选择具体模型。

连接成功后不自动打开兼容性测试。模型服务的“更多”菜单提供只调用推荐文字模型的
“测试连接”，失败只保存诊断结果，不回滚凭据或禁用模型；图片与自定义协议测试保留在
“高级兼容性测试”中，并明确提示真实图片请求可能计费。

目录接口因余额或厂商服务状态失败时仍算连接成功；Client 显示连接状态，实际调用错误
直接提醒用户，并提供厂商控制台入口。

### 连接已有服务

1. 用户填写服务名称、地址和 API Key。
2. Runtime 规范化地址并尝试自动探测 Adapter。
3. 成功时读取模型；模型目录缺失时允许手动填写 Model ID。
4. 用户可跳过付费兼容测试；跳过后模型保持“未验证”。
5. 探测失败时才显示高级协议选择。

## 模型选择和对话

- 每个对话保存具体 `local:<connection_id>:<model_id>`。
- 新对话沿用最近一次成功使用的模型，但发送前始终可见、可修改。
- 模型不存在或 Connection 被删除时，不选择列表第一项，也不静默替换。
- 跨模型服务切换时提示一次：后续上下文会发送给新的模型服务。
- 同一服务内切换模型不额外提示。
- 删除 Connection 不删除旧对话；旧对话显示“未连接”，继续发送时要求重新连接或
  明确选择其他模型。

## 缓存和性能

- Runtime 启动和 Client 进入设置页时不检查所有外部服务。
- 列表先从 SQLite 返回。
- 打开模型选择器时先显示缓存，只异步刷新当前 Connection。
- 刷新不清空当前列表，不触发整页 loading，也不并发请求其他 Connection。
- 目录刷新不改变执行连接版本，也不取消已接纳的 Run。
- 官方模型目录失败时保留内置目录和上一次成功缓存。

## 错误与重试

第一版沿用 Runtime 已有的失败分类，至少区分：

```text
invalid_api_key
insufficient_balance
permission_denied
rate_limited
model_unavailable
provider_unavailable
content_rejected
incompatible_model
```

普通界面显示厂商返回的可操作错误。第一版不建立逐厂商错误表，也不新增模型请求重试；
现有 Run 失败策略继续生效，且绝不切换到其他模型服务。

## 安全、导入导出和诊断

- API Key 只存在于操作系统凭据库。
- 官方注册和登录页使用系统浏览器，不使用内嵌 WebView。
- 导出包含连接名称、地址、Adapter 和模型选择，不包含 API Key。
- 导入后的官方连接要求重新填写 API Key；自定义服务必须手动重建，避免导入文件把
  API Key 引导到未确认的地址。
- 诊断可包含厂商、Adapter、模型 ID、错误码和请求 ID。
- Authorization、Cookie、API Key 和地址中的敏感查询参数必须脱敏。
- 错误、探测和测试结果不自动上报 SheJane。

## 删除策略

项目尚未公开，因此不迁移旧 Provider 数据：

- 删除旧凭据库条目后再删除 `local_model_providers`；
- 删除旧 Provider schema、SDK 方法、Client 表单和文案；
- 新建 `model_connections`；
- 不保留旧 endpoint、类型别名或数据迁移。

运行记录、模型账本和 `local:<connection>:<model>` 的具体选择规则继续保留。

## 验证

Runtime：

- 官方 preset 不允许覆盖 URL 或 Adapter；
- API Key 不进入 SQLite、HTTP 响应、Run 快照、日志和诊断；
- 同厂商多 Connection；
- 自定义服务自动探测与手动回退；
- 内置目录兜底、缓存刷新和手动模型；
- 推荐/未验证/能力接纳；
- 厂商错误映射；
- 删除 Connection 后旧 Run/对话仍可读取。

Client：

- 首次连接六家官方服务；
- 中国站默认、国际站次级入口；
- 普通流程不出现 URL、协议、模型 ID 和 Token 上限；
- 自定义服务失败后才出现高级设置；
- 推荐模型和“更多模型”；
- 不自动读取剪贴板、不静默替换模型；
- 跨服务切换确认；
- 连接删除与重新连接。

验收场景（由 Runtime HTTP、Client 组件与 Electron 关键路径分层覆盖）：

- API Key 连接成功；
- 目录失败时使用内置模型；
- 余额不足连接成功但显示提醒；
- 自定义 OpenAI Chat 与 Anthropic Messages；
- 手动模型保持未验证；
- 失效 Key 的修复入口；
- 删除连接后旧对话保留。
