# SheJane Client 安装包体积进一步优化调研

> 调研日期：2026-08-02
>
> 基线提交：`725980b`
>
> 基线产物：本机 macOS arm64、Client `0.1.19`、未作为正式公证发布包
>
> 资料范围：Electron、electron-builder、PyInstaller、Uvicorn、MarkItDown、Anthropic Sandbox Runtime 与 Tauri 的官方文档、官方源码，以及当前仓库和本地产物。外部资料访问日期均为 2026-08-02。

## 结论

还有优化空间，但下一阶段的主要矛盾已经不是 Browser QA 和 RapidOCR 的大型 Runtime Asset，而是两个基础运行时：

1. frozen Python Runtime 约 **241.6 MB**，占安装后应用约 52.9%。
2. Electron Frameworks 约 **202.7 MB**，占安装后应用约 44.4%。
3. Client 自身的 `app.asar` 与 `app.asar.unpacked` 合计约 **11.8 MB**，语言包约 **1.5 MB**。继续调整 ASAR、语言或压缩等级，只会得到很小的收益。

按“收益、风险、改造量”综合排序，建议继续做：

1. **先处理 MarkItDown 的启动和识别成本**：当前调用点已经提供文件扩展名，但 `MarkItDown()` 仍无条件构造 Magika，并带入 ONNX Runtime。先把 MarkItDown 改为首次读取文档时加载，再用扩展名、魔数和现有解析器替代常驻 Magika；本地可直接移出约 **55.9 MB 安装后、16.8 MB 压缩代理**，并明显降低 Runtime 启动内存。
2. **再拆 Runtime 核心里的文档重依赖**：先从 XLSX 转换移除 pandas，再评估把 MarkItDown/PDF/Office 解析整体迁到按需 Worker。完整迁移的本地目录上限约 **107 MB 安装后、39 MB 压缩下载**，现实目标可定为 **90 到 105 MB 安装后、30 到 40 MB 安装包**。
3. **让 Browser QA 与 OCR 的插件 Worker 包也按需下载**：大型 Runtime Asset 已经外置，但两个 `.shejane-plugin` 仍在 frozen Runtime 中，合计约 **13.0 MB**，且自身已经是压缩包，移出后安装包收益接近安装后收益。
4. **穿插做可回退的打包清理实验**：收窄 PyInstaller `collect_all()`，清掉 Client 生产依赖中的 source map / TypeScript 源文件和 macOS 不会执行的 Linux seccomp 二进制，并在构建阶段而不是成品阶段测试 `strip`。这部分单项收益小，但不改变产品能力。
5. **Tauri 只做战略原型，不作为当前减包补丁**：它理论上可移除约 **202.7 MB** 的 Electron Frameworks，当前 Frameworks 单独压缩约 **86.8 MB**；但 SheJane 目前借 Electron 可执行文件充当 Node 运行 SRT，迁移后可能需要额外 Node sidecar，且 Main/Preload、更新、凭据、托盘、通知、Crash 与 IPC 都要重写。净收益必须由真实原型测量，不能引用“最小 Tauri 应用小于 600 KB”来外推 SheJane。

不建议继续投入的方向：切 PyInstaller `onefile`、关闭 ASAR、把 `asarUnpack` 塞回 ASAR、构建 universal macOS 包、打开 `compression: maximum`、在 macOS 使用 UPX。这些要么不减总字节，要么增加启动、签名或安全风险。

## 实施进度：核心文档解析瘦身

2026-08-02 已完成前两项并合并验证：附件读取与 `office.read` 依据已知后缀，先校验 OOXML 包结构或 PDF 头，再直接调用现有 `python-docx`、`openpyxl`、`python-pptx` 与 `pdfminer-six`。因此没有保留一版马上会被删除的 pandas 过渡实现。

| 指标 | 修改前 | 修改后 | 变化 |
|---|---:|---:|---:|
| frozen Runtime 磁盘分配 | 234,108 KiB | 148,640 KiB | **-85,468 KiB，-36.5%** |
| `tar.gz` 压缩代理 | 109,677,572 bytes | 80,101,840 bytes | **-29,575,732 bytes，-27.0%** |
| 轻量转换路由模块导入峰值 RSS | 不适用 | 22,265,856 bytes | 不在导入时加载各格式解析器 |

锁文件净移除了 22 个不再需要的包，包括 MarkItDown、Magika、ONNX Runtime、pandas 与 NumPy；`pdfplumber`/`pypdfium2` 暂时保留，以免 PDF 表格和表单提取质量倒退。实现审查后又补上 200 MB 源文件上限、OOXML 成员数量/单项/总展开大小/压缩比上限、XLSX 100,000 单元格上限，以及 Office/附件文本输出上限，避免小型压缩文件在主 Runtime 内无限展开。同步后的干净环境通过 Runtime 全量测试（1069 passed，57 skipped）；冻结产物通过 `--help`、完整启动和 `/v1/health`。这里记录的是主 Runtime，不是最终 DMG；最终安装包和安装后体积仍要在后续改造完成后统一重建测量。

## 实施进度：安全裁剪原生依赖

2026-08-02 已在 PyInstaller `COLLECT` 阶段对 macOS/Linux 的 `BINARY` 与 `EXTENSION` 启用裁剪，Windows 依照 PyInstaller 的兼容性警告保持关闭。主可执行文件不在裁剪范围内；Wasmtime 动态库也从事后复制改为正常 binary collection，统一经过 PyInstaller 的路径修正和 macOS 重签名。

| 指标 | 裁剪前 | 裁剪后 | 变化 |
|---|---:|---:|---:|
| frozen Runtime 磁盘分配 | 148,640 KiB | 146,724 KiB | **-1,916 KiB，-1.3%** |
| `tar.gz` 压缩代理 | 80,101,840 bytes | 79,277,885 bytes | **-823,955 bytes，-1.0%** |

正式实现采用 PyInstaller 在 macOS 上默认的保守 `strip -S`，因此收益小于此前手工副本使用 `strip -S -x` 的上限实验，但避免额外维护一条符号裁剪和签名链。冻结主程序与全部可执行原生依赖通过 `codesign --verify --strict`，并通过 `--help`、完整启动和 `/v1/health`。Developer ID 深签名与公证仍需等最终 Client 导出产物统一验证。

## 实施进度：Browser QA 与 OCR 运行 payload 按需

2026-08-02 已保留用于工具发现的 manifest/schema 外壳，把 Browser QA bridge + Playwright 代码和冻结 OCR Worker 分别迁入现有的 Browser QA、RapidOCR Runtime Asset。下载、进度、删除、租约和 digest 校验继续走同一条已有链路，不新增第二套插件包下载器。固定插件均升到 `0.1.3`；Browser QA Runtime Asset 升到 `1.61.1+chromium1228.3`，RapidOCR composite Runtime Asset 升到 `3.9.1+ppocrv6-small.2`。

| 指标 | 修改前 | 修改后 | 变化 |
|---|---:|---:|---:|
| Browser QA `.shejane-plugin` | 3,831,096 bytes | 3,377 bytes | **-3,827,719 bytes** |
| OCR `.shejane-plugin` | 9,201,564 bytes | 2,464 bytes | **-9,199,100 bytes** |
| frozen Runtime 磁盘分配 | 146,724 KiB | 134,004 KiB | **-12,720 KiB，-8.7%** |
| `tar.gz` 压缩代理 | 79,277,885 bytes | 66,322,606 bytes | **-12,955,279 bytes，-16.3%** |

相对最初 234,108 KiB / 109,677,572 bytes 基线，当前 frozen Runtime 累计减少 **100,104 KiB（42.8%）**，压缩代理累计减少 **43,354,966 bytes（39.5%）**。这些 payload 并未消失，而是进入用户明确下载的独立 Release 资产；因此它减少安装器和干净安装体积，不减少用户下载并启用两项能力后的总占用。

真实 macOS arm64 最终资产已通过 Browser QA headed/headless E2E、RapidOCR 冻结 Worker 原生质量/hostile-input 门禁；无大型资产的 frozen Runtime 仍能在 P1 安装 `0.1.3` 元数据外壳、到达 `/v1/health`，并为两项能力报告 `downloaded:false`。

## 当前基线与测量方法

### 原始结果

| 指标 | 本地实测 | 说明 |
|---|---:|---|
| DMG | 201,304,778 bytes，约 **201.3 MB** | `stat -f '%z'` |
| `.app` 规则文件逻辑字节 | 452,171,890 bytes，约 **452.2 MB** | `find -type f` 后汇总 `stat` |
| `.app` 磁盘分配 | 446,164 KiB，约 **456.9 MB** | `du -sk`；和 Finder 显示会因统计口径不同而略有差异 |
| Frameworks | 197,912 KiB，约 **202.7 MB** | 主要是 Electron Framework |
| Resources | 247,812 KiB，约 **253.8 MB** | 包含 frozen Runtime 和 Client 内容 |
| frozen Runtime | 235,944 KiB，约 **241.6 MB** | `Contents/Resources/runtime` |
| `app.asar` | 9,264 KiB，约 **9.5 MB** | Client JS、CSS、Main、生产依赖 |
| `app.asar.unpacked` | 2,276 KiB，约 **2.3 MB** | 主要是 SRT vendor 可执行文件 |

安装包内部各部分没有独立的 DMG 压缩账单。为了判断优先级，本次还用 macOS `ditto -c -k` 对子树做了 ZIP 压缩代理测量：

| 子树 | 安装后本地实测 | ZIP 压缩代理 | 解释 |
|---|---:|---:|---|
| Frameworks | 202.7 MB | **86.8 MB** | Electron/Tauri 方向的理论上限输入，不是 Tauri 成品预测 |
| frozen Runtime | 241.6 MB | **111.2 MB** | PyInstaller/依赖拆分的主要对象 |

两个代理之和约 198.0 MB，和实际 201.3 MB DMG 接近，因此适合用于排序；但 ZIP 与 DMG、签名、公证、文件边界不同，文中的“安装包预计”仍须标记为推断。

### Runtime 最大组成

以下均为本地产物的 `du -sk`，不是依赖声明大小：

| Runtime 项 | 安装后 | 压缩代理 | 当前用途 |
|---|---:|---:|---|
| ONNX Runtime | 52.6 MB | 13.8 MB | MarkItDown → Magika 文件识别 |
| Wasmtime | 23.0 MB | 8.3 MB | WASI 插件宿主 |
| `libpython3.12.dylib` | 19.3 MB | 未单测 | frozen Python 基础 |
| pandas | 17.9 MB | 5.1 MB | MarkItDown XLSX extra |
| fixed plugin packages | 13.4 MB | 13.3 MB | Computer Use、Browser QA、OCR 的插件包 |
| Pillow | 11.9 MB | 4.4 MB | 图片工具及 Office |
| cryptography | 11.4 MB | 3.7 MB | 插件签名、MCP/JWT 等安全边界 |
| pdfminer | 9.4 MB | 7.3 MB | MarkItDown PDF extra |
| lxml | 9.2 MB | 4.0 MB | DOCX/PPTX/MarkItDown |
| pypdfium2 | 7.7 MB | 3.4 MB | PDF 渲染/解析 |
| NumPy | 6.9 MB | 2.1 MB | Magika、ONNX Runtime、pandas |
| Magika | 3.4 MB | 3.0 MB | MarkItDown 文件识别模型 |

仓库配置与这个结果一致：Runtime 核心直接依赖 `markitdown[docx,pdf,xlsx]`、Office 库、Wasmtime、Pillow 与 PyCDLib（[`runtime/pyproject.toml`](../../runtime/pyproject.toml#L36-L49)）；PyInstaller 又为 ONNX Runtime 和多个动态导入包做了全量收集（[`runtime/shejane-runtime.spec`](../../runtime/shejane-runtime.spec#L47-L96)）。

## 官方机制核对

### electronLanguages：已经生效，剩余收益很低

electron-builder 的 [`electronLanguages`](https://www.electron.build/docs/api/electron-builder.interface.configuration/#electronlanguages) 只保留指定 Electron locale，未配置时才保留全部语言。当前配置列出中英文变体（[`client/electron-builder.yml`](../../client/electron-builder.yml#L13-L19)），成品实际只有：

- `en.lproj/locale.pak`
- `zh_CN.lproj/locale.pak`
- `zh_TW.lproj/locale.pak`

三份 `locale.pak` 合计 1,472,680 bytes。`en-US`、`zh-CN` 等别名没有额外产生文件，因此整理别名不会继续减包；再删语言最多只是亚 MB 级，还会影响 Chromium 内置 UI、系统对话框或回退语言。

结论：保留现状，不列入下一轮重点。

### ASAR 与 asarUnpack：是布局和加载机制，不是压缩算法

Electron 的 [ASAR 文档](https://www.electronjs.org/docs/latest/tutorial/asar-archives)与官方 [`@electron/asar`](https://github.com/electron/asar)说明，ASAR 是带索引的扁平归档，不负责压缩。electron-builder 也默认启用 ASAR，并把它定位为加载和浅层代码隐藏机制（[Application Contents](https://www.electron.build/docs/contents/#asar-packaging)）。所以：

- `asar: true` 不等于把 11.8 MB Client 内容压缩成更小安装后体积。
- [`asarUnpack`](https://www.electron.build/docs/contents/#unpacking-files-from-asar) 只是把需要真实路径、随机访问或直接执行的文件放到 `app.asar.unpacked`，文件仍随应用交付。
- Electron 明确记录了 ASAR 内执行、`cwd` 和部分原生 API 限制；SRT vendor 二进制不能为了表面上减少 `unpacked` 数字就强行塞回 ASAR。

当前 `asarUnpack` 只有约 2.3 MB，修改布局几乎不改变总字节。真正可做的是删掉当前平台不会执行的 vendor 文件，不能只调整它们放在哪。

### files 与 extraResources：Client 已经白名单化，Runtime 是复制进来的真实成本

electron-builder 的 [`files`](https://www.electron.build/docs/api/electron-builder.interface.configuration/#files) 决定应用目录内容；出现正向 include 后，默认 `**/*` 不再自动追加，但 `package.json` 与生产 `node_modules` 仍会加入，开发依赖不会复制。当前 Client 已使用正向白名单（[`client/electron-builder.yml`](../../client/electron-builder.yml#L21-L32)），方向正确。

[`extraResources`](https://www.electron.build/docs/api/electron-builder.interface.configuration/#extraresources) 会直接把目录复制到 macOS `Contents/Resources` 或 Windows/Linux `resources`。当前 frozen Runtime 正是通过它完整复制（[`client/electron-builder.yml`](../../client/electron-builder.yml#L34-L66)），所以 241.6 MB 必须在 PyInstaller 或能力分层处优化，ASAR 配置碰不到它。

### macOS 架构：当前已经是单 arm64

当前 Electron Framework 和 frozen Runtime 都由 `lipo -archs` 确认为 `arm64`；发布矩阵也只构建 macOS arm64 和 Windows x64（[`release-client.yml`](../../.github/workflows/release-client.yml#L1025-L1037)）。

Electron 官方的 [`@electron/universal`](https://packages.electronjs.org/universal/v3.0.4/index.html)说明 universal 应用本质是把 x64 与 arm64 两个应用合并，FAQ 明确提示体积约翻倍；`mergeASARs` 只能避免 JS/ASAR 重复，不能消除两个架构的原生 slice。

结论：当前 macOS 产物已没有架构瘦身空间。若未来恢复 Intel 支持，应发布独立 x64 与 arm64 包，不要为了单一下载链接改成 universal；同时验证 updater 不会交叉升级架构。

### PyInstaller hidden imports、collect_all 与 excludes：还有小到中等空间

PyInstaller 官方文档说明：

- [`hiddenimports`](https://pyinstaller.org/en/stable/usage.html#what-to-bundle-where-to-search) 用来补静态分析看不到的动态导入。
- [`--exclude-module` / `Analysis.excludes`](https://pyinstaller.org/en/stable/usage.html#what-to-bundle-where-to-search) 会把模块当作不存在；用错会形成仅打包版故障。
- [`collect_all()`](https://pyinstaller.org/en/latest/hooks.html#PyInstaller.utils.hooks.collect_all) 会收集全部数据文件、动态库、包元数据和所有子模块，且默认把原始 Python 文件也作为 data 收集。

当前 spec 对 Magika、LangGraph、LangChain、Deep Agents 与 MarkItDown 使用 `collect_all()`（[`runtime/shejane-runtime.spec`](../../runtime/shejane-runtime.spec#L58-L69)），并对整个 `shejane_runtime`、Uvicorn 做 `collect_submodules()`（[`runtime/shejane-runtime.spec`](../../runtime/shejane-runtime.spec#L94-L96)、[`runtime/shejane-runtime.spec`](../../runtime/shejane-runtime.spec#L133-L160)）。这是为了动态导入正确性，但也容易过收集。

本地产物中，六个 `collect_all()` 包目录共有 427 个原始 `.py`，逻辑字节 5,544,750。它们同时已经进入 PyInstaller `PYZ` 的模块图。第一步可以只实验 `collect_all(pkg, include_py_files=False)`，不要立即改成手工长列表。预期上限约 **5.5 MB 安装后、1 到 3 MB 安装包**。

风险：LangGraph、MarkItDown 或其他库可能通过 `__file__`、`inspect.getsource()`、插件发现读取源文件。必须跑 frozen Runtime、所有 Provider、Checkpoint、Office 附件、MCP、插件和 Agent E2E，不能只以 `/v1/health` 通过为准。

### MarkItDown：当前同时制造磁盘和启动内存成本

当前两个调用点都已经知道扩展名：附件读取把 `file_extension` 传给 `convert_stream()`，`office.read` 也从路径取得后缀（[`agent/backends.py`](../../runtime/src/shejane_runtime/agent/backends.py#L572-L584)、[`tools/office.py`](../../runtime/src/shejane_runtime/tools/office.py#L315-L323)）。但 MarkItDown `0.1.5` 的 `MarkItDown.__init__()` 仍会无条件执行 `magika.Magika()`；Runtime spec 因而显式收集 Magika 与 ONNX Runtime。

本地独立进程测量（macOS `resource.getrusage().ru_maxrss`）：

| 阶段 | 峰值 RSS |
|---|---:|
| 空 Python 进程 | 17.6 MiB |
| `from markitdown import MarkItDown` 后 | 114.7 MiB |
| `MarkItDown()` 后 | 129.6 MiB |
| 另一独立进程直接导入 python-docx/openpyxl/python-pptx | 56.7 MiB |

这不能直接等同于完整 Runtime 的常驻 RSS，但足以证明当前模块级 `_md = MarkItDown()` 在启动阶段支付了不必要的峰值。最小内存优化是把 MarkItDown 导入和构造延后到第一次文档读取；磁盘优化则需要避免其强制 Magika 依赖，可选择维护一个极小上游补丁，或用已有 Office 库按已知格式直接转换。

如果不用 Magika，不能只目信任后缀：PDF 校验 `%PDF`，OOXML 用标准库 `zipfile` 校验 `[Content_Types].xml` 和文档目录，再交给现有解析器。这样可以保留损坏/伪装文件的拒绝行为，不改变 SRT、Managed Worker 或插件权限边界。

**本地上限：** ONNX Runtime 与 Magika 合计约 55.9 MB 安装后，gzip 代理约 16.8 MB。若 XLSX 同时改走 openpyxl，再减少 pandas 约 17.9 MB 安装后、4.9 MB 压缩代理。

### onedir 与 onefile：继续保留 onedir

PyInstaller 的 [usage](https://pyinstaller.org/en/stable/usage.html#what-to-generate) 和 [spec 文档](https://pyinstaller.org/en/stable/spec-files.html)说明，`onefile` 只是把依赖封进一个可执行文件，运行时仍要解压到临时目录；官方还特别提醒 macOS app bundle 的 onefile 组合效率低，每次启动解包并可能被 OS 重新扫描。

当前 spec 明确选择 onedir，以避免每次启动解包、杀毒重新扫描和信号处理问题（[`runtime/shejane-runtime.spec`](../../runtime/shejane-runtime.spec#L4-L11)）。切 onefile 可能让目录看起来更整洁，但不会消灭 Python、ONNX、Wasmtime 等字节，还会增加启动和清理失败面。

结论：不改。

### strip 与 UPX：strip 有实测收益，但必须在构建阶段完成

PyInstaller 的 [`--strip`](https://pyinstaller.org/en/stable/usage.html#how-to-generate) 会处理可执行文件和共享库符号表，官方注明 Windows 不推荐。本次对副本中的 `_internal` Mach-O 做 `strip -S -x`、逐个重新 ad-hoc 签名后，结果是：

| 对象 | 安装后前 | 安装后后 | gzip 代理前 | gzip 代理后 |
|---|---:|---:|---:|---:|
| frozen Runtime `_internal` | 234,108 KiB（含主程序目录） | 200,180 KiB | 109,677,572 B | 104,192,042 B |
| RapidOCR Runtime Asset | 238,900 KiB | 199,880 KiB | 103,898,438 B | 98,737,379 B |

裁剪后的 frozen Runtime `--help` 正常；RapidOCR engine 无输入时仍按协议以状态 2、空 stdout/stderr 退出。也就是说，当前二进制并非已经完全裁剪：核心 Runtime 的毛收益约 **34.7 MB 安装后、5.5 MB 压缩**，OCR 资产约 **40.0 MB 安装后、5.2 MB 下载代理**。

这些数字不能与移除 ONNX 直接相加：核心裁剪收益中约 18.9 MB 来自 ONNX Runtime，而且副本实验使用了比 PyInstaller macOS 默认值更激进的 `-x`。正式构建在移除 Magika/ONNX 后采用保守 `strip -S`，实测仅再减少 1,916 KiB 磁盘分配与 823,955 bytes 压缩代理。

不要对已经组装完成的 PyInstaller 主可执行文件手工执行 `strip`：本次验证会把附加在 Mach-O 后的 PKG archive 截掉，程序报 `Could not load PyInstaller's embedded PKG archive`。正确位置是 PyInstaller 组装/签名前的 `strip=True` 或等价 build hook，之后重新签名、公证并跑 packaged smoke。

PyInstaller 的 [UPX 文档](https://pyinstaller.org/en/stable/usage.html#using-upx)更加明确：UPX 目前只在 Windows 使用；macOS dylib 处理失败，压缩文件也无法通过 Apple Silicon 所需的 `codesign` 校验；Windows 某些 CFG DLL 和原生模块也可能被破坏。

结论：macOS/Linux 已在 PyInstaller 收集阶段启用保守裁剪，Windows 保持关闭；UPX 仍不纳入 macOS，Windows 只有独立实验能证明稳定净收益时才考虑，并保留 DLL 排除列表和签名后 smoke test。

### electron-builder compression：maximum 不值得

electron-builder 的 [`compression`](https://www.electron.build/docs/configuration/#compression) 默认是 `normal`，官方直接说明 `maximum` 不会带来明显体积差异，只会增加构建时间。

结论：保留默认 `normal`。可以做一次同输入 A/B 作为记录，但不应把它当优化路线。

## 可执行优化建议

### 1. 清理 Client 生产依赖中的非运行文件

本次解开 `app.asar` 后，本地实测：

- 生产 `node_modules` 逻辑字节约 9.2 MB。
- source map 约 1.27 MB。
- `zod/src` 约 1.71 MB，其中包含大量 TypeScript 源码与测试。
- `@anthropic-ai/sandbox-runtime` 的 map/type 辅助文件约 0.28 MB。
- macOS 包内仍携带 Linux-only `apply-seccomp` x64 与 arm64 两个二进制，合计 1,358,920 bytes。

Anthropic 官方仓库明确说明 `apply-seccomp` 是 Linux 的 seccomp 实现，并按 x64/arm64 发布（[Sandbox Runtime README](https://github.com/anthropic-experimental/sandbox-runtime#building-seccomp-binaries)）。macOS 使用自己的沙盒路径，不会执行这两个 Linux ELF 文件。

最小实验：

1. 仅对 macOS 排除 `@anthropic-ai/sandbox-runtime/vendor/seccomp/**`。
2. 精确排除已确认不参与运行的 `zod/src/**` 和 `**/*.map`，不要使用会误删许可证、WASM、模板或 Worker 的通用大网。
3. 保留所有 `.js`、`package.json`、SRT vendor 中当前平台可执行文件和许可证。

**预计效果：** 安装后约 4.3 MB 上限，安装包约 1 到 3 MB，均为本地内容推断。

**风险：** 仅打包版路径依赖、错误堆栈 source map 质量下降、未来上游改变 exports。

**验证：** `asar list` 前后清单 diff；macOS 实际跑一次 SRT 文件/网络限制；Windows/Linux 分别构建，确认没有错误复用 macOS 排除规则；Client E2E 与更新安装。

### 2. 收窄 PyInstaller collect_all

第一轮只关闭 raw source collection；第二轮才按包把 `collect_all` 换为“明确 data + 动态 imports”。不要同时改多个包，否则 packaged-only 回归难定位。

**预计效果：** 第一轮上限约 5.5 MB 安装后、1 到 3 MB 安装包；第二轮未知，必须由 `Analysis-00.toc` 和成品 diff 决定。

**风险：** 动态 Provider、插件 entry point、模板或源文件反射缺失。

**验证：** 比较 `Analysis-00.toc` / `PYZ-00.toc`；启动 frozen Runtime；跑 OpenAI/Anthropic/Google 模型各一轮；MCP、Checkpoint resume、Subagent、Office 附件、WASI、固定插件完整 smoke。

### 3. 先移除 pandas，再拆完整 Office/MarkItDown

Microsoft MarkItDown 官方 README 明确把 PDF、DOCX、XLSX 等格式能力作为可分别选择的 optional dependency（[Optional Dependencies](https://github.com/microsoft/markitdown#optional-dependencies)）。当前 Runtime 同时：

- 为 MarkItDown 安装 `xlsx` extra，带入 pandas。
- 直接安装并使用 openpyxl。
- 在 `agent/backends.py` 和 `tools/office.py` 两处直接构造/调用 MarkItDown。

因此可以分两阶段：

#### 3A. XLSX 不再走 MarkItDown pandas 路径

用现有 openpyxl 生成 LLM 需要的工作表、范围和值摘要，把 `markitdown[docx,pdf,xlsx]` 收窄为不含 `xlsx`。

**本地上限：** pandas 目录约 17.9 MB，单独 ZIP 约 5.1 MB。

**现实预计：** 约 16 到 18 MB 安装后、4 到 6 MB 安装包。

**风险：** Markdown 输出细节、公式/格式、隐藏 sheet、合并单元格与大表截断行为变化。

**验证：** 现有 XLSX 测试外，增加真实大表、公式、合并单元格、多 sheet 与非英文文件名；比较转换语义，不要求字节级一致。

#### 3B. 文档解析整体迁到按需 Worker

把 MarkItDown、PDF、DOCX/XLSX/PPTX 解析从常驻 Runtime 移到现有 Managed Worker / Runtime Asset 能力边界；核心只保留协议和安全校验。需要同时替换 `agent/backends.py` 的附件读取和 `tools/office.py`，不能只迁一处。

可迁候选目录的本地合计：ONNX Runtime、Magika、pandas、pdfminer、pypdfium2、NumPy、lxml、MarkItDown，约 **107.4 MB 安装后**；这些子树单独 ZIP 合计约 **38.6 MB**。Pillow 与 cryptography 被核心其他功能共享，不计入候选。

**现实预计：** 90 到 105 MB 安装后、30 到 40 MB 安装包。

**风险：** 首次打开文档需要下载；离线行为；Worker/Runtime Asset digest；历史任务回放；不可信文档解析的隔离；下载失败后的可恢复状态。

**安全要求：** 继续使用锁定 URL、长度、SHA-256、签名/manifest、原子 staging 和 immutable `(plugin_id, version)`；不能把解析退回主进程或系统 Python。

**验证：** 冷启动无资产、下载中断/恢复、离线、旧版本数据、DOCX/XLSX/PPTX/PDF、恶意/损坏文档、lease 与清理历史版本；正式发布前对旧数据副本启动 frozen Runtime。

### 本地历史数据：当前清理按钮还不等于完整磁盘回收

当前设置页的“清理历史版本/清理缓存”调用 `/v1/plugins/runtime-assets/storage`，只管理 Runtime Asset store；它不会删除 `plugins/packages` 下解包后的固定插件版本，也不会整理 SQLite。

本机当前 `plugins/packages` 约 261,364 KiB，但逐项核对后全部处于 active 或被 `run_plugin_bindings` 引用，包括一个体积很大的开发 fixture。直接删除会破坏历史任务恢复或重放，不能把它并入现有按钮做无条件清理。

如果还要优化用户数据占用，正确顺序是：用户明确删除对应历史任务 → 计算不再被 active installation 或 run binding 引用的 package digest → 删除内容寻址目录 → 保留必要的 immutable 版本元数据 → 对数据库做受控 checkpoint/VACUUM。它解决的是长期使用后的磁盘增长，不影响 DMG 或干净安装大小。

### 4. Browser QA 与 OCR 的运行 payload 按需（已完成）

当前大型 Browser QA 和 RapidOCR Runtime Asset 已经在安装器外，发布流程也把它们单独 stage（[`release-client.yml`](../../.github/workflows/release-client.yml#L1413-L1425)）。但 PyInstaller 仍把 Browser QA 与 OCR 的 `.shejane-plugin` 放进 `builtin-plugins`（[`runtime/shejane-runtime.spec`](../../runtime/shejane-runtime.spec#L101-L132)）。本地产物：

- Browser QA plugin：3,831,096 bytes。
- OCR plugin：9,201,564 bytes。
- 两者合计约 **13.0 MB**。

它们自身已经是压缩 ZIP，外层 DMG 再压缩的收益很小，所以移出后安装包与安装后体积都接近减少 13 MB。

实际实现没有新增“插件包 + Runtime Asset”双下载协议，而是保留几 KB 的锁定插件元数据，把 Browser QA bridge/Playwright 与 OCR Worker 直接并入原本就按需下载、由插件 manifest 精确 digest 绑定的 Runtime Asset。这样未下载时仍可发现工具，首次使用或手动按钮只需获取一个原子、内容寻址的资产。

**实测效果：** frozen Runtime 减少 12,720 KiB，压缩代理减少 12,955,279 bytes。

**风险：** 未下载时插件发现、历史任务绑定旧版本、离线安装、包与资产版本错配。固定插件和 Asset 已同步升版，旧版本行继续保留，当前版本不复用旧 digest。

**验证：** 定向包/资产/HTTP/分发测试、旧版本升级测试、Browser headed/headless、原生 OCR 质量/hostile input、冻结构建、干净数据目录启动和缺资产状态均已通过；Windows 原生最终构建与签名仍由 release matrix 门禁执行。

### 5. 精简 Uvicorn standard extras

Uvicorn 官方 [Installation](https://www.uvicorn.org/installation/)说明 `[standard]` 会安装 uvloop、httptools、websockets、watchfiles、python-dotenv、PyYAML 等可选依赖；[Settings](https://www.uvicorn.org/settings/)说明 watchfiles 用于 reload，WebSocket 可设为 `none`，HTTP 可用纯 Python `h11`，event loop 可用标准 `asyncio`。

SheJane frozen Runtime 是 loopback HTTP/SSE 服务，不启用 reload，也未使用 WebSocket。候选方案是依赖普通 `uvicorn`，并显式配置：

- `loop="asyncio"`
- `http="h11"`
- `ws="none"`

当前可见 native 目录中 uvloop、watchfiles、httptools、websockets 合计约 3.3 MB；纯 Python和 metadata 还能再少一点。

**预计效果：** 3 到 5 MB 安装后、1 到 2 MB 安装包。

**风险：** 本机高并发/SSE 性能下降，平台行为差异，Uvicorn `auto` 默认变化。

**验证：** 明确配置而不是依赖自动探测；真实 HTTP/SSE contract、长输出、取消、重连、Runtime 并发与 Windows packaged smoke；记录吞吐和首 token 延迟前后对比。

### 6. Wasmtime 按需化，只在产品愿意改变插件可用性时做

Wasmtime 目录约 23.0 MB，压缩代理约 8.3 MB。它是 WASI 插件宿主，不是死文件。可以把 WASI host 做成受版本和 digest 管理的 Runtime Asset，首次启用 WASI 插件时下载；也可以继续保留，换取开箱即用和离线能力。

**预计效果：** 约 23 MB 安装后、8 MB 安装包。

**风险：** 第三方 WASI 插件首启、离线、版本兼容、宿主安全更新和旧任务恢复。

**建议：** 先看真实 WASI 插件使用率。没有产品数据，不为了 8 MB 安装包扩大插件启动协议。

### 7. Tauri/system WebView：潜力最大，迁移成本也最大

Tauri 官方说明其小体积来自复用系统 WebView，而不是随每个应用携带浏览器引擎；最小应用可以小于 600 KB（[What is Tauri](https://tauri.app/start/#smaller-app-size)）。官方也说明：

- Windows 使用 WebView2；macOS 使用系统 WKWebView/WebKit。
- Tauri 不随应用携带 WebView，因此实际 Web 平台版本取决于操作系统或 WebView provider（[Webview Versions](https://v2.tauri.app/reference/webview-versions/)）。
- Python、Node 等外部运行时需要作为 sidecar 交付（[Embedding External Binaries](https://v2.tauri.app/develop/sidecar/)）。

对 SheJane 的本地理论上限是移除约 202.7 MB Frameworks，压缩代理约 86.8 MB。但这不是净收益，因为：

1. frozen Python Runtime 241.6 MB 仍完整存在。
2. 当前 SRT 命令把 Electron `process.execPath` 当 Node 运行 `srt-launcher.mjs`（[`client/electron/main.cjs`](../../client/electron/main.cjs#L311-L315)、[`client/electron/main.cjs`](../../client/electron/main.cjs#L395-L415)）。Tauri 后必须重写 SRT 宿主，或额外交付 Node sidecar。
3. BrowserWindow/Preload/IPC、安全存储、Updater、Tray、Menu、Notification、Dialog、Crash Reporter、single-instance 与窗口生命周期都需要对应迁移和安全复核。
4. Chromium 到 WKWebView/WebView2 会引入渲染、下载、剪贴板、编辑器、文件预览和自动化差异。

**本地理论上限：** 202.7 MB 安装后、86.8 MB 安装包。

**净收益预计：** 未知；在 Tauri shell + Python sidecar + SRT/Node sidecar 原型之前，不给承诺值。

**推荐决策门槛：** 只有产品明确要求安装后再减少 150 MB 以上，且愿意承担跨平台 Shell 重写，才进入迁移。否则优先处理 Runtime 的 90 到 105 MB 文档栈。

原型只需验证一个纵向切片：窗口加载现有 `dist` → Rust Main 启动 frozen Runtime → bearer 留在宿主 → SRT 执行一次安全沙盒命令 → 更新和退出能关停 Runtime。测出 `.app`、DMG、冷启动、空闲内存和 WebView UI 差异后再决定，不先迁完整产品。

## 不建议采用的“看起来会变小”方案

| 方案 | 不建议原因 |
|---|---|
| 关闭 ASAR | ASAR 本身不压缩；只是把文件散开，安装后总字节基本不变，还改变路径/只读语义。 |
| 缩窄 `asarUnpack` 但不删除文件 | 文件仍被交付，只是从 `app.asar.unpacked` 回到 `app.asar`；可执行文件可能无法运行。 |
| PyInstaller `onefile` | 依赖仍在，只改成交付时封装、运行时解包；启动、杀毒扫描、临时目录和信号处理更复杂。 |
| macOS universal | 官方说明约等于两个架构应用合并；单用户下载更大。 |
| `compression: maximum` | electron-builder 官方说明体积差异不明显，只增加构建时间。 |
| macOS UPX | PyInstaller 官方说明 dylib 处理和 codesign 不兼容。 |
| 删除 cryptography/签名检查 | 11 MB 不值得换取插件包篡改、JWT/MCP 或证书边界失效。 |
| 直接删除 Wasmtime/Office 依赖 | 会形成 packaged-only 缺能力；应做显式按需状态与完整性协议。 |
| 整个 frozen Runtime 首启下载 | 能让 DMG 小约 111 MB，但首次启动仍下载相同字节，离线和更新风险明显；除非产品目标只看下载页数字，否则价值有限。 |

## 建议实验顺序与验收

### Phase A：一周内可验证的小实验

每次只改变一个变量，产物输出到隔离目录，不覆盖发布基线：

1. `collect_all(include_py_files=False)`。
2. macOS 排除 Linux seccomp；精确排除 `zod/src` 和 source map。
3. 普通 Uvicorn + 明确 `asyncio/h11/ws=none`。
4. MarkItDown 移除 `xlsx` extra，XLSX 改走 openpyxl。

每项都记录：

```text
DMG bytes
ZIP updater bytes
.app logical bytes
.app allocated KiB
Frameworks / Runtime / app.asar / app.asar.unpacked
Runtime cold-start to /v1/health
first command accepted latency
first token latency
```

### Phase B：能力真正按需

1. Browser QA/OCR 插件包与 Runtime Asset 成对下载。
2. 文档解析 Worker/Runtime Asset 化。
3. 有使用数据后再决定 Wasmtime 是否按需。

这些改动涉及 P6“绑定资源”与相邻 P7“启动/恢复图”。权威状态仍由 Plugin Catalog/Runtime Asset store 和 immutable plugin version 持有，不能把下载真相移到 Client 本地布尔值。

### Phase C：战略原型

只有 Phase A/B 后仍无法达到目标，再做 Tauri 纵向原型。原型必须保留 frozen Runtime 和 SRT 安全边界，不能为了证明体积而跳过 Node/SRT、更新或凭据路径。

### 发布级验证清单

- `make test`
- `make build`
- `make test-contract`
- packaged Runtime `/v1/health` 只是第一关，还要跑模型、SSE、MCP、Subagent、Checkpoint、Office、WASI、Browser QA、OCR 与 SRT。
- 旧版本 Runtime 数据副本启动，保留旧 `(plugin_id, version)` 和 digest。
- macOS `codesign --verify --deep --strict`、公证；Windows 签名、NSIS 安装/卸载与 Defender smoke。
- arm64/x64 分别验证架构和 updater 元数据。
- 下载中断、离线、digest 错误、正在使用时清理拒绝、历史版本清理与重新下载。
- 同一签名/公证条件下比较体积；未签名开发包只能用于排序，不能作为正式发布最终数字。

## 建议目标

按当前基线，合理的分层目标是：

| 目标 | 安装包 | 安装后 | 性质 |
|---|---:|---:|---|
| 当前基线 | 201.3 MB | 456.9 MB（`du` 口径） | 本地实测 |
| Phase A | 188 到 195 MB | 415 到 430 MB | 推断，主要来自 pandas、收集与依赖清理 |
| Phase A+ 去除常驻 Magika/ONNX 并裁剪剩余原生库 | 175 到 182 MB | 370 到 390 MB | 推断，基于子树与裁剪副本实测 |
| Phase B 文档栈按需 | 150 到 165 MB | 325 到 355 MB | 推断，需真实重构产物确认 |
| Tauri 理论追加空间 | 再少最多约 86.8 MB 压缩代理 | 再少最多约 202.7 MB | 仅上限，不含 Node/SRT sidecar 和 Tauri 宿主 |

最值得先追求的是 **150 到 165 MB 安装包、325 到 355 MB 干净安装**。这不需要立即重写 Electron Shell，也不会把安全沙盒或 Python Runtime 变成不受校验的网络下载。达到这个区间后，再用产品需求决定是否值得为更大的 Tauri 迁移买单。
