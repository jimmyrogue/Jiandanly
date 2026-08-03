# Agent 沙盒实现与 SheJane 内存优化调研

> 检索日期：2026-08-01  
> 范围：OpenAI Codex、Anthropic Claude Code、Google Gemini CLI、OpenHands，以及 Firecracker、Wasmtime 等代表性沙盒技术。只采用项目官方文档、官方源码说明和官方工程文章。官方没有披露的数据统一标为“未公布”，不把配置上限当作实测内存。

## 结论先行

1. **主流本地编程 Agent 通常不会为每条命令启动一台 VM。** Codex、Claude Code 和 Gemini CLI 的默认轻量路径，主要是 macOS Seatbelt、Linux bubblewrap、Windows 受限令牌或 ACL。它们隔离的是命令进程树，启动快、没有独立 Guest OS 的常驻内存，但仍与宿主共享内核。
2. **需要执行真正不可信的原生代码时，行业仍会选择更重的边界。** Claude Code Web 使用每会话隔离 VM；OpenHands 默认使用每会话容器；Firecracker 用 microVM；远程沙盒服务也普遍把工作负载放进容器或 VM。轻量本地沙盒并不是虚拟机的等价替代品。
3. **SheJane 当前的 Managed Worker VM 是“沙盒的一种实现”，不是“沙盒”这个概念本身。** 它用 Linux Guest、无虚拟网卡、显式 `/input`/`/output`、资源上限和销毁流程实现强隔离；WASI Worker 则是另一条更轻的沙盒路径。
4. **现在不建议把 Managed Worker 全面换成 Seatbelt 或普通进程沙盒。** 这会降低对恶意原生二进制、依赖漏洞、子进程树和宿主内核攻击面的隔离，尤其不适合文档渲染、媒体处理、本地语音与本地视觉等会运行大型原生依赖的能力。
5. **最值得先做的内存优化不是复用一台常驻 VM，而是按动作校准内存、限制并发，并把适合的能力迁到 WASI。** 这三项既能降低峰值，又不需要把原生不可信代码放回宿主。Apple 的内存气球可作为长任务的后续实验，但不应在缺少宿主 `phys_footprint` 数据时先实现。

## 1. 研究口径：必须分清四件事

“沙盒”是安全目标，“VM、容器、系统沙盒、Wasm”是不同实现。比较时至少要分开：

| 维度 | 要回答的问题 |
| --- | --- |
| 隔离边界 | 共享宿主内核，还是拥有独立 Guest 内核？攻击者突破后能碰到什么？ |
| 生命周期 | 每条命令、每个 Agent 会话、每个任务，还是常驻复用？ |
| 资源数据 | 是配置上限、预留量、实测 RSS，还是只算 VMM 自身开销？ |
| 权限模型 | 默认可读什么、可写什么、能否联网、凭据是否进入沙盒？ |

特别需要避免两个误判：

- VM 的 `memorySize` 是 Guest 可见的容量，不等于宿主立即产生同等 RSS。Apple 明确说明，这块连续虚拟地址空间不会立即全部获得物理页，物理页会随着 Guest 使用而分配（[VZVirtualMachineConfiguration.memorySize](https://developer.apple.com/documentation/virtualization/vzvirtualmachineconfiguration/memorysize)）。
- Firecracker 的“低于 5 MiB”是 VMM 额外开销，不包含 Guest 配置内存、Guest kernel、rootfs 和工作负载本身（[Firecracker FAQ](https://github.com/firecracker-microvm/firecracker/blob/main/FAQ.md)）。

## 2. 主流 Agent 如何做沙盒

### 2.1 OpenAI Codex

#### 本地路径

- **macOS：** 使用 Apple Seatbelt；它是宿主内核上的进程策略沙盒，不启动 VM。OpenAI 的 Windows 沙盒工程文章同时概括了 Codex 在 macOS 使用 Seatbelt、在 Linux 使用 Landlock/bubblewrap 的平台路径（[Building the Codex Windows sandbox](https://openai.com/index/building-codex-windows-sandbox/)）。
- **Linux：** 当前默认使用 bubblewrap。Codex 会只读绑定根文件系统，只把声明的 writable roots 重新绑定为可写，再把 `.git`、`.codex` 等受保护路径覆盖为只读；同时使用 user/PID namespace、`PR_SET_NO_NEW_PRIVS` 和 seccomp。禁网模式会创建独立 network namespace；需要代理时，宿主代理通过显式桥接进入 namespace，并用 seccomp 阻止任意 AF_UNIX 连接。Landlock 是显式选择的 legacy fallback（[Codex Linux sandbox README](https://github.com/openai/codex/blob/main/codex-rs/linux-sandbox/README.md)）。
- **Windows：** 原生沙盒不是 VM。`elevated` 模式创建专用低权限用户，利用受限令牌、ACL、防火墙规则和独立桌面隔离网络与文件；`unelevated` 模式从当前用户令牌派生受限令牌，兼容性更好但边界较弱（[Codex Windows sandbox 文档](https://learn.chatgpt.com/docs/windows/windows-sandbox)、[工程实现说明](https://openai.com/index/building-codex-windows-sandbox/)）。OpenAI 明确评估过 Windows Sandbox，但没有选择这种一次性 VM，因为它需要复杂的宿主—Guest 桥接、初始化成本，而且并非所有 Windows SKU 都可用。
- **WSL2：** 走 Linux bubblewrap 路径；WSL1 不受支持（[Codex Linux sandbox README](https://github.com/openai/codex/blob/main/codex-rs/linux-sandbox/README.md)）。

Codex 本地隔离单元是“被启动的命令及其子进程”，不是每条命令一台 VM。文件、网络权限来自启动时构造的策略。官方没有公布 Seatbelt/bubblewrap/Windows helper 的单次增量 RSS、空闲内存或绝对冷启动数据；本地包包含平台二进制和沙盒 helper，不包含 Guest kernel/rootfs，沙盒部分的准确包体增量也未公布。

#### 云端路径

Codex Cloud 为每个任务创建独立隔离环境，任务之间互不共享运行环境（[Introducing Codex](https://openai.com/index/introducing-codex/)）。OpenAI 后续披露容器缓存令中位任务完成时间下降 90%，说明镜像准备与依赖缓存是重要成本，但没有公布单任务空闲内存、峰值内存或绝对冷启动时长（[Introducing upgrades to Codex](https://openai.com/index/introducing-upgrades-to-codex/)）。

### 2.2 Anthropic Claude Code

#### Claude Code 内置沙盒

- **macOS：** Bash 工具用 Seatbelt。
- **Linux / WSL2：** Bash 工具用 bubblewrap。
- **原生 Windows：** Claude Code 内置 Bash 沙盒当前不支持，官方建议使用 WSL2。

内置边界覆盖 Bash 命令及其子进程，不自动覆盖 Claude Code 自己的 Read/Edit/WebFetch，也不自动约束独立运行的 MCP server 或 hook。默认允许读取较广的文件范围，只允许向工作区和会话临时目录写入；网络请求通过宿主代理和域名 allowlist 控制。官方特别警告：宽泛域名规则和允许 Unix socket 都可能成为数据外泄通道（[Claude Code Sandboxing](https://code.claude.com/docs/en/sandboxing)）。

官方把本地沙盒性能成本描述为“minimal”，但没有给出空闲 RSS、每命令峰值或毫秒级冷启动数字。Seatbelt/bubblewrap 路径没有 Guest image。

#### SRT 与云端 VM

Anthropic 还开源了实验性的 Sandbox Runtime（SRT）：macOS 用 Seatbelt，Linux 用 bubblewrap；其独立项目目前也提供 Windows 路径，通过专用本地用户、按会话 ACL 和基于账户 SID 的 Windows Filtering Platform 出站规则隔离。这一点不要与“Claude Code 产品内置沙盒尚不支持原生 Windows”混为一谈（[sandbox-runtime](https://github.com/anthropic-experimental/sandbox-runtime)）。

Claude Code Web 则为每个会话运行 Anthropic 管理的隔离 VM，通过网络代理允许指定域名，并把 GitHub token 留在 VM 外部、通过单独代理发放范围受限的凭据。官方对不可信仓库也明确建议使用专用 VM 或云环境，而不是只依赖本地命令沙盒（[Sandboxing environments](https://code.claude.com/docs/en/sandbox-environments)）。每会话 VM 的内存、镜像大小和冷启动时间未公布。

### 2.3 Google Gemini CLI

Gemini CLI 把多个后端放在同一配置入口下，安全和资源成本因此差异很大（[Gemini CLI Sandboxing](https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/sandbox.md)）：

- **macOS Seatbelt：** 不启动 VM。默认 `permissive-open` 限制写入位置，但允许较广读取和网络；另有 restrictive/strict 与代理配置文件。
- **Linux gVisor/runsc：** 需要 Docker，借助用户态内核拦截系统调用；比普通容器多一层内核隔离，但仍要承担容器基础设施和镜像成本。
- **Docker / Podman：** 跨平台使用，默认镜像为 `ghcr.io/google/gemini-cli:latest`，把工作区以相同绝对路径挂进容器，也允许自定义镜像。
- **LXC：** Linux 上进入已有容器。
- **Windows 原生：** 官方文档描述了基于 `icacls` 的 Low Mandatory Level 路径；它会修改文件/目录的完整性标签，属于宿主 ACL 隔离，不是 VM。

Seatbelt 和 Windows ACL 路径没有 Guest image；Docker/gVisor 路径需要容器镜像。官方没有公布镜像的稳定准确尺寸、各模式 RSS 或绝对启动耗时。贡献文档说明首次构建沙盒镜像常需约 20–30 秒，主要耗在拉取基础镜像；之后启动成本被描述为较小，但仍没有稳定的跨平台数值（[Gemini CLI Contributing](https://github.com/google-gemini/gemini-cli/blob/main/CONTRIBUTING.md)）。

因此，Gemini CLI 也不是“统一使用 VM”：本地 Seatbelt/ACL 不启动 VM；Docker 模式每个 CLI 沙盒会话创建容器。在 macOS/Windows 上，容器通常依赖容器运行时的一台共享 Linux VM，而不是每次任务各启一台 VM；这部分属于容器运行时成本，不是 Gemini 自己的 per-task microVM。

### 2.4 OpenHands

OpenHands 默认推荐 Docker sandbox，也提供 Process 和 Remote Runtime（[Sandboxes overview](https://docs.openhands.dev/openhands/usage/sandboxes/overview)）：

- **Docker：** 每个 Agent 会话启动一个带 Agent Server 的容器；工作区可读写挂载，意味着 Agent 可以真实修改挂载文件。SDK 的上下文管理器负责拉取/构建镜像、等待服务启动，并在结束时清理容器；预构建镜像可以降低准备时间（[Docker sandbox](https://docs.openhands.dev/sdk/guides/agent-server/docker-sandbox)）。
- **Process：** 直接以普通宿主进程运行，没有隔离，可访问当前用户能访问的文件；官方明确标记为不安全，只适合受信任代码（[Process sandbox](https://docs.openhands.dev/openhands/usage/sandboxes/process)）。
- **Remote：** 连接外部 Runtime，实际边界取决于远程提供方（[Remote sandbox](https://docs.openhands.dev/openhands/usage/sandboxes/remote)）。
- **Kubernetes 企业部署：** 每个对话一个 sandbox pod；官方示例默认内存 request/limit 为 3072 MiB、CPU request 500m、临时盘 10 GiB（[Resource limits](https://docs.openhands.dev/enterprise/k8s-install/resource-limits)）。这是调度预留与上限，不是实测 RSS。

OpenHands 的默认生命周期更接近“每会话一个容器”，不是“每条命令一台 VM”。`KEEP_RUNTIME_ALIVE=false`、`SANDBOX_PAUSE_AT_EXIT=false`，以及默认 300 秒关闭延迟等选项影响容器回收时机（[Environment variables](https://docs.openhands.dev/openhands/usage/environment-variables)）。其本地容器实际内存、空闲内存、镜像稳定尺寸与冷启动时间均未公布。macOS/Windows 若经 Docker Desktop 运行，会额外承受共享 Linux VM 的常驻成本；它能摊薄多个会话的启动成本，但对只有一个短任务的桌面应用不一定比 SheJane 的按需 VM 更省。

## 3. 代表性基础技术

### 3.1 Firecracker microVM

Firecracker 运行在 Linux/KVM 上，每个 sandbox 可以拥有独立 Linux Guest 内核。官方给出的目标数据是 VMM 启动低于 125 ms、每台 microVM 的 VMM 额外内存低于 5 MiB；这两个数字来自特定 Linux 裸机场景，不包括 Guest 内存与应用工作集，也不能直接外推到 macOS Virtualization.framework（[Firecracker FAQ](https://github.com/firecracker-microvm/firecracker/blob/main/FAQ.md)、[AWS engineering introduction](https://aws.amazon.com/blogs/opensource/firecracker-open-source-secure-fast-microvm-serverless/)）。

Firecracker 需要 VMM、Linux kernel 和 rootfs；总包体由运营方选择的镜像决定，官方没有统一值。它支持 snapshot：恢复时按需载入内存页，并通过 Copy-on-Write 保护 snapshot；但 snapshot 会携带设备与 Guest 状态，复用前必须处理随机数、凭据和唯一身份问题（[Snapshot support](https://github.com/firecracker-microvm/firecracker/blob/main/docs/snapshotting/snapshot-support.md)）。

对 SheJane 的含义：Firecracker 很适合未来的 Linux 远程执行平面，却不是本地 macOS/Windows 的直接替换。要在桌面端使用，仍需另一层 Linux VM 或远程服务器，反而会增加本地复杂度与常驻内存。

### 3.2 Wasmtime / WASI

Wasm 模块默认只能通过宿主显式提供的 import/export 与外部世界交互；WASI 文件系统是 capability-based，未授予的目录不可访问（[Wasmtime Security](https://docs.wasmtime.dev/security.html)）。`ResourceLimiter` 能限制每个 Store 的实例、memory 和 table，但官方明确提醒：它不能统计 Store 的全部宿主侧分配，CPU 还需 fuel 或 epoch interruption 等独立机制（[ResourceLimiter](https://docs.wasmtime.dev/api/wasmtime/trait.ResourceLimiter.html)）。

Wasmtime 不需要 Guest kernel/rootfs，跨 macOS、Linux、Windows。官方曾展示特定基准中新实例创建约 5 微秒，并说明可以用 Copy-on-Write 与惰性内存支持“每次工作一实例”；这不是 SheJane workload 的通用冷启动承诺（[Wasmtime 1.0: Fast, Safe and Production Ready](https://bytecodealliance.org/articles/wasmtime-1-0-fast-safe-and-production-ready)）。准确 idle RSS、模块包体和真实任务峰值取决于 Runtime、预编译缓存和模块本身，官方没有可直接套用的统一数字。

WASI 是最有希望的本地低内存路线，但不能无损承载任意 Python、Node、LibreOffice、FFmpeg 或带本地动态库的工具。适合纯解析、格式转换、结构化数据处理等 ABI 可控能力；不应把“不兼容的原生 worker 强行塞进 Wasm”当作免费迁移。

## 4. 横向对比

| 方案 | 默认隔离单元 / 是否每次启 VM | 平台路径 | 空闲与任务内存、冷启动 | 文件与网络 | 包或镜像成本 |
| --- | --- | --- | --- | --- | --- |
| Codex 本地 | 每命令进程树；**否** | macOS Seatbelt；Linux/WSL2 bwrap；Windows 受限令牌/ACL | 未公布；无 Guest 常驻 | writable roots + 受保护路径；namespace/proxy/firewall | 无 Guest image；helper 增量未公布 |
| Claude Code 本地 | 每 Bash 命令树；**否** | macOS Seatbelt；Linux/WSL2 bwrap；原生 Windows 产品路径暂不支持 | 官方仅称开销很小，具体未公布 | 工作区写、广泛读；网络代理 allowlist | 无 Guest image |
| Claude Code Web | 每会话隔离 VM；**是** | 云端 | 未公布 | 代理 allowlist；凭据留在 VM 外 | 未公布 |
| Gemini CLI | Seatbelt/ACL 时否；Docker 时每 CLI 会话容器 | macOS、Linux、Windows；另有 Docker、gVisor、LXC | 首次镜像构建约 20–30 秒；RSS 与后续冷启动未公布 | 随 profile 或容器挂载变化 | native 无 image；容器镜像大小未公布 |
| OpenHands | 每会话容器/pod；通常**不是每命令 VM** | Docker、Kubernetes、Remote；Process 不隔离 | 企业默认上限 3072 MiB；实际 RSS/冷启动未公布 | workspace 挂载可写；联网与凭据需部署控制 | 需要 Runtime image，尺寸未公布 |
| Firecracker | 每 workload microVM；通常是 | Linux/KVM | VMM <5 MiB、启动 <125 ms；不含 Guest workload | 由 VMM 设备、宿主策略和 Guest 配置决定 | VMM + kernel + rootfs，非固定 |
| Wasmtime/WASI | 每动作可新建 instance；**否** | macOS/Linux/Windows | 特定基准约 5 μs 建实例；真实 RSS 未公布 | 默认无能力；只开放预授权 capability | Runtime + Wasm module，无 Guest OS |

## 5. SheJane 当前方案：VM 是 Managed Worker 的沙盒后端

### 5.1 当前安全边界

本研究对应的主要 Runtime 阶段是 **P6 Resource Binding**；紧邻上游是 P5 Permission Gate，执行发生在 P10 Agent Loop，最终资源销毁由 P11 Finalization/Terminalization 承担。Runtime 是 sandbox lease、资源限制和终止状态的权威 owner。本文只研究现状和方案，不替换任何执行路径。

当前需要区分三条执行路径：

- **日常 Agent `execute`：** [`RuntimeLocalShellBackend`](../../runtime/src/shejane_runtime/agent/backends.py) 为每条命令生成 SRT 策略；工作区只读、临时 scratch 可写、网络关闭。它不启动 VM，命令结束后 launcher 和子进程一起退出。
- **WASI Worker：** [`WasiActionExecutor`](../../runtime/src/shejane_runtime/plugins/executor.py) 在 Wasmtime 实例内执行，通过显式能力访问输入输出，适合可编译到 Wasm 的轻量任务。
- **Managed Worker：** macOS arm64 使用 `darwin_vf_linux_vm_v1`。Runtime 只在 frozen 构建且本次 action lease 的 `execution_kind == managed_worker` 时加载 VM 资产；每次调用建立新的 staging 和 VM 命令，完成后清理，而不是在 Client 空闲时常驻一台 VM（[`builder.py`](../../runtime/src/shejane_runtime/agent/builder.py)、[`managed_worker.py`](../../runtime/src/shejane_runtime/plugins/managed_worker.py)）。

Managed Worker 的关键边界是：

- Linux Guest 与宿主不同内核；launcher 配置 1 个 vCPU（[`managed-worker-vm.swift`](../../client/native/managed-worker-vm.swift)）。
- 没有添加虚拟网卡，因此 worker 不是靠 Guest 防火墙“禁网”，而是根本没有网络设备。
- 输入与输出通过受控目录进入 Guest；worker 不获得宿主任意路径与宿主凭据。
- Runtime 对内存、进程数、输出和 scratch 设上限，并由 P11 负责回收。

这比 Codex/Claude Code 的本地命令沙盒更重，但也服务于不同威胁模型：SheJane 要分发和运行固定 capability 中的原生 worker，而不是只约束用户已经选择在自己账户下运行的 shell 命令。

### 5.2 当前内存配置不能直接当 RSS

macOS VM 的配置公式是：

```text
guest memorySize = max(256 MiB, action memory limit + 128 MiB VM overhead)
```

该公式当前由 [`macos_vm.py`](../../runtime/src/shejane_runtime/plugins/macos_vm.py) 固定执行。

按当前 manifests，常见配置落在这些档位：

| Worker 类型 | action limit 示例 | Guest 可见 `memorySize` |
| --- | ---: | ---: |
| 文档读写、云端视觉等 | 512 MiB | 640 MiB |
| PDF/媒体/部分 Office | 1024 MiB | 1152 MiB |
| 文档渲染、部分 Office | 2048 MiB | 2176 MiB |
| 本地语音、本地视觉 | 4096 MiB | 4224 MiB |

这些数字是 Guest 上限，不是宿主物理峰值。Virtualization.framework 会按 Guest 实际触页分配物理内存，因此要优化的是宿主 `phys_footprint`、峰值和并发总量，而不是只看 `memorySize` 字面值。

此前同一轮打包盘点得到 macOS VM 资产约 **58.49 MiB 压缩产物贡献、约 331 MiB 逻辑体积**；这影响下载与安装包，不等同于每次运行内存。把这些资产改成首次按需下载可以缩包，但不会自动降低单任务峰值内存。

Windows 当前尚没有达到 release gate 的 Managed Worker 后端；规划中的 QEMU Linux VM 仍有阻塞项。因此，Windows 不能简单地说“与 macOS 一样已经有 VM 沙盒”，也不应在发布文档中把计划能力写成现有能力。

### 5.3 本轮本机空载采样

为了确认日常 Agent shell 的实际量级，本轮用当前打包所依赖的 Electron-as-Node 与 SRT 0.0.65 启动一个 20 秒的空载 `sleep` 命令，并读取一次进程 RSS：

| 进程 | 单点 RSS |
| --- | ---: |
| Electron-as-Node 空载基线 | 54,000 KiB（约 52.7 MiB） |
| Electron-as-Node + SRT launcher | 72,336 KiB（约 70.6 MiB） |
| 沙盒内 `sleep` 子进程 | 1,184 KiB（约 1.2 MiB） |

这只是当前机器上的一次空载采样，不是跨平台 benchmark，也不代表真实编译、浏览器或媒体任务的峰值。它能确认两点：SRT 的 launcher 开销是几十 MiB 而不是一台 Guest 的数百 MiB 级别；launcher 随命令结束退出，不是 Client 空闲常驻内存。

本轮也尝试运行现有 macOS VM Gate 采样宿主 RSS，但当前执行环境返回 `Virtualization is not available on this hardware`，因此没有得到有效 VM 数字。下文继续把 `memorySize` 标为配置容量，并把真实宿主 `phys_footprint` 留作必须在可用物理机/发布 runner 上完成的测量。

## 6. 降低内存：保留当前安全边界的方案

### 优先级 A：先测真实物理峰值，再校准每个 action 的 limit

这是收益最确定、架构风险最低的路径。

1. 为每个 managed action 做 3 次冷启动、3 次热文件缓存运行，记录 Runtime、launcher、VM 进程树的宿主 `phys_footprint` 峰值、任务耗时、Guest OOM、输出正确性。
2. 记录 action 自身在 Guest 内的 peak working set；把“Guest limit”和“宿主实际峰值”分开存储。
3. 按 p95 工作集加明确安全余量下调 manifest。最先审计 4096 MiB 的本地语音/本地视觉和 2048 MiB 的渲染任务，它们具有最大的绝对下降空间。
4. 任何固定插件字节或 manifest 变化都必须遵守 `(plugin_id, version)` 不可变约束，完成版本升级和旧数据 frozen Runtime smoke test。

例如，某个 4096 MiB action 若实测 p95 只有 1.4 GiB，把上限改到 2 GiB 会把 Guest 可见容量从 4224 MiB 降到 2176 MiB；最终宿主节省多少仍必须由物理峰值确认，不能按差值直接宣称节省 2048 MiB。

### 优先级 A：增加宿主级并发预算

单任务优化解决不了两个 4 GiB worker 同时启动的峰值。P6 在发放 lease 前应检查全局 managed-worker budget：

- 用 action 的配置上限做保守 admission control；同时参考宿主可用内存和 memory pressure。
- 大任务默认串行；小任务只有在总预算允许时并行。
- 保留排队与取消语义，不用 OOM 后重试充当调度策略。
- P11 无论成功、失败还是取消，都必须释放 budget。

这不会降低单任务内存，但能直接压住最危险的总峰值，而且保持“一次任务一个干净 VM”的隔离语义。

### 优先级 A：扩大 WASI 覆盖面，但只迁移合适的能力

适合迁移的候选应满足：确定输入输出、无需任意子进程、依赖可编译到 WASI、无需完整浏览器或 Office/媒体原生栈。迁移后每次 action 创建新的 Store/instance，继续保留 capability 文件访问、fuel/epoch CPU 限制、memory/table/instance 上限和输出配额。

这能同时省去 Guest kernel、启动器与 VM 配置内存，也减少 VM 资产依赖。它对“Wasm 可表达的插件”可以维持强能力隔离；但它不是对任意原生二进制的同等级替换，原生 Office、FFmpeg、本地模型等仍留在 VM。

### 优先级 B：审计 `+128 MiB` overhead 与 256 MiB floor

当前固定 overhead/floor 是保护性常量。可以用最小 Guest 启动、worker bootstrap、错误回传、最大输出等测试逐级降低，但必须保留：

- Guest 正常启动与关机；
- 最大进程数与输出量下无随机 OOM；
- 所有固定 worker 的冷启动回归；
- 老数据、老插件版本不被修改。

如果最终只能把 128 MiB 降到 96 MiB，单任务收益不大；若高并发场景很多，累计收益才明显。因此它应排在 action limit 校准之后。

### 优先级 B：只为长任务试验 Virtio memory balloon

Apple 提供 Virtio traditional memory balloon。宿主可以请求 Guest 归还未使用页，但 Guest 是自愿归还，无法保证请求一定满足（[VZVirtioTraditionalMemoryBalloonDevice](https://developer.apple.com/documentation/virtualization/vzvirtiotraditionalmemoryballoondevice)、[configuration](https://developer.apple.com/documentation/virtualization/vzvirtiotraditionalmemoryballoondeviceconfiguration)）。

对几秒钟的短任务，气球初始化与策略复杂度可能大于收益；对长时间本地语音、视觉或渲染任务，如果内存呈“前期峰值、后期空闲”，它才可能有效。应先用时间序列证明存在可回收阶段，再做实验性实现。

### 不建议：为了“省启动”而常驻复用 Managed Worker VM

VM 池会把当前接近零的空闲 VM 内存变成持续占用，并引入跨任务残留：临时文件、页缓存、随机状态、子进程、环境变量和潜在恶意持久化。若未来冷启动真的成为首要瓶颈，可研究经过擦除与唯一性重建的 snapshot/CoW，而不是直接复用运行过不可信 worker 的 Guest；这需要新的安全设计与 P11 证明。

## 7. 更低内存、但降低或改变隔离等级的方案

### 方案 1：可信 first-party worker 使用宿主 OS 沙盒

macOS 可参考 Seatbelt/SRT，Linux 可参考 bubblewrap，Windows 可参考受限令牌 + ACL + WFP。它们没有 Guest OS，通常是最低本地内存路径；但共享宿主内核，且跨平台资源限制语义并不一致。尤其在 macOS 上，要同时实现进程树级内存、PID、磁盘和可靠清理，并不如 Linux cgroup 直接。

只有满足下列条件时才值得作为独立执行等级：代码由 SheJane 自己签名和审计、不加载用户原生依赖、默认断网、输入目录最小化、用户明确知道隔离较弱。不能在协议里仍把它标成与 Managed Worker 相同的安全等级。

### 方案 2：Docker / Podman 容器

Linux 原生容器比完整 VM 轻，并能用 namespace、seccomp、cgroup 控资源；macOS/Windows 则通常依赖一台共享 Linux VM。多任务时共享 VM 可摊薄 Guest kernel 成本，单个短任务却会引入容器运行时常驻内存、安装依赖和镜像体积。

它适合开发者模式或已有 Docker 的高级用户，不适合作为默认桌面依赖。普通容器仍共享 Linux 宿主内核；若叠加 gVisor，隔离更强但系统调用兼容性和性能成本更高。

### 方案 3：Process 模式

像 OpenHands Process sandbox 一样，直接在宿主跑 worker 的确最省额外内存，但几乎取消了安全边界。它只能是显式开发模式，不能承载来自插件包、文档附件或模型决策触发的不可信处理链。

### 方案 4：远程 sandbox / microVM

把 Managed Worker 放到远程 Firecracker、容器或专用 VM，可以把本地峰值降到网络客户端量级，同时保留甚至增强隔离；代价是上传本地文档、网络时延、运营成本、离线能力和隐私边界变化。它不是“免费省内存”，而是把资源成本迁到云端，并改变 SheJane 当前的本地文档承诺，必须做独立产品决策。

## 8. 推荐执行顺序

建议按以下顺序推进，前两步完成前不要选替换技术：

1. **建立事实基线。** 对 512/1024/2048/4096 MiB 四档代表 action 测宿主峰值、Guest 峰值和冷启动，明确“配置容量”和“真实物理占用”。
2. **控制峰值。** 在 P6 增加全局并发预算，并对本地语音、本地视觉、渲染、Office 四类高内存 action 校准 manifests。
3. **分流低风险能力。** 盘点能迁到 WASI 的纯处理能力；原生依赖继续每 action 新 VM，不启用 warm pool。
4. **做有证据的实验。** 只有长任务曲线显示大量可回收页时试 memory balloon；只有冷启动成为主要用户瓶颈时再研究安全 snapshot。
5. **另立较弱等级。** 如果确实需要极低内存模式，新增明确命名的 trusted-native / developer 模式，使用 OS sandbox，并在 UI、协议和文档中明确它不等价于 Managed Worker。

最终判断是：**CubeSandbox 或其他新 VM 框架不是当前降内存的第一抓手。** SheJane 已经做到了 VM 按 action 启动、空闲时不常驻；真正可能浪费的是 action limit 过宽和高内存任务并发。先用数据把这两项收紧，再决定是否值得引入 balloon、snapshot、容器共享 VM 或远程 microVM，能避免用更复杂的基础设施换来不可验证的收益。
