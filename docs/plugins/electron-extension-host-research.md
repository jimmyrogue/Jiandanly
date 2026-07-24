# Electron Extension Host 对 SheJane 插件的可借鉴部分

> 核验日期：2026-07-24
>
> 原文：[为 Electron App 设计扩展系统](https://cy4n.dev/post/designing-ext-system-in-electron)
>
> 范围：只评估身份、宿主能力、声明式贡献和生命周期；不设计新的扩展平台

## 定位校正

原文设计的不是单纯的“UI 扩展宿主”，而是一套 **通用 Electron extension host**：

- 每个扩展运行在独立的 `utilityProcess`，可以使用完整 Node.js 环境和本地 CLI。
- 主进程创建并转交专属 `MessagePort`，扩展进程与 Renderer 通过 RPC 通信。
- 宿主向扩展注入高层 API，代理鉴权请求、Rust 和原生能力。
- WebView 是当前最主要的 contribution，同时允许无头扩展。

Electron 官方确认 `utilityProcess` 是带 Node.js 集成和 MessagePort 的 Chromium 子进程；它适合承载易崩溃或高 CPU 的组件，并能与 Renderer 建立直连通道。[Electron `utilityProcess`](https://www.electronjs.org/docs/latest/api/utility-process) [Electron Process Model](https://www.electronjs.org/docs/latest/tutorial/process-model#the-utility-process)

因此，原文和 SheJane 解决的是相邻问题：

```text
原文：扩展进程 -> Extension API -> Electron App 能力 -> 可选 WebView
SheJane：Action 实例 -> ActionExecutor -> Runtime capability -> Artifact / receipt
```

SheJane 不需要复制 Electron Extension Host，但可以检验其中四项宿主原则。

## 1. 身份绑定到进程或通道，而不是消息自报

### 原文与一手依据

原文为每个扩展创建独立进程和独立 `IpcEndpoint`，权限层根据消息来自哪个 Port 识别扩展，不在 wire message 中接受 `plugin_id` 作为授权依据。

这个结论有明确的传输基础，但不是 MessagePort 自动提供的认证功能：

- WHATWG Channel Messaging 规定一个 channel 只有两个相互 entangled 的 Port，一端发送的数据由另一端接收；Port 转移的是所有权，不是复制，并可作为 object-capability 的基础。[HTML Standard: Message ports](https://html.spec.whatwg.org/multipage/web-messaging.html#ports-as-the-basis-of-an-object-capability-model-on-the-web)
- Electron 主进程创建 Port，并把指定一端转移给指定 `utilityProcess`；远端断开时，宿主持有的一端收到 `close`。[Electron `UtilityProcess.postMessage`](https://www.electronjs.org/docs/latest/api/utility-process#childpostmessagemessage-transfer) [Electron `MessagePortMain`](https://www.electronjs.org/docs/latest/api/message-port-main)
- Electron 对普通 IPC 仍要求验证真实 `sender`，不能相信任意 frame 发来的消息。[Electron Security: validate IPC sender](https://www.electronjs.org/docs/latest/tutorial/security#17-validate-the-sender-of-all-ipc-messages)

所以“Port 即身份”准确说是：**宿主创建专属通道后，可以把本地授权上下文绑定到持有的 endpoint；消息内容仍然全部不可信。**

### SheJane 对照

SheJane 已经采用了更强的 Runtime 身份链：

- P3 冻结精确插件 ID、版本、package digest 和 Action catalog hash。
- P6 的 [`PluginExecutionLease`](../../runtime/src/shejane_runtime/plugins/catalog.py) 从冻结绑定重新校验实际 package digest、manifest 身份和 catalog hash，返回不可变 contribution view。
- P10 由 [`PluginToolAdapter`](../../runtime/src/shejane_runtime/plugins/tools.py) 根据 lease 中的 `PluginActionDescriptor` 构造 invocation；Worker 自己不能选择自己代表哪个插件。
- Managed Worker 每次 invocation 新建一个进程，`initialize` 的身份和 grant 来自 Runtime；结果中回显的 `invocation_id`、`operation_id` 只做一致性校验，不产生权限。
- Worker 发起 `model/vision/invoke` 时，授权上下文来自当前 executor 闭包中的 invocation 和 model binding，而不是 Worker 自报的 publisher 或 plugin ID。[Action Protocol v1](./action-protocol-v1.md#managed-worker-adapter)

### 判断

**已实现**

- package digest、frozen binding、execution lease 和 one-shot executor 已经把身份绑定到 Runtime 创建的执行域。
- “不相信 Worker 自报身份”已经落实在 catalog、invocation 和 host call 校验中。

**值得补强**

- 把以下规则保留为所有未来 Worker-to-Runtime host call 的 conformance 要求：授权只读取 lease / invocation context，消息中的身份字段只能用于检测矛盾。
- 身份 endpoint 一经绑定，不再向插件开放任意 Port 转移；MessagePort 可以再次 transfer，所以“专属 Port”还需要宿主限制转移面。
- 如果未来引入进程池或一个进程承载多次调用，必须重新建立每次 invocation 的独立 endpoint 或等价不可伪造绑定；当前“一调用一进程”没有这个混淆问题。

**不应采用**

- 不把 `plugin_id`、`publisher`、`permissions` 或 Worker 返回的隔离状态当作授权证据。
- 不建立所有插件共享、再依赖消息自报身份的全局双向 RPC endpoint。

## 2. Host-mediated capability broker

### 原文与一手依据

原文让扩展调用宿主提供的 API，由主进程或 Rust Core 执行鉴权请求并注入 Token；扩展只获得结果，不直接持有 Token。

这个方向成立，但原文的“双方都是有状态的巨型单体服务”不适合作为 SheJane 的安全接口。Electron 官方明确指出：

- 不能把 `ipcRenderer.send` 这类强能力整体暴露给不可信内容。
- 应当为每一种允许的 IPC 行为暴露一个窄方法，并在 privileged side 校验 sender 和参数。[Electron Context Isolation](https://www.electronjs.org/docs/latest/tutorial/context-isolation#security-considerations)

另外，`utilityProcess` 默认是完整 Node.js 环境，`env` 默认继承 `process.env`，还能使用 Node API 和网络。它是进程故障边界，不是 capability sandbox。[Electron `utilityProcess` options](https://www.electronjs.org/docs/latest/api/utility-process#utilityprocessforkmodulepath-args-options)

### SheJane 对照

SheJane 当前的 `model.vision.invoke` 已经是更窄的 capability broker：

1. manifest 声明 capability，但 Runtime 只把有效 grant 放进 invocation；
2. Action 必须有冻结的 model binding；
3. Managed Worker 只允许一次 `model/vision/invoke`；
4. Runtime 重新校验 input ID、MIME、大小、摘要和请求上限；
5. Provider key 从 credential store 获取，Worker 永远看不到 key、credential reference、base URL 或任意 headers；
6. 未声明方法、第二次调用、错误 binding 和未知字段全部 fail closed。

WASI 路径更严格：默认 capability set 为空，Host 不开放文件系统、网络、环境、时钟或真实随机数。[Action Protocol v1](./action-protocol-v1.md#wasi-adapter)

### 判断

**已实现**

- [`ActionExecutor`](../../runtime/src/shejane_runtime/plugins/executor.py) 是执行 seam；插件只能经由 Runtime 构造的 invocation 和显式 host call 使用能力。
- `model.vision.invoke` 已证明“窄方法 + 冻结 grant + Runtime 持有凭证 + 有界结果”的模式可行。

**值得补强**

- 每增加一个 host capability，单独冻结方法名、输入 Schema、调用次数、资源上限、审批语义、receipt 影响和输出清理；复用现有 Action Protocol，不先造通用 broker 框架。
- capability 的实现只能缩小 manifest 请求，不能让 manifest 扩大平台策略；不可用能力继续 fail closed。
- 将敏感能力的原始凭证、宿主路径、任意命令和网络 socket 永久排除在 Worker envelope 外。

**不应采用**

- 不复制原文的“巨型 AppService”或任意 `runtime.call(method, args)`。
- 不把独立进程、TypeScript 类型或 case-by-case 校验误认为 trust-boundary validation。
- 不允许 Node Worker 通过原生 `fetch`、`fs`、`child_process` 绕过 broker；只有 OS adapter 实际执行访问隔离时才能标记 `sandboxed=true`。

## 3. Manifest contributions 与 lazy activation

### 原文与一手依据

原文把影响宿主 UI 的入口静态写入 `package.json.contributions`，宿主无需先运行扩展代码就能展示侧边栏；真正需要能力时再调用 `activate()`。

VS Code 使用同一原则：

- Contribution Points 是 extension manifest 中的静态 JSON 声明。[VS Code Contribution Points](https://code.visualstudio.com/api/references/contribution-points)
- Extension Host 根据 activation events 惰性加载代码，避免无关扩展占用启动时间、CPU 和内存。[VS Code Extension Host](https://code.visualstudio.com/api/advanced-topics/extension-host#stability-and-performance)
- `*` 启动激活只应在其他 activation event 都无法满足时使用。[VS Code Activation Events](https://code.visualstudio.com/api/references/activation-events#start-up)

### SheJane 对照

SheJane 已经把贡献和执行分开：

- [`PluginManifest`](../../runtime/src/shejane_runtime/plugins/manifest.py) 严格声明 Actions、Skills、Commands 和 MCP bindings，并拒绝未知字段、重复 ID 和非规范包路径。
- `/v1/plugins` 直接经过 `PluginRegistry.list()` 查询 SQLite 中的安装记录和已存 manifest；展示插件列表不读取 package、不创建 executor，也不启动 Worker。[Plugin Registry](../../runtime/src/shejane_runtime/plugins/registry.py) [SQLite store](../../runtime/src/shejane_runtime/store/sqlite.py)
- P6 的 PluginCatalog 只在 Run 已冻结插件绑定后加载精确 package 和 contribution snapshot。
- 真正 Action invocation 才经 [`ActionExecutor`](../../runtime/src/shejane_runtime/plugins/executor.py) 创建新的 WASI instance 或短命 Managed Worker。

这比传统 `activate()` 更适合 Agent Run：发现、选择、冻结和执行是四个不同阶段，打开 UI 不能隐式改变运行状态。

### 判断

**已实现**

- manifest contribution、严格解析、固定 catalog hash、Run snapshot 和 invocation-time execution 已形成懒加载链。
- 当前 v1 明确没有插件自启动、后台任务和任意 lifecycle hook。[Runtime 插件 ADR](../adr/0001-runtime-plugin-platform.md#4-非目标)

**值得补强**

- “插件列表和命令发现只读 Registry 投影，不启动代码”已经由集成测试守住：测试包中的 bridge 一旦启动就写入 marker，读取 `/v1/plugins` 后 marker 必须不存在。
- 用随仓库发布的 Browser QA Runtime Asset 实测：247 MB 压缩、约 535 MB 解压、359 个归档文件；P6 完整摘要校验约 `0.25–0.27s`。将同一份资产解除 symlink 后模拟 Windows 文件布局，别名首次准备约 `0.24s`、后续身份校验约 `0.17s`。完整摘要校验仍留在 P6；Browser QA 专属别名准备已推迟到首次 Browser QA Action，不再由只使用其他工具的 Run 支付。[Browser QA service](../../runtime/src/shejane_runtime/plugins/browser_qa.py) [Agent builder](../../runtime/src/shejane_runtime/agent/builder.py)
- 未来如出现真实 UI contribution，也只能静态声明固定挂载点，并继续绑定同一个 plugin ID、version 和 package digest；它不是第二套 Electron 插件。
- UI contribution 的显示条件可以类似 activation event，但执行权限仍必须在 Runtime invocation 时重新判断。

**不应采用**

- 不在 Client 启动、进入插件 Tab、读取 manifest 或显示命令时运行插件 `activate()`。
- 不加入 `*` 式全局启动事件、长期 Node 进程池或“先激活再询问它提供什么”的动态发现。
- 没有独立页面/面板的明确产品需求前，不增加 WebView contribution。

## 4. Lifecycle、disposables 与故障恢复

### 原文与一手依据

原文由 `ExtensionContext.disposables` 收集卸载时的清理任务，以此代替扩展自行导出的 `deactivate()`。这是良好的资源归属模式，但它只解决“集中登记”，不能证明崩溃后的清理已经完成。

VS Code 的 `ExtensionContext.subscriptions` 也会在 extension deactivation 时统一 dispose，但官方注明异步 `dispose()` 不会被等待；另一个可选的 `deactivate()` 可以返回 Promise 完成异步清理。[VS Code `ExtensionContext.subscriptions`](https://code.visualstudio.com/api/references/vscode-api#ExtensionContext) [VS Code Activation lifecycle](https://code.visualstudio.com/api/references/activation-events#start-up)

Electron 提供的信号也只是生命周期输入：

- `UtilityProcess` 会发出 `error` 和 `exit`，`kill()` 是 graceful termination。[Electron `UtilityProcess`](https://www.electronjs.org/docs/latest/api/utility-process#class-utilityprocess)
- `MessagePortMain.close` 表示远端断开。[Electron `MessagePortMain`](https://www.electronjs.org/docs/latest/api/message-port-main#event-close)

Port 关闭或直接子进程退出都不能单独证明其子进程树、临时目录、外部副作用和持久化状态已经安全收敛。

### SheJane 对照

SheJane 的生命周期比 extension-level disposables 更强：

- Run 创建 Runtime-owned `AsyncExitStack`，P6 通过它获取 [`PluginExecutionLease`](../../runtime/src/shejane_runtime/plugins/catalog.py)。
- [`invoke_managed_worker`](../../runtime/src/shejane_runtime/plugins/managed_worker.py) 在正常路径请求 `shutdown`；超时、取消、协议错误和 Runtime 错误进入同一停止路径，先协作取消，再终止并 reap 整个进程树。
- invocation staging 由 `try/finally` 回收；VM staging 带 lease，启动时只清理能证明不再使用的 stale directory。
- P11 在终态提交前等待 `AsyncExitStack.aclose()`；如果清理失败，Runtime 把 execution attempt 标记为 `execution_cleanup_unconfirmed` 并隔离，不自动重试。
- P12 只在清理与结果语义明确后结算 receipt、Artifact 和 Run。[Action Protocol v1](./action-protocol-v1.md#runtime-ownership)

这是一种比全局常驻 Extension Host 更适合 SheJane 的混合生命周期：Run 级 lease 保证定义冻结与统一清理，WASI/Managed Worker 按 invocation 短命，只有确实需要跨多个 browser/computer Action 保存会话的 built-in service 才活到 Run 结束。

### 判断

**已实现**

- Runtime-owned cleanup、one-shot process、process-tree reap、staging lease、P11 await 和 cleanup failure quarantine 已经覆盖原文 disposables 的主要价值。
- [`PluginExecutionLease.aclose()`](../../runtime/src/shejane_runtime/plugins/catalog.py) 当前只关闭 snapshot 状态是合理的：v1 不持有跨 invocation 的插件进程，真正执行资源由 Adapter 的受控生命周期管理。

**值得补强**

- 每个新 Adapter 或 host capability 在取得资源后立即把清理责任交给 Runtime-owned stack，或在同一函数中用不可绕过的 `try/finally` 管理；不能依赖插件主动登记。
- 继续用黑盒 Gate 验证 crash、timeout、cancel、Runtime shutdown 和 cleanup failure 后没有残留进程、pipe、staging lease 或可提交 Artifact。
- Port/EOF/exit 只能触发故障处理；P11 的“资源已经关闭”证据和 quarantine 规则不能退化成“收到了 exit”。

**不应采用**

- 不向插件开放任意 disposable、`activate()` / `deactivate()` hook 或后台常驻资源。
- 不让插件返回“清理完成”作为 Runtime 的唯一证据。
- 不在 `outcome_unknown` 或 cleanup unconfirmed 时盲目重启并重放 effectful Action。

## 汇总结论

| 原文原则 | SheJane 状态 | 决定 |
| --- | --- | --- |
| 身份绑定到专属进程/通道 | frozen binding + digest + lease + one-shot executor 已更强 | 保持现有身份链；未来多路复用时重新证明隔离 |
| 宿主代理敏感能力 | `model.vision.invoke` 已落地窄 broker | 按真实需求逐项增加能力，不建巨型 RPC |
| manifest contributions + lazy activation | manifest / registry / P6 snapshot / P10 invoke 已分层 | 把“浏览不激活代码”固化为测试 |
| disposables + failure boundary | AsyncExitStack + process-tree cleanup + P11 quarantine 已更强 | 保留 Runtime-owned cleanup，不交给插件 |

当前没有需要实施的新平台。真正值得吸收的是四条约束：

1. 授权上下文来自宿主创建的执行域，消息只作为不可信输入。
2. 每项敏感能力使用独立、有界、可审计的 Host method。
3. discovery 完全静态，代码只在确定 invocation 时启动。
4. 清理由 Runtime 等待并证明；无法证明时隔离，而不是假定成功。
