# 图片理解、OCR 与 Browser QA 插件方案

> 核验日期：2026-07-22。本文只讨论三个由 SheJane 固定提供、允许启用或关闭的一方能力，不设计第三方插件市场。

## 结论

三个插件都值得做，但不是三项从零开始的新工程：

| 插件 | 用户价值 | 推荐实现 | 当前基础 | 判断 |
|---|---|---|---|---|
| 图片理解 | 理解照片、截图、图表和界面并回答问题 | 产品化现有 `vision.analyze_images` Cloud Worker，绑定一个明确支持图片输入的模型 | Cloud 候选已通过 fake-provider VM bridge；本地小模型质量不合格 | **保留 Cloud，拒绝当前 Local** |
| OCR | 准确提取文字、置信度和文字位置 | 产品化现有 RapidOCR 3.9.1 + PP-OCRv6 small + ONNX Runtime Worker | macOS arm64 已通过多语言、布局、旋转和确定性 Gate | **最接近发布** |
| Browser QA | 打开网页、交互、检查控制台/网络并验证结果 | 新建受限的一方 Browser Adapter，底层使用固定版本 Playwright + Chromium | 旧 `browser.task` 从未接入模型，且架构不适合继续使用 | **需要重新实现** |

它们与 Computer Use 的边界也很清楚：Browser QA 优先操作有 DOM 和可访问性树的网页；Computer Use 只处理桌面应用、系统界面和网页能力覆盖不到的兜底场景。

## 统一产品形态

三个插件都出现在当前“插件”页，只显示名称、简短描述和开关：

- **图片理解**：理解照片、截图、图表和界面，并回答问题。
- **OCR**：从图片和扫描件中准确提取文字。
- **Browser QA**：打开、操作并检查网页，验证页面是否符合预期。

启用时由 Runtime 返回结构化 `readiness`，Client 只渲染统一准备流程，不再像当前 Computer Use 那样按插件 ID 写特殊分支：

1. 图片理解：选择一个 `image_inputs=true` 的具体模型，并明确显示“图片会发送到 Provider / Model”。
2. OCR：准备本地 Runtime Asset；无需账号和系统权限。
3. Browser QA：准备独立 Chromium；首次需要登录时打开可见的 SheJane 浏览器，由用户自己登录。

图片理解与 OCR 不合并。前者输出语义解释，允许模型存在不确定性；后者输出逐字文本、置信度和多边形坐标。视觉模型官方也明确提示小字、旋转文字、精确定位、计数和图表理解存在局限，因此不能把视觉模型包装成“准确 OCR”。

## 1. 图片理解

### 推荐方案

保留现有 `org.shejane.vision.cloud` Managed Worker 和 `vision.analyze_images` 合同。Worker 不持有密钥和网络权限，只通过 Runtime 的一次有界 `model.vision.invoke` 调用已冻结的具体模型绑定。不得静默切换当前聊天模型、供应商或本地后端。

用户附图后的路由顺序：

1. 当前对话模型本身支持图片输入时，直接随请求发送，不额外产生一次插件调用。
2. 当前模型不支持图片，但图片理解插件已启用并绑定时，Agent 调用 `vision.analyze_images`。
3. 两者都不可用时，给出可操作的启用/绑定提示，不做隐式回退。

现有 Local Vision 候选不能发布。它在中文、图表数值与品牌图片上出现失败或幻觉；“离线”不能抵消错误理解给用户带来的伤害。以后只有新模型通过同一套质量 Gate，才新增 Local 后端。

### 输出、隐私与质量

- 回执记录具体 provider、model、binding revision、图片身份、detail、usage、耗时、终态和产物；不伪造置信度。
- 图片只允许来自本次 Run 已授权的附件/Artifact；远程发送前在首次绑定和插件卡片中持续披露目标供应商。
- 默认 `detail=auto`；小字、密集图表或界面分析可升为 `high/original`，同时纳入 token/费用预算。
- Gate 覆盖中文/英文、小字、旋转、截图、图表、空间关系、计数、歧义图片、恶意图片文字、取消和重复调用计费。

OpenAI 与 Anthropic 当前官方接口都支持多图片理解，但也会按分辨率消耗视觉 token，并可能缩放图片；这进一步支持“明确模型 + 明确 detail + 真实 usage 回执”的设计。

## 2. OCR

### 推荐方案

直接发布路径应建立在仓库现有候选上：

- RapidOCR `3.9.1`
- ONNX Runtime `1.27.0`
- PP-OCRv6 small 检测与识别
- PP-OCRv4 mobile 方向分类
- CPU 单线程、无网络、无首次下载、无平台 OCR 或视觉模型回退

PP-OCRv6 small 官方支持简体中文、繁体中文、英语、日语和 46 种拉丁文字语言。SheJane 已把引擎、模型和执行参数冻结成 Runtime Asset，因此相同输入能得到可复现输出；这比 macOS 用 Apple Vision、Windows/Linux 再换另一套引擎更符合产品的一致性要求。

`ocr.recognize_images` 继续接受最多 16 张显式选择的图片，返回：

- 按输入顺序排列的 `full_text`
- 每行文字、置信度和多边形坐标
- 固定引擎身份、截断状态和警告
- 可选 `.txt` 与结构化 `.json` Artifact

PDF 不在 OCR 中再实现一遍：先由 PDF 插件渲染选中页面为同一 Run 的 PNG Artifact，再交给 OCR。这是明确组合，不是隐式文件转换。

### 仍需关闭的发布门槛

macOS arm64 固定资产已经通过简体中文、繁体中文、日文、英文、低对比度、多栏、手写风格、180° 旋转、敌意图片、无部分产物和确定性回放。正式发布前补齐：

- 真实签名/公证后的应用内安装证据
- macOS arm64 与 Windows/Linux amd64 的固定资产 Gate
- 日文、繁体中文、真实手写与长扫描件基准
- 模型许可证/来源清单与离线安装恢复测试

## 3. Browser QA

### 为什么删除旧路径

当前 `runtime/src/shejane_runtime/tools/browser.py` 的 `browser.task` 会启动一个 `browser-use` 二级 Agent，让它自行规划最多 25 步。实际构建器传入 `browser_llm=None`，所以它从未真正暴露给主 Agent。

这个方向即使补上 LLM 也不理想：第二个模型循环会重复消费上下文和费用，并把点击、授权、取消、失败原因与回执藏在内部历史里。应删除该旧路径，由主 Agent 直接调用 Runtime 管理的浏览器动作。

### 推荐技术与动作合同

使用固定版本 Playwright 和匹配的 Chromium/Chrome for Testing，封装成一方 Browser Adapter；不把完整 Playwright MCP 或 Chrome DevTools MCP 工具目录直接暴露给模型。Playwright 提供浏览器 Context 隔离、定位器自动等待、网络/控制台事件、下载和截图，适合作为稳定执行层。

对模型只提供五类有界动作：

| 动作 | 作用 |
|---|---|
| `browser.open` | 创建/恢复 SheJane 浏览器会话并导航到 URL |
| `browser.observe` | 返回页面标题、URL、可访问性快照和新鲜元素引用 |
| `browser.act` | 执行少量类型化步骤：点击、填写、选择、按键、滚动、上传授权文件 |
| `browser.inspect` | 有界读取控制台错误、失败网络请求或截图 |
| `browser.close` | 保存允许的产物并确定性关闭 Context/进程树 |

快照优先于截图；只有视觉验证、Canvas 或快照不足时才截图。每次导航或 DOM 明显变化后旧引用失效，Agent 必须重新 observe。Playwright 的自动 actionability 检查应保持开启，不允许模型使用 `force` 绕过可见、稳定、可点击等检查。

不要暴露：任意 JavaScript 执行、Cookie/密码导出、堆快照、浏览器扩展安装、第三方 DevTools 工具、任意本地文件访问或用户日常 Chrome Profile。Chrome DevTools MCP 的完整目录包含这些高权限能力，并默认启用使用统计与更新检查，因此只可作为设计参考或未来受限的调试后端，不能原样成为产品插件。

### 会话、权限与恢复

- 使用独立的“工作区 + 用户”SheJane Browser Profile；不连接用户的默认 Chrome。用户可在可见窗口中手动登录，Cookie 仅保存在该隔离 Profile，绝不进入模型上下文或回执。
- 默认可访问公网；访问 localhost、局域网、云元数据地址、跨域重定向和新域名时由 Runtime 网络策略判断并按风险请求批准。
- 文件上传只接受显式授权的附件/Artifact；下载先进入隔离临时目录，校验后提升为 Runtime Artifact。
- 登录、验证码和同意页面保持可见并交给用户；不尝试绕过 CAPTCHA。
- 导航、表单提交、发布、购买、删除等动作沿用 P10 风险授权。取消时先停止录制，关闭 Context，再终止整个进程组；P11 输出清理摘要。
- 回执包含动作序列、URL/origin、元素语义、前后页面状态、控制台/网络摘要、截图/trace/download Artifact、审批与终态，但对密码、Authorization、Cookie 和敏感请求头做脱敏。

### Browser QA Gate

用固定站点与本地夹具验证 SPA 导航、表单、弹窗、iframe、登录接管、上传/下载、重定向、控制台错误、失败请求、过期元素引用和取消。判断成功必须基于最终页面断言与 Artifact，不接受 Agent 的自然语言“看起来成功”。另外覆盖网页提示注入、私网跳转、敏感表单、Profile 锁、僵尸 Chromium 进程和崩溃恢复。

## Runtime 落点

- **Primary stage：P10** — 工具执行、风险授权、回执、取消和结果顺序。
- **Adjacent stages：P6** — 固定插件、具体模型/Runtime Asset 绑定和资源租约；**P11** — Worker/Chromium/临时文件清理；**P12** — Artifact 与终态结算。
- **Canonical owner：Runtime**。Client 只展示插件目录、统一 readiness 与用户授权，不自行启动模型、OCR Worker 或浏览器。
- **Old path replaced：** `browser.task` / `browser-use` 二级 Agent。

三项能力应随应用作为固定一方目录交付，只允许启用/关闭；不出现导入、删除、更新或签名确认。仍保留 manifest、digest、版本、依赖锁定和回执身份，它们用于构建复现、升级和故障定位，不等于开放第三方安装。

## 实施顺序

1. **OCR**：复用已验证 Worker，补平台/发布 Gate 和统一 readiness，最快形成可靠用户价值。
2. **图片理解 Cloud**：补 Client 模型绑定与远程处理披露，跑真实供应商质量/取消/计费 Gate；继续拒绝 Local 候选。
3. **Browser QA**：先冻结动作合同与安全策略，再实现 Playwright Adapter、独立 Profile、Artifact/receipt 和端到端夹具；同时删除旧 `browser.task`。

## 主要资料

仓库事实：

- [`runtime/plugins/vision/README.md`](../../runtime/plugins/vision/README.md)
- [`docs/plugins/phase6-vision-research.md`](./phase6-vision-research.md)
- [`runtime/plugins/ocr/README.md`](../../runtime/plugins/ocr/README.md)
- [`docs/plugins/phase6-ocr-research.md`](./phase6-ocr-research.md)
- [`docs/harness-runtime-stages.md`](../harness-runtime-stages.md)

官方资料：

- [OpenAI Images and Vision](https://developers.openai.com/api/docs/guides/images-vision)
- [Anthropic Vision](https://platform.claude.com/docs/en/build-with-claude/vision)
- [PP-OCRv6](https://www.paddleocr.ai/latest/en/version3.x/algorithm/PP-OCRv6/PP-OCRv6.html)
- [RapidOCR model list](https://rapidai.github.io/RapidOCRDocs/latest/model_list/)
- [Playwright Browser Context isolation](https://playwright.dev/docs/browser-contexts)
- [Playwright actionability and auto-waiting](https://playwright.dev/docs/actionability)
- [Playwright network](https://playwright.dev/docs/network)
- [Playwright downloads](https://playwright.dev/docs/downloads)
- [Chrome DevTools MCP](https://github.com/ChromeDevTools/chrome-devtools-mcp)
