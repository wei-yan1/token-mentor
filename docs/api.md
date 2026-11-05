# API 合约

服务通过 `token-mentor api` 启动。客户 Key 由账户/项目接口签发；运营密钥从 `TOKEN_MENTOR_OPERATOR_KEY` 读取。除 `GET /healthz` 和支付宝验签回调外，路由要求 `Authorization: Bearer <key>`。原共享密钥仅在显式开启兼容模式后可用，默认关闭。

## POST `/v1/recommend`

请求：

```json
{
  "allow_project_content": true,
  "intent": "SaaS 后端，需要生成中英文产品摘要",
  "tasks": ["summarization", "translation"],
  "technologies": ["PostgreSQL", "Redis"],
  "input_tokens": 4000,
  "output_tokens": 1200,
  "weights": {"cost": 0.35, "quality": 0.4, "stability": 0.15, "speed": 0.1}
}
```

`intent` 必填；`tasks` 只接受 `coding`、`reasoning`、`translation`、`summarization`、`rewrite`、`general`。token 数限制为 0 到 10,000,000。响应包含 `report_id`、`cached`、`markdown`。

## POST `/v1/feedback`

```json
{"report_id": "16-character-id", "rating": 4, "note": "翻译质量好，延迟偏高"}
```

`rating` 为 1–5。报告缓存过期后不再接受该报告反馈。

反馈由 CLI 命令 `token-mentor feedback-list` 提供给服务运营者查看。HTTP API 不开放全体用户的反馈列表，避免互相暴露反馈内容。

## GET `/v1/catalog`、`GET /v1/discoveries`

仅供运营者使用 `TOKEN_MENTOR_OPERATOR_KEY` 读取，客户密钥返回 403。返回最新报价快照或搜索发现列表。`/v1/discoveries?limit=50` 的 limit 会限制在 1–1000。

## HTTP 状态

- `200`：推荐/读取成功。
- `201`：反馈已保存。
- `401`：缺失或无效 Bearer key。
- `404`：反馈报告 ID 不存在或缓存已过期。
- `422`：请求参数或任务值错误。
- `503`：本地报价存储不可用。

客户 Key 只保存哈希，绑定账户或项目，支持撤销。账户级 Key 调用推荐时另传 `X-Project-ID`；项目级 Key 自动绑定项目。三个收费接口 `/v1/recommend`、`/v1/recommend/profile`、`/v1/comparisons` 必须传 `Idempotency-Key`；成功扣 1 次，失败退回。余额不足返回 402，重复键内容不符返回 409，已过期幂等响应返回 410。



## 五节点比较工作流

除原有 `/v1/recommend` 外，服务提供：

- `POST /v1/comparisons`：读取当前报价和 benchmark 快照，执行 `collect → quantify → analyze → recommend → assemble`，返回 `run_id` 和 Markdown。请求包含 `task`、`input_tokens`、`output_tokens`，可选 `benchmark_id`、`benchmark_version`、`protocol`。
- `GET /v1/workflows/{run_id}`：返回状态、当前节点、固定输入快照、检查点和事件。
- `POST /v1/workflows/{run_id}/resume`：从第一个未完成节点恢复，使用原始快照。

工作流详情和恢复接口仅允许独立运营密钥；创建比较仍使用客户密钥。工作流只读当前报价，不替客户项目配置；配置差异、校验、应用和回滚是后续服务能力。


## 市场巡检接口

所有市场接口要求独立运营密钥。`GET /v1/market/targets?scope=domestic|foreign|all`、`/v1/market/models`、`/v1/market/leads`、`/v1/market/checks`、`/v1/market/price-changes` 分别查看厂商清单、模型资料、搜索线索、巡检状态和历史价格变化。定时采集由 Compose collector 每 24 小时执行，或由外部 scheduler 调用 CLI。


## 私有目录与两种接入路径

报价、模型目录和推荐缓存保存在服务端 SQLite（Compose 中为 `/data/catalog.sqlite3` 持久卷），不向客户分发数据库。客户只得到其请求生成的推荐报告，其中仍包含推荐理由及相关费用估算，不提供缓存浏览或全量目录下载接口。

`TOKEN_MENTOR_OPERATOR_KEY` 至少 32 字符，必须不同于客户密钥；未设置时禁止全部内部 HTTP 读取。`/v1/catalog`、`/v1/discoveries`、全部 `/v1/market/*`、工作流详情和恢复均仅运营者可访问。客户密钥不能读取固定输入快照或检查点。

### 敏感项目：本地分析

在客户项目目录执行：

```sh
token-mentor profile --repo .
```

该命令只在本地扫描并输出 `tasks` 和 `technologies`，不请求远程 API，不导出源码、路径、文件证据和业务摘要。检查输出后，将此 JSON 提交到 `POST /v1/recommend/profile`，使用客户 Bearer key。可附加 token 用量和权重；其他字段会被拒绝。技术栈字段应只填技术名称。服务端按画像生成报告并写入数据库缓存，同画像可共享计算结果。本接口不创建对话记忆。

### 授权项目：云端判断

`POST /v1/recommend` 必须提交 `allow_project_content: true`，表示同意服务处理提交的用途描述。无需上传仓库；项目内容可作为 intent 的一部分提交（最多 8000 字符）。该路径的报告缓存可能包含用户描述。现有 API 不接受服务器文件路径去读取客户仓库。

可同时提交 `user_id` 和 `session_id` 启用短期记忆：只保存按上一轮摘要、本轮输入和答复要点压缩的摘要，不保存完整逐轮日志；默认 30 分钟 TTL，访问时清理过期记录。`DELETE /v1/conversations/{user_id}/{session_id}` 清除摘要，不清除独立的推荐缓存。压缩是确定性规则压缩，不调用额外付费模型。

`user_id` 不代表账户身份；短期记忆命名空间由已认证的账户、项目和 user_id 共同生成。客户不能清除其他账户或项目的会话。支付宝订单、独立 Key 和次数账本现已接入，完整配置与限制见 [计费文档](billing.md)。


## 商业化接口

| 方法与路径 | 权限 / 用途 |
|---|---|
| `POST /v1/operator/accounts` | 运营密钥；`{"name":"客户"}`，创建账户并返回一次账户 Key |
| `POST /v1/projects` | 账户 Key；`{"name":"项目"}`，返回项目及独立 Key |
| `GET /v1/projects` | 账户 Key 列出自己的项目，项目 Key 只列出绑定项目 |
| `POST /v1/keys` | 账户 Key；`{"project_id":"..."}` 发项目 Key，`{}` 发账户 Key |
| `GET /v1/keys` | 账户 Key；只返回 Key ID、归属和撤销状态，不返回原始 Key |
| `DELETE /v1/keys/{key_id}` | 账户 Key；撤销自己的 Key |
| `GET /v1/billing/pack` | 客户 Key；查看当前次数包价格（整数分）和次数 |
| `GET /v1/billing/balance` | 客户 Key；账户共享可用次数 |
| `GET /v1/billing/usage` | 客户 Key；用量元数据，项目 Key 只能查自己的项目 |
| `POST /v1/billing/requests/{request_id}/cancel` | 归属客户 Key；释放已中断请求预扣次数 |
| `POST /v1/billing/orders` | 账户 Key；创建服务端定价的待支付订单，不接受自定义金额 |
| `GET /v1/billing/orders/{order_id}` | 账户 Key；自己的订单详情 |
| `POST /v1/billing/orders/{order_id}/checkout` | 账户 Key；返回支付宝扫码链接，可重试同一订单 |
| `POST /v1/billing/orders/{order_id}/reconcile` | 账户 Key；主动查询支付宝并幂等补记充值 |
| `POST /v1/payments/alipay/notify` | 不用客户 Key；支付宝表单回调，校验 RSA2 签名和商户订单金额 |

建账户的初始次数为零。充值和推荐扣次均保存在同一个服务端 SQLite 中。没有浏览缓存的客户接口。


## 网页会话与订单列表

- `POST /v1/auth/email-code`：`{"email":"user@qq.com","purpose":"register|login"}` 发送 QQ 邮箱验证码，需配置 QQ SMTP。
- `POST /v1/auth/register`：`name`、`username`、QQ `email`、至少 10 字符的 `password` 和 6 位 `code`，注册并建立会话。
- `POST /v1/auth/login`：`account`（用户名或已绑定邮箱）和 `password` 登录。
- `POST /v1/auth/email-login`：`email` 和 6 位 `code` 登录。
- **不提供 API Key 网页登录端点。** API Key 只用于以 Bearer 身份调用业务 API。
- `GET /v1/auth/me`：当前会话的账户名称、邮箱和项目管理权限。
- `POST /v1/auth/logout`：注销当前会话。
- `GET /v1/billing/orders`：账户级凭据读取自己的最近 50 笔订单。
- `GET /v1/billing/orders/{order_id}/qr.svg`：账户级凭据读取自己订单已创建的支付二维码。

网页登录使用 8 小时 HttpOnly 会话。无 Bearer 时，原有客户接口也接受有效会话；会话写请求必须发送 `X-Requested-With: TokenMentor`，存在 Origin 时必须匹配本站。运营接口仍只接受独立运营 Bearer 密钥。详细行为见 [网页工作台](web.md)。
