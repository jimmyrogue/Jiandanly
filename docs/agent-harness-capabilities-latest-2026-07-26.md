# Agent Harness 最新能力基线（2026-07-26）

> 研究范围：OpenAI Agents SDK / Codex、Anthropic Claude Agent SDK / Managed Agents、LangChain Deep Agents / LangGraph、Pi。只使用第一方文档、官方仓库和官方工程文章。本文定义的是后续审计使用的去重能力表，不表示每个产品都应该实现所有增强项。

## 结论

业界没有一份由单一标准组织或厂商维护的“Agent Harness 完整功能清单”。本文没有把某一家产品目录当标准，而是取 OpenAI、Anthropic、Deep Agents/LangGraph 与 Pi 的**跨厂商能力交集**作为核心项，再把已经产品化但并非最小 Harness 必需的能力归入现代增强项。

截至 2026-07-26，一个可投入产品使用的 Agent Harness 有 **12 项核心能力**；现代 Harness 还普遍加入 **12 项增强能力**。最小 Harness（Pi）刻意不内置多 Agent、MCP、Plan Mode 和权限弹窗，说明这些能力不是“能运行 Agent”的必要条件；但 OpenAI、Anthropic 和 Deep Agents 已把其中多项做成官方一等能力，产品审计时不能再把它们当成遥远设想。

成熟度必须随能力一起记录：OpenAI Sandbox Agents 仍是 **Beta**，Anthropic Managed Agents 使用 `managed-agents-2026-04-01` **Beta**，其 memory store 还是 2026-07-22 新增的 Beta；Deep Agents async subagents 也是 **Preview**。这些可以用来定义方向，不能在没有本地验证时标成稳定完成。

分类规则：

- **核心**：负责模型循环、工具执行、状态、流、恢复和安全；缺失时 Harness 不能稳定地完成真实任务。
- **现代增强**：至少一个主流官方 Harness 已做成一等能力，但可以按产品边界选择不实现。
- **完整实现**：不只存在 UI 或数据结构，还要有 Runtime 所有权、失败与恢复语义、可运行验证。

## 一、核心能力

| ID | 能力 | 可验收定义 | 第一方证据 |
| --- | --- | --- | --- |
| C01 | Agent 循环与终止 | 调模型，识别工具调用，执行并回填结果，直到最终输出；同时有 turn/step 上限和明确终态。 | [OpenAI Agents SDK](https://openai.github.io/openai-agents-python/running_agents/#the-agent-loop) 明确定义 `final_output`、handoff、tool call 三条循环分支和 `max_turns`；[Anthropic 工具循环](https://platform.claude.com/docs/en/agents-and-tools/tool-use/how-tool-use-works)明确模型只发结构化请求、Harness 执行并回传结果。 |
| C02 | 模型、Provider 与能力绑定 | 能选择模型/Provider，并把上下文窗、思考、流式、工具和输入模态等差异约束到真实请求，而不是只保存模型名。 | [Pi](https://pi.dev/) 官方列出 15+ Provider、会话中切换模型与自定义 Provider；[OpenAI Agents SDK Models](https://openai.github.io/openai-agents-python/models/)提供模型与 Provider 抽象。 |
| C03 | 指令与上下文组装 | 稳定组合系统指令、项目指令、运行时上下文、工具描述和当前工作目录，并有覆盖优先级。 | [Deep Agents Context Engineering](https://docs.langchain.com/oss/python/deepagents/context-engineering)列出 system prompt、memory、skills、tools 和 runtime context 的组装顺序；[Codex AGENTS.md](https://developers.openai.com/codex/agent-configuration/agents-md)说明全局与项目指令分层加载。 |
| C04 | 类型化工具注册与执行生命周期 | 工具有名称、说明、输入 schema、结果或错误；支持调用 ID、并行策略、取消信号和长调用进度。 | [OpenAI Agents SDK](https://openai.github.io/openai-agents-python/)提供自动 schema 与 Pydantic 校验；[Pi Extensions](https://pi.dev/docs/latest/extensions)的工具接口包含 `toolCallId`、`AbortSignal` 和 `onUpdate`。 |
| C05 | 输出契约、Guardrail 与错误边界 | 最终输出可按 schema 校验；输入、输出和工具调用可阻断；工具异常不会破坏循环或伪装成成功。 | [OpenAI Agents SDK](https://openai.github.io/openai-agents-python/)把 Guardrails、strict schema 和 tool error behavior 列为正式能力；[Anthropic Permission Policies](https://platform.claude.com/docs/en/managed-agents/permission-policies)定义工具暂停、允许或拒绝。 |
| C06 | 工作区、文件与执行原语 | Agent 能在明确工作区内读、搜、写、编辑和执行命令；大输出不会无限塞进模型上下文。 | [Deep Agents Overview](https://docs.langchain.com/oss/python/deepagents/overview)内置文件工具并在 Sandbox backend 提供 `execute`；[Claude Managed Agents Tools](https://platform.claude.com/docs/en/managed-agents/tools)内置 Bash/Read/Write/Edit/Glob/Grep，并把超大工具结果落盘后只给模型预览。 |
| C07 | Run、Thread/Session 与状态持久化 | 会话和运行有稳定 ID、生命周期与权威状态；进程或客户端重启后可继续读取历史，而不是只依赖内存消息数组。 | [LangGraph Persistence](https://docs.langchain.com/oss/python/langgraph/persistence)按 thread 保存每一步 checkpoint；[Claude Managed Agents Sessions](https://platform.claude.com/docs/en/managed-agents/sessions)定义持久会话与多次交互；[Pi](https://pi.dev/)用树形 JSONL 会话保存全部分支。 |
| C08 | 流式事件与权威终态 | 文本、思考、工具、状态和错误可增量传输；预览与持久记录分离，断线重连后以权威记录收敛。 | [Claude Session Event Stream](https://platform.claude.com/docs/en/managed-agents/events-and-streaming)明确 delta 只是不可重放预览、完整 `agent.message` 才是权威记录；[Codex App Server](https://developers.openai.com/codex/app-server)提供 thread、turn、approval 与 streamed events。 |
| C09 | 取消、转向、重试与恢复 | 用户能中断运行、改变方向或追加任务；取消传播到正在执行的工具；瞬时失败可重试，恢复不会重复已完成副作用。 | [Claude Session Event Stream](https://platform.claude.com/docs/en/managed-agents/events-and-streaming)支持 `user.interrupt` 后重定向；[Pi](https://pi.dev/)支持运行中 steering 和完成后 follow-up；[LangGraph Durable Execution](https://docs.langchain.com/oss/python/langgraph/durable-execution)要求副作用可重放或幂等；[OpenAI Background Mode](https://developers.openai.com/api/docs/guides/background)支持后台响应的轮询、取消与按 sequence cursor 续流。 |
| C10 | 上下文预算、压缩与结果外置 | 接近上下文上限时能压缩旧历史、裁剪或外置大结果，并保留继续任务所需状态。 | [Pi Compaction](https://pi.dev/docs/latest/compaction)支持自动/手动压缩且完整历史仍保留；[Deep Agents Overview](https://docs.langchain.com/oss/python/deepagents/overview)内置自动 summarization 和文件系统 context offload；[Anthropic 长任务 Harness](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents)指出 compaction 是长任务基础但单独不足够。 |
| C11 | Sandbox、权限与人工审批 | OS/远程环境限制文件、网络和进程能力；敏感动作在执行前可审批；审批不是 sandbox 的替代品。 | [Codex Sandbox](https://developers.openai.com/codex/concepts/sandboxing)明确 sandbox 是技术边界、approval 是越界决策；[Deep Agents Sandboxes](https://docs.langchain.com/oss/python/deepagents/sandboxes)说明隔离边界与 secrets 风险；[Pi](https://pi.dev/)反例明确其核心没有权限弹窗，需容器或扩展。 |
| C12 | 可观测性、用量与调试 | 每次模型、工具、handoff、错误与状态转换可关联到 run/trace，能检查 token/成本和失败路径。 | [OpenAI Agents SDK Tracing](https://openai.github.io/openai-agents-python/tracing/)内置 agent、LLM、tool、handoff 与 guardrail span；[Claude Session Event Stream](https://platform.claude.com/docs/en/managed-agents/events-and-streaming)提供 session/span/agent events 和累计 token usage；[LangGraph Streaming](https://docs.langchain.com/oss/python/langgraph/streaming)提供节点、消息、自定义与调试流。 |

## 二、现代增强能力

| ID | 能力 | 可验收定义 | 第一方证据 |
| --- | --- | --- | --- |
| M01 | Checkpoint、分支、回放与 Time Travel | 能从已提交检查点恢复、fork 另一条轨迹、回放失败步骤；副作用边界清楚。 | [LangGraph Persistence](https://docs.langchain.com/oss/python/langgraph/persistence)把 checkpoint 用于 time travel、fork 和 fault tolerance；[Pi](https://pi.dev/)提供树形历史、任意节点继续和单文件保留全部分支。 |
| M02 | Skills 与渐进披露 | 可复用工作流由说明、资源和可选脚本组成；默认只暴露元数据，需要时再载入全文。 | [Codex Skills](https://developers.openai.com/codex/build-skills)明确 skill 的 progressive disclosure；[Deep Agents Skills](https://docs.langchain.com/oss/python/deepagents/skills)支持 `SKILL.md`、资源与脚本；[Pi](https://pi.dev/)也按需加载 Skills。 |
| M03 | 生命周期 Hooks、插件与可分发扩展 | 扩展可以注册工具和命令、订阅生命周期、修改或阻断调用，并能版本化分发；宿主必须标注信任边界。 | [Codex Hooks](https://developers.openai.com/codex/hooks)允许在 agent loop 生命周期运行确定性脚本；[Pi Extensions](https://pi.dev/docs/latest/extensions)支持 tool/event/command/UI 和 package 分发，同时明确扩展拥有完整系统权限；[Codex Plugins](https://developers.openai.com/codex/plugins)可捆绑 skills 与 connectors。 |
| M04 | MCP 与外部连接器 | 以标准协议发现并调用外部工具/资源，处理认证、生命周期、审批和错误；不是简单把任意 HTTP 接口称作 MCP。 | [Codex MCP](https://developers.openai.com/codex/mcp)支持 STDIO、Streamable HTTP 与 OAuth；[OpenAI Agents SDK](https://openai.github.io/openai-agents-python/)内置 MCP server tool calling；[Claude Agent Setup](https://platform.claude.com/docs/en/managed-agents/agent-setup)把 MCP server 作为版本化 Agent 配置。Pi 则[明确不内置 MCP](https://pi.dev/)。 |
| M05 | 跨会话长期记忆 | 与当前 thread 历史分开，按用户/Agent/Workspace 隔离，能查询、更新、审计并控制写权限。 | [Claude Agent Memory](https://platform.claude.com/docs/en/managed-agents/memory)把 workspace-scoped memory store 挂载为独立目录；[Deep Agents Memory](https://docs.langchain.com/oss/python/deepagents/memory)提供跨 thread 文件式记忆；Pi 只把长期记忆留给扩展实现。 |
| M06 | 计划、任务与持续目标 | Agent 能分解任务、维护进度并在新信息出现时调整；计划状态不是只存在于模型自然语言中。 | [Deep Agents Overview](https://docs.langchain.com/oss/python/deepagents/overview)内置 `write_todos`；Codex 当前配置把[持久 goals 与自动 continuation](https://developers.openai.com/codex/config-basic)列为稳定能力。Pi [刻意不内置 plan mode 和 todos](https://pi.dev/)。 |
| M07 | 多 Agent 委派与 Handoff | 子 Agent 有隔离上下文、可限定模型/工具/指令；支持并行、汇总、取消，长任务最好还能中途 steering。 | [OpenAI Agents SDK](https://openai.github.io/openai-agents-python/)提供 agents-as-tools 与 handoffs；[Codex Subagents](https://developers.openai.com/codex/subagents)支持并行专门 Agent；[Deep Agents Async Subagents](https://docs.langchain.com/oss/python/deepagents/async-subagents)支持后台任务、follow-up 和取消；[Claude Managed Agents Multi-agent](https://platform.claude.com/docs/en/managed-agents/multiagent-orchestration)为每个 Agent 建持久、隔离的 session thread，并允许单独中断。 |
| M08 | Artifact、文件输入输出与隔离工作区快照 | 大文件/生成物通过文件或 Artifact 引用传递，不塞进消息 JSON；运行环境能 seed、checkpoint、导出和清理。 | [OpenAI Sandbox Agents](https://developers.openai.com/api/docs/guides/agents/sandboxes)提供 manifest-defined files、隔离 workspace、snapshot 和 resumable sandbox session；[Claude Files](https://platform.claude.com/docs/en/managed-agents/files)把上传文件挂载进 sandbox；[Deep Agents Sandboxes](https://docs.langchain.com/oss/python/deepagents/sandboxes)描述 seed 文件与取回 artifacts。 |
| M09 | 后台、远程与定时执行 | 任务离开前台客户端后仍有独立生命周期、隔离环境、日志和可恢复结果；定时运行保留审批策略。 | [Codex Scheduled Tasks](https://developers.openai.com/codex/automations)支持项目或独立 worktree 后台定时运行；[Codex Cloud](https://developers.openai.com/codex/cloud)支持隔离远程环境和并行任务；[Claude Scheduled Deployments](https://platform.claude.com/docs/en/managed-agents/scheduled-deployments)按 cron/timezone 创建独立 session run 并保留事件与 webhook。 |
| M10 | 多模态、实时与 Computer Use | Harness 能处理图片/音频/屏幕等输入输出，并为实时中断、桌面操作和外部状态变化定义独立安全边界。 | [OpenAI Agents SDK](https://openai.github.io/openai-agents-python/)提供 Realtime/Voice Agents；[Codex Computer Use](https://developers.openai.com/codex/app/computer-use)提供 GUI 操作并要求权限复核。它是场景增强，不是文本/代码 Harness 的必选项。 |
| M11 | Agent Evals、验证与反馈回路 | 记录完整轨迹与环境最终状态，用确定性 grader、模型 grader 或人工检查评估；验证失败不能被最终文案掩盖。 | [Anthropic Agent Evals](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents)区分 transcript、outcome、grader、evaluation harness 与 agent harness；最新 [Managed Agents Outcomes](https://platform.claude.com/docs/en/managed-agents/define-outcomes)会用隔离 context 的 grader 按 rubric 检查 Artifact 并把反馈交回 Agent 迭代；[OpenAI Agents SDK](https://openai.github.io/openai-agents-python/)把 tracing 接到 evaluation、fine-tuning 与 distillation。 |
| M12 | 独立 Runtime 的 SDK/RPC/Client 协议 | Harness 能作为库、子进程或服务嵌入多个客户端；协议覆盖命令、事件、历史、审批、取消和版本协商。 | [Codex App Server](https://developers.openai.com/codex/app-server)是面向 rich client 的 thread/turn/approval/event 接口；[Pi](https://pi.dev/)提供 interactive、print/JSON、RPC、SDK 四种模式；[Deep Agents Overview](https://docs.langchain.com/oss/python/deepagents/overview)提供 SDK、CLI 与 ACP editor connector。 |

## 三、四类官方 Harness 的边界差异

| 官方实现 | 它证明了什么 | 不应误推的结论 |
| --- | --- | --- |
| OpenAI Agents SDK / Codex | 轻量 agent loop 也可以同时具备 tools、guardrails、sessions、HITL、tracing、sandbox specialists；Codex 进一步提供 app-server、subagents、skills、MCP、hooks 和后台任务。 | Agents SDK 的普通 `Runner` 本身不等于 durable workflow engine；其文档把 Temporal、Dapr、Restate、DBOS 列为外部 durable execution 集成。 |
| Anthropic Claude Agent SDK / Managed Agents | 最新托管形态已把 agent config、sandbox、event stream、permissions、memory、multi-agent 和可恢复 session 做成服务资源。 | Compaction 不等于长期任务完整性；Anthropic 的长任务研究仍依赖增量任务、结构化交接和可验证环境状态。 |
| Deep Agents / LangGraph | Deep Agents 是明确自称的 agent harness；LangGraph 提供 durable runtime、checkpoint、interrupt、streaming 和 time travel。 | Deep Agents 的 filesystem permission 只覆盖内置文件工具；官方明确它不约束 custom/MCP tools，也不约束 sandbox 内 shell。 |
| Pi | 极小核心依然可以靠 tool loop、workspace、tree session、compaction、steering、events、SDK/RPC 和 extensions 成为完整可用 Harness。 | Pi 明确不内置 MCP、subagents、permission popups、plan mode、todos、background bash；第三方 package 或示例 extension 不能算 Pi 核心已实现，而且 extension 以完整用户权限运行。 |

## 四、用于 SheJane 交叉审计的判定口径

后续对代码逐项标记时使用四态，避免把“有类型”“有按钮”误报成已完成：

- **已实现**：Runtime 主链真实可达，有持久状态、失败/取消/恢复语义和自动测试。
- **部分实现**：核心路径存在，但只覆盖单客户端、单 Provider、成功路径，或缺少安全/恢复/测试。
- **仅设计**：只存在文档、schema、Feature Flag、UI、空 handler 或不可达代码。
- **未实现/不采用**：代码不存在，或产品明确选择不做；不采用必须说明替代边界。

特别需要分开判断的近义项：

1. 流式预览不等于持久事件；最终内容必须由权威 terminal record 收敛。
2. Session history 不等于 checkpoint；checkpoint 还要能恢复执行位置和副作用边界。
3. Context compaction 不等于 long-term memory；前者保当前任务，后者跨会话学习。
4. 文件路径不等于 Artifact；Artifact 需要身份、所有权、完整性、生命周期和授权。
5. 工具确认弹窗不等于 sandbox；一个是决策，一个是技术强制边界。
6. 子 Agent 函数不等于 multi-agent runtime；还要有隔离 thread、事件、取消、资源与汇总语义。
7. 插件注册不等于安全插件平台；还要有来源、版本、能力、隔离、凭据与卸载边界。
8. Trace 日志不等于 eval；eval 必须检查最终环境结果，而非只评价 Agent 自述。

## 主要第一方入口

- OpenAI：[Agents SDK](https://openai.github.io/openai-agents-python/) · [Codex 文档](https://developers.openai.com/codex/) · [Codex 开源仓库](https://github.com/openai/codex)
- Anthropic：[Claude Managed Agents](https://platform.claude.com/docs/en/managed-agents/overview) · [Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk/overview) · [Engineering](https://www.anthropic.com/engineering)
- LangChain：[Deep Agents](https://docs.langchain.com/oss/python/deepagents/overview) · [LangGraph](https://docs.langchain.com/oss/python/langgraph/overview)
- Pi：[pi.dev](https://pi.dev/) · [官方仓库](https://github.com/earendil-works/pi)

---

## 五、SheJane 交叉审计范围

本轮以当前源码、自动测试和真实可达路径为准，不把路线图、类型或 UI 单独算作完成。审计覆盖 Client 发起 Run 后的整条 Agent 主链：

- **主阶段：P6 Agent Definition**，权威所有者是 Runtime 的 Agent 图定义、模型绑定、工具/Skills/Memory/SubAgent 目录及中间件装配。
- **直接上游：P5 Context Construction**，检查指令、历史、附件、工作区和预算如何进入图。
- **直接下游：P7–P12**，检查模型回合、工具执行、验证/路由、暂停恢复、结算清理和 Client 投影。
- 当前实现流以 [`docs/run-loop.md`](./run-loop.md) 为准；目标阶段编号只使用 [`docs/harness-runtime-stages.md`](./harness-runtime-stages.md) 的 P1–P12，不另造编号。

判定摘要：24 个基线类别中，**14 个已实现、10 个部分实现、0 个大类完全空白**。这不表示不存在缺失功能；缺失项主要位于“部分实现”类别内部，详见第八节。

## 六、12 项核心能力对照

| ID | 状态 | SheJane 当前实现 | 尚缺或边界 | 主要证据 |
| --- | --- | --- | --- | --- |
| C01 Agent 循环与终止 | ✅ 已实现 | Deep Agents/LangGraph 执行模型—工具循环；Runtime `RunCoordinator` 管理 job、attempt、终态和资源结算，并有模型调用、步骤、时间、输出上限。 | 无核心缺口。 | [`agent/builder.py`](../runtime/src/shejane_runtime/agent/builder.py)、[`runs.py`](../runtime/src/shejane_runtime/runs.py)、`test_e2e_capabilities.py`、`test_run_jobs.py` |
| C02 模型、Provider 与能力绑定 | 🟡 部分实现 | BYOK 密钥在系统凭据库；Run 冻结具体 `local:<connection>:<model>`；模型资料约束上下文、输出、工具与图片能力；禁止静默 Provider/模型回退。 | 官方声明与静态 metadata 仍不能证明当前 key、地域端点、adapter 和流式工具调用真实可用；真实“模型→工具→模型”离线 Gate 仍是路线图第一项。 | [`model_services.py`](../runtime/src/shejane_runtime/model_services.py)、[`model_profiles.py`](../runtime/src/shejane_runtime/model_profiles.py)、[`llm/`](../runtime/src/shejane_runtime/llm/)、[`roadmap.md`](./roadmap.md) |
| C03 指令与上下文组装 | ✅ 已实现 | 身份、开发者指令、任务、AGENTS.md、Skills、附件、工作区、运行状态、repair/retry/steering 按层组装，模型边界还有工具结构裁剪。 | 无核心缺口；真实模型是否严格服从 AGENTS.md 仍属于 Eval 问题。 | [`agent/context_builder.py`](../runtime/src/shejane_runtime/agent/context_builder.py)、[`agent/prompts/`](../runtime/src/shejane_runtime/agent/prompts/)、`test_context_builder.py`、`test_model_ledger.py` |
| C04 类型化工具与执行生命周期 | ✅ 已实现 | 静态工具、Deep Agents 文件/Shell 工具、MCP 和插件 Action 均进入统一图；有 schema、call ID、并发、进度、取消、重试分类、持久回执和大结果外置。 | 无核心缺口；具体 Managed Worker 是否可发布属于 M03。 | [`tools/registry.py`](../runtime/src/shejane_runtime/tools/registry.py)、[`middleware/tool_execution.py`](../runtime/src/shejane_runtime/middleware/tool_execution.py)、[`plugins/tools.py`](../runtime/src/shejane_runtime/plugins/tools.py)、`test_plugin_tool_execution.py` |
| C05 输出契约、Guardrail 与错误边界 | 🟡 部分实现 | 工具参数/输出有 schema；输入 Guard、出站密钥/PII 脱敏、权限审查、Completion Router、验证修复和结构化失败分类已接入。 | 主 Run 没有通用 `response_format`/最终输出 schema；Output Guard 对空白/拒答最终候选主要是 observe-only；没有面向扩展方的通用 per-tool input/output guardrail API。 | [`middleware/input_guard.py`](../runtime/src/shejane_runtime/middleware/input_guard.py)、[`middleware/outbound_policy.py`](../runtime/src/shejane_runtime/middleware/outbound_policy.py)、[`middleware/completion_router.py`](../runtime/src/shejane_runtime/middleware/completion_router.py) |
| C06 工作区、文件与执行原语 | ✅ 已实现 | 工作区内读、搜、写、编辑和 Shell；附件只读挂载；PDF 转文本；直接读取、输出和 Artifact 有大小边界。 | 无核心缺口。 | [`agent/backends.py`](../runtime/src/shejane_runtime/agent/backends.py)、`test_agent_builder.py`、`test_agent_shell_sandbox.py` |
| C07 Run、Thread 与状态持久化 | ✅ 已实现 | SQLite 权威保存 thread、run、job、event、receipt、checkpoint head；Client 只保存可丢弃投影和待提交命令。 | 无核心缺口。 | [`store/sqlite.py`](../runtime/src/shejane_runtime/store/sqlite.py)、[`store/fenced_checkpointer.py`](../runtime/src/shejane_runtime/store/fenced_checkpointer.py)、`test_runs_http.py` |
| C08 流式事件与权威终态 | ✅ 已实现 | SSE 区分临时 delta 与持久事件；支持游标续流、重放、超窗快照重同步和多订阅者；P11 原子提交最终消息与事件高水位。 | 无核心缺口。 | [`event_translator.py`](../runtime/src/shejane_runtime/event_translator.py)、`test_sse_envelope.py`、`test_run_result_commit.py` |
| C09 取消、转向、重试与恢复 | ✅ 已实现 | 持久 `run.cancel`、运行中 steering、类型化 permission/question/plan/tool-reconcile resume、租约/attempt fencing、失败策略、启动恢复和 sandbox reaper。 | 无核心缺口；远程分布式 worker 不在当前本地 Runtime 边界内。 | [`runs.py`](../runtime/src/shejane_runtime/runs.py)、[`middleware/steering.py`](../runtime/src/shejane_runtime/middleware/steering.py)、[`sandbox_reaper.py`](../runtime/src/shejane_runtime/sandbox_reaper.py)、`test_run_jobs.py` |
| C10 上下文预算、压缩与结果外置 | ✅ 已实现 | Deep Agents 自动 summarization；Provider 硬上下文限制；动态隐藏无关工具 schema；大工具结果和插件输出转 Artifact；SubAgent 隔离上下文。 | 未实现跨多个长会话的“启动全新 Agent + 结构化交接”模式，但不影响单 Run 核心能力。 | [`agent/builder.py`](../runtime/src/shejane_runtime/agent/builder.py)、[`llm/ledger.py`](../runtime/src/shejane_runtime/llm/ledger.py)、`test_tool_visibility.py` |
| C11 Sandbox、权限与人工审批 | ✅ 已实现 | 主 Agent Shell 只有获得平台 sandbox launcher 才执行，否则 fail closed；工作区/附件受路径边界保护；ask/auto/full-access、批量审批、编辑参数、拒绝和 Run scope 都有持久语义。 | 这里仅判定主 Agent 本地执行边界；跨平台 Managed Worker 发布 Gate 另在 M03 标为部分。 | [`agent/backends.py`](../runtime/src/shejane_runtime/agent/backends.py)、[`middleware/tool_review.py`](../runtime/src/shejane_runtime/middleware/tool_review.py)、`test_agent_shell_sandbox.py`、`test_tool_receipts.py` |
| C12 可观测性、用量与调试 | 🟡 部分实现 | `RuntimeObserver` 记录模型、工具、Agent、错误和 usage；有结构化日志，安装 SDK 且配置凭据时可接 Langfuse，也可与 LangSmith 并存。 | 没有 Runtime 自己持久化的 span/trace 图、内置追踪查看器或 OpenTelemetry 导出；排障仍依赖日志、事件、diagnostics 与外接平台拼接。 | [`observability.py`](../runtime/src/shejane_runtime/observability.py)、`test_observability.py` |

## 七、12 项现代增强对照

| ID | 状态 | SheJane 当前实现 | 尚缺或边界 | 主要证据 |
| --- | --- | --- | --- | --- |
| M01 Checkpoint、分支、回放与 Time Travel | ✅ 已实现 | 每个 superstep 同步 checkpoint；分支头用租约保护 CAS 更新；`run.fork` 从公开 checkpoint 创建新产品对话；事件可重放。 | 没有面向用户的任意图节点浏览器，但核心 fork/replay 已可达。 | [`store/fenced_checkpointer.py`](../runtime/src/shejane_runtime/store/fenced_checkpointer.py)、`test_runs_http.py`、`test_run_jobs.py` |
| M02 Skills 与渐进披露 | ✅ 已实现 | 扫描用户 Skills，先暴露目录元数据、按需读取 `SKILL.md`/资源；Run 冻结 catalog hash，Client 有管理界面。 | 没有内置远程市场；这不是 Harness 必需项。 | [`agent/builder.py`](../runtime/src/shejane_runtime/agent/builder.py)、`test_skills.py` |
| M03 生命周期 Hooks、插件与可分发扩展 | 🟡 部分实现 | 有插件 manifest/schema、安装启停回滚、Action、WASI、Managed Worker、运行时资产、权限/回执和版本冻结。 | 没有通用 Agent lifecycle hooks API；Managed Worker 的真实签名/公证及 Windows/Linux 发布 Gate 未完成，Speech/OCR/Office/Media/Vision 等候选因此仍 fail closed。 | [`plugins/registry.py`](../runtime/src/shejane_runtime/plugins/registry.py)、[`plugins/`](../runtime/src/shejane_runtime/plugins/)、[`plans/runtime-plugin-platform.md`](./plans/runtime-plugin-platform.md) |
| M04 MCP 与外部连接器 | ✅ 已实现 | STDIO/HTTP 配置、目录快照、阈值后 `mcp.search_tools`、调用、错误、审批、回执与生命周期都进入 Runtime。 | OAuth/远程连接器的广度取决于具体 MCP server，但协议主链已完成。 | [`tools/mcp.py`](../runtime/src/shejane_runtime/tools/mcp.py)、`test_mcp.py` |
| M05 跨会话长期记忆 | 🟡 部分实现 | 有 `memory.search/write`、principal/workspace namespace、明确用户授权写入、全局事实继承和子 Agent 禁写。 | 检索是关键词/substring，不是 semantic；没有事实合并、冲突/过期验证、episodic→semantic consolidation 或后台整理。 | [`tools/memory.py`](../runtime/src/shejane_runtime/tools/memory.py)、`test_memory.py`、`test_memory_http.py` |
| M06 计划、任务与持续目标 | ✅ 已实现 | `write_todos`、Plan-First auto/off/always、2–8 项及单一 in-progress 状态约束、计划审批、进度 ledger 和交接新鲜度。 | 当前是 Run 内计划执行，不是跨 Run 自动续跑的长期 Goal 服务。 | [`middleware/plan_first.py`](../runtime/src/shejane_runtime/middleware/plan_first.py)、[`middleware/completion_router.py`](../runtime/src/shejane_runtime/middleware/completion_router.py)、`test_plan_first.py` |
| M07 多 Agent 委派与 Handoff | 🟡 部分实现 | 内置 general-purpose/researcher/writer 与用户 Markdown Agent；子上下文/工具/预算隔离；同一模型轮可发多个 `task()` 并行，仍受父 Run 的权限和回执边界。 | 只有同步 `task()` 委派；未接 Deep Agents Preview 的远程后台子 Agent launch/check/update/cancel/list；没有把当前控制权转给另一个 Agent 的真正 handoff，也没有独立 durable subagent thread/UI。 | [`agent/subagents.py`](../runtime/src/shejane_runtime/agent/subagents.py)、`test_subagents.py`、`test_e2e_capabilities.py` |
| M08 Artifact、文件输入输出与隔离工作区快照 | 🟡 部分实现 | 附件由 Runtime 接纳并校验；插件 Artifact 有身份、完整性、授权、生命周期、清理和同 Run 链接；大结果不进消息 JSON。 | 主工作区是授权目录上的受控实时操作，不是每次 Run 都 seed/snapshot/export 的独立 workspace；没有通用 sandbox snapshot/resume 产品 API。 | [`plugins/executor.py`](../runtime/src/shejane_runtime/plugins/executor.py)、[`agent/backends.py`](../runtime/src/shejane_runtime/agent/backends.py)、`test_plugin_run_bindings.py` |
| M09 后台、远程与定时执行 | 🟡 部分实现 | Run job 可在 Client 断开后继续；有租约、恢复、结果通知和一次性 `run_at` 延时任务。 | Schedule schema 只有 `run_at`，没有 cron、timezone 或 recurrence；Runtime 只监听 loopback，没有远程 gateway/hosted worker。 | [`scheduler.py`](../runtime/src/shejane_runtime/scheduler.py)、[`api_schemas.py`](../runtime/src/shejane_runtime/api_schemas.py)、`test_scheduled_runs.py`、[`roadmap.md`](./roadmap.md) |
| M10 多模态、实时与 Computer Use | 🟡 部分实现 | 图片附件和能力 Gate、Browser QA、Computer Use、Image/OCR/Media/Office/Vision/Speech 插件包或候选已存在。 | `speech.transcribe` 是尚未发布的离线文件转写，不是实时语音；没有 realtime audio transport、双向语音 Agent 或语音输出。部分 Managed Worker 还受 M03 发布 Gate 限制。 | [`plugins/computer_use.py`](../runtime/src/shejane_runtime/plugins/computer_use.py)、[`plugins/speech/`](../runtime/plugins/speech/)、`test_computer_use_e2e.py` |
| M11 Agent Evals、验证与反馈回路 | 🟡 部分实现 | `task.verify`、有界 repair、Completion/Clarification reviewer、重复确定性失败熔断；独立 Eval harness 支持 heuristic 与 LLM judge。 | 当前只有 3 个 seed Eval case；真实 Provider 语义质量、真实 SubAgent 研究、AGENTS.md 行为和模型工具回环尚未形成默认 CI/发布门禁。 | [`eval/`](../runtime/src/shejane_runtime/eval/)、[`middleware/completion_reviewer.py`](../runtime/src/shejane_runtime/middleware/completion_reviewer.py)、`test_eval_harness.py` |
| M12 独立 Runtime 的 SDK/RPC/Client 协议 | ✅ 已实现 | Runtime 独立进程通过 loopback HTTP/SSE 提供 commands、runs、events、snapshots、审批、取消、capability/version 协商；TypeScript SDK 与 Client 使用同一协议。 | 当前只面向本机 Client；安全远程 gateway 是明确的未来边界，不应把 Runtime 直接暴露公网。 | [`server.py`](../runtime/src/shejane_runtime/server.py)、[`sdk/`](../runtime/sdk/)、`test_sse_contract.py`、`make test-contract` |

## 八、尚未实现或尚未完整产品化的具体功能

以下不是把 10 个“部分实现”重复一遍，而是拆出可以单独验收的真实缺口：

| 优先级 | 缺口 | 当前事实 | 建议验收条件 |
| --- | --- | --- | --- |
| Now | 真实 Provider 工具回环 Gate | 当前兼容性测试与官方 metadata 不能覆盖真实 key、地域、账户权限、思考参数、SSE 分片和第二次模型调用。 | 对每个预置 Provider/地域执行真实 `model → tool call → tool result → final`；结果绑定 connection + model + endpoint + adapter version，失败原因可诊断。 |
| Now | 可进入发布门禁的 Agent Evals | Eval 基础设施存在，但只有 3 个 seed case，未覆盖真实 Provider、工具完成质量、AGENTS.md 和 SubAgent 结果。 | 建立稳定任务集、确定性结果 grader 和少量 LLM judge；关键模型/Runtime 变更必须跑，报告可比较。 |
| Next | 持久 Trace/Span 与统一查看 | 目前是结构化日志、事件、diagnostics 和可选 Langfuse，缺少一个稳定的 run→model→tool→checkpoint→terminal 因果图。 | 每个 span 有稳定 ID、父子关系、usage、错误和脱敏输入摘要；可导出或在 diagnostics 打开。 |
| Next | Managed Worker 正式发布 Gate | 大量 Worker/资产和 VM/隔离测试已完成，但真实签名、公证及跨平台发布证据未闭环，Registry 继续 fail closed。 | 真实 release runner 通过签名、公证、Gatekeeper/平台隔离、资产完整性与冒烟测试后才启用相应插件。 |
| Next | 语义记忆与整理 | 现有 Memory 是安全、显式的事实存取，但检索和事实生命周期较弱。 | 先增加可审计的语义检索，再按明确策略处理重复、冲突、过期和来源；不要让模型静默改写用户事实。 |
| Later | 真正 Handoff 与后台 SubAgent 生命周期 | 当前同步 `task()` 足以覆盖多数本地研究/写作委派，但不能独立查询、follow-up、取消或长期运行。 | 只有产品出现长并行任务需求时，接入独立 thread/job、事件、预算、审批、steering、cancel 和汇总；不要只暴露依赖里的 Preview 工具。 |
| Later | 通用结构化最终输出 | 工具和插件 schema 完整，但普通 Run 最终响应固定为文本。 | API 接受可冻结的 output schema；模型结果、repair、持久事件和 SDK 类型都按同一 schema 校验。 |
| Later | 周期定时任务 | 已有一次性 `run_at`，没有 recurrence。 | 明确 cron/timezone/DST、错过触发、并发、审批等待、取消和每次 Run 独立状态后再增加。 |
| Explore | 长周期新上下文 Agent 交接 | 目前依靠 compaction、checkpoint 和 progress handoff，未自动在新会话启动 fresh agent。 | 仅当单 Run compaction 的质量/成本数据证明不足时，引入结构化 handoff artifact、环境验证和 fresh-agent continuation。 |
| Explore | 实时语音 Agent | 只有文件转写候选，没有实时输入输出。 | 独立设计低延迟音频 transport、可中断 turn、durable transcript、设备权限、供应商/本地模型身份和隐私边界。 |

## 九、明确不应补成“功能”的边界

这些不是遗漏，而是当前正确的产品约束：

- **不做自动模型选择或静默 Provider 回退**：用户选择具体模型，失败应可诊断，不能暗中换模型。
- **不把 Runtime 直接暴露公网**：未来 Remote Client 必须经过独立 TLS、设备身份、撤销和限流网关。
- **不为“完整度”强加账号体系、Web 聊天端或托管模型服务**：它们不是本地 Agent Harness 的必要组成。
- **不把依赖已有但未接入的 Preview/Beta 能力算作完成**：尤其是 Deep Agents async subagents、OpenAI Sandbox Agents 和 Anthropic Managed Agents 新能力。

## 十、建议实施顺序

1. 先完成真实 Provider 工具回环 Gate；它直接解决“配置显示可用、真实 Agent 却不可用”的产品风险。
2. 用这套真实调用路径扩充 Eval，并把关键用例接入变更/发布门禁。
3. 补持久 Trace/Span，让后续模型、工具、恢复和 Eval 失败能一次定位。
4. 闭环 Managed Worker 的真实发布 Gate；在此之前继续 fail closed。
5. Memory 只做安全、可审计的语义检索升级；Handoff、异步 SubAgent、Cron、实时语音等按真实需求和数据再做。

这一路径刻意没有优先增加更多工具。SheJane 当前的主要短板已经从“缺功能”转向“真实 Provider 验证、质量门禁、可观测性和发布证据不够完整”。

## 十一、最新多 Agent 协作模式研究

### 11.1 先区分模式，不把所有并行都叫 Agent Team

截至 2026-07-26，官方实现已经形成七种不同协作模式。它们不是同一个功能的不同名字，而是有不同的控制权、状态和恢复边界：

| 模式 | 控制权与数据流 | 适用场景 | 官方实现 | SheJane |
| --- | --- | --- | --- | --- |
| Manager-as-tools | 主 Agent 始终拥有对话；把一个有界子任务作为工具交给 Specialist，等待摘要返回。 | 研究、审查、写作等只需要最终结果的支线任务。 | [OpenAI Agents-as-tools](https://openai.github.io/openai-agents-python/multi_agent/#core-sdk-patterns)、[Claude Agent SDK Subagents](https://code.claude.com/docs/en/agent-sdk/subagents)、[Deep Agents Subagents](https://docs.langchain.com/oss/python/deepagents/subagents) | ✅ 已实现。`task()` 就是这一层。 |
| 并行 fan-out/fan-in | Router 或主 Agent 一次派出多个独立任务，并行完成后由主 Agent 汇总；子 Agent 之间不通信。 | 多来源调研、独立文件审查、多个假设验证。 | [OpenAI code orchestration](https://openai.github.io/openai-agents-python/multi_agent/#orchestrating-via-code)建议用 `asyncio.gather`；[LangChain Router](https://docs.langchain.com/oss/python/langchain/multi-agent/router)用 `Send` 并行分发；[Codex Subagents](https://developers.openai.com/codex/subagents)并行收集结果。 | ✅ 已实现基础层。同一模型轮发多个 `task()`，LangGraph 并行执行，主 Agent 汇总。 |
| Handoff | 当前 Agent 把用户会话控制权转给 Specialist；后续回复由 Specialist 直接负责，而不是返回工具摘要。 | 客服分流、领域会话、需要 Specialist 直接追问用户的场景。 | [OpenAI Handoffs](https://openai.github.io/openai-agents-python/handoffs/)、[LangGraph Swarm](https://github.com/langchain-ai/langgraph-swarm-py)用 handoff tool 切换 active agent。 | ❌ 未实现。当前 diagnostics 中的 “handoff” 只是运行交接摘要，不是 Agent 控制权转移。 |
| 模型内树形协作 | Root 模型按需 spawn 子 Agent；Agent 之间可 message、follow-up、wait 和 interrupt，Root 负责最终综合。 | 单次请求内高度可拆分的探索、审查与实现。 | [OpenAI Responses Multi-agent](https://developers.openai.com/api/docs/guides/responses-multi-agent)是 GPT-5.6 的 **Beta**；默认建议最多 3 个并发子 Agent。 | ❌ 未实现，也不应绑定为 BYOK Runtime 的通用协作层；它是特定 Provider/模型的托管编排能力。 |
| 后台 durable subagent | 启动后立即返回 task/thread ID；子任务独立运行，主 Agent 可继续对话，之后可查询、追加指令或取消。 | 数分钟以上任务、远程执行、需要中途 steering 的并行工作。 | [Deep Agents Async Subagents](https://docs.langchain.com/oss/python/deepagents/async-subagents)为 **Preview**；[Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk/subagents)支持后台 Agent；[Codex Subagents](https://developers.openai.com/codex/subagents)可检查、steer 和停止独立 Agent thread。 | ❌ 未实现。安装的 Deep Agents 包含 Preview 中间件，但 SheJane 没有传入 `graph_id` 类型配置，也没有后台子任务 API。 |
| 持久 Coordinator threads | Coordinator 管理一组配置固定的 Agent；每个 Agent 有持久隔离 thread，可 follow-up、独立查看事件和中断。 | 长会话里的反复专家协作，需要保留每个专家此前上下文。 | [Claude Managed Agents Multiagent](https://platform.claude.com/docs/en/managed-agents/multiagent-orchestration)是 **Public Beta**；每个 Agent 有独立持久 thread，Coordinator 只允许一层委派。 | ❌ 未实现。子 Agent 没有独立持久 thread，调用结束后只有摘要进入父 Run。 |
| 共享任务/群体协作 | Lead 与 teammates 共享任务列表和 mailbox；成员可互相发消息、认领任务并直接接受用户 steering。 | 需要相互挑战、重新分工和持续协调的大型项目。 | [Claude Code Agent Teams](https://code.claude.com/docs/en/agent-teams)是**实验性、默认关闭**；[Claude Code parallel agents](https://code.claude.com/docs/en/agents)明确其共享任务和 peer messaging 边界。 | ❌ 未实现，也不应现在照搬。当前没有共享队列、成员 mailbox、peer messaging 或独立 teammate session。 |

这里最重要的区别是：**并行调用多个 `task()` 仍然是 Manager-as-tools 的 fan-out/fan-in，不是 Agent Team，也不是 Handoff。** OpenAI 官方同样把 agents-as-tools 与 handoff 分开：前者由 Manager 汇总，后者由 Specialist 接管当前对话。[OpenAI Agent orchestration](https://openai.github.io/openai-agents-python/multi_agent/#core-sdk-patterns)

### 11.2 最新官方能力与成熟度

| 官方实现 | 当前能力 | 成熟度与限制 |
| --- | --- | --- |
| OpenAI Agents SDK | `Agent.as_tool()`、结构化输入、审批、嵌套流；handoff；代码级串行、并行和 evaluator loop。 | 正式 SDK 文档，未标 Beta。普通 `Runner` 不自动变成 durable multi-agent runtime；持久任务仍需应用或外部 durable engine 承担。[Orchestration](https://openai.github.io/openai-agents-python/multi_agent/) · [Agents as tools](https://openai.github.io/openai-agents-python/tools/#agents-as-tools) |
| OpenAI Responses Multi-agent | Root 可建立分层 Agent tree，并使用 spawn/message/follow-up/wait/interrupt/list 协作；各 Agent 保持独立上下文，Root 负责最终答案。 | **Beta**，仅 GPT-5.6；所有 Agent 共享请求的模型和工具，item schema 仍可能变化，且不支持 `max_tool_calls`。它是 Provider 托管能力，不能替代 SheJane 的跨 Provider Run/权限/结算协议。[Multi-agent](https://developers.openai.com/api/docs/guides/responses-multi-agent) |
| Codex | 专门 Agent thread、并行汇总、查看 thread、follow-up、steer、stop；当前本地版本默认支持 Subagent workflow。 | 官方文档未标 Preview，但每个 Agent 都会独立消耗模型和工具资源；应只分发边界清楚的独立任务。[Codex Subagents](https://developers.openai.com/codex/subagents) |
| Claude Agent SDK | 新鲜隔离上下文、同步/后台子 Agent、模型与工具限制、Agent ID、usage 与工具统计；SDK 能恢复子 Agent。 | 正式 Agent SDK 能力。后台大量 fan-out 会触发速率限制；没有 per-subagent wall-clock deadline，官方建议使用 turn 上限和 stall watchdog。[Subagents](https://code.claude.com/docs/en/agent-sdk/subagents) · [Hosting](https://code.claude.com/docs/en/agent-sdk/hosting) |
| Claude Managed Agents | Coordinator roster、持久隔离 session thread、线程事件、follow-up、独立 interrupt、Agent 级工具/MCP、跨线程权限路由。 | **Public Beta**，要求 `managed-agents-2026-04-01` header；最多一层委派、20 个 roster Agent、25 个并发 thread。[Multiagent orchestration](https://platform.claude.com/docs/en/managed-agents/multiagent-orchestration) |
| Claude Code Agent Teams | 独立 teammate session、共享 task list、依赖、自领取、peer messaging、直接 steering、计划审批和 hooks。 | **Experimental，默认关闭**；官方列出恢复、任务状态、shutdown、固定 lead、无 nested team 等限制。没有 worktree 隔离，必须避免同文件并行编辑。[Agent Teams](https://code.claude.com/docs/en/agent-teams) |
| Deep Agents | 稳定同步 `task()` Specialist；远程 Agent Protocol 后台任务支持 launch/check/update/cancel/list。 | 同步 Subagent 是常规能力；Async Subagent 是 **Preview**，API 可能变化，而且依赖 Agent Protocol server/部署。[Sync](https://docs.langchain.com/oss/python/deepagents/subagents) · [Async Preview](https://docs.langchain.com/oss/python/deepagents/async-subagents) |
| LangChain/LangGraph | 官方推荐用普通 tools 直接构造 supervisor；Router 支持 fan-out；Swarm 通过 handoff 切换 active Agent 并保存当前 Agent。 | 官方已明确：多数场景优先手写 tool-based supervisor，而不是先引入 `langgraph-supervisor` 封装。[Supervisor repo](https://github.com/langchain-ai/langgraph-supervisor-py) · [Router](https://docs.langchain.com/oss/python/langchain/multi-agent/router) · [Swarm repo](https://github.com/langchain-ai/langgraph-swarm-py) |
| Pi | 核心只提供 Agent loop、session、events、steering、SDK/RPC 和 extensions；Subagent 可由扩展实现。 | 多 Agent 不是 Pi 核心能力。第三方 Subagent extension 不能算 Pi 自带，也不能据此要求每个 Harness 都实现 Team runtime。[Pi primitives](https://pi.dev/#primitives-not-features) |

因此，“最近看到的多 Agent 模式”确实已经进入主流产品，但成熟度差异很大：Codex/Claude Agent SDK 的 Subagent 已是普通产品能力；OpenAI Responses Multi-agent 与 Claude Managed Agents 仍是 Beta；Claude Agent Teams 与 Deep Agents Async Subagents 分别仍是 Experimental 和 Preview。路线图不能把这些托管或预览能力直接当作 SheJane 的稳定跨 Provider 依赖。

### 11.3 多 Agent Runtime 的最低验收合同

只有同时满足以下合同，才能从“能调用另一个模型”升级为“完整多 Agent Runtime”：

| 维度 | 最低验收条件 |
| --- | --- |
| 上下文 | 明确子 Agent 获得的是完整父历史、选择性摘要还是全新上下文；任务描述、项目指令、附件和 workspace 的继承规则固定且可检查；子结果以结构化边界返回，不能把整个内部 transcript 无限制灌回父上下文。 |
| 状态 | 每个异步子任务有稳定 `subagent_task_id`/`thread_id`、父 Run ID 和 Agent definition version；至少有 queued/running/waiting/completed/failed/cancelled 终态；进程重启后仍能查询。 |
| 权限 | 子 Agent 不能因委派而提权；工具、MCP、secret 和 workspace 权限按 Agent 定义取最小集合；审批必须携带来源 thread，批准结果只回到发起者。 |
| 取消 | 取消父 Run 时向所有未结算子 Agent 传播；后台模式还要支持只取消指定子 Agent；工具取消完成前不能提前写 `cancelled` 终态。 |
| Steering | follow-up 必须指向稳定 child ID；要定义是排队到下一轮、立即 interrupt 后续跑，还是创建新 attempt；重复请求必须幂等。 |
| 事件 | 父流至少持久记录 spawned/running/waiting/completed/failed/cancelled 和结果摘要；子流可提供独立 delta/tool/approval 事件；临时 preview 与权威 terminal record 分开。 |
| 资源预算 | 同时有父 Run 总预算和每个 child 的模型调用、token、工具、时间及并发上限；达到 child 上限不能吞掉给父 Agent 保留的收尾预算；UI/diagnostics 可看到消耗。 |
| 失败传播 | 子失败必须以失败状态和原因返回，不能伪装成空摘要；fan-in 要明确 all-required、best-effort 或 quorum；重试不得重复已成功的外部副作用，父 Agent 结束前必须处理所有 required child。 |
| 工作区冲突 | 并行只读可以共享；写操作需要文件所有权、worktree、写锁或冲突检测。不能让两个 Agent 靠最后写入覆盖彼此结果。 |

这个合同也是判断是否应采用现成框架的标准：如果框架只提供 `spawn()`，而取消、审批、预算、事件和恢复仍落回产品自己处理，它并没有替代 Runtime。

### 11.4 SheJane 当前实际达到的层级

| 验收维度 | 当前事实 | 状态 |
| --- | --- | --- |
| 协作模式 | 内置 general-purpose/researcher/writer 和用户 Markdown Agent；主 Agent 通过同步 `task()` 委派，单轮多个调用可并行。 | ✅ Manager-as-tools + fan-out/fan-in |
| 上下文 | 子 Agent 使用独立 context window；父对话不会整体复制，只传模型生成的 task description；共享授权 workspace backend，返回摘要。 | ✅ |
| 工具与权限 | Specialist 有工具 allowlist；researcher 禁写/执行且限制 web 调用；所有子 Agent 经过 Runtime ToolReview、ToolExecution、持久回执和文件冲突中间件；`memory.write` 明确移除。 | ✅ |
| 预算 | 子 Agent 有模型调用上限，researcher 有 web 上限；`RuntimeModelProxy` 让调用进入父 Run 的持久模型账本，并为父 Agent 保留收尾调用。 | ✅ 基础预算 |
| 并行写安全 | 只读工具和子 Agent 编排可并行；后果性工具共享执行门，另有文件写冲突检测。 | ✅ 本地基础边界 |
| 独立状态 | 生命周期只存在于父 Run 的 `task` tool call/checkpoint；没有稳定 child thread/job，也不能在 Runtime 重启后独立列出一个子 Agent。 | ❌ |
| 取消与 steering | 父 Run 可以整体取消；没有面向单个正在运行 `task()` 的独立取消、follow-up 或 steering。 | 🟡 仅父级 |
| 事件 | 有 `subagent.spawned` 与带 status 的 `subagent.completed`；没有独立 child stream、明确 `subagent.failed/cancelled/waiting` 终态，也没有 child usage 摘要。 | 🟡 |
| 失败汇总 | 子 Agent 异常会生成失败 ToolMessage 和持久失败 receipt，不会按未知外部副作用处理；但没有显式 all-required/best-effort fan-in 策略。 | 🟡 |
| Handoff/Team | 没有 active Agent 转移、peer messaging、共享 task list、自领取、teammate session 或用户直接进入 child thread。 | ❌ |

证据：[`agent/subagents.py`](../runtime/src/shejane_runtime/agent/subagents.py)、[`agent/builder.py`](../runtime/src/shejane_runtime/agent/builder.py)、[`event_translator.py`](../runtime/src/shejane_runtime/event_translator.py)、[`middleware/tool_execution.py`](../runtime/src/shejane_runtime/middleware/tool_execution.py)、`test_subagents.py`、`test_e2e_capabilities.py` 和 `test_tool_receipts.py`。

当前定位应写成：**已完成同步 Specialist 委派和并行汇总；尚未实现持久后台 Subagent、真正 Handoff 或共享任务 Agent Team。**

### 11.5 最小分阶段路线，不追逐复杂 Swarm

| 阶段 | 要做什么 | 为什么现在做/不做 |
| --- | --- | --- |
| Step 1：补齐当前同步层的产品合同 | 在现有事件上增加稳定 child ID、Agent type、`failed/cancelled` 终态和 child usage 摘要；增加小而明确的并发上限及 fan-in required/best-effort 语义；扩充真实 Subagent Eval。 | 复用现有 `task()`、Run ledger、SSE 和 receipt，不引入新编排框架；直接修复当前诊断和质量盲点。 |
| Step 2：有长任务需求时增加本地 durable child run | 用现有 `RunCoordinator`、job/lease/checkpoint/event/permission 基础设施派生 child Run；提供 list/check/follow-up/cancel，并让 Client 可打开 child thread。 | 比直接绑定 Deep Agents 远程 Preview 更符合本地 Runtime 边界，也复用已经验证的恢复与审批能力。没有真实长任务前不做。 |
| Step 3：有多轮领域接管需求时增加 Handoff | 让 thread 保存 `active_agent_definition_id`；handoff 事件原子切换 active Agent，并定义历史过滤、返回主 Agent 和审批边界。 | 只有 Specialist 必须直接与用户多轮对话时才值得做；研究/审查不需要。 |
| Step 4：最后评估共享任务 Agent Team | 先用 Eval 证明 peer debate、自领取或动态重分工明显优于 Manager fan-out；再设计共享 task ownership、mailbox、workspace 隔离和 lead failure recovery。 | Claude Agent Teams 仍是实验性能力且官方已列出恢复、状态和 shutdown 问题。SheJane 当前没有足够收益证据，不应先造 Swarm。 |

明确不建议：

- 不把依赖中的 `AsyncSubAgentMiddleware` 打开就宣称完成；它需要独立 Agent Protocol server，仍是 Preview，而且绕不开 SheJane 自己的权限、凭据、事件和结算合同。
- 不增加多层递归 Supervisor。Anthropic Managed Agents 也把 Coordinator 限制为一层；LangChain 当前官方建议优先普通 tool-based supervisor。
- 不让多个写 Agent 无所有权地共享同一工作区。先按文件/模块分区；真正需要重叠编辑时再用 worktree 或显式合并。
- 不把 Agent 数量当能力指标。Codex、Claude 和 Agent Teams 官方都指出并行会线性增加 token/速率/协调成本，只应派发彼此独立且结果边界清楚的任务。

这一阶段的产品目标不是“做一个 Swarm”，而是让当前同步多 Agent 具备可诊断、可计费、可失败的完整合同；只有用户确实需要长时间后台协作时，再把 child 提升为独立 durable Run。
