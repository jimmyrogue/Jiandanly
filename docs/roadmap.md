# SheJane 路线图

> 更新于 2026-07-26。当前能力基线和源码证据见 [Agent Harness 能力审计](./agent-harness-capabilities-latest-2026-07-26.md)。Runtime 的 P1–P12 只表示 [`harness-runtime-stages.md`](./harness-runtime-stages.md) 中的请求阶段；本路线图使用 Now / Next / Later / Explore，不另建一套阶段编号。

## 当前基线

SheJane 已经具备本地 Agent Harness 的主干，不需要继续以“增加工具数量”作为主要进度指标。

| 判定 | 数量 | 主要能力 |
| --- | ---: | --- |
| 已实现 | 14 / 24 | Agent 循环、指令与上下文、工具生命周期、工作区、持久 Run/Thread、SSE、取消恢复、压缩、主 Agent Sandbox/HITL、Checkpoint/Fork、Skills、MCP、计划、Runtime SDK |
| 部分实现 | 10 / 24 | Provider 真实兼容性、输出 Guard/结构化结果、Tracing、插件发布、长期记忆、多 Agent、Artifact workspace snapshot、周期调度、多模态实时能力、Agent Evals |

路线图的目标是把“代码存在”推进为“真实 Provider 可用、失败可诊断、结果可验证、安装包可发布”。

## 决策原则

- **Runtime 继续拥有权威状态。** Client 只保留可丢弃投影和待提交命令。
- **真实证据优先于 capability metadata。** 官方文档证明厂商声称支持；只有 SheJane 的真实回环测试才能标记当前连接可用。
- **安全能力必须 fail closed。** Sandbox、凭据、插件隔离和发布 Gate 不允许用普通子进程或提示词替代。
- **先完成可靠的单 Agent，再扩大协作拓扑。** 多 Agent 只有在任务可以明确拆分、并行收益可测且失败能够独立处理时才启用。
- **不把依赖中的 Beta/Preview 自动算作产品能力。** 接入前必须补齐 Runtime 状态、权限、事件、取消、恢复和测试。

## Now：真实可用性与质量门禁

### 1. 真实 BYOK 模型工具回环

目标：解决“设置显示可用，但真实 Agent 不能完成流式工具调用”的问题。

交付：

- 对每个预置 Provider 的中国区/国际区分别运行真实 `模型 → 工具调用 → 工具结果 → 最终回答`。
- 结果绑定 connection、model、endpoint、adapter version 和关键请求模式，不把一个模型的结果复用给另一个模型。
- 明确区分网络/鉴权、账户权限、参数不兼容、SSE 解析、工具调用缺失和第二轮模型失败。
- 官方模型目录只决定 bundled/recommended；`verified` 只来自真实探测。

完成标准：新增或编辑模型后无需人工猜测“测试兼容性”为何失败；失败原因可操作，成功模型可直接进入 Agent Run。

### 2. Agent Eval 发布门禁

目标：验证 Agent 最终完成了任务，而不是只验证 API 和事件没有报错。

交付：

- 扩充当前 3 个 seed case，覆盖文件操作、工具回环、规划、恢复、AGENTS.md、Memory、SubAgent 和权限等待。
- 优先使用确定性结果 grader；只有语义质量必须判断时才使用隔离的 LLM judge。
- 保存 Runtime/模型版本、轨迹摘要、最终环境结果、grader 结果和回归差异。
- 将最小稳定集接入关键 Runtime/模型变更门禁，真实 Provider 集可在受控发布任务运行。

完成标准：关键改动不能在核心任务成功率明显回退时发布。

## Next：可诊断性、状态收敛与发布证据

### 3. 持久 Trace/Span

- 建立 `run → model → tool/subagent → checkpoint → terminal` 的稳定父子关系。
- 记录 usage、耗时、错误分类和脱敏输入摘要，不复制密钥、原始附件或大结果。
- diagnostics 能直接打开或导出同一条执行链；Langfuse/LangSmith 保持可选出口，而不是唯一事实来源。

### 4. Runtime 状态所有权收敛

- 继续删除 Client 中仅为旧本地对话状态保留的兼容逻辑。
- 所有恢复、等待、计划、工具对账和最终结算都从 Runtime snapshot/event 推导。
- 使用 contract/E2E 测试保证 Client 重启或事件游标失效后仍能收敛。

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

- **多 Agent 团队：** 当前同步子 Agent 已能并行查资料、审查和写作，暂不开发 Handoff、后台 Agent、共享任务板或 Swarm。只有真实任务证明现有委派不足时再评估，研究依据保留在 [Agent Harness 能力审计](./agent-harness-capabilities-latest-2026-07-26.md)。
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
