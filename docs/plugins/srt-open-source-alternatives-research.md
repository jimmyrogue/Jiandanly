# SRT 开源替代方案调研

> 调研日期：2026-08-01  
> 外部资料范围：仅使用项目官方仓库、官方文档和操作系统规范。  
> 目标：判断是否存在比 `@anthropic-ai/sandbox-runtime`（SRT）更适合 SheJane 桌面端普通 Agent 命令执行的开源替代方案，同时比较安全边界、跨平台支持、包体、内存、许可证和接入风险。

主要阶段：P10，执行工具或等待用户  
上游输入：P9 产生的待执行命令与工具调用  
下游输出：P11 可结算、可清理的进程结果  
状态所有者：Runtime 的工具执行与回执链路  
替换的当前路径：无；本文只做研究，不改变当前实现

## 结论

**目前没有一个开源项目能作为 SRT 的“直接替换品”，同时在 macOS、Linux、Windows 三端提供更成熟的任意进程沙盒、更低的接入风险和更小的资源成本。**

对 SheJane 当前的普通 Agent shell，建议继续固定 SRT，而不是迁移：

1. SRT 已经把三端最难维护的策略组合成同一个 CLI/TypeScript API：macOS 使用 Seatbelt，Linux 使用 Bubblewrap，Windows Alpha 使用独立本地用户、Restricted Token、Job Object、NTFS ACL 和 WFP。它仍是 Beta Research Preview，Windows 仍是 Alpha，但它是本次调查中唯一与 SheJane 当前形态直接匹配的跨平台开源包装层。[SRT README](https://github.com/anthropic-experimental/sandbox-runtime)
2. OpenAI Codex 的实现更值得作为**安全设计和测试用例参考**，不适合作为依赖直接搬入。它也是三套平台实现：macOS Seatbelt、Linux Bubblewrap、Windows 专用用户/ACL/Restricted Token/WFP，并与 Codex 自己的 Rust workspace、权限模型和多个辅助二进制耦合。[Codex core support matrix](https://github.com/openai/codex/blob/main/codex-rs/core/README.md) [Codex Linux sandbox](https://github.com/openai/codex/blob/main/codex-rs/linux-sandbox/README.md) [Codex Windows design](https://openai.com/index/building-codex-windows-sandbox/)
3. Bubblewrap、Landlock、NsJail 和 Firejail 只解决 Linux；OpenSandbox、gVisor 和 Firecracker 面向容器/云端强隔离，不是低成本桌面进程包装器。Wasmtime/WASI 是更轻、更容易跨平台收紧 capability 的路线，但只能承载可编译为 WebAssembly、ABI 明确的能力，不能替换任意 shell。
4. 换掉 SRT 也不是有效的包体优化。当前工作树中 SRT 0.0.65 安装目录约 **7.9 MiB**，其中 `vendor/` 约 **6.7 MiB**；但实际 `0.1.19` macOS arm64 发布 ZIP 已排除 Windows helper，ZIP 中直接属于 SRT 的条目压缩后约 **0.82 MB**，其中 `vendor/` 约 **0.61 MB**。按平台和架构继续裁剪只能获得不足 1 MiB 的小收益，优先级远低于删除尚未开放的 VM 资产。以上数字是本地发布产物测量，不等同于所有平台和后续版本。[依赖声明](../../client/package.json) [打包配置](../../client/electron-builder.yml)

## 评价边界

本文所说的“更好”必须同时满足：

- 能包装任意已有 shell 命令或原生程序，而不是要求把程序重编译为特定格式；
- macOS、Windows、Linux 都有真实的 OS 强制边界，并且失败时能 fail closed；
- 能表达 SheJane 当前的只读工作区、独立可写临时目录和默认断网策略；
- 不要求桌面端常驻 Linux VM、容器守护进程或大体积系统镜像；
- 可合法再分发、可固定版本，并有可自动化的逃逸、兼容性和清理测试。

“共享宿主机内核的进程沙盒”和“独立 guest 内核的 VM”不是同一安全等级。前者适合普通 Agent 命令的误操作约束，不能自动升级为恶意原生代码的强租户隔离。

## 五条最相关路线

| 路线 | macOS | Linux | Windows | 包体与内存含义 | 接入与维护 | 对 SheJane 的判断 |
|---|---|---|---|---|---|---|
| **现有 SRT 0.0.65** | Seatbelt / `sandbox-exec` | Bubblewrap、seccomp helper、代理 | Alpha；独立用户、Restricted Token、Job、ACL、WFP | 无 guest RAM；有 JS launcher/代理进程；本地包目录约 7.9 MiB | Apache-2.0；统一 CLI/库；Beta，Windows 需一次 UAC 安装 | **当前最佳整体匹配，保留** |
| **OpenAI Codex sandbox 组件** | Seatbelt | Bubblewrap 默认；Landlock 旧路径 | 两个 sandbox 用户、ACL、Restricted Token、WFP、多个 helper | 无 guest RAM；Rust/native 可能避免 Node launcher，但官方没有可比较的独立 RSS 数据 | Apache-2.0；源码活跃；crate 和 policy 深度依赖 Codex workspace | **参考实现，不是 drop-in 依赖** |
| **Linux 原生栈**：Bubblewrap / Landlock / NsJail / Firejail | 否 | 是 | 否 | 通常是最低固定内存；无 guest；Bubblewrap/Landlock 最小，NsJail 增加资源控制 | 平台策略、seccomp、cgroup、发行版兼容全部由 SheJane 自己承担 | **只适合加强 Linux 后端** |
| **OpenSandbox / gVisor / Firecracker** | OpenSandbox 可在 macOS 管理 Docker；隔离 workload 仍是 Linux | 是 | OpenSandbox 依赖 Docker/WSL2；其内置镜像仍是 Linux | 增加 daemon、镜像、Sentry 或 guest；Firecracker 默认 guest 128 MiB | OpenSandbox 是 control plane；本地轻量 PC sandbox 尚在 roadmap | **适合远程/Managed Worker，不是本地替代** |
| **Wasmtime / WASI** | 是 | 是 | 是 | 无 guest kernel/rootfs；按 invocation 建 instance；真实 RSS 取决于 host 和 module | Apache-2.0；成熟可嵌入；程序必须编译为 Wasm 并使用显式 ABI/capability | **扩大适用能力，但不能替代任意 shell** |

## 1. SRT：当前基线

SRT 官方把自己定义为无需容器、在 OS 层限制任意进程文件系统与网络访问的轻量工具，并同时提供 CLI 和 TypeScript library API。macOS 使用动态 Seatbelt profile，Linux 使用 Bubblewrap bind mount 与 network namespace，网络允许列表由宿主 HTTP/SOCKS5 proxy 执行。[SRT architecture and API](https://github.com/anthropic-experimental/sandbox-runtime)

Windows 支持仍明确标为 Alpha。一次 elevated `windows-install` 会建立 `srt-sandbox` 用户和组、用 DPAPI 保存随机凭据并安装按 SID 匹配的 WFP filter；执行时由 helper 两跳启动 Restricted Token 子进程并放入 Job Object。文件权限通过会话级 NTFS ACE 实现，崩溃后的 ACE 依赖下次初始化恢复清理。[SRT Windows model](https://github.com/anthropic-experimental/sandbox-runtime#Windows-alpha)

这意味着 SRT 的主要风险不是“隔离原理太弱”，而是：

- 官方仍称其为 Beta Research Preview，API 和配置可能变化；
- Windows 需要 SheJane 安装器真正闭环 UAC 初始化、卸载、恢复与升级；
- Linux 依赖 user namespace、Bubblewrap、`socat` 和 `ripgrep`，Ubuntu 24.04+ 的 AppArmor user namespace 限制需要安装侧处理；
- SRT 不提供完整 CPU、内存、进程数、磁盘配额；这些仍需平台 Job/cgroup/rlimit 或更强执行后端补齐。[SRT platform dependencies and limitations](https://github.com/anthropic-experimental/sandbox-runtime#platform-support)

SheJane 目前通过 [RuntimeLocalShellBackend](../../runtime/src/shejane_runtime/agent/backends.py) 为每条命令生成只读工作区、可写 scratch、默认断网策略，再调用 [SRT policy builder](../../runtime/src/shejane_runtime/plugins/sandbox_runtime.py)。如果 launcher 缺失会拒绝执行，不会静默回退到裸 shell。这已经满足普通 Agent shell 的产品边界。

## 2. OpenAI Codex：最值得参考，但不是更省事的库

Codex 的主机沙盒同样是平台适配层：

- macOS 要求 `/usr/bin/sandbox-exec`，由 `SandboxPolicy` 生成 Seatbelt policy；
- Linux 当前默认使用 Bubblewrap，只读绑定 `/`，叠加 writable roots，隔离 user/PID/network namespace，并在进程内应用 `PR_SET_NO_NEW_PRIVS` 和 seccomp network filter；Landlock 保留为明确的 legacy fallback；
- Windows 的最终实现需要 elevated setup、在线/离线两个专用本地用户、ACL、Restricted Token、WFP、防泄漏凭据存储、setup binary 和 command-runner binary。[Codex platform matrix](https://github.com/openai/codex/blob/main/codex-rs/core/README.md) [Linux helper details](https://github.com/openai/codex/blob/main/codex-rs/linux-sandbox/README.md) [Windows architecture](https://openai.com/index/building-codex-windows-sandbox/)

源码是 Apache-2.0，Linux 和 Windows 目录也各自暴露 Rust library target；但它们引用 Codex workspace 内的 protocol、network proxy、policy、install context 和 utility crates，而不是发布成稳定、产品无关的沙盒 SDK。[Linux crate](https://github.com/openai/codex/blob/main/codex-rs/linux-sandbox/Cargo.toml) [Windows crate](https://github.com/openai/codex/blob/main/codex-rs/windows-sandbox-rs/Cargo.toml) [Codex license](https://github.com/openai/codex/blob/main/LICENSE)

迁移到 Codex 代码不会消除三端差异或 Windows elevated setup，只会把上游整合、Rust 构建、policy 转换、二进制签名和安全回归的责任转移给 SheJane。除非实测 SRT launcher 成为显著并发内存瓶颈，否则不值得 fork。

## 3. Linux 原生栈：有些能力比 SRT 强，但不能解决桌面三端

### Bubblewrap

Bubblewrap 使用 unprivileged user namespace、mount/PID/network/IPC namespace 和可选 seccomp 构造沙盒。它明确声明自己只是底层环境构造器，不是带完整安全策略的成品；安全强度完全取决于调用者传入的参数和暴露的 mount/socket。[Bubblewrap README](https://github.com/containers/bubblewrap)

它是 SRT 和当前 Codex Linux 默认后端，因此“从 SRT 换成 Bubblewrap”不会获得新的 Linux 内核边界，只会删除 SRT 的统一配置、代理和其他平台实现。Bubblewrap 主程序采用 LGPL-2.0-or-later。[Bubblewrap source license](https://github.com/containers/bubblewrap/blob/main/bubblewrap.c)

### Landlock

Landlock 是 Linux 内核的 unprivileged、stackable LSM。它能给当前线程及后代叠加不可放宽的 filesystem policy；较新 ABI 也能按 TCP/UDP 端口限制 bind/connect，但不能按域名表达网络策略，也不负责 CPU、内存或进程数限制。[Linux Landlock userspace API](https://cdn.kernel.org/doc/html/latest/userspace-api/landlock.html)

直接使用 Landlock 固定开销很小，适合作为 Linux defense-in-depth，但内核 ABI 和能力随版本变化，仍要组合 seccomp、namespace、cgroup/rlimit 和网络代理。它不是跨平台 SRT 替代品。

### NsJail 与 Google agent-shell-tools

NsJail 提供一次执行模式、mount/user/PID/network 等 namespaces、rlimit、cgroup v1/v2、seccomp-BPF 和 Kafel policy，能力上比裸 Bubblewrap更接近完整 Linux Worker sandbox；项目使用 Apache-2.0，2026-03 发布了 3.6。[NsJail official repository](https://github.com/google/nsjail)

Google 新公开的 `agent-shell-tools` 也选择 NsJail，把 container boundary 作为 Agent shell 的主要安全模型；但仓库明确声明不是受官方支持的 Google 产品，当前仍很年轻且只解决 Linux/Unix-socket 组合。[Google agent-shell-tools](https://github.com/google/agent-shell-tools)

如果 SheJane 将来为 Linux Managed Worker 增加硬内存/CPU/PID 上限，NsJail 值得做对照 Gate；它不应替换 macOS/Windows 普通 shell 的 SRT。

### Firejail

Firejail 是 Linux-only SUID sandbox，组合 namespaces、seccomp-BPF、capabilities、SELinux/AppArmor 和 cgroup，并包含大量桌面程序 profile。项目仍活跃，但它是面向系统安装和用户 profile 的完整工具，主许可为 GPL-2.0-or-later，分发、安装和 profile 审计成本高于 Bubblewrap。[Firejail official repository](https://github.com/netblue30/firejail) [Firejail license](https://github.com/netblue30/firejail/blob/master/COPYING)

对 SheJane 的最小、可审计命令策略，它没有形成足够收益。

## 4. OpenSandbox、gVisor 和 Firecracker：更强、可扩展，但不是本地替代

OpenSandbox 是 Alibaba 开源的通用 sandbox control plane：用统一 SDK/API 管理 sandbox 生命周期、命令和文件，内置 Docker、Kubernetes runtime，并能把 gVisor、Kata Containers、Firecracker 作为更强的容器后端。它解决的是“如何调度和访问 sandbox”，不是用一个轻量进程库在三端实现 OS policy。[OpenSandbox official repository](https://github.com/alibaba/OpenSandbox) [OpenSandbox architecture](https://github.com/alibaba/OpenSandbox/blob/main/docs/architecture.md) [Secure container guide](https://github.com/alibaba/OpenSandbox/blob/main/docs/secure-container.md)

这一区别对桌面产品很关键：OpenSandbox 当前内置 workload 是 Linux container；Windows 通常通过 Docker Desktop/WSL2 接入。官方 roadmap 仍把“Local lightweight sandbox for AI tools running directly on PCs”列为未来项，因此它现在不是 SRT 的本地 drop-in replacement。[OpenSandbox Windows support discussion](https://github.com/alibaba/OpenSandbox/issues/438) [OpenSandbox roadmap](https://github.com/alibaba/OpenSandbox#roadmap)

Podman 官方说明 macOS 和 Windows 必须先运行 Linux VM，因为容器核心能力依赖 Linux kernel；Docker Desktop 在 macOS 同样用 VMM 驱动 Linux VM。把普通命令切到容器会重新引入常驻 VM、镜像、文件映射和工具链可见性问题。[Podman machine](https://docs.podman.io/en/latest/markdown/podman-machine.1.html) [Docker Desktop VMM](https://docs.docker.com/desktop/features/vmm/)

gVisor 是 Linux OCI runtime，用 userspace application kernel 拦截系统调用，减少 workload 直接接触 host kernel 的表面；这比普通 namespace/seccomp 更强，但 Sentry/Gofer 会增加内存和系统调用层级，官方也明确说明存在额外内存与性能开销。它要求 Linux 4.14.77+，不是 macOS/Windows 原生桌面 wrapper。[gVisor security architecture](https://gvisor.dev/docs/architecture_guide/intro/) [gVisor installation](https://gvisor.dev/docs/user_guide/install/) [gVisor performance](https://gvisor.dev/docs/architecture_guide/performance/)

Firecracker 提供 KVM microVM 边界，只支持 Linux host/guest。官方 `<5 MiB` 是 VMM 自身 footprint，不包含 guest 内存、kernel、rootfs 和 workload；默认 guest 内存是 128 MiB，因此不能把该数字与 SRT launcher RSS 直接比较。[Firecracker FAQ](https://github.com/firecracker-microvm/firecracker/blob/main/FAQ.md) [Firecracker getting started](https://github.com/firecracker-microvm/firecracker/blob/main/docs/getting-started.md)

Windows Sandbox 是系统虚拟化功能而不是可嵌入库。Microsoft 要求设备至少 4 GB RAM、1 GB 空闲磁盘并启用虚拟化；安装后的动态 base image 约占 500 MB，默认实例最大 memory capacity 为 4 GB。[Windows Sandbox install](https://learn.microsoft.com/en-us/windows/security/application-security/application-isolation/windows-sandbox/windows-sandbox-install) [Windows Sandbox architecture](https://learn.microsoft.com/en-us/windows/security/application-security/application-isolation/windows-sandbox/windows-sandbox-architecture) [Windows Sandbox configuration](https://learn.microsoft.com/en-us/windows/security/application-security/application-isolation/windows-sandbox/windows-sandbox-configure-using-wsb-file)

这些方案适合不可信租户代码、云端执行或 Managed Worker，不适合为了普通 Agent shell 的包体和内存目标替换 SRT。

## 5. Wasmtime/WASI：最值得扩大覆盖面的轻量路线，但不是 shell

Wasmtime 是 Apache-2.0 的可嵌入 WebAssembly runtime，官方支持 Linux、macOS、Windows，并提供 Rust、C/C++、Python、.NET、Go、Ruby 等嵌入接口。它不需要 guest kernel/rootfs，实例只能调用 host 显式提供的 import；WASI 文件系统使用 capability model，默认只能访问 host preopen 的目录，且不能通过 `..` 或 symlink 越出 capability 根。[Wasmtime official repository](https://github.com/bytecodealliance/wasmtime) [Wasmtime WASI tutorial](https://github.com/bytecodealliance/wasmtime/blob/main/docs/WASI-tutorial.md) [Wasmtime security](https://docs.wasmtime.dev/security.html)

SheJane 已有按 invocation 创建实例的 [`WasiActionExecutor`](../../runtime/src/shejane_runtime/plugins/executor.py)，产品契约也明确 WASI 默认没有环境文件系统、网络、环境变量和凭据，只开放授权 capability。[WASI security model](./security-model.md#wasi) [Plugin manifest](./manifest-v1.md#wasi)

因此 Wasmtime/WASI 是本次候选中唯一同时有跨平台、低固定开销和更窄默认权限潜力的方向；但它要求能力编译成 Wasm 并遵守受控 ABI，无法无损运行任意 Python、Node、FFmpeg、Office 或本机动态库。正确策略是把纯解析、格式转换、结构化数据处理等能力逐步迁入 WASI，而不是删除 SRT 后把 shell 强行改写为 Wasm。

## 包体与内存判断

### 已确认

- SRT、Codex、Bubblewrap、Landlock、NsJail、Firejail 和 Wasmtime 都不需要为每次执行预留 guest RAM；Wasmtime 还不携带 guest kernel/rootfs，但会有 runtime、JIT/AOT code 和 module memory。
- SheJane 当前 SRT 0.0.65 目录本地测得约 7.9 MiB：`dist/` 约 1.1 MiB，`vendor/` 约 6.7 MiB；其中两个 Windows helper 合计约 5.4 MiB，两个 Linux seccomp helper 合计约 1.3 MiB。
- [electron-builder 配置](../../client/electron-builder.yml) 当前将 `node_modules/@anthropic-ai/sandbox-runtime/vendor/**/*` 全部列为 `asarUnpack`；不过实际 `0.1.19` macOS arm64 ZIP 已排除 Windows helper，只留下两个 Linux seccomp 架构 helper。直接属于 SRT 的 ZIP 条目压缩后约 0.82 MB，其中 `vendor/` 约 0.61 MB。
- OpenSandbox 的 Docker/Kubernetes 路线需要 runtime/daemon 和镜像；gVisor 需要额外 Sentry/Gofer；Firecracker 和桌面容器需要 VM/guest；Windows Sandbox 要系统镜像和虚拟化。因此它们不会比进程沙盒更适合“低内存桌面默认值”。

### 不能从官方资料推出

- 不能仅凭“Rust/native”断言 Codex helper 的峰值 RSS 一定低于 SRT。两边缺少同 workload、同 policy、同平台的官方对照基准。
- Firecracker 的 `<5 MiB` 不能解释为“一次沙盒只占 5 MiB”；该数字不含 guest。
- 包目录大小不能直接等同于 DMG/ZIP 下载体积，最终收益需要真实打包后测量。

### 最小验证方法

若要判断是否值得为了内存自研 native launcher，只需要一个小型对照 Gate：

1. 在 macOS、Windows、Linux 各执行相同的 30 秒 idle、文件遍历、编译和拒网任务；
2. 记录 launcher + proxy + command 的进程树峰值 RSS/working set、启动时间和退出后残留；
3. 对照 SRT、Codex CLI `sandbox` 子命令和 Linux 的 direct Bubblewrap/NsJail；
4. 同时跑 workspace escape、symlink、socket、process-tree cancellation 和 crash cleanup；
5. 只有 SRT 的稳定额外开销达到产品阈值，才评估 native launcher。

## 推荐决策

### 现在

- **保留 SRT 作为普通 Agent shell 的统一 launcher。**
- **不引入新沙盒依赖。** 没有候选同时降低三端维护、安全风险、包体和内存。
- **不为了包体更换 SRT。** 如果主要大项清理完成后仍需要挤压不足 1 MiB，再按平台和架构裁剪 SRT `vendor/`；它不是当前优先级最高的包体优化。
- **扩大适合能力的 WASI 覆盖面。** 这是降低原生 Worker/VM 使用与并发内存的长期方向，但不能替代通用 shell。
- **Windows 保持 fail closed，直到安装器完成 elevated setup、卸载、升级、崩溃恢复和真实逃逸 Gate。** “SRT 支持 Windows Alpha”不等于 SheJane 已经开箱即用。

### 有明确触发条件时再升级

- Linux 需要硬 CPU/memory/PID 配额：对照 NsJail，或在现有 Bubblewrap 路径外加 cgroup/rlimit；不要为了 Linux 优势拆掉三端统一 API。
- 实测 SRT launcher 在多 Run 并发下造成不可接受的峰值内存：参考 Codex platform helper 自建极小 native launcher，但必须连同三端安全 Gate 一起迁移。
- 需要运行恶意或多租户原生代码：使用 Managed Worker 的平台强隔离或远程 sandbox；不要把 SRT/Chromium process sandbox 宣称为 VM 等价边界。

最终判断：**市面上有在单个平台、特定 ABI 或更强隔离目标上优于 SRT 的开源组件，但没有对 SheJane 跨平台桌面普通 Agent shell 整体更好的直接替代库。**
