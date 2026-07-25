---
name: "石间 · SheJane"
description: "石间书案：以温暖纸张、墨色与克制层次构成的本地 Agent 工作台。"
colors:
  paper: "#FAF9F6"
  paper-raised: "#FFFFFF"
  paper-sunken: "#F3F1EC"
  paper-wash: "#EFEDE7"
  ink: "#2B2A28"
  ink-soft: "#6F6B63"
  ink-faint: "#A8A39A"
  ink-ghost: "#C9C5BC"
  line: "#E8E5DE"
  line-strong: "#D9D5CC"
  seal: "#B3532F"
  seal-deep: "#8F3E27"
  moss: "#5E7A6E"
  ochre: "#8A6D3B"
  ochre-bg: "#F4EBDD"
  ochre-text: "#7A5D30"
  info: "#4A6B8A"
typography:
  display:
    fontFamily: "-apple-system, BlinkMacSystemFont, PingFang SC, Noto Sans SC, Source Han Sans SC, Helvetica Neue, sans-serif"
    fontSize: "24px"
    fontWeight: 600
    lineHeight: 1.28
    letterSpacing: "0"
  headline:
    fontFamily: "-apple-system, BlinkMacSystemFont, PingFang SC, Noto Sans SC, Source Han Sans SC, Helvetica Neue, sans-serif"
    fontSize: "18px"
    fontWeight: 600
    lineHeight: 1.35
    letterSpacing: "0"
  title:
    fontFamily: "-apple-system, BlinkMacSystemFont, PingFang SC, Noto Sans SC, Source Han Sans SC, Helvetica Neue, sans-serif"
    fontSize: "14px"
    fontWeight: 600
    lineHeight: 1.4
    letterSpacing: "0"
  body:
    fontFamily: "-apple-system, BlinkMacSystemFont, PingFang SC, Noto Sans SC, Source Han Sans SC, Helvetica Neue, sans-serif"
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.76
    letterSpacing: "0"
  label:
    fontFamily: "-apple-system, BlinkMacSystemFont, PingFang SC, Noto Sans SC, Source Han Sans SC, Helvetica Neue, sans-serif"
    fontSize: "12px"
    fontWeight: 500
    lineHeight: 1.4
    letterSpacing: "0"
  wordmark:
    fontFamily: "Noto Serif SC, Songti SC, STSong, serif"
    fontSize: "16px"
    fontWeight: 600
    lineHeight: 1.2
    letterSpacing: "0.12em"
  mono:
    fontFamily: "SF Mono, JetBrains Mono, ui-monospace, monospace"
    fontSize: "12px"
    fontWeight: 400
    lineHeight: 1.5
    letterSpacing: "0"
rounded:
  sm: "6px"
  md: "10px"
  dialog: "12px"
  lg: "14px"
  pill: "999px"
spacing:
  xs: "4px"
  sm: "6px"
  md: "8px"
  lg: "10px"
  xl: "14px"
  2xl: "18px"
  3xl: "24px"
components:
  button-primary:
    backgroundColor: "{colors.ink}"
    textColor: "{colors.paper-raised}"
    typography: "{typography.label}"
    rounded: "{rounded.md}"
    padding: "0 10px"
    height: "32px"
  button-ghost:
    backgroundColor: "transparent"
    textColor: "{colors.ink-soft}"
    typography: "{typography.label}"
    rounded: "{rounded.md}"
    padding: "0 8px"
    height: "28px"
  input:
    backgroundColor: "{colors.paper-raised}"
    textColor: "{colors.ink}"
    typography: "{typography.label}"
    rounded: "{rounded.md}"
    padding: "0 10px"
    height: "32px"
  card:
    backgroundColor: "{colors.paper-raised}"
    textColor: "{colors.ink}"
    rounded: "{rounded.lg}"
    padding: "14px"
  dialog:
    backgroundColor: "{colors.paper-raised}"
    textColor: "{colors.ink}"
    rounded: "{rounded.dialog}"
    padding: "20px 22px 18px"
---

# Design System: 石间 · SheJane

## Overview

**Creative North Star: "石间书案"**

SheJane 的界面是一张安静但可工作的书案：温暖纸面承载内容，墨色建立秩序，细线与轻微层次帮助用户判断边界。它不是展示型网站，也不是充满仪表盘卡片的 SaaS；它首先是一套让用户阅读、决定并完成任务的桌面工具。

整体气质安静、审慎、带有轻微编辑感，并通过准确的间距、克制的状态反馈和清晰的权限界面建立可信赖感。品牌存在于纸、墨、留白、圆相与极少量朱砂中，而不是装饰性插画或高饱和配色。

**Key Characteristics:**

- 温暖纸张与墨色占据绝大多数界面。
- 信息密度紧凑但不拥挤，控件以任务完成为先。
- 主要层次来自色阶、留白和细边框，阴影只做环境抬升。
- 朱砂红和苔青严格保留给少量具有明确语义的状态。
- 动效短促、安静，并尊重系统的减少动态效果设置。

## Colors

配色以温暖的纸白与近中性的墨色构成，唯一的高辨识强调来自朱砂红和苔青。

### Primary

- **朱砂红** (`#B3532F`): 品牌圆相缺口、运行中状态、破坏性意图和真正重要的计数；不作为普通选中态或常规开关颜色。

### Secondary

- **苔青** (`#5E7A6E`): 在线、成功、完成状态；只表达正向系统状态，不用于装饰。

### Tertiary

- **警示赭** (`#8A6D3B` / `#7A5D30` / `#F4EBDD`): 等待、警示和需要注意但不具破坏性的状态。
- **信息蓝灰** (`#4A6B8A`): 少量中性信息状态；不扩展为界面主色。
- **深朱砂** (`#8F3E27`): 朱砂系状态文字，确保淡朱砂背景上的可读性。

### Neutral

- **纸白** (`#FAF9F6`): 应用画布和聊天背景。
- **抬升纸白** (`#FFFFFF`): 对话输入、用户消息、弹窗、预览和需要从画布上轻微抬起的表面。
- **宣纸灰** (`#F3F1EC`): 侧栏、代码片段、轻量选中与嵌套表面。
- **纸面洗色** (`#EFEDE7`): 悬停、低对比工具背景和柔和分隔区。
- **墨黑** (`#2B2A28`): 主文字、强操作和高优先级结构。
- **淡墨** (`#6F6B63`): 次要文字、工具和说明。
- **浅墨** (`#A8A39A`): 元数据、占位符、非活动图标。
- **幽墨** (`#C9C5BC`): 最低优先级提示和免责声明。
- **纸纹线** (`#E8E5DE`): 常规 `0.5px` 发丝线。
- **深纸纹线** (`#D9D5CC`): 输入边框、强调分隔和需要更清晰轮廓的表面。

### Named Rules

**The 朱砂稀缺 Rule.** 朱砂红在任何屏幕上都必须稀少；普通启用态、常规按钮、导航选中和装饰不使用它。

**The 墨色层级 Rule.** 建立层级时先在墨黑、淡墨、浅墨和幽墨之间选择，再考虑增加新颜色。

## Typography

**Display Font:** 系统无衬线字体栈（优先 `PingFang SC` 等原生中文字体）
**Body Font:** 与 Display 共用系统无衬线字体栈
**Wordmark Font:** `Noto Serif SC` / `Songti SC` / `STSong`
**Label/Mono Font:** `SF Mono` / `JetBrains Mono` / `ui-monospace`

**Character:** 正文依赖原生系统字体获得稳定、清晰和本地感；衬线体只用于石间品牌字标。技术标记、文件类型和快捷键使用等宽体，不把技术气质扩散到普通内容。

### Hierarchy

- **Display** (600, `24px`, `1.28`): 欢迎状态和少量页面级标题。
- **Headline** (600, `18px`, `1.35`): 设置分区、重要空状态和对话框层级。
- **Title** (600, `14px`, `1.4`): 卡片名称、列表主标签和局部标题。
- **Body** (400, `14px`, `1.76`): 助手回答；常规界面正文使用 `13px`–`14px` 与 `1.55`–`1.65`。
- **Label** (500, `12px`, `1.4`): 按钮、工具、状态和辅助说明。
- **Wordmark** (600, `16px`, `1.2`, `0.12em`): 石间品牌字标，禁止用于普通标题。
- **Mono** (400, `12px`, `1.5`): 代码、计数、快捷键、技术标记和文件类型字形。

### Named Rules

**The 原生阅读 Rule.** 正文优先使用平台原生字体和正常字重；不要用全大写、夸张字距或装饰字体制造层级。

## Layout

桌面端采用“侧栏 + 工作区”结构。默认侧栏宽度约 `252px`，可在 `190px`–`340px` 范围调整；聊天内容列最大宽度 `700px`，输入器作为独立书案浮岛保持 `560px`。工作区与滚动区域铺满窗口，内容列在内部居中，避免把整页包进大型外框卡片。

基础间距来自 `4 / 6 / 8 / 10 / 14 / 18 / 24px` 的紧凑节奏。列表行通常为 `28px`–`34px` 高，控件优先保持一行。设置页、插件页和 Skill/MCP 列表共享对齐的内容列、工具栏和稳定滚动槽。

在 `860px` 以下隐藏桌面侧栏并让主工作区占满宽度；`820px` 以下设置导航改为顶部横向滚动；`640px` 以下双列列表变为单列。窄屏通过折叠和重排保持功能，不缩放成难以操作的桌面缩略图。

## Elevation & Depth

系统采用“色阶优先、环境阴影辅助”的混合层次。纸白、宣纸灰和洗色先承担父子关系；只有输入器、弹窗、浮层、预览和真正悬浮的表面使用阴影。静止列表项、普通设置行和助手消息不应被卡片化。

### Shadow Vocabulary

- **纸面接触** (`0 1px 2px rgba(43, 42, 40, 0.04)`): 用户消息、小卡片和轻微抬升表面。
- **浮岛抬升** (`0 2px 8px rgba(43, 42, 40, 0.06), 0 1px 2px rgba(43, 42, 40, 0.04)`): 输入器、预览和中等浮层。
- **模态抬升** (`0 8px 28px rgba(43, 42, 40, 0.10), 0 2px 6px rgba(43, 42, 40, 0.05)`): 菜单、弹窗和需要明确遮挡关系的表面。

### Named Rules

**The 平面优先 Rule.** 表面默认平坦；只有对象真的离开文档流、覆盖内容或承担焦点时才获得阴影。

## Shapes

形状语言以 `6px / 10px / 14px` 三档圆角为主：`6px` 用于紧凑操作和文件对象，`10px` 用于输入、列表选择与中型容器，`14px` 用于消息、输入浮岛和预览面板。弹窗使用实际实现中的 `12px` 中间值。圆形和 `999px` 胶囊只用于状态点、开关、头像、计数或真正的短标签。

边框通常为 `0.5px` 纸纹线；需要更清晰输入边界时使用深纸纹线。用户消息保留 `14px 14px 4px 14px` 的轻微尾角，其他容器避免无意义的不对称。

## Components

### Buttons

- **Shape:** 常规按钮 `10px`，紧凑图标按钮 `6px`–`8px`。
- **Primary:** 墨黑底、抬升纸白文字，通常高 `32px`；只用于当前流程的主要确认。
- **Hover / Focus:** 悬停通过纸面洗色或墨色强度变化反馈；键盘焦点使用低对比墨色环，不使用系统彩色描边。
- **Secondary / Ghost:** 普通工具保持透明，悬停时出现纸面洗色；破坏性操作仅在必要时使用朱砂文字或淡朱砂背景。

### Chips

- **Style:** 仅在表达已选能力、项目、附件或短状态时使用，背景为宣纸灰或极淡墨色混合。
- **State:** 选中仍以墨色和轻微表面变化表示；不要用多种高饱和颜色区分普通类别。

### Cards / Containers

- **Corner Style:** `10px` 或 `14px`。
- **Background:** 抬升纸白；嵌套内容可使用宣纸灰。
- **Shadow Strategy:** 默认无阴影；浮岛使用“纸面接触”或“浮岛抬升”。
- **Border:** `0.5px` 纸纹线。
- **Internal Padding:** 以 `10px`–`14px` 为主，复杂内容可使用 `18px`。

### Inputs / Fields

- **Style:** 高 `32px`–`36px`、`10px` 圆角、抬升纸白或透明背景、深纸纹线。
- **Focus:** 边框加深并出现低对比墨色焦点环。
- **Error / Disabled:** 错误使用朱砂系文字和淡背景；禁用降低透明度但保留标签可读性。

### Navigation

侧栏行高约 `34px`，图标使用 Tabler `1.5px` 线条，默认透明；悬停出现轻微白纸混合，当前项使用抬升纸白和发丝线。插件内部标签保持文字优先，不用大型分段胶囊。

### Composer

输入器是聊天页的主要浮岛：宽 `560px`、`10px` 圆角、深纸纹线和浮岛阴影。编辑区与工具栏共享一个表面，工具按钮保持无边框；仅当内容可发送时，发送按钮变为墨黑实心状态。

### Message and Tool Surfaces

助手回答直接排在纸面上，不加气泡；用户消息使用小型抬升纸白气泡。工具进度、权限、问题和失败状态按重要性逐级增加结构，普通进度不应被包装成大型面板。

## Do's and Don'ts

### Do:

- **Do** 让温暖纸张和墨色承担约 95% 的界面。
- **Do** 先使用字重、墨色强度、留白和 `0.5px` 发丝线建立层级。
- **Do** 保持桌面工具的紧凑密度，并为长名称、中文与英文文案预留截断或换行策略。
- **Do** 使用 Tabler `1.5px` 单色线性图标和单色文件类型字形。
- **Do** 为键盘焦点、减少动态效果、错误和禁用状态保留清晰可见的反馈。

### Don't:

- **Don't** 使用紫蓝渐变、彩色光斑、装饰性渐变或多彩应用图标。
- **Don't** 把设置行、列表项、助手回答和普通进度层层包装成卡片。
- **Don't** 把朱砂红用于普通开关、一般选中态或无语义装饰。
- **Don't** 使用厚重阴影、厚边框、超大圆角或充满页面的胶囊控件。
- **Don't** 用夸张缩放、弹跳或持续动画打断任务；状态动效应短促并支持 `prefers-reduced-motion`。
