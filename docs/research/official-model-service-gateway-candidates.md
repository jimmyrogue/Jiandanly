# SheJane 官方模型服务中转底座调研

> 调研日期：2026-07-28。范围：面向邀请内测用户的账号、API Key、模型中转、用量和后续充值；只使用项目官方仓库、官方文档和标准原文。

## 结论

产品方向成立：SheJane Client/Runtime 继续开源、BYOK 和本地能力继续独立可用；SheJane 官方托管模型服务作为一个可选的普通模型服务连接，降低新用户配置门槛并承载后续商业化。

第一选择是 **New API，但必须先解决商业授权和品牌条款**。它已经覆盖用户、Token、用量、额度、支付、OpenAI/Anthropic/Gemini 协议和路由，最接近所需的消费者门户。不要重写网关；只维护一小段 SheJane 原生应用授权扩展。

如果无法取得可接受的 New API 授权，第二选择不是 Sub2API，而是 **LiteLLM 数据面 + SheJane 自有薄账号层**。该路线工程量更大，但边界更清楚。Sub2API 的核心定位是分发 AI 产品订阅额度和共享上游账号，与合法授权的官方模型 API 服务不完全一致。

## 候选比较

| 候选 | 适合之处 | 主要问题 | 结论 |
|---|---|---|---|
| [New API](https://github.com/QuantumNous/new-api) | 用户、Token、额度、用量、支付、OIDC；OpenAI Responses、Claude Messages、Gemini；路由、重试和限流齐全 | AGPLv3 加项目声明的品牌保留限制；去品牌/OEM/闭源 SaaS 需要商业授权；仍需补原生应用 PKCE 授权 | **首选，授权先行，小 fork** |
| [Sub2API](https://github.com/Wei-Shaw/sub2api) | 注册、API Key、用量、并发/限流、支付和管理后台齐全；LGPL-3.0-or-later | 官方定位包含消费订阅 OAuth、共享/拼车和粘性调度；上游账号合规风险高；PostgreSQL + Redis 起步较重 | **不作为 SheJane 官方服务首选** |
| [One API](https://github.com/songquanpeng/one-api) | MIT；用户、Token、额度、模型映射和管理 API；部署简单 | 最新正式 release 和现代协议能力落后于 New API；安全和支付能力需要自己补齐 | **许可证兜底，不是功能首选** |
| [LiteLLM](https://github.com/BerriAI/litellm) | 提供商覆盖、虚拟 Key、预算、用量、路由和观测能力成熟 | 面向普通用户的注册、钱包、充值门户不是其开源核心；部分企业能力另有许可；当前稳定性清单仍包含预算执行问题 | **适合只做数据面，再包薄控制层** |
| [Helicone](https://github.com/Helicone/helicone) | 账号、积分、Stripe、用量和 LLM 观测接近完整产品样板 | 自托管组件多；主仓和独立网关许可证边界需要逐项核对；对 SheJane 而言过重 | **适合参考，不建议作为第一底座** |

## 推荐系统边界

保持同一个账号门户，但分成三条权限和数据通道：

1. **模型网关**：只接收独立的 inference token，负责 `/v1/models`、模型请求、额度、成本和速率限制。
2. **应用授权**：外部浏览器登录，Authorization Code + PKCE，通过随机 loopback 端口回到本机 Runtime；一次性 code 换取模型网关凭据。桌面原生应用不能保存 client secret。[RFC 8252](https://www.rfc-editor.org/rfc/rfc8252.html)
3. **诊断中转**：使用不同的 telemetry token 和 endpoint，只接收用户同意后的脱敏诊断；服务端持有 LangSmith service key。模型 key 不能上传日志。
4. **网站控制台**：管理设备连接、独立 API Key、用量和余额。SheJane 自动创建的设备 key 默认不可再次显示，只允许撤销或轮换。

官方连接进入 Runtime 后仍然是普通 `ModelServiceConnection`：固定 SheJane 官方 base URL，凭据进操作系统 credential store，模型列表只作为候选，继续执行现有真实能力验证。任务仍显式提交 `local:<connection>:<model>`；网关不得静默切换到另一个模型。

## 分阶段路线

### P0：授权与合法上游，3–5 个工作日

- 向 New API 获取书面商业授权报价和允许的品牌/源码方式。
- 只选择允许当前服务地域和 Customer Application/End User 模式的上游；不得购买、出售或转移第三方 API Key。
- 中国大陆公开服务上线前完成生成式 AI 服务、模型备案/登记公示、内容安全、日志、实名、税务和支付的专项评估。

### P1：邀请内测官方连接，2–3 周

- 部署 New API 的稳定固定版本和 PostgreSQL/Redis，后台只接合法取得的服务账号/API Key。
- 增加 `SheJane 官方服务（推荐）`，点击后用系统浏览器登录；Runtime 用 PKCE 和一次性 code 自动取得独立 inference token。
- Runtime 将 token 存入现有系统 credential store，使用固定 base URL 拉取模型并执行现有工具调用、流式和多轮能力验证。
- 网站先提供邀请注册、设备撤销、API Key、用量和赠送额度；暂不开放充值。

### P2：集中诊断，1–2 周

- Runtime 单独换取 telemetry token，显式同意、默认关闭正文，只上传 release/run/attempt、模型类别、工具名、耗时、token 数和脱敏错误。
- 诊断中转再写入 LangSmith；模型网关只保留计费和请求 ID，不复制 Agent prompt、tool result 或本地文件内容。
- Electron/启动器/更新器/native crash 仍使用独立 crash reporter，LangSmith 只覆盖 Agent/Runtime 执行树。

### P3：充值与正式运营，3–5 周并在合规确认后开始

- 建立不可变余额流水、价格快照、请求预留/结算、退款、对账和告警。
- 支付 webhook 必须验签、幂等、可重放审计；加入风控、欠费 fail-closed、额度和速率限制。
- 再开放充值、发票/税务和客服流程，不直接把开源项目自带支付页视为生产完成。

## 关键风险

- New API 官方文档声明：修改后 SaaS 需要公开对应源码，并要求保留原品牌；去品牌需要商业许可。[New API licensing](https://github.com/QuantumNous/new-api-docs/blob/main/docs/en/wiki/project-introduction.md#-license)
- New API 自身也明确提醒公开生成式 AI/API 转售场景需要处理备案、许可、内容安全、实名、日志、税务、支付和上游授权。[New API README](https://github.com/QuantumNous/new-api#-project-description)
- OpenAI 当前商业条款允许把 API 集成进 Customer Application 供 End Users 使用，但禁止转移 API Key，并限制在支持地区提供访问；每个上游都要单独确认，不能假设规则相同。[OpenAI Services Agreement](https://openai.com/policies/services-agreement/)
- 中国《生成式人工智能服务管理暂行办法》把通过可编程接口提供服务也纳入“生成式人工智能服务提供者”的定义；是否适用及具体手续需专业法律意见。[中国网信办](https://www.cac.gov.cn/2023-07/13/c_1690898327029107.htm)

## 最终决策门

1. **New API 商业授权可接受**：用 New API，小 fork 只加 SheJane native-app PKCE/device 管理；日志使用独立中转。
2. **授权不可接受**：停止 New API 品牌二开，改为 LiteLLM 数据面 + SheJane 薄账号/钱包/授权层；不转向 Sub2API 的订阅共享路径。
