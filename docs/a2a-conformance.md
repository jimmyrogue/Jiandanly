# A2A 1.0 兼容性清单

> 最近验证：2026-08-03。本文固定 SheJane A2A Gateway 的声明面、官方测试版本、可复现命令和已知偏差。

## 产品声明面

SheJane 只在独立 `shejane-a2a-gateway` 进程提供 A2A；loopback Runtime 不挂载 A2A 路由，也不直接暴露公网。Gateway 使用自己的 SQLite 保存外部 peer、租户、ID 映射、push config/outbox 和审计记录，通过 Runtime 的公开 `/v1` 协议创建和观察权威 Run。

当前 Agent Card 只声明已经通过验证的能力：

| 项目 | 当前声明 |
|---|---|
| 规范来源 | A2A 仓库 `v1.0.1` |
| 线协议版本 | `A2A-Version: 1.0` |
| binding | `JSONRPC` |
| 普通调用 | `SendMessage`、Get/List/Cancel |
| 流式调用 | `SendStreamingMessage`、`SubscribeToTask` over SSE |
| push | per-task push config CRUD、持久 outbox、至少一次尝试 |
| Card | public Card、认证后的 extended Card、ETag/Last-Modified 缓存协商 |
| 认证 | Gateway opaque bearer；可选 OIDC JWT；可选 mTLS 与 bearer/OIDC 组合 |

没有声明 HTTP+JSON 或 gRPC，因此对应测试跳过不算通过，也不算当前产品能力。A2A 是独立 Agent 服务之间的 federation 协议，不是内部 Subagent/mailbox wire format，也不是手机 Client 的远程 Runtime 协议。

## 固定测试矩阵

| 组件 | 固定版本 | 用途 |
|---|---|---|
| A2A TCK | commit `5996b79f9cefa6fc390980e383e358a66fb9e49e` | Card、JSON-RPC、错误、Task/Message/Artifact、SSE、push 一致性 |
| A2A ITK | commit `486e7add944daaf1a6e247a433782fa0824039ac` | 多节点、双向、跨语言互操作 |
| `a2a-sdk` / `a2a-python` | `1.1.2` | SheJane 生产实现和 Python oracle |
| `a2a-go/v2` | `2.4.0` | Go oracle |
| `@a2a-js/sdk` | `1.0.1` | TypeScript oracle |

[`run_a2a_tck.py`](../runtime/tests/run_a2a_tck.py) 和 [`run_a2a_itk.py`](../runtime/tests/run_a2a_itk.py) 会拒绝错误的 checkout commit；ITK runner 还会把 Go/TypeScript reference agent 临时升级到上表版本，不修改官方 checkout。

## 可复现入口

先准备固定 checkout：

```bash
git clone https://github.com/a2aproject/a2a-tck /path/to/a2a-tck
git -C /path/to/a2a-tck checkout 5996b79f9cefa6fc390980e383e358a66fb9e49e

git clone https://github.com/a2aproject/a2a-itk /path/to/a2a-itk
git -C /path/to/a2a-itk checkout 486e7add944daaf1a6e247a433782fa0824039ac
```

从仓库 `runtime/` 运行：

```bash
uv run python tests/run_a2a_tck.py \
  --tck-root /path/to/a2a-tck \
  --output /tmp/shejane-a2a-tck-compatibility.json

uv run python tests/run_a2a_itk.py \
  --itk-root /path/to/a2a-itk \
  --output /tmp/shejane-a2a-itk.json
```

两个 runner 都会启动临时 Gateway 和 reference agent、使用临时数据库，并在退出时关闭进程。TCK runner 只有在官方命令成功且没有 MUST failure 时返回 0；ITK runner 只有在全部选定场景通过时返回 0。

## 当前验证结果

固定 TCK 的完整 JSON-RPC 运行：

- 265 个 pytest case：99 passed、164 skipped、2 xfailed；0 error、0 hard failure。
- MUST compatibility `100.0%`。
- Agent Card 10/10 passed。
- JSON-RPC requirement 93 passed、2 个书面偏差、7 skipped。

固定 ITK 的 14 个场景全部通过：Python、Go、TypeScript 分别双向覆盖 standard、streaming、push、resubscribe + cancel；四节点 Python/Go/TypeScript/SheJane 多跳同时覆盖 standard 和 streaming。三个 resubscribe 场景另行验证退出时没有遗留 asyncio Task。

## 书面偏差

| Requirement | 等级 | SheJane 选择 |
|---|---|---|
| `DM-SERIAL-005` | SHOULD | A2A 信任边界继续拒绝未知 ProtoJSON 字段并返回 `Invalid params`。这与 Harness P3.2 的稳定结构、未知字段拒绝和明确字段位置要求一致；协议升级必须显式升级 schema，不能静默吞掉拼写错误或未协商字段。 |
| `CORE-MULTI-002` | MAY | 不接受任意客户端提供的新 `contextId`。调用者只能省略它让 Gateway 分配，或使用自己已获授权的既有 context；这避免跨租户/跨任务关联探测。配套 MUST `CORE-MULTI-002a` 的拒绝行为通过。 |

这两项都不是 MUST failure，不能通过放宽生产解析或租户边界来消除。

## 仅测试适配，不进入生产

官方测试 fixture 与生产安全边界有意不同。适配只存在于 [`a2a_tck_sut.py`](../runtime/tests/a2a_tck_sut.py) 和 [`a2a_itk_sut.py`](../runtime/tests/a2a_itk_sut.py)：

- 给 TCK 请求注入临时 peer token、重写其 loopback webhook，并为固定 commit 的两个 fixture 预期错误缺口做定向适配。
- 把 ITK 私有 `application/x-protobuf` instruction 映射为测试输入；生产支持列表不增加该 media type。
- 给 ITK 的 message-only verifier 临时投影 Artifact 文本；生产仍把最终产物放在 Artifact。
- 为 Go SDK 的 `StringList` 解析差异转换测试 Card；生产 Card 保持 canonical ProtoJSON。
- 用内存 transport 把官方 reference agent 的 loopback HTTP 呈现为测试 HTTPS facade；生产 outbound 仍要求 HTTPS、固定 DNS/IP、同源和无重定向。
- Go reference agent 只在临时副本启用官方测试选项 `AllowPrivateNetworks`，以访问 ITK 本地通知服务；SheJane 生产 push SSRF 防护不变。

升级 spec、TCK、ITK 或任一 SDK 时，先更新本清单，再运行两个固定入口并审查 raw compatibility report；禁止使用 `latest` 或只看进程退出码。
