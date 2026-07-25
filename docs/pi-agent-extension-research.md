# Pi Agent 扩展与 SheJane 产品插件对比

> 核验日期：2026-07-22。Pi 的当前官方项目是 [`earendil-works/pi`](https://github.com/earendil-works/pi)，历史 `badlogic/pi-mono` 链接会重定向到这里。本文只把 Pi Package Catalog 当作需求信号，不把第三方包等同于 Pi 官方内置能力。

## 结论

对比 Pi 后，SheJane 现在真正值得新增的产品插件仍然只有 **Browser QA**。

Pi 官方把自己定义为 minimal agent harness，并明确刻意不内置 Subagent、Plan Mode 等功能，让开发者按需安装或自己编写扩展。Pi Package 只是一个可以捆绑 extension、skill、prompt 和 theme 的分发容器，而且官方警告这些包拥有完整系统权限。这个模式适合开发者工具，不适合直接成为 SheJane 的产品路线图。

来源：

- [Pi 首页：minimal harness 与刻意跳过的功能](https://pi.dev/)
- [Pi Packages：包结构、安装方式与完整系统权限警告](https://pi.dev/docs/latest/packages)
- [Pi 官方 Extension Examples](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/examples/extensions/README.md)
- [Pi Package Catalog](https://pi.dev/packages)

## 对比表

| Pi 扩展类别 | Pi 代表包或官方示例 | SheJane 当前情况 | 产品判断 |
|---|---|---|---|
| 浏览器自动化 | `pi-agent-browser-native`、`pire-browser`、`pi-browser-search` | `browser.task` 有代码入口，但 Runtime 当前传入 `browser_llm=None`，模型无法使用 | **需要：Browser QA** |
| Computer Use | `@injaneity/pi-computer-use` | 已做成固定的一方 Computer Use 能力 | 不再新增 |
| Web 搜索与抓取 | `pi-web-access`、多种 web-search 扩展 | 已有 `web.search`、`web.fetch` | 不需要 |
| MCP | `pi-mcp-adapter` | 已有 MCP Runtime、管理页和对话入口 | 不需要 |
| Subagent | `pi-subagents` 及多个变体 | 已有受控子 Agent | 不需要 |
| 生图 | `pi-codex-image-gen`、多个 image-gen 变体 | 已有 `image.generate` 功能入口 | 不需要 |
| 图片理解 | `pi-inspect-image`、vision bridge | 聊天模型已经支持图片输入；另有 Cloud Vision 候选 | 暂不做独立插件 |
| Plan/Todo/Preset | 官方示例和大量第三方扩展 | 属于 Agent 交互方式，不是面向用户的新能力 | 不做产品插件 |
| LSP/代码分析 | `pi-lens`、`pi-lsp` | 对编码 Agent 有帮助，但偏开发者工作台 | 当前产品方向不做 |
| 记忆/上下文压缩 | memory、context-mode、session-history 扩展 | 已有显式记忆和 Runtime checkpoint/compaction | 先完善现有能力 |
| 遥测/状态栏/主题 | telemetry、statusline、pretty、theme 扩展 | 属于运行维护或 TUI 个性化 | 不做产品插件 |
| 远程控制/聊天渠道 | remote-pi、Telegram/Discord/WhatsApp 类扩展 | 会把 SheJane 变成消息网关产品 | 无明确需求前不做 |
| 转写/PDF/媒体 | 第三方 transcription、PDF、video 工具 | 仓库已有候选 Worker，但用户尚未认可为核心产品能力 | 保留候选，不进入近期产品清单 |

## 为什么 Browser QA 是例外

Computer Use 解决的是“只能操作桌面界面”的兜底问题；Browser QA 解决的是网页本身。它能够读取 DOM、可访问性树、控制台、网络请求和页面状态，因此比截图找坐标更稳定，也更适合登录流程、表单、网页验证和前端测试。

Pi 市场里浏览器包反复出现，且形态相近：浏览器工具 + 少量使用说明，而不是新的 Agent 框架。这是明确、独立、用户能直接理解的能力缺口。

参考：

- [Pi 扩展目录中的 `pi-agent-browser-native`](https://pi.dev/packages?type=extension)
- [`pire-browser`](https://pi.dev/packages/pire-browser)
- [`pi-browser-search`](https://pi.dev/packages/pi-browser-search)

## 不照搬 Pi 的部分

1. 不开放任意 npm/git 包直接执行；Pi 官方明确说明 Package 可执行任意代码并拥有完整系统权限。
2. 不把 Plan Mode、Todo、Subagent、状态栏、主题分别变成插件卡片。
3. 不让插件再实现一套 Runtime、权限、Session 或调度系统。
4. 不根据包数量或下载量决定产品路线；Catalog 是开放生态，不是经过产品筛选的能力集合。

## 最小产品清单

近期只保留：

1. **Computer Use**：已经完成，桌面应用兜底。
2. **Browser QA**：下一项，网页理解、操作与验证。

其余能力继续使用 SheJane 已有的 Web、MCP、Skill、Subagent、生图和文件工具，不再重复包装。
