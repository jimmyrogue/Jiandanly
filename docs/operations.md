# SheJane 运维手册

SheJane 只有 Client 与 Runtime 两个产品模块；Runtime 目录同时拥有公共 Runtime SDK 和插件，默认不读取根目录环境变量。

## 本地开发

首次安装：

```bash
make setup-hooks
corepack enable
pnpm install
cd runtime && uv sync
```

启动完整开发栈：

```bash
make dev
```

该命令启动源码 Runtime、Vite 和 Electron。

两个模块也可以独立运行：

```bash
make dev-runtime                     # 只启动 Runtime
make dev-client                      # 只启动 Client，使用 SHEJANE_RUNTIME_URL 与 SHEJANE_RUNTIME_TOKEN
cd runtime && uv run shejane-runtime --help
```

常用排障：

```bash
make doctor
make restart-runtime
make logs-runtime
```

## Runtime 配置

Runtime 默认不要求用户环境变量。

- Client Main 启动托管 Runtime 时，通过命令行传入本机地址、随机端口和配对 Token。
- Client 不提供 Runtime 连接设置。开发者接入外部 loopback Runtime 时，地址与 Token 由 Electron Main 配置和保存，不回传明文 Token 给 Renderer。
- 模型服务连接、模型资料和高级默认设置通过 Runtime API 保存。
- 新 Run 默认最多使用 100 次主执行模型调用；单次最多派发 5 个子 Agent，每个子 Agent 最多使用 50 次模型调用。Runtime 为主 Agent 保留最后 5 次调用，researcher 单次最多执行 10 次网页搜索和 10 次 `web.fetch`，且不能调用 shell 或写文件。这些都是代码强制的上限，不依赖提示词自律。
- `web.fetch` 保持 DNS 固定和 SSRF 私网拦截；当系统代理使用 RFC 2544 `198.18.0.0/15` fake-IP DNS 时，仅 HTTPS 请求可通过该代理网段，TLS 仍校验原始主机名。HTTP fake-IP 与其他私网、回环、链路本地地址继续拒绝。
- BYOK 密钥写入操作系统凭据库，不写入 SQLite、Run 快照或环境变量。
- `--data-dir` 可以修改 Runtime 数据目录。

开发和测试可以使用 `SHEJANE_FAKE_LLM`、tracing 变量以及 Skills/MCP 路径覆盖，但这些不是用户安装配置，也不提供公开 `.env.example`。

## 连接 BYOK 模型服务

Client 的“模型服务”设置调用 Runtime 的 `/v1/model-services` 接口。API Key 只写入操作系统凭据库；连接配置和缓存模型目录写入 Runtime SQLite。

入口包括 DeepSeek、Kimi、千问、GLM、MiniMax、硅基流动、OpenAI、Anthropic、Google Gemini 和“连接已有服务”。官方服务的地址与接口格式由 Runtime 固定，用户只选择区域并填写 API Key；已有服务会先自动识别 OpenAI Chat 或 Anthropic Messages 格式，也可在高级设置中明确选择 Google GenerateContent。

OpenAI 官方连接默认使用 Responses，也可按模型选择 Chat Completions；Anthropic 使用原生 Messages；Google Gemini 使用原生 GenerateContent。Runtime 在 Run 接纳时冻结具体协议，不会按品牌猜测或在失败后静默换协议。这些工具调用路径共用同一套可逆 wire name：工具定义、`tool_choice`、历史 assistant 调用和 `ToolMessage.name` 一致编码，返回 Runtime 后恢复内部点号名称；改名不会改变 call ID、调用顺序、OpenAI reasoning item、Anthropic thinking/signature 或 Gemini thought signature。

Runtime 不在 Client 启动时访问外部服务。新增或更新官方连接时，每个内置推荐模型会使用正式 Agent 共用的 Provider 适配器，依次完成流式 `模型 → shejane.ping 工具 → 工具结果 → 最终回答`；探针故意使用内部点号名称，以覆盖生产别名和第二轮历史重放。只有完整闭环成功的模型才标记为 `verified` 并可用于 Agent Run。单个模型遇到限流、临时故障、流内语义失败或格式不兼容时保持 `unverified`，不会阻止其他模型保存；鉴权、账户权限或余额错误会直接阻止连接。`/models` 失败不会阻止官方服务连接，Runtime 会保留内置或最近缓存目录，但不会把目录推荐误当作连接验证。

中转站返回的模型只作为候选目录，不再默认声明能力。一个模型可以分别验证多项能力：Agent 对话执行完整工具闭环，图片理解发送最小内联图片，图片生成调用 OpenAI-compatible `/images/generations`，图片编辑调用 `/images/edits`。图片生成和编辑测试都会产生一次真实请求，可能计费。只有验证过 Agent 对话能力的模型进入主模型选择器；图片生成和编辑模型在设置中分别选择默认绑定，不与主模型混列。

Client 的“生图”入口发送结构化的 `required_tools: ["image.generate"]`，不再向用户文本拼接隐藏提示词。Runtime 在 Run 接纳时冻结默认图片模型的连接版本、模型 ID、协议和绑定修订；Agent 通过 `image.generate` 或 `image.edit` 调用该模型，结果下载并校验后保存为 Runtime Artifact，对话只接收 Artifact 元数据并通过鉴权接口显示图片，不在事件或模型上下文中传递 Base64。模型服务或绑定在执行前发生变化时，旧 Run 会安全失效，不会静默切换到另一个模型。

任务使用明确的 `local:<连接编号>:<模型编号>`。Runtime 不自动选择模型，也不会在连接之间静默切换。API Key 失效时，可在原连接上更新，不需要删除连接。

## 自动审批

Client 新对话默认使用“自动审批”。Runtime 会先执行确定性安全规则，只把外部或未知灰区交给当前 Run 已冻结的具体模型；审查器没有工具，也不能授予插件 capability、扩大工作区或绕过沙箱。审查超时、供应商失败、无效 JSON 或不完整决定都会回退到人工审批，不会自动放行或切换模型。

审查调用和主 Agent 使用同一持久模型账本，但记录为独立的 `approval_review` purpose；每个 Run 最多 20 次，不占主执行模型的 100 次预算。自动决定保存在 Tool Receipt；诊断时可以通过 Run diagnostics 查看 `review_source`、`review_reason` 和 `review_model`。Client 时间线中的“规则自动允许”表示固定策略决定，“智能自动允许”表示当前模型决定。

## Agent Evals 与诊断 Trace

`make eval-gate` 运行不需要 API Key 的确定性核心 Agent 结果集，CI 和 Client release 都会先通过该门禁，并把 JUnit 报告写入 `.tmp/eval-gate.xml`。真实 Provider 验证使用正在运行的 Runtime：

```bash
SHEJANE_EVAL_TOKEN=<runtime-token> \
SHEJANE_EVAL_MODEL=local:<connection>:<model> \
make eval
```

默认报告写入 `.tmp/eval-report.json`。可用 `SHEJANE_EVAL_REPORT` 修改输出路径，用 `SHEJANE_EVAL_BASELINE` 指向上一份报告以计算通过率和 case 变化；报告包含 Runtime/模型版本、轨迹、工作区结果和 grader 结论，不保存 API Key。

`GET /v1/runs/{run_id}/diagnostics` 的 `trace` 字段从持久 Run、模型账本、Tool Receipt、子 Run、最新 Checkpoint 和终态记录生成 `run → model → tool/subagent → checkpoint → terminal` 执行链。现有 diagnostics 导出会包含同一结构；Span 只包含状态、usage、耗时、错误分类和内容摘要哈希，不包含原始提示词、工具参数、附件正文、密钥或大结果。外部 Langfuse/LangSmith tracing 仍是可选出口，不是事实来源。

## 插件安装与信任

Runtime 接受单个 `.shejane-plugin` ZIP，通过 `plugin.install` Command 安装到数据目录下的内容寻址存储：

```text
<data-dir>/plugins/packages/<sha256>
```

来源 ZIP 只用于限额解包和校验，Runtime 不从来源路径直接执行文件。安装、启停、更新、回滚和移除都写入现有 Command 日志；移除先标记 retired，不立即删除旧版本字节。

未签名包必须由调用方显式提交 `allow_unsigned=true`。签名包使用部署方维护的只读信任文件：

```text
<data-dir>/plugins/trusted-publishers.json
```

```json
{
  "schema_version": 1,
  "keys": [
    {
      "publisher_id": "com.example",
      "key_id": "ed25519:sha256:<64 lowercase hex characters>",
      "public_key": "<base64 raw 32-byte Ed25519 public key>",
      "status": "trusted",
      "not_before": "2026-01-01T00:00:00Z",
      "expires_at": "2027-01-01T00:00:00Z"
    }
  ]
}
```

同一 publisher 可以保留多把 key 进行轮换。将 `status` 改为 `revoked` 会阻止后续安装；签名有效只证明来源和完整性，不授予额外文件、网络或执行权限。

第三方插件以 `.shejane-plugin` 文件分发。用户下载、接收或自行构建后，从“插件”页本地导入；Runtime 不维护远程插件来源、索引或来源公钥。普通插件继续执行上述签名或未签名确认策略。

Computer Use、Browser QA 和 OCR 是 Runtime 随应用提供的固定能力，不属于外部插件分发面。Runtime 只自动接纳构建时固定的身份、版本、平台和 `computer_use` / `browser_qa` / `ocr` 适配器；外部安装、更新、回滚和移除都会被拒绝，因此不要求用户确认这些内置包的发布者签名。插件包和 Runtime Asset 仍进入内容寻址存储并冻结到 Run，不能携带另一种宿主执行器。Browser QA 与 OCR 提供 macOS arm64、Windows AMD64 原生产物；Computer Use 仍只提供 macOS arm64。

打包版 Client 冷启动时，托管 Runtime 会校验并安装 Browser QA 与 OCR 的大型固定 Runtime Asset；P1 就绪握手为这条路径保留最多 120 秒。外部本机 Runtime 不执行这项安装，连接检查仍为 30 秒。打包 smoke 必须等待真实 `/v1/runtime` 会话和正常退出，不得用强制清理成功掩盖启动超时或残留进程。

macOS 首版固定 `injaneity/pi-computer-use` 提交 `9f59ed0eeac09b115897732c46b794ee8ca4e5b0`（0.5.0/MIT），只向模型暴露八个 state-scoped 桌面 Action。启用时由“插件”页依次完成 Helper、屏幕录制、辅助功能三步；每次用户操作最多触发一个系统授权，返回 SheJane 后自动复检。安装器把 Helper 固定在 `~/Applications/pi-computer-use.app`，并保留稳定的 macOS 代码签名身份；这里不能用“内置包免验签”替代 Helper 签名，否则系统可能把升级后的 Helper 视为新应用并重复要求 TCC 授权。每个 Run 只保持一个服务，P11 关闭；所有桌面 Action 继续经过参数校验、审批和持久回执。当前只完成 macOS arm64，其他平台不属于已发布能力。

`@anthropic-ai/sandbox-runtime@0.0.65` 现在承担主 Agent `execute` 的宿主访问隔离：默认禁止网络，只允许读取已授权工作区和运行工具所需的系统/PATH 路径，只允许写入每次命令的私有临时目录；启动器缺失或策略创建失败时命令 fail closed，不回退到宿主 shell。开发入口 `scripts/dev.sh` 使用 pnpm 安装的 SRT CLI，打包入口由 Electron 注入包内 launcher。代码改写继续使用 Runtime 的 `write_file` / `edit_file` 等受工作区约束且有回执的结构化工具。

这层 SRT 是主 Agent shell 的 access sandbox，不等同于不受信任插件的完整资源隔离，也不会得到 Managed Worker 的 `resource_isolated=true` 证明。Managed Worker 在 Linux 使用随 Runtime 冻结的 Bubblewrap 0.11.2、原生 launcher、seccomp、私有 tmpfs、Artifact broker 与 delegated cgroup v2；macOS arm64 使用下述短命 VM。

macOS arm64 VM 资产集由 `client/vm-assets/build_darwin.py` 构建。生成器只接受 lock 中精确大小与 SHA-256 的 Fedora 44 已签名 kernel RPM/SRPM、Fedora keyring、e2fsprogs 1.47.2 源码/签名和固定 kernel.org OpenPGP key；它验证 RPM 身份与签名、源码签名、Xcode/Clang/SDK/Go 工具链，确定性生成 Linux Image、guestd initramfs、host-native `mke2fs`、带 `com.apple.security.virtualization` entitlement 的 launcher、许可证、SPDX SBOM 和 canonical manifest。两次完整构建已经逐字节一致。

Electron Builder 用 `build/vm-assets-arm64` 把完整资产集放入 `Contents/Resources/sandbox/vm-assets`。资产集中的 Mach-O 在生成 manifest 前完成签名，打包时跳过整套只读资产，最终由最外层 App 签名封存；最终 `.app` 内的资产与构建输出逐字节一致。发布 workflow 在凭据完整时执行 Developer ID、Hardened Runtime、secure timestamp、App Store Connect API key 公证、staple、Gatekeeper 与 nested-code 验证；凭据全部缺失时只生成明确标记的 ad-hoc 签名预览包，凭据只配置一部分则 fail closed。只有 Developer ID/公证路径在原生 runner 上真实通过后，才能移除最后的 `release_ci_gate`。

Client 在 Darwin 上把包内 `sandbox/vm-assets/manifest.json` 作为显式 CLI 参数交给 Runtime；不存在系统路径或 `$PATH` fallback。P6 只有在冻结 lease 含 Managed Worker 时加载一次该资产集，并按 [`managed-worker-vm-assets-v1.schema.json`](../runtime/plugins/schemas/managed-worker-vm-assets-v1.schema.json) 对 host/guest 架构、协议、canonical asset-set ID、HTTPS 来源、普通文件、无 symlink、size、SHA-256 和 executable bit 做 fail-closed 预检。打包门禁还会调用包内 Runtime 的 `--validate-managed-worker-vm-assets`，在启动 Client 前执行同一生产 preflight，防止 schema 过期或资产被替换的包通过 lifecycle smoke。预检通过只代表资产身份成立，不会绕过平台 release Gate。

macOS VM 黑盒 Gate 是 `runtime/tests/test_macos_managed_worker_vm_gate.py`。执行时必须用 `SHEJANE_TEST_MACOS_VM_ASSETS` 指向最终 `.app` 内的绝对 manifest 路径；测试由生产 preflight 加载精确包内资产，并直接调用生产 Executor。Gate 覆盖成功、显式失败、非法 JSON、取消、hostile symlink、超限 Artifact、scratch `ENOSPC`、invocation 私有且 scratch-backed 的 `/tmp`、`/tmp` noexec、只读 rootfs、descendant OOM 和 PID exhaustion，并验证 cgroup 与 invocation staging 清理。GitHub 托管的 arm64 macOS runner [不支持嵌套虚拟化](https://docs.github.com/en/actions/reference/runners/github-hosted-runners#limitations-for-arm64-macos-runners)，所以自动发布 job 只执行最终包内 manifest/摘要/签名/entitlement、launcher 自检和 Runtime 生命周期 smoke；必须启动 VM 的动态 Gate 只允许在支持 Virtualization.framework 的物理或 self-hosted Mac 上启用。缺少这项执行证据的自动产物不构成 `release_ci_gate`。

这仍不代表 Managed Worker 已开放：`darwin/arm64` 已证明冻结资产集、完整包内 launcher、生产 manifest preflight，以及静态 Linux Worker 的包内 14-mode VM 往返；Worker 与 descendant 均无法访问宿主文件、凭据、进程、Unix socket、宿主 loopback 或外网，Worker/launcher 崩溃会清理 VM staging，Runtime 被 `SIGKILL` 后也由可继承 `flock` lease 在 launcher 退出后安全回收孤儿目录。最终 `.app` 还已从正常 Client 入口建立带 token 的 P1 Runtime 会话，核对 Main 注入同一包内 VM manifest，并通过 `app.quit` 证明 bundled Runtime 随应用退出。Linux/arm64 Debian 只读 rootfs 现由固定 OCI manifest 与 e2fsprogs 1.47.2 确定性构建；真实 PyInstaller onedir Worker、内容寻址 Node.js 24.18.0 LTS Runtime Asset、共享 Office Runtime Asset 和独立 MuPDF Runtime Asset 均已在 VM 内以 UID/GID 65534 完成协议往返。Node 主动验证 Runtime Asset 只读；Office 覆盖 Writer/Calc/Impress rich golden，PDF 覆盖 Unicode、无文本层、精确 PNG golden、hostile corpus 与中途取消清理。上述动态 Gate 保留在 release workflow 中，但 GitHub 托管 runner 明确禁用；真实 self-hosted VM Gate 与 Developer ID/公证 runner 尚未运行，因此只保留 `release_ci_gate`，Registry 继续关闭。`darwin/amd64`、Windows 和 Linux 各自保持独立 fail-closed。签名或用户确认不能绕过对应平台门槛。

原生 Linux Runtime 现包含可复现构建的 Bubblewrap 0.11.2、`shejane-managed-worker-linux` 和匹配的 `libcap`。P6 只接受绝对的包内 manifest，并逐文件核对 size/SHA-256、普通文件、无 symlink 和 executable bit；随后从 `/proc/self/cgroup` 找到带 `user.delegate=1` 的 systemd 父级，要求 Runtime 已位于 `DelegateSubgroup=`，并启用、回读 `cpu`、`memory`、`pids` controller。普通 `/sys/fs/cgroup`、系统 `$PATH` 中的 bwrap 和未委托 scope 都会 fail closed。

Linux launcher 用 `CLONE_INTO_CGROUP` 原子启动 Worker，组合只读 root/package/input、空网络/PID/IPC/UTS namespace、按架构 seccomp、计入 cgroup memory 的定容私有 tmpfs，以及只复制声明 Artifact 的 host broker。当前 Docker Desktop Linux/arm64 真实 Gate 已通过文件、凭据、宿主 PID、Unix/TCP/外网隔离、只读路径、禁止嵌套 user namespace、scratch `ENOSPC`、内存耗尽、忽略取消的 descendant 清理与 leaf 回收。发布 workflow 还会在最终 PyInstaller 资产上通过 `systemd-run` 的 `Delegate=yes`、`DelegateSubgroup=supervisor` 重跑同一组测试；该 workflow 尚未真实成功，因此 `systemd_delegation_gate` 和 `release_ci_gate` 仍关闭，非受信任 Worker 仍不得启用。

Office 插件使用内容寻址、平台专用的 LibreOffice/MuPDF Runtime Asset，不探测用户安装的 Office。macOS arm64 的实际 execution platform 是 `linux/arm64`：当前 Linux Asset 固定 LibreOffice 25.8.7、MuPDF 1.27.2 和 Noto Sans CJK 2.004，验证 LibreOffice OpenPGP 签名和所有输入摘要，离线双构建 `mutool`，两次完整 Asset 归档逐字节一致。Documents、Spreadsheets 与 Presentations 的 Linux/arm64 onedir Worker 和插件包也已确定性构建，并通过生产 VM 中的 DOCX 两页/CJK、XLSX 公式重算/日期/区域格式/图表、PPTX CJK/表格/图片 rich golden。用最终 `.app` 内 VM manifest 重跑这三项的 Gate 已保留给 self-hosted Mac；真实 Developer ID/公证 runner 尚未成功执行，`release_ci_gate` 仍关闭，所以 Office 仍不能宣称为已发布产品能力。

Media Foundation 现在有真实 `linux/arm64` 执行候选：`org.ffmpeg.runtime` 从已验证签名的 FFmpeg 8.1.2 源码构建，冻结 Debian OCI/toolchain/package closure，禁用网络、GPL 与 nonfree，并携带源码、签名证据、许可、SBOM 与 provenance。两份完整 Asset 归档逐字节一致（archive `1a8e20a1...e93`，canonical asset `sha256:64026538...4d55`）；冻结 onedir Worker 已在生产 VM 中通过 probe、精确缩略图/抽帧/音频 hash、hostile corpus、取消无部分输出和重放。最终 `.app` 动态 Gate 已保留给 self-hosted Mac，但真实签名/公证 runner 尚未运行，因此仍不是已发布产品能力；其他平台需独立资产与 Gate。详见 `docs/plugins/phase6-media-foundation-research.md`。

PDF 插件现在有 `linux/arm64` 真实执行候选：独立 `org.mupdf.runtime` 从固定 SHA-256 的官方 HTTPS MuPDF 1.27.2 源码构建（上游未提供与 FFmpeg 相同的 PGP 验证流程），冻结 Debian OCI/toolchain/package closure，离线双构建，并携带完整对应源码、许可、SBOM 与 build provenance。Asset 归档逐字节一致；冻结 onedir Worker 已在 macOS arm64 的生产 VM 中通过 inspect、Unicode 页窗文本、无文本层 OCR 标记、精确选页 PNG golden、hostile/truncated corpus、中途取消无部分输出和取消后重放。最终签名/公证 `.app` 的动态 VM Gate 已保留给 self-hosted Mac；真实 release runner、Linux amd64、Windows 尚未完成，所以仍不是已发布产品能力。详见 `docs/plugins/phase6-pdf-research.md`。

OCR 的固定 macOS arm64 与 Windows AMD64 路径使用各平台原生、内容锁定的 `org.rapidocr.runtime`：RapidOCR 3.9.1、ONNX Runtime 1.27.0、PP-OCRv6 medium、CPU provider 和三个精确模型，离线安装锁定依赖、双构建并拒绝 Tesseract/Leptonica。Windows 必须在原生 runner 冻结 PyInstaller Worker/引擎，随后运行真实识别、确定性和 hostile-input Gate；macOS 不做伪交叉编译。受信任的固定 OCR Worker 由 `ocr` host adapter 启动，输入仍由 Runtime 物化，输出仍经严格 schema 和 Artifact 提升；它不会绕过或打开第三方 Managed Worker 的 VM release gate。Linux/arm64 VM 候选及其多语言、布局、方向、hostile-input 与取消 Gate 继续保留作独立平台验证。详见 `docs/plugins/phase6-ocr-research.md`。

Speech 现在有真实 `linux/arm64` 候选：`speech.transcribe` 固定 `whisper.cpp 1.8.6`、`large-v3-turbo Q5_0`、CPU 单线程 greedy，并复用精确 FFmpeg 资产做 16 kHz 单声道 PCM 归一化。官方 checkpoint 转换/量化模型 SHA-256 固定为 `39422170...a7e2`；两份 525 MiB Asset 完全一致（archive `883900b6...5cdd`，canonical asset `sha256:dc6ec9da...4f11`）。生产 VM 已通过重复转写/Artifact hash、显式中英文、带背景噪声/双音干扰和四秒停顿的日文 `auto`、66.7 秒且 45% 音量的印度英语技术长文、hostile 音频、取消清理、300 秒双运行预算，以及真实 Media→Speech 文件 Artifact 组合；引擎报告 7,200,001ms 会在 Artifact 创建前拒绝。专名仍可能误识别，`initial_prompt` 不提供词典保证；真实音乐、混合语种/拉丁文字、真实编码两小时边界及过量输出仍待补。最终 `.app` 动态 Gate 已保留给 self-hosted Mac，但真实签名/公证 runner 尚未运行，因此不得宣称为已发布能力。详见 `docs/plugins/phase6-speech-research.md`。

Cloud Vision 已形成 `linux/arm64` release candidate：管理员先配置明确支持 `image_inputs` 的 Runtime 模型，再通过幂等 `plugin.model.bind` 把具体 `local:<connection>:<model>` 绑定到 `org.shejane.vision.cloud`；未绑定时拒绝启用。绑定在 Run 接纳时冻结；冻结 onedir Worker 只能对授权图片发起一次有界 `model.vision.invoke`，不获得密钥、服务地址或网络。Worker 双构建一致、确定性包已检查（digest `sha256:33ff82dc...381f8`），并在生产 VM 中通过 host-call bridge；Runtime adapter 测试覆盖图片身份/预算、凭据脱敏、具体模型和规范化 usage。最终 `.app` 动态 Gate 已保留给 self-hosted Mac，但真实签名/公证 runner 尚未运行，所以 Registry 继续关闭。Local Vision 仍保持拒绝：`llama.cpp b10025 + SmolVLM2 500M Q8_0` 虽可复现，但质量 Gate 仅 3/5，中文与图表失败；不得发布、不得回退到聊天模型。详见 `docs/plugins/phase6-vision-research.md`。

插件大文件不会写进 SQLite。Run 接纳附件时把正文流式导入：

```text
<data-dir>/inputs/sha256/<prefix>/<digest>
```

文件 Artifact 的目录是：

```text
<data-dir>/artifacts/sha256/<prefix>/<digest>
```

SQLite 只保存授权关系、逻辑大小、摘要和内部 body key。启动时会有界清理超过一小时、没有目录记录引用的孤儿正文；存在目录记录但正文丢失时会按损坏状态失败，不会盲目重跑可能有副作用的 Action。内联文本上限 32 MiB；文件 Artifact 上限为单项 2 GiB、单 Run 4 GiB、单 principal 16 GiB、本机总计 64 GiB。

## 构建 Runtime

Runtime 暂不单独发布二进制文件。请在目标操作系统和 CPU 架构上从源码构建：

```bash
make package-runtime
```

构建结果位于 `runtime/dist/shejane-runtime/`。其中包含平台相关的原生依赖，不能用于其他操作系统或 CPU 架构。

## 发布

公开发布使用两个标签：

```text
client-vX.Y.Z
runtime-sdk-vX.Y.Z
```

Client CI 在两个原生 runner 上分别构建 Runtime 和安装包：

```text
client-macos-arm64
client-windows-x64
```

macOS 正式分发必须配置以下全部 GitHub Actions secrets：

- `MACOS_DEVELOPER_ID_P12_BASE64`：Developer ID Application `.p12` 的 base64；
- `MACOS_DEVELOPER_ID_P12_PASSWORD`：该 `.p12` 的密码；
- `APPLE_API_KEY`：App Store Connect API `.p8` 的 base64；
- `APPLE_API_KEY_ID`、`APPLE_API_ISSUER`、`APPLE_TEAM_ID`。

全部凭据存在时，发布 job 会验证 `.app` 与 DMG 的 staple ticket、Gatekeeper、Hardened Runtime、secure timestamp、Developer ID、VM launcher entitlement 和包内 manifest 身份。全部凭据缺失时仍会生成 ad-hoc 签名、未公证的预览 DMG/ZIP，并验证包内 Runtime、VM 资产静态完整性、launcher 自检和 Runtime 生命周期 smoke；这种产物会触发 Gatekeeper 警告，且不构成 `release_ci_gate` 的发布证据。macOS 原地自动更新同样要求 Developer ID 签名；预览包只能在设置页检查失败后转到 GitHub Releases 手动安装。必须启动 VM 的功能 Gate 需要另在支持虚拟化的 physical/self-hosted Mac 上运行，凭据只配置一部分会 fail closed。配置依据见 [electron-builder macOS signing](https://www.electron.build/mac/)、[electron-builder auto update](https://www.electron.build/docs/features/auto-update/)、[electron-builder notarization](https://www.electron.build/docs/notarization/) 与 [Apple notarization requirements](https://developer.apple.com/documentation/security/notarizing-macos-software-before-distribution)。

手动运行 Client 发布工作流只生成 GitHub Actions 产物。推送 `client-vX.Y.Z` 标签才会创建 GitHub Release。

正式 Client 安装包必须：

- 从同一次提交构建并内置对应平台和架构的 Runtime；
- 固定并内置 Managed Worker sandbox launcher，且在该原生安装包上通过 descendant conformance；
- 只停止 Electron Main 自己启动的 Runtime，不停止外部 Runtime。

## 验证

```bash
make lint
make test
make build
make test-e2e
git diff --check
```

`make test-contract` 会验证真实 Runtime HTTP/SSE 与 SDK，且不启动 Electron。`make test-fixed-plugins-e2e` 会单独验证 Browser QA、Computer Use 和 OCR 的执行路径；`make test-e2e` 先运行该插件门禁，再继续执行进程恢复、官方 MCP client conformance 和 Playwright Electron 关键路径。详细范围见 [Runtime 端到端测试](./runtime-e2e-testing.md)。

发布前还应确认：

- 清空用户环境变量后，Client 和 Runtime 可以启动；
- BYOK 模型能够完成“模型 → 工具 → 模型”；
- 仓库没有根 `.env.example`、模块 `package-lock.json` 或旧目录引用；
- Client 源码只连接 Runtime；
- Client 安装包包含由同一次提交构建的 Runtime。

## 安全边界

- Runtime 只监听 loopback；远程连接必须经过未来的独立网关，不能直接暴露 Runtime。
- 不要打印或提交任何 `.env`、Token 或 BYOK 密钥。
- 不要增加产品私有的会话、模型或工具网关。
- 外部能力通过标准模型服务或 MCP 接入。
