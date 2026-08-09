# SheJane 路线图

> 更新于 2026-08-03。当前能力基线和源码证据见 [Agent Harness 能力审计](./agent-harness-capabilities-latest-2026-07-26.md)。Runtime 的 P1–P12 只表示 [`harness-runtime-stages.md`](./harness-runtime-stages.md) 中的请求阶段；本路线图使用 Now / Next / Later / Explore，不另建一套阶段编号。

## 当前基线

SheJane 已经具备本地 Agent Harness 的主干，不需要继续以“增加工具数量”作为主要进度指标。

| 判定 | 数量 | 主要能力 |
| --- | ---: | --- |
| 已实现 | 18 / 24 | Agent 循环、指令与上下文、工具生命周期、工作区、持久 Run/Thread、SSE、取消恢复、压缩、主 Agent Sandbox/HITL、Checkpoint/Fork、Skills、MCP、计划、Runtime SDK、Provider 真实兼容性、Tracing、Agent Evals、有界多 Agent 协作 |
| 部分实现 | 6 / 24 | 输出 Guard/结构化结果、插件发布、长期记忆、Artifact workspace snapshot、周期调度、多模态实时能力 |

路线图的目标是把“代码存在”推进为“真实 Provider 可用、失败可诊断、结果可验证、安装包可发布”。

## 决策原则

- **Runtime 继续拥有权威状态。** Client 只保留可丢弃投影和待提交命令。
- **真实证据优先于 capability metadata。** 官方文档证明厂商声称支持；只有 SheJane 的真实回环测试才能标记当前连接可用。
- **安全能力必须 fail closed。** Sandbox、凭据、插件隔离和发布 Gate 不允许用普通子进程或提示词替代。
- **先完成可靠的单 Agent，再扩大协作拓扑。** 多 Agent 只有在任务可以明确拆分、并行收益可测且失败能够独立处理时才启用。
- **不把依赖中的 Beta/Preview 自动算作产品能力。** 接入前必须补齐 Runtime 状态、权限、事件、取消、恢复和测试。

## Now：真实可用性与质量门禁

### 0. SheJane 官方服务授权（真实 Cloud 核心链路已验收，平台发布门禁待完成）

Runtime 已拥有固定 origin、PKCE、动态 IPv4 loopback callback、一次性 code exchange、
系统凭据库和普通 `ModelServiceConnection` 的完整链路；Client 已接入系统浏览器与成功、
拒绝、超时和失败状态。普通 API Key 创建、导入和替换接口不能修改官方托管凭据，BYOK
与显式 `local:<connection>:<model>` 选择保持不变。

正式 Cloud origin 已冻结为 `https://app.shejane.com`，`admin.shejane.com` 只保留同路径
重定向。2026-07-29 的公开邀请环境已经通过邀请码注册、密码登录、动态 loopback、PKCE、
一次性 code、拒绝、本地超时、重放、交换响应体丢失、跨 Runtime 进程凭据读取，以及网页
撤销后旧 token 立即返回 401，并分别验证 2FA 与虚拟平台认证器 Passkey 登录后继续同一
授权流；测试设备和测试账号已清理。发布 Gate 仍等待 Windows 最终安装包、Developer ID/
公证包，以及真实硬件 Passkey 和外部 OAuth 返回链路的独立证据。

运维方配置 DeepSeek 渠道后，源码 Runtime 与重新冻结的 macOS arm64 0.1.19 包内 Runtime
均已以固定 `/v1` 推理地址完成官方授权、两个模型的完整工具回环、跨 Runtime 进程凭据读取、
模型目录刷新和撤销后 401；临时连接、凭据和设备均已清理。该技术验收不替代上游授权或
价格/预算决定。

2026-07-29 已在 macOS arm64 本机构建 0.1.19 ad-hoc 签名本地预览包并通过最终
`.app` 的 `make test-packaged`：包内 Runtime、VM manifest、官方 preset、固定
`https://app.shejane.com`、仅绑定 loopback 的 callback、本地 crash dump 和正常退出清理均
通过。该证据不包含 Developer ID、公证或 Windows；公开 Cloud 核心流程的后续验收不能
替代这些平台发布证据，因此发布状态保持不变。

当前 Client 发行路径已主动移除 macOS VM 资产与 manifest 注入；上述 0.1.19 结果只作为
历史隔离证据。新的打包门禁要求 `.app` 不含 VM 资产，Managed Worker 继续 fail closed。

### 1. 真实 BYOK 模型工具回环（已完成）

目标：解决“设置显示可用，但真实 Agent 不能完成流式工具调用”的问题。

交付：

- 对每个预置 Provider 的中国区/国际区分别运行真实 `模型 → 工具调用 → 工具结果 → 最终回答`。
- 结果绑定 connection、model、endpoint、adapter version 和关键请求模式，不把一个模型的结果复用给另一个模型。
- 明确区分网络/鉴权、账户权限、参数不兼容、SSE 解析、工具调用缺失和第二轮模型失败。
- BYOK 预置目录只决定 bundled/recommended，`verified` 来自真实探测；固定 Cloud origin 返回的 SheJane 官方能力声明由 Runtime 直接信任。

完成标准：连接成功后 Agent 对话模型可直接进入 Run；连接后不自动弹出兼容性测试，兼容性测试只由用户从模型服务“更多”中手动触发并保留可操作结果，不作为可用性门禁。SheJane 官方目录声明的图片生成和图片编辑能力无需真实探针即可自动建立缺失的默认绑定。

实现结果：BYOK 预置模型不再凭静态目录标记 `verified`。连接或更新服务只尝试读取模型目录，不批量调用目录中的模型；凭据可读且属于 Agent 对话目录的模型立即可用于 Run。用户明确点击“测试模型”后，Runtime 才使用正式 Agent 共用的 Provider 适配器完整执行两轮流式 `模型 → shejane.ping 工具 → 工具结果 → 最终标记`，测试结果只记录兼容性，不启用或禁用模型。探针限制为 512 tokens 和 30 秒总时限；点号内部名称在工具定义、历史调用和工具结果上使用同一可逆别名。国内 OpenAI-compatible 服务、OpenAI Chat/Responses、Anthropic Messages 与 Google GenerateContent 共用该边界，Run 会冻结明确协议；reasoning/thinking/thought signature、call ID 和并行顺序保持不变。DeepSeek 等 thinking 模型不再被强制 `tool_choice` 误判，GLM 的 `tool_stream` 同时作用于探测和正式 Agent；Gemini 的协议级 finish reason 也会 fail closed。鉴权、权限、余额、限流、临时不可用和格式不兼容分别返回可操作错误。SheJane 官方服务只信任固定 Cloud origin 返回的结构化用途声明，不做静默切换。

### 2. Agent Eval 发布门禁（已完成）

目标：验证 Agent 最终完成了任务，而不是只验证 API 和事件没有报错。

交付：

- 扩充当前 3 个 seed case，覆盖文件操作、工具回环、规划、恢复、AGENTS.md、Memory、SubAgent 和权限等待。
- 优先使用确定性结果 grader；只有语义质量必须判断时才使用隔离的 LLM judge。
- 保存 Runtime/模型版本、轨迹摘要、最终环境结果、grader 结果和回归差异。
- 将最小稳定集接入关键 Runtime/模型变更门禁，真实 Provider 集可在受控发布任务运行。

完成标准：关键改动不能在核心任务成功率明显回退时发布。

实现结果：真实 Provider seed case 已从 3 个扩到 10 个，覆盖文件读写、工具回环、计划、AGENTS.md、Memory、SubAgent、权限等待和问题等待恢复；`make eval` 保存 Runtime/模型版本、完整轨迹、工作区结果、grader 结果及可选基线差异。CI 与 Client release 先运行 `make eval-gate` 的确定性 Agent 结果集并保存 JUnit 报告；有 BYOK 凭据的受控环境继续使用 `make eval` 跑真实 Provider 集。

## Next：可诊断性、状态收敛与发布证据

### 3. 持久 Trace/Span（已完成）

- 建立 `run → model → tool/subagent → checkpoint → terminal` 的稳定父子关系。
- 记录 usage、耗时、错误分类和脱敏输入摘要，不复制密钥、原始附件或大结果。
- diagnostics 能直接打开或导出同一条执行链；Langfuse/LangSmith 保持可选出口，而不是唯一事实来源。

实现结果：Runtime 从已有 SQLite Run、模型调用账本、Tool Receipt、子 Run、Checkpoint 和终态记录投影稳定父子 Span，不新增第二套事实表。Trace 只带 usage、耗时、状态、错误分类和内容摘要哈希，不复制提示词、工具参数、原始附件、密钥或大结果；同一结构已经进入 diagnostics API、OpenAPI、Runtime SDK 与现有 diagnostics 导出。

### 4. Runtime 状态所有权收敛

- 继续删除 Client 中仅为旧本地对话状态保留的兼容逻辑。
- 所有恢复、等待、计划、工具对账和最终结算都从 Runtime snapshot/event 推导。
- 使用 contract/E2E 测试保证 Client 重启或事件游标失效后仍能收敛。

多 Agent 的 P4 收敛已完成：同步 Subagent 生命周期、同 Run Team Graph、durable child Run、typed mailbox、dependency/required/best-effort/quorum、workspace resource owner 和 root collaboration snapshot 都以 Runtime SQLite/Receipt/Run 为事实源。父终态自动处理 child，不依赖模型记得收口；未来手机端可消费同一快照与现有 steering/HITL/cancel 接口，但远程网关仍是独立后续项目。

独立 Agent federation 也已通过单独的 A2A Gateway 落地：只声明 A2A 1.0 JSON-RPC，把 peer/OIDC/mTLS 身份、租户作用域、外部 ID、Task/Message/Artifact、SSE 和持久 push 映射到 Runtime 权威状态。本地 Runtime 不增加公网路由。固定 TCK 的 MUST 为 100%，14 个 ITK 场景覆盖 Python/Go/TypeScript 双向 standard、stream、push、resubscribe/cancel 和四节点多跳；版本与偏差见 [`a2a-conformance.md`](./a2a-conformance.md)。这项能力服务 Agent-to-Agent，不替代手机端所需的设备身份、配对和撤销网关。

### 5. 插件与安装包发布 Gate

- 在真实 runner 闭环 Managed Worker 的签名、公证、Gatekeeper/平台隔离和资产完整性证明。
- 完成 Runtime 安装包、Client 内置 Runtime 锁定和 macOS/Windows/Linux 对应的原生验证。
- 审计第三方依赖、许可证、签名、SBOM、provenance 和供应链默认值。
- Gate 成功前继续关闭相应 Speech、Vision、Media、PDF、Office 等 Managed Worker 候选。

## Later：Agent 质量与协作

### 6. 可审计的语义记忆

- 在现有用户授权写入和 principal/workspace namespace 上增加语义检索。
- 保留事实原文、来源、写入者、时间和作用域；冲突、过期与删除必须可见。
- 后台整理不能静默改变用户事实，先证明关键词搜索无法满足的真实场景。

### 7. 通用结构化最终输出

- Run 接纳时冻结 output schema，并贯穿模型请求、repair、持久事件和 SDK 类型。
- Schema 失败不能伪装成成功文本；恢复和重试沿用现有 Runtime 失败策略。
- 先服务插件编排、自动化和外部 Client，不改变普通聊天的文本默认值。

### 8. 周期任务

在现有一次性 `run_at` 基础上，补齐 cron、timezone/DST、错过触发、并发、审批等待、取消和每次 Run 独立状态后再开放重复计划。

## Explore：有需求证据后再做

- **A2A 额外 binding 与目录：** JSON-RPC federation 已完成；只有真实互操作需求证明必要时才声明 HTTP+JSON、gRPC、私有 extension、托管 Agent 目录或更多 SDK oracle。每个新增 binding 必须单独通过固定 TCK MUST 和跨语言 ITK，不能从 JSON-RPC 结果推定兼容。
- **长周期 fresh-agent 交接：** 只有 compaction 的质量或成本数据证明不足时，才增加结构化 handoff Artifact 和新上下文 continuation。
- **Remote Client：** 移动端先连接用户自己的 Runtime；远程接入必须经过独立 TLS、设备身份、撤销和限流网关，Runtime 仍只监听 loopback。
- **实时语音 Agent：** 与离线 `speech.transcribe` 分开设计低延迟音频 transport、可中断 turn、durable transcript、设备权限和隐私边界。
- **完整 workspace snapshot：** 只有任务隔离、可复现或远程 worker 需要时，才增加 seed/snapshot/export/resume 产品 API。

## 明确不做

- 不增加自动模型选择或静默 Provider 回退。
- 不把 Runtime 直接暴露到公网。
- 不默认提供模型服务、强制账号体系或 Web 聊天客户端。
- 不为了展示“多 Agent”而引入开放式 swarm、隐式共享长期记忆或无法单独取消的后台任务。
- 不为尚未选择的移动端、托管 Runtime 或远程网关预建空目录和抽象。
