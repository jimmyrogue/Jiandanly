# macOS Managed Worker VM：生产资产分发研究与发布 Gate

> 调研日期：2026-07-16
>
> 范围：Apple Virtualization.framework 上的本地 Linux VM；`kernel`、`initramfs`、VSOCK modules、`mke2fs` 与 Swift launcher 的生产分发。
>
> 资料原则：只采用项目官方文档、官方仓库源码和官方发布源。
>
> 状态：历史研究，已被 2026-08-01 的包体决策取代。当前 Client 不再打包或注入 VM 资产，Managed Worker 受 packaged backend 与 release Gate 双重阻断；现状以 [`managed-worker-isolation.md`](managed-worker-isolation.md) 为准。

## 当时的决策摘要（已撤销）

**v1 选择“构建时冻结、随对应架构的 macOS App 一起打包”，不选择首次运行下载。**

每个 macOS 安装包只携带与自己架构匹配的一组不可变 VM 资产：

```text
SheJane.app/Contents/Resources/sandbox/
└── vm-assets/
    ├── manifest.json               # 资产集合身份、架构、摘要、来源与许可证索引
    ├── linux-kernel                # 与 host/guest 架构匹配的裸 kernel Image
    ├── initramfs.cpio              # guestd + 精确匹配 kernel 的 VSOCK modules
    ├── mke2fs                       # 当前 host 架构的原生、固定版本 helper
    ├── shejane-managed-worker-vm    # Swift launcher
    ├── sbom.spdx.json
    └── licenses/                    # license text、source offer、构建与补丁索引
```

这样做的关键原因不是实现更短，而是安全边界更完整：

- 首次调用可以完全离线，不会在用户真正使用插件时才发现 CDN、代理或证书问题。
- kernel、initramfs 和 `mke2fs` 是沙箱可信计算基的一部分，应该与使用它们的 Runtime/launcher 作为一个已测试、不可拆分的发布单元。
- macOS 代码签名会封装 App 的资源；签名后的资源变化会使校验失败。Apple 也要求分发到 App Store 之外的软件使用 Developer ID、hardened runtime、secure timestamp，并对所有可执行代码正确签名后再公证。[Apple Code Signing Guide：sealed resources](https://developer.apple.com/library/archive/documentation/Security/Conceptual/CodeSigningGuide/AboutCS/AboutCS.html) [Apple：Notarizing macOS software](https://developer.apple.com/documentation/security/notarizing-macos-software-before-distribution)
- Electron 的 `extraResources` 正是把数据或原生二进制放入 `Contents/Resources` 的机制，`mac.binaries` 可声明额外需要签名的原生二进制；electron-updater 又要求 macOS 应用已经签名，因此整个 App 已经具备一条统一的签名与升级链。[electron-builder：Application Contents](https://www.electron.build/docs/contents/) [electron-builder：Signing Additional Binaries](https://www.electron.build/mac/) [electron-builder：Auto Update](https://www.electron.build/docs/features/auto-update/)

**不在 v1 再发明第二套 VM 资产 updater。** 如果未来量化数据证明安装包体积或内核修复频率不可接受，再把同一资产 manifest 扩展为签名的按需下载格式；这不是当前 Gate 的前置条件。

## 1. 调研时仓库事实（已过期）

### 1.1 已经具备的能力

- `client/vm-assets/darwin-arm64.lock.json` 冻结 Fedora 44 已签名 kernel RPM/SRPM、Fedora keyring、e2fsprogs 1.47.2 源码/签名、kernel.org OpenPGP key 和原生工具链身份。
- `client/vm-assets/build_darwin.py` 验证上游签名、大小、摘要、RPM 身份和工具链，确定性生成 kernel、含精确 VSOCK modules 的 initramfs、host-native `mke2fs`、launcher、SBOM、许可证与 canonical manifest；两次完整构建逐字节一致。
- `runtime/src/shejane_runtime/plugins/macos_vm.py` 已从绝对 manifest 路径 fail-closed 验证 schema、host/guest 架构、协议、canonical asset-set ID、HTTPS provenance、普通文件、无 symlink、size、SHA-256 与 executable bit。
- `client/electron-builder.yml` 按 `${arch}` 打包完整资产集；`main.cjs` 只在 `darwin/arm64` 向 Runtime 传入固定包内 manifest。最终 `.app` 资产与构建输出逐字节一致，顶层严格签名校验通过。
- 最终 `.app` 内资产已由生产 Executor 完成成功、失败、非法 JSON、取消、hostile symlink、Artifact/scratch/OOM/PID 上限、Worker/descendant 宿主逃逸探针、Worker/launcher crash cleanup 和 Runtime `SIGKILL` lease recovery 十三种真实 VM Gate。细节见 [`managed-worker-isolation.md`](managed-worker-isolation.md)。

### 1.2 仍然阻塞生产发布的事实

- `.github/workflows/release-client.yml` 已加入 arm64 锁定输入获取、完整构建、包内身份检查、13-mode VM Gate、正常 `.app` P1 启动/manifest 注入/退出清理黑盒，以及 Developer ID、Hardened Runtime、secure timestamp、App Store Connect API key notarization/staple 与 Gatekeeper 验证。正常 App 入口已在本机 unsigned package 通过；真实 secrets 的 release runner 执行仍未完成。
- 当前 guest 构建明确只支持 Linux `arm64`。因此 x64 安装包即使可以构建，也不能宣称 Managed Worker VM 可用。
- 调研发现时，`managed_worker_release_gate()` 对所有 `darwin/*` 返回同一份 proved/blocker 集合；本轮已立即拆分：真实证据只属于 `darwin/arm64`，`darwin/amd64` 保持空 proved 和 `architecture_conformance_gate`。x64 仍须完成自己的全套 Gate。
- `runtime/src/shejane_runtime/plugins/sandbox_runtime.py` 已把冻结、打包资产、宿主逃逸、Worker/launcher crash cleanup、Runtime hard-crash lease recovery、冻结 Debian 只读 rootfs、动态 Python onedir Worker 与 Node.js LTS Runtime Asset 转为 proved；`sandbox_escape_and_cleanup_gate` 和 `guest_userspace_runtime` 均已移除。rootfs 使用精确 Debian Linux/arm64 OCI manifest，在原生 `ubuntu-24.04-arm` runner 以锁定 e2fsprogs 生成并双构建比较；Node Runtime Asset 验证官方签名、签名人、归档摘要和 ELF 架构，再由真实 VM 证明非特权执行与资产只读。只有尚未真实运行的 Developer ID/公证 `release_ci_gate` 保留。[Kata guest rootfs](https://github.com/kata-containers/kata-containers/blob/main/docs/design/architecture/README.md) [Gondolin guest assets](https://github.com/earendil-works/gondolin) [Debian rootfs checksums](https://docker.debian.net/) [Node.js binary verification](https://github.com/nodejs/node#verifying-binaries) [GitHub arm64 runners](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)

### 1.3 Canonical Runtime stage

```text
主要阶段：P6，绑定资源并取得 Agent 定义
上游输入：P5 冻结的执行上下文、有效作业租约、固定插件版本与授权
下游输出：P7 可恢复图执行所使用的不可变 Agent 定义和已验证 VM 资产租约
状态所有者：Runtime 的 PluginCatalog / Managed Worker backend；本次执行持有资源租约
替换的当前路径：外部测试资产路径和未接线的 MacOSVMResources，改为 App 内不可变 asset set
```

直接相邻契约是 P5→P6→P7：P6 只能绑定已经冻结并验证的资产，不能在这里下载 `latest`、改变插件版本或扩大能力。后续 P10 使用同一资产集合执行 Action 并生成工具回执，P11 必须停止 VM、清理 image/staging/进程树，P12 只能提交 P11 已静止且 Runtime 重新验证的 Artifact。当前实现中 `PluginCatalog.acquire_snapshot` 已在 P6 重验精确 package digest 并持有 lease；VM asset set 应复用同一“固定身份 + 租约”原则，而不是建立独立的可变选择路径。阶段定义以 [`harness-runtime-stages.md`](../harness-runtime-stages.md) 为准，当前调用路径以 [`run-loop.md`](../run-loop.md) 为准。

## 2. 为什么不默认首次运行下载

首次下载看起来可以减小安装包，但它会立刻增加一套独立的高风险状态机：

1. 解析 host/guest 架构并选版本；
2. 获取签名元数据；
3. 防止 manifest 回滚和 `latest` 被替换；
4. 下载中断恢复、空间检查、摘要验证；
5. 缓存导入、并发首次启动、原子激活；
6. 离线和 CDN 故障回退；
7. 旧 App 与新资产协议兼容；
8. 缓存清理、许可证展示和源代码提供。

这些工作不是“下载一个 zip”，而是第二套软件更新系统。当前资产与 launcher、guest protocol 和 Runtime executor 强耦合，单独热更新反而会扩大未经组合测试的状态空间。

Gondolin 展示了这种方案至少应达到的基线：内置 registry 按 `arch` 和不可变 build ID 记录 URL 与 SHA-256；image store 校验 schema/架构并以临时目录导入，再原子更新引用；release workflow 分别构建 `aarch64` 与 `x86_64`、生成摘要和 metadata 后更新 registry。[Gondolin registry](https://github.com/earendil-works/gondolin/blob/main/builtin-image-registry.json) [Gondolin image store](https://github.com/earendil-works/gondolin/blob/main/host/src/images.ts) [Gondolin image release workflow](https://github.com/earendil-works/gondolin/blob/main/.github/workflows/image-release.yml)

Gondolin 适合未来参考，但其公开 registry 主要依赖 HTTPS 和预置 SHA-256，没有展示 SheJane v1 所需的独立签名元数据与回滚保护。因此不能把它直接复制为生产安全结论。

## 3. 推荐的生产资产模型

### 3.1 一个 App 版本对应一个不可变资产集合

建议 `manifest.json` 至少包含：

```json
{
  "schema_version": 1,
  "asset_set_id": "darwin-arm64/sha256:<canonical-payload-digest>",
  "host": { "os": "darwin", "arch": "arm64" },
  "guest": { "os": "linux", "arch": "arm64" },
  "protocol_version": 1,
  "files": {
    "kernel": { "path": "linux-kernel", "sha256": "...", "size": 0 },
    "initramfs": { "path": "initramfs.cpio", "sha256": "...", "size": 0 },
    "mke2fs": { "path": "mke2fs", "sha256": "...", "size": 0 },
    "launcher": { "path": "../shejane-managed-worker-vm", "sha256": "...", "size": 0 }
  },
  "sources": [],
  "build": {},
  "licenses": {},
  "sbom": "sbom.spdx.json"
}
```

生产 schema 还应记录：

- Fedora package 的精确 NVR、架构、RPM 摘要、签名 key fingerprint、对应 SRPM；
- Linux kernel config、解包/提取脚本摘要和所有补丁；
- VSOCK modules 的精确文件摘要，并声明它们已嵌入哪个 initramfs；
- `guestd` 的 Git commit、Go 版本、构建 flags 和二进制摘要；
- e2fsprogs release tag、源码 tarball 摘要、上游签名、host toolchain 和动态库闭包；
- 生成环境、构建脚本 commit、SBOM、license 文件和 source-offer 索引。

`asset_set_id` 必须由 manifest 中除 `asset_set_id` 字段外的 canonical payload 计算，避免自引用；不能使用可变的 `latest`。App 版本只引用一个确定的 `asset_set_id`，Runtime 不能静默切换到另一组资产。

### 3.2 摘要和签名分别解决什么

- **上游来源验证**：获取 Fedora RPM 时校验 RPM OpenPGP 签名和固定摘要；获取 e2fsprogs 时校验 kernel.org 官方 `.sign` / signed checksum，不信任未固定的镜像 URL。RPM 官方文档定义 `rpmkeys --checksig` 校验包签名与摘要，Fedora 官方下载安全页使用 OpenPGP 和 checksums 验证下载；e2fsprogs 1.47.2 官方目录提供源码、`.tar.sign` 和 `sha256sums.asc`。[RPM keys manual](https://rpm.org/docs/6.0.x/man/rpmkeys.8.html) [Fedora security downloads](https://fedoraproject.org/en/security/) [e2fsprogs 1.47.2 official release](https://www.kernel.org/pub/linux/kernel/people/tytso/e2fsprogs/v1.47.2/)
- **构建产物身份**：CI 对每个最终文件记录 SHA-256 和 size；Runtime 每次 preflight 重新计算，并且只接受 manifest 中的精确值。
- **发行者身份和资源封装**：Developer ID 签名封装 `.app`；公证服务检查提交物。kernel/initramfs/manifest 是数据资源，不需要再各自创造一套应用层签名；它们由 App resource seal 保护。
- **嵌套原生代码**：launcher 和 `mke2fs` 是 Mach-O 可执行文件，必须按 Apple 的 nested-code 顺序正确签名，不能只依赖它们是 `extraResources`。Apple 要求先签内层代码，再签外层 bundle；electron-builder 的 `binaries` 可声明额外二进制。[Apple Code Signing Guide：sign code inside out](https://developer.apple.com/library/archive/documentation/Security/Conceptual/CodeSigningGuide/Procedures/Procedures.html) [electron-builder mac binaries](https://www.electron.build/mac/)

即使 App 签名有效，Runtime 仍应校验 manifest 与每个文件摘要。这能把“包是否被修改”和“Runtime 是否加载了设计时那一组资产”分成两个独立事实，并防止路径接错或加载包外文件。

### 3.3 文件路径和加载规则

生产路径必须来自 Electron `process.resourcesPath` 下的 App bundle，并由生产代码按只读资源处理；Client 以明确字段把路径传给 Runtime。不得通过生产环境变量覆盖，不搜索 `$PATH`，不回退到 Homebrew、Android SDK、系统 kernel 或用户 cache。

生产 preflight（Runtime 资产身份检查 + Client/macOS 可执行检查）应拒绝：

- 相对路径、symlink、非 regular file、超出 `Contents/Resources/sandbox` 的路径；
- manifest schema、host arch、guest arch 或 protocol version 不匹配；
- size 或 SHA-256 不匹配；
- launcher 缺少 `com.apple.security.virtualization` entitlement；
- `mke2fs -V` 不等于 manifest 固定版本，或其 Mach-O 架构不匹配；
- kernel、initramfs 或 modules 没有来自同一受支持的 kernel build；
- release metadata 宣称的 App/asset set 身份与当前 manifest 不一致。

App resource seal、Developer ID、公证与 Gatekeeper 检查由打包/发布 Gate 和 macOS 安装启动链验证；Runtime 负责独立重验自己将要加载的 manifest、架构和文件摘要，不依赖“App 曾经签名”来替代运行时身份检查。

测试可以保留显式 fixture path，但必须与生产 factory 分开，不能让 `SHEJANE_TEST_*` 或 `SHEJANE_MANAGED_WORKER_*` 变成生产逃生口。

## 4. 双架构策略

Apple Virtualization.framework 支持由 `VZVirtualMachineConfiguration` 配置 Linux VM 的 CPU、memory、boot loader、storage 和 socket devices；Linux boot loader 可以指定 initramfs。[Apple Linux VM sample](https://developer.apple.com/documentation/virtualization/running-linux-in-a-virtual-machine) [VZVirtualMachineConfiguration](https://developer.apple.com/documentation/virtualization/vzvirtualmachineconfiguration) [VZLinuxBootLoader initialRamdiskURL](https://developer.apple.com/documentation/virtualization/vzlinuxbootloader/initialramdiskurl)

Firecracker 的官方 FAQ 明确要求 guest kernel 和 rootfs 与 host CPU architecture 相同；这是本地硬件虚拟化方案应采用的保守规则。[Firecracker FAQ](https://github.com/firecracker-microvm/firecracker/blob/main/FAQ.md)

因此：

| 安装包 | launcher / `mke2fs` | kernel / initramfs / modules | 当前状态 |
| --- | --- | --- | --- |
| macOS arm64 | Darwin arm64 | Linux arm64 | 冻结、打包、逃逸/清理与正常入口证据已完成；仍需真实签名/公证 release runner 执行 |
| macOS x64 | Darwin x64 | Linux x86_64 | 当前 guest builder 未支持；必须 fail closed |

不能在 x64 安装包中放 arm64 guest，也不能依赖 Rosetta 把 guest 架构问题“转换”掉。`arm64` 和 `x64` 必须是两条独立的构建、签名、公证和原生 Runner 黑盒流水线。

如果短期只完成 arm64，应在 x64 Runtime 的 release matrix 中保持 blocker，并在 UI 明确显示 Managed Worker VM 当前不可用；不能因为 Client x64 安装包存在就把平台标为支持。

## 5. 各资产的建议来源与构建方法

### 5.1 kernel 与 VSOCK modules

v1 推荐继续使用**固定 Fedora 官方 RPM 作为二进制来源**，而不是立刻维护自定义 kernel：

- 固定完整 NVR 和架构，从官方 Fedora 仓库获取 kernel 与匹配的 `kernel-modules-core`；
- 用 Fedora trusted keyring 执行 RPM 签名校验，同时校验内部固定摘要；
- 从同一 kernel build 提取适合 `VZLinuxBootLoader` 的裸 `Image` 和三个 VSOCK modules；
- 归档对应 SRPM、kernel config、提取脚本和验证日志；
- 对最终 Image 和 module 文件单独计算 SHA-256；modules 随 `guestd` 一起进入确定性 initramfs。

这条路径比自行维护最小内核拥有更小的补丁和 CVE 维护负担。只有启动体积、启动时间或攻击面数据证明必要时，再立项构建自有最小 kernel；那时仍应借鉴 Firecracker 和 Kata 的版本冻结方法。

Firecracker 官方 CI 构建自己的 guest kernel，并把 rootfs/kernel 构建脚本作为生产可复现输入；官方 getting-started 明确将演示资产与生产使用区分开。[Firecracker rootfs and kernel setup](https://github.com/firecracker-microvm/firecracker/blob/main/docs/rootfs-and-kernel-setup.md) [Firecracker getting started](https://github.com/firecracker-microvm/firecracker/blob/main/docs/getting-started.md)

Kata 使用中心 `versions.yaml` 固定 kernel 版本、来源和架构相关 image；kernel 构建脚本在解包前验证 kernel.org 的 `sha256sums.asc`。可借鉴的是“版本 map + 来源签名验证 + 每架构独立产物”，而不是 Kata 的完整容器栈。[Kata versions.yaml](https://github.com/kata-containers/kata-containers/blob/main/versions.yaml) [Kata kernel build script](https://github.com/kata-containers/kata-containers/blob/main/tools/packaging/kernel/build-kernel.sh)

### 5.2 initramfs 与 `guestd`

- 在 hermetic Linux builder/container 中编译静态、同 guest 架构的 `guestd`；固定 Go toolchain、环境、flags、UID/GID、mtime 和文件顺序。
- 只嵌入 `/init`、所需 VSOCK modules 和必需 metadata；不附带 shell、包管理器、完整 Fedora userspace 或调试工具。
- 同一输入至少构建两次并要求 initramfs 逐字节相同；如果工具链无法做到，应先修复确定性，不能仅在 manifest 中接受两个不同摘要。
- kernel/module ABI 必须由 CI 启动真实 VM 并完成 VSOCK handshake 来验证，不能只比较文件名或 `uname` 字符串。

### 5.3 `mke2fs`

不要分发当前探针使用的 Android helper，也不要从用户 Homebrew 复制二进制。建议：

1. 从 e2fsprogs 1.47.2 官方签名 source tarball 构建；
2. 在原生 macOS arm64/x64 CI job 分别生成对应 Mach-O；
3. 关闭不需要的工具与特性，固定 build flags；
4. 记录 `mke2fs -V`、SHA-256、size、toolchain 和 `otool -L` 动态库闭包；
5. 优先产出只依赖系统库的二进制；若存在第三方 dylib，必须一起封装、签名、SBOM 并验证 rpath；
6. 作为 nested executable 签名后，再签整个 App。

e2fsprogs 官方仓库提供 `mke2fs` 源码和项目 NOTICE；具体分发义务必须按实际构建所链接的文件集合确认，不能只给整个 e2fsprogs 仓库贴一个笼统许可证标签。[mke2fs source](https://github.com/tytso/e2fsprogs/blob/master/misc/mke2fs.c) [e2fsprogs NOTICE](https://github.com/tytso/e2fsprogs/blob/master/NOTICE)

## 6. 更新策略

v1 的更新单位是 **SheJane Client 版本**：

- 新 kernel、initramfs、guestd 或 `mke2fs` 形成新的 immutable `asset_set_id`；
- CI 对这个组合执行完整 Gate 后，随新 Client 版本签名、公证和发布；
- 老 App 继续使用自己包内的老资产，不读取新 App 的资源，也不自动使用共享 cache；
- 安全修复通过正常 Client 安全版本发布；可使用 electron-updater 的 staged rollout，但同一 Client 版本内的资产不可变。[electron-builder staged rollout](https://www.electron.build/docs/features/auto-update/)
- 更新失败时保留 OS/Updater 原有的旧 App，不在 Runtime 层回退到不匹配的 kernel 或 helper。

发布流水线必须验证已签名旧版本到已签名新版本的真实升级，并在网络断开时验证新安装 App 的首次 Managed Worker invocation 仍可成功。

### 6.1 何时才考虑独立下载

只有满足以下条件才重新做 ADR：

- 量化证明 VM 资产显著影响下载转化、安装成功率或更新成本；或者
- kernel/guest 安全修复频率显著高于 Client 发布频率，且整包 staged rollout 无法满足响应时间。

届时最低要求是：

- 签名、带 expiry 和 monotonic version 的 metadata；rollback protection；
- `host_arch + guest_arch + protocol_version + build_id` 全部精确匹配；
- 内容寻址缓存、下载时流式 SHA-256、临时目录导入、fsync、原子 rename/引用切换；
- 不允许 mutable `latest`，不允许后台静默激活未经 Gate 的组合；
- 明确磁盘上限、并发锁、崩溃恢复、离线缓存和撤销策略；
- 下载资产的许可证、SBOM 和 source offer 与二进制一起缓存并可从 UI 查看。

Gondolin 可提供 immutable registry、arch validation 和 atomic import 的实现参考，但 SheJane 需要在其上补齐独立签名 metadata 与 rollback protection。

## 7. 许可证与供应链义务

SheJane 是 AGPL-3.0-only 项目；把第三方 VM 资产放进安装包后，发布流程还必须处理各组件自己的许可证和源代码义务：

- Linux kernel 顶层 `COPYING` 声明 GPL-2.0-only WITH Linux-syscall-note。分发 kernel 和 modules 时，应随每个二进制版本提供对应 source、config、补丁、构建脚本和适用的 source offer。[Linux kernel COPYING](https://github.com/torvalds/linux/blob/master/COPYING)
- e2fsprogs 是多组件、可能含不同许可证文件的项目。对实际打包的 `mke2fs` 及其静态/动态链接闭包生成逐文件 SBOM，随包提供相应 license text、源码、补丁和重建说明。
- Fedora RPM 应保留 RPM 签名验证证据、精确 NVR、SRPM 和 Fedora 相关 notices；不要只保留最终提取后的无来源文件。Fedora packaging guidance 也要求验证上游 source signature 并记录签名 key。[Fedora packaging source verification](https://docs.fedoraproject.org/nn/packaging-guidelines/)
- 自有 `guestd` 的 AGPL-3.0-only source、构建 commit 和重建脚本必须与发布版本对应。
- 每个安装包生成 SPDX 或 CycloneDX SBOM，列出 binary/source relationship、版本、架构、摘要、license expression 和 source URL。

这部分需要在首次公开分发前完成许可证审查；本文不是法律意见。Gate 的判定对象应是“这个具体 asset set 的材料是否齐全”，不能使用一次性的全局勾选。

## 8. 可借鉴的现有 Agent / Sandbox 方案

### 8.1 OpenAI Codex：最接近 v1 的完整性模式

Codex 没有分发本地 Linux VM；它按平台使用 host sandbox。Linux 发布流程对 bundled bubblewrap 先 strip，再计算 SHA-256，把摘要嵌入 Codex 二进制，并把同一份 bubblewrap 放入发布 artifact；运行时打开文件、校验 digest 后通过 `/proc/self/fd/N` 执行已经验证的 fd，降低验证后路径被替换的风险。Linux release artifact 还生成 Sigstore 签名 bundle。[Codex Linux sandbox](https://github.com/openai/codex/blob/main/codex-rs/linux-sandbox/README.md) [Codex release workflow](https://github.com/openai/codex/blob/main/.github/workflows/rust-release.yml) [Codex bundled bwrap verification](https://github.com/openai/codex/blob/main/codex-rs/linux-sandbox/src/bundled_bwrap.rs) [Codex Sigstore action](https://github.com/openai/codex/blob/main/.github/actions/linux-code-sign/action.yml)

SheJane 应借鉴：构建时固定最终 bytes、把摘要绑定到调用方、运行时再次校验、发布 artifact 有可验证签名。macOS 没有 `/proc/self/fd` 同等执行路径，但可以通过只读 App bundle、拒绝 symlink、摘要校验和内外层签名达到对应目标。

### 8.2 Pi / Gondolin：未来下载型资产的参考

Pi 本身默认在 host 上执行，官方 containerization 文档把 Gondolin 描述为把内置工具路由到本地 Linux microVM，并要求 QEMU。它不是 Pi 核心内置的 macOS Virtualization.framework VM。[Pi containerization](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/containerization.md)

Gondolin 首次使用自动解析并缓存约数百 MB 的 VM image，适合参考按架构 registry、build ID、SHA-256、缓存导入和发布 workflow；不适合直接作为 v1 选择，原因是 SheJane 当前只有较小的 kernel/initramfs/helper 集合，且 macOS 已有统一 App 签名/更新链。[Gondolin README](https://github.com/earendil-works/gondolin)

### 8.3 LangGraph / Deep Agents：只借鉴 provider seam

Deep Agents 允许选择 LangSmith、Daytona、Modal、Runloop 等 sandbox provider 或自定义 backend；它没有提供可直接打包进 SheJane 的本地 VM asset pipeline。LangGraph local server 是本地进程/容器开发运行方式，也不负责 kernel/initramfs 分发。[Deep Agents sandboxes](https://docs.langchain.com/oss/python/deepagents/sandboxes) [Deep Agents repository](https://github.com/langchain-ai/deepagents) [LangGraph local server](https://docs.langchain.com/oss/python/langgraph/local-server)

SheJane 应借鉴 executor/provider seam，让本地 VM 与未来远程 sandbox 共享 Action/Artifact 协议；不能把 provider abstraction 当成已经解决本地资产供应链。

### 8.4 Firecracker 与 Kata：借鉴 guest 构建，不照搬运行时

Firecracker 假定生产 host 和输入可信、使用预格式化 file-backed block devices，并提供 kernel/rootfs 构建路径；Kata 通过中心版本表、架构产物和构建脚本管理 VM guest 组件。[Firecracker production host setup](https://github.com/firecracker-microvm/firecracker/blob/main/docs/prod-host-setup.md) [Firecracker design](https://github.com/firecracker-microvm/firecracker/blob/main/docs/design.md) [Kata architecture](https://github.com/kata-containers/kata-containers/blob/main/docs/design/architecture/README.md)

SheJane 应借鉴它们的 immutable guest、版本锁定、架构拆分和真实 VM 测试；不需要引入 Firecracker/Kata 本体，因为 macOS hypervisor 与产品协议已经确定为 Virtualization.framework + VSOCK。

## 9. 实施路径

### Phase A：资产规范和 hermetic builder

1. 冻结 `manifest-v1` schema、目录布局和 canonical digest 规则，并把 `darwin/arm64`、`darwin/x64` 的 release evidence 拆开。
2. 新增只接受固定 NVR/tag/digest/signature 的资产获取脚本；归档 Fedora SRPM 与 e2fsprogs source。
3. 先以 hermetic job 生成 arm64 kernel Image、initramfs 和 host-native `mke2fs`；同一 recipe 随后扩展并独立验证 x64。
4. 生成 SBOM、licenses/source-offer 目录和 provenance；重复构建验证确定性。
5. x64 guest builder 未完成前，明确保持 x64 blocker。

### Phase B：打包和 Runtime 接线

1. 通过 electron-builder `extraResources` 放入完整 asset set。
2. 明确 launcher 与 `mke2fs` 的 nested-code 签名顺序和 entitlement；移除 ad-hoc 发布基线，接入 Developer ID、hardened runtime、公证和 staple。
3. Client 从 `process.resourcesPath` 构造生产只读路径并传给 Runtime。
4. Runtime 新增 manifest/arch/digest/version preflight；Client/发布 Gate 验证 launcher entitlement、App 签名和公证；生产路径无环境变量覆盖和系统 fallback。
5. Runtime factory 只有在对应 host release blocker 为空时才注册为可用 executor。

当前实现进度：manifest v1 schema、canonical asset-set ID、架构/协议/HTTPS provenance、无 symlink、size/SHA-256/executable preflight，以及 Client 固定包内路径 → Runtime CLI → P6 → production Executor 接线已完成。arm64 完整 asset set 已双构建逐字节一致，并由 Electron Builder 放入最终 `.app`；包内资产与构建输出一致。正常 `.app` 入口已建立认证 Runtime 会话、核对 runtime 注入相同包内 manifest 并通过 `app.quit` 清理。workflow 已配置同一黑盒及 Developer ID/Hardened Runtime/notarization/staple 严格验证，但尚无本轮真实凭据执行证据，因此 release matrix 继续 fail closed。

### Phase C：打包黑盒与攻击 Gate

1. 在原生 arm64/x64 macOS runner 上安装最终 `.app`，不设置外部 kernel/modules/`mke2fs` 环境变量。
2. 从 App 的正常入口执行一次真实 Managed Worker，验证输入、Action、Artifact digest、shutdown 和 staging cleanup。
3. 执行 tampered/wrong-arch/wrong-version/missing-file/symlink 测试，要求 fail closed。
4. 完成 scratch `ENOSPC`、memory OOM、PID limit、CPU throttle、descendant escape、取消/错误/崩溃清理 Gate。
5. 网络断开验证 clean install 首次调用；验证旧版到新版的签名更新。

当前实现进度：最终 `.app` 内资产已经通过生产 Executor 的 `ENOSPC`、group OOM、PID limit、1 vCPU + cgroup CPU cap、取消、协议错误和 staging/cgroup 清理 Gate；Worker 与 descendant 还主动证明宿主 file/credential/PID/Unix socket/loopback/外网不可达。Worker 与 launcher crash 都会清理 staging；Runtime `SIGKILL` 时由 launcher 继承的 `flock` 防止抢删活跃 VM，launcher 退出后下一次 Runtime 只回收所有者、权限、inode 与锁状态均匹配的孤儿目录。发布 workflow 以绝对包内 manifest 运行，缺失时失败而不是 skip；另一个黑盒从最终 `.app` 正常入口启动 Main 与冻结 Runtime、核对相同 manifest，并正常退出进程树。真实签名/公证 runner 与升级 Gate 仍未完成。

### Phase D：发布与运维

1. 发布页面同时提供 App、摘要、签名/公证信息、SBOM、licenses、source offer 和 provenance。
2. 定义 kernel/e2fsprogs CVE 监测、资产更新 SLA、撤回与 staged rollout 流程。
3. 将每个 release 的 asset set 和 source materials 按保留策略归档，保证旧二进制仍可重建。
4. 只有全部 Gate 通过，才从对应平台 release matrix 删除 blocker 并允许 Registry 启用非受信任 Managed Worker。

## 10. 明确 Release Gate

| Gate | 必须证明的事实 | 失败行为 |
| --- | --- | --- |
| G1 来源与 provenance | 所有 upstream 使用精确版本/NVR/URL/摘要/签名；保存 key fingerprint、SRPM/source、补丁和构建 recipe；没有 `latest` | 停止构建 |
| G2 产物身份 | kernel、initramfs、launcher、`mke2fs` 均有 size/SHA-256；重复构建确定；kernel/modules 匹配 | 停止构建 |
| G3 架构完整性 | arm64 与 x64 使用独立 manifest、release evidence、原生产物和真实 VM 证明；host/guest 同架构，arm64 proved 不得出现在 x64 Gate | 对未完成架构 fail closed |
| G4 打包、签名、公证 | 完整 `extraResources`；Mach-O nested code 签名；Developer ID、hardened runtime、timestamp、notarize/staple；`codesign --verify --deep --strict` 与 Gatekeeper 检查通过 | 不发布 |
| G5 生产 preflight | Runtime 只从 App resources 加载并验证 schema/arch/protocol/path/size/digest/version；Client/发布链验证 entitlement、App 签名和公证；无 env/系统 fallback | `executor_unavailable` |
| G6 打包 App 黑盒 | 原生两架构 runner 从最终 `.app` 完成真实 invoke/Artifact；不使用测试环境资产；测试不得 skip | 不删除 `release_ci_gate` |
| G7 资源与逃逸 | 非特权 Worker 和 descendant 实际证明 OOM、PID、CPU、ENOSPC、无网络、无 host file、取消/错误/崩溃全清理 | `sandboxed=false` |
| G8 离线与更新 | clean install 断网首次调用成功；已签名旧版→新版升级成功；篡改、降级和错误资产 fail closed | 不发布/停止 rollout |
| G9 许可证与 SBOM | 每个 asset set 的 license、SBOM、source、source offer、重建说明完整并公开 | 不发布 |

发布前还应执行 Apple 建议的严格签名与公证问题检查；公证 ticket 应 staple 到最终分发物，并验证嵌套代码和顶层 bundle 的关系。[Apple：Resolving common notarization issues](https://developer.apple.com/documentation/security/resolving-common-notarization-issues) [Apple：Customizing the notarization workflow](https://developer.apple.com/documentation/security/customizing-the-notarization-workflow)

**最终开放条件只有一个：对应 `darwin/<arch>` 的 blocker 集合为空。** 资产存在、摘要正确、App 已签名或单次 VM 成功都只是其中一项证据，任何一项都不能单独把 `sandboxed` 改为 `true`。

## 11. 最小生产路径

在不牺牲最优方案的前提下，最小路径是：

1. 先只完成 macOS arm64 的完整 bundled asset set，因为当前真实 guest 证据和 builder 都在 arm64；x64 明确 fail closed。
2. 使用固定 Fedora kernel/kernel-modules-core RPM + 对应 SRPM，暂不维护自定义 kernel。
3. 使用现有确定性 initramfs builder，把匹配 modules 和静态 `guestd` 固定进去。
4. 从 e2fsprogs 1.47.2 官方签名源码在原生 arm64 构建 `mke2fs`，不复用探针 helper。
5. 将四项资产与 manifest/SBOM/licenses 一起放入 App，完成 Developer ID、公证、Runtime factory 接线和无外部资产黑盒 Gate。
6. 完成当前 release matrix 中全部 hostile/resource/cleanup blocker 后，才开放 arm64 Registry Gate。
7. 复制同一供应链到 x64，完成 Linux x86_64 guest builder 和原生 x64 VM Gate 后再开放 x64。

这条路径没有把未来下载能力堵死：manifest、asset set ID、arch split 和 CAS 友好摘要都已经为它留出接口；但 v1 只维护一条用户可理解、可离线验证、可由 App 更新回滚的生产链。
