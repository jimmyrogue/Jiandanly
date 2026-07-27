# Tuzi `gpt-image-2` 分组与接口排查

核对日期：2026-07-27
来源范围：Tuzi 官方 Apifox 文档、New API 官方文档与源码。

## 结论

这次失败首先是 **Token 分组没有可用渠道**，不是请求体格式错误。

Tuzi 把 `gpt-image-2` 文档按分组拆开：

- `原价 / openai / codex` 分组使用官方兼容的 `POST /v1/images/generations`；
- `default` 分组既提供 `POST /v1/chat/completions` 包装，也提供 `POST /v1/images/generations` 兼容格式。

因此，当前 Token 属于 `Codex` 分组时，SheJane 使用 `/v1/images/generations` 本来就是与 Tuzi 文档匹配的。服务返回 `分组 Codex 下模型 gpt-image-2 的可用渠道不存在`，表示平台在该分组下没有选出这个模型的有效渠道；仅把请求改成 Chat 格式不会解决路由问题。

## 用户现在怎么做

1. 登录 Tuzi 控制台，进入 **API Key 管理**，编辑 SheJane 使用的 Token。
2. 如果可选分组中有 `default`，把 Token 从 `Codex` 改为 `default` 后保存。
3. 在 SheJane 继续选择 `gpt-image-2` 的 **图片生成 / OpenAI Images** 协议并重新测试；不需要改成 Agent 对话。
4. 如果没有 `default`，或切换后仍提示无可用渠道，联系 Tuzi 管理员，提供：请求时间、模型 `gpt-image-2`、Token 分组、接口 `/v1/images/generations`、完整的 `get_channel_failed` 错误和响应中的 request ID（如有）。要求其执行二选一：
   - 给该账户开放一个确实包含 `gpt-image-2` 图片渠道的分组；
   - 在现有 `Codex` 分组中恢复/绑定 `gpt-image-2` 渠道。

普通用户可以把 Token 切到**账户已经获准使用**的分组，但不能自行创建分组、配置上游渠道或把模型绑定到渠道；这些是平台管理员操作。

## 为什么这是最短路径

- Tuzi 提供 `GET /api/user/self/groups`，定义为“返回当前用户可切换的所有分组”；Token 的创建和修改接口也都接受 `group` 字段。因此用户可在获准的分组之间切换。
- New API 的用户文档同样允许在创建或编辑 Token 时指定渠道分组；其管理员文档明确说明，用户分组、Token 分组和渠道开放分组由管理端配置。
- New API 路由时先用 Token 分组决定 `UsingGroup`，再按“分组 + 模型”选择渠道；找不到时直接产生 `get_channel_failed`。这类错误发生在请求真正进入上游模型适配器之前。

## 一手来源

- [Tuzi：获取当前用户可切换的分组](https://tuzi-api.apifox.cn/472335673e0)
- [Tuzi：修改 Token，可修改 `group`](https://tuzi-api.apifox.cn/472335677e0)
- [Tuzi：Codex 等分组的官方兼容图片接口](https://tuzi-api.apifox.cn/448333922e0)
- [Tuzi：default 分组的 Chat 图片接口](https://tuzi-api.apifox.cn/343646951e0)
- [Tuzi：default 分组的 Images 兼容接口](https://tuzi-api.apifox.cn/343646952e0)
- [New API：用户 Token 管理](https://docs.newapi.pro/zh/docs/guide/feature-guide/user/token)
- [New API：管理员分组管理](https://docs.newapi.pro/zh/docs/guide/feature-guide/admin/group)
- [New API：Token 分组进入请求上下文](https://github.com/QuantumNous/new-api/blob/c3db41407dd1a0662ef630c41de4ac0c48c83e3c/middleware/auth.go#L458-L475)
- [New API：按分组和模型找不到渠道时返回错误](https://github.com/QuantumNous/new-api/blob/c3db41407dd1a0662ef630c41de4ac0c48c83e3c/controller/relay.go#L296-L319)
