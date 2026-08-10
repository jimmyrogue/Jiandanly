# Runtime SQLite store

Runtime SQLite 是 P3 接纳后的执行真相：命令回执、Run、Job 租约、等待状态、Tool Receipt、Artifact 和事件投影都由 Runtime 持有。LangGraph checkpoint 与 LangGraph Store 仍使用独立数据库，不并入这里。

## 稳定入口

业务代码继续从 `shejane_runtime.store.sqlite` 导入 `LocalStore` 以及既有错误和容量常量。`sqlite.py` 只负责：

- 组装各领域 store，保持原有 `LocalStore` 方法签名；
- 打开连接、创建 schema、执行兼容迁移；
- 为旧调用方重新导出稳定错误与常量。

拆分不要求 HTTP handler、RunCoordinator、middleware 或 tool 改变导入路径。新代码应把实现放进状态所有者模块，而不是继续向 facade 添加领域逻辑。

## 模块所有权

| 模块 | 所有权 |
|---|---|
| `database.py` | SQLite 连接配置、共享连接生命周期、租约绑定与 fenced 写事务 |
| `schema.py` / `migrations.py` | 新数据库完整 schema / 旧数据库兼容迁移 |
| `command_receipts.py` | Run、等待命令与插件共享的不可变命令回执 |
| `run_commands.py` | Run 接纳、线程消息写入与启动 Job 的原子事务 |
| `run_control_commands.py` | Run tree 取消、取消命令回执与运行中指令注入 |
| `run_wait_commands.py` | question、permission、plan 与 tool reconciliation 的不可变恢复决定 |
| `run_jobs.py` | Job 入队、领取、续租与过期恢复 |
| `run_job_quarantine.py` | 执行尝试隔离、清理证明确认与 fence 校验 |
| `run_records.py` | Run 记录、graph branch head 与查询投影 |
| `run_state.py` | 原子结果提交、事件日志、线程投影与 steering |
| `threads.py` | Thread 分页、快照、元数据更新、删除与增量游标 |
| `waits.py` | plan、permission 与 wait-cycle 恢复负载 |
| `wait_questions.py` | `user.ask` question 的创建、查询与回答 |
| `wait_reconciliation.py` | tool outcome-unknown reconciliation 状态与 Receipt 修复 |
| `tool_receipts.py` | P10 Tool Receipt、重放和 outcome-unknown 状态转换 |
| `subagent_receipts.py` | P10 `task` Receipt 的子代理快照、生命周期事件和线程游标投影 |
| `collaboration.py` | durable child Run 接纳、依赖与资源声明的原子写事务 |
| `collaboration_projection.py` | child Run 查询、资源所有权校验与 root collaboration 一致性快照 |
| `agent_messages.py` | 同一 collaboration root 内的 Agent mailbox、额度、投递与确认 |
| `artifacts.py` | Artifact、Run input 正文、配额与内容生命周期 |
| `plugins.py` | 稳定的 Plugin Store 聚合门面 |
| `plugin_catalog.py` | plugin catalog 查询与 Run binding 冻结解析 |
| `plugin_packages.py` | plugin package 安装、更新、回滚与移除命令 |
| `plugin_installations.py` | installation 启用状态与模型绑定配置 |
| `plugin_setup.py` | revision-fenced plugin setup 流程 |
| `configuration.py` | Runtime settings 与 MCP catalog 的稳定聚合门面 |
| `model_connections.py` | model connection 生命周期与 official connection 原子替换 |
| `model_capability_bindings.py` | model capability binding 的查询与修订 |
| `model_calls.py` | 模型调用预算/结算、assistant draft 与 sandbox process 记录 |
| `schedules.py` / `workspaces.py` | 定时 Run / 授权 workspace |
| `codec.py` / `ids.py` / `errors.py` / `events.py` | 无状态共享原语与稳定类型 |

这些类是 `LocalStore` 的实现分片，共享同一个 `SqliteDatabase` 连接模型；它们不是独立 repository，也不拥有第二份状态。

## 事务规则

一次产品状态变化必须只有一个提交边界：

1. 需要 Attempt fencing 的 Run 写入使用 `run_write_transaction(run_id)`。
2. 跨表原子更新在同一个 `aiosqlite.Connection` 上完成。
3. 事务内复用以 `_uncommitted` 结尾的窄方法，并显式传入 `conn`；不要从事务内调用会另开连接或自行提交的公开方法。
4. 命令先持久化再返回回执；Job 领取、Tool Receipt、等待决定和最终结算不得降级为内存状态。
5. `LocalStore` 是唯一面向上层的组合对象。领域模块之间可以通过 `self` 调用既有窄原语，但不得复制 SQL 来绕过状态所有者。

这保持 P2 → P3 → P4/P5 的原子边界，并让 P10/P11/P12 继续从同一 Runtime 真相投影。

## 扩展步骤

新增持久化能力时：

1. 先在 `docs/harness-runtime-stages.md` 确定 `primary_stage` 和上下游契约。
2. 选择现有状态所有者模块；只有出现新的真实领域时才新增模块。
3. 新表或索引加入 `schema.py`；已有安装需要的兼容变化加入 `migrations.py`。不要在领域方法中临时迁移 schema。
4. 保持既有 facade 方法与错误语义；确需公开的新错误放入 `errors.py` 并由 `sqlite.py` 兼容导出。
5. 先跑所属领域的聚焦测试，再跑 `make test-runtime`、`make lint` 和 `git diff --check`。

测试需要调整容量上限时，应 monkeypatch 常量的所有者模块；生产调用方仍可从 `sqlite.py` 导入兼容常量。
