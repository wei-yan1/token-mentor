# 按次收费与支付宝接入

当前实现是次数包充值，不代理用户的 LLM token 消耗。一次成功的画像推荐、云端用途推荐或模型比较扣 1 次；命中内部推荐缓存同样扣 1 次。客户付费购买的是选型服务。价格由运营者配置，示例为 10 元 / 10 次，并非已经确定的商业定价。

## 账号、项目与 Key

```text
客户账户 ── 账户级 Key（建项目、发 Key、买次数包）
        ├── 项目 A ── 项目级 Key（调用推荐，仅访问此项目）
        └── 项目 B ── 项目级 Key
        └── 共享次数余额、支付订单、独立用量账本
```

运营者用 `TOKEN_MENTOR_OPERATOR_KEY` 调用 `POST /v1/operator/accounts` 创建客户，初始余额为零。创建账户和发 Key 时返回一次原始 Key；数据库只存哈希。账户 Key 能创建多个项目、轮换或撤销 Key。项目 Key 无权建项目、管理 Key 或读取支付订单。账户 Key 调用收费接口时提供 `X-Project-ID`，项目 Key 自动绑定项目，也会拒绝与绑定值不符的请求头。

报价、模型目录、推荐缓存依旧只在服务端数据库内。客户只能收到自己的请求结果；用量列表不含响应正文、缓存目录和工作流快照。反馈接口只接受该账户及项目已获得的报告。

## 最短调用流程（PowerShell）

先设置服务环境变量，启动 API：

```powershell
uv sync --extra api --extra payments
$env:TOKEN_MENTOR_OPERATOR_KEY = '<至少 32 字符的运营密钥>'
uv run --extra api --extra payments token-mentor api --host 127.0.0.1 --port 8080
```

另开终端操作，变量值只在本次终端中保存：

```powershell
$base = 'http://127.0.0.1:8080'
$operatorHeaders = @{ Authorization = 'Bearer <运营密钥>' }
$account = Invoke-RestMethod "$base/v1/operator/accounts" -Method Post -Headers $operatorHeaders -ContentType 'application/json' -Body '{"name":"测试客户"}'
$accountHeaders = @{ Authorization = "Bearer $($account.api_key)" }
$project = Invoke-RestMethod "$base/v1/projects" -Method Post -Headers $accountHeaders -ContentType 'application/json' -Body '{"name":"客服项目"}'
$order = Invoke-RestMethod "$base/v1/billing/orders" -Method Post -Headers $accountHeaders
$checkout = Invoke-RestMethod "$base/v1/billing/orders/$($order.order_id)/checkout" -Method Post -Headers $accountHeaders
$checkout.qr_code
```

将 `qr_code` 渲染成二维码让对应环境的买家扫码。成功支付后查询订单和余额；回调未到时，可对同一订单调用 `POST /v1/billing/orders/{order_id}/reconcile` 主动查询支付宝。

```powershell
Invoke-RestMethod "$base/v1/billing/balance" -Headers $accountHeaders
$projectHeaders = @{ Authorization = "Bearer $($project.api_key)"; 'Idempotency-Key' = 'first-recommendation' }
Invoke-RestMethod "$base/v1/recommend/profile" -Method Post -Headers $projectHeaders -ContentType 'application/json' -Body '{"tasks":["general"],"technologies":["FastAPI"]}'
```

敏感项目的 `tasks`、`technologies` 可由客户本地的 `token-mentor profile --repo .` 生成。愿意提交业务用途的客户使用 `/v1/recommend`，显式传 `allow_project_content: true`。短期记忆按认证账户、项目和用户标识隔离，不信任请求中的 user_id 作为账户身份。

## 支付宝配置

使用[支付宝官方 Python SDK](https://github.com/alipay/alipay-sdk-python-all)与当面付接口 `alipay.trade.precreate`、`alipay.trade.query`，目前仅支持 RSA2 公钥模式。证书模式、退款 API、自动续费和月度或永久项目更新订阅尚未实现。客户可通过 [网页登录与工作台](web.md) 使用现有能力。当前不自动安装文章里的 Skill，也不代用户完成商户入驻。

| 配置 | 含义 |
|---|---|
| `TOKEN_MENTOR_PACK_AMOUNT_FEN` | 次数包价格，整数分，默认 1000 |
| `TOKEN_MENTOR_PACK_CREDITS` | 次数包数量，默认 10；当前充值次数不设到期日 |
| `ALIPAY_SANDBOX` | 默认 true；正式环境为 false |
| `ALIPAY_APP_ID` | 对应环境的应用 ID |
| `ALIPAY_SELLER_ID` | 对应环境的收款商户 ID |
| `ALIPAY_PRIVATE_KEY_FILE` | 应用私钥 PEM 文件；支持 PKCS#1 / PKCS#8 |
| `ALIPAY_PUBLIC_KEY_FILE` | 支付宝公钥 PEM 文件，不是应用公钥 |
| `ALIPAY_NOTIFY_URL` | 公网 HTTPS 地址，以 `/v1/payments/alipay/notify` 结尾 |

订单价格由服务端取配置并落库，客户不能传金额或购买次数来修改价格。配置改价后，旧订单继续按创建时的价格和次数结算。下单先创建本地订单，再调用 checkout，网络失败后重试同一个订单即可。

回调不使用客户 Key：它验证支付宝 RSA2 签名、app_id、seller_id、订单号、金额和支付成功状态。回调事务一次性写入已支付状态、充值账本和余额；支付宝重复通知只确认，不重复充值。支付宝交易号不能被其他订单复用。返回支付宝要求的纯文本 `success` / `failure`。

主动查询通过同一应用的 SDK 发起，并由 SDK 验证响应签名；随后核对订单号、金额、支付状态及返回中可用的商户字段。客户端跳转和本地订单创建均不视为支付成功。

Compose 的 API 镜像已安装支付依赖，将私钥和支付宝公钥放入 `secrets/alipay/app-private.pem`、`secrets/alipay/alipay-public.pem`，目录只读挂载到 `/run/alipay`。沙箱与正式服务必须使用独立数据库或 Compose project（例如沙箱使用 `docker compose -p token-mentor-sandbox --profile api up --build`），避免混用测试充值余额。当前仓库没有配置支付宝真实应用凭据，因此未宣称完成真实沙箱收款。

## 扣费、重试与恢复

- 三个收费接口必须传 `Idempotency-Key`，同一项目内保持唯一。字母、数字、下划线、短横线，最多 128 字符。
- 同键同请求重试返回原响应，不重复生成、不重复扣费；同键不同内容返回 409。
- SQLite 立即事务扣减余额，并发请求不会透支。余额不足返回 402。
- 普通执行异常退回 1 次，失败请求可用原键重试。
- OS 锁覆盖扣次、计算和完成登记。进程崩溃会释放锁，原幂等键重试沿用已预扣次数；运行中的并发重试返回 409，可稍后重试。
- 若不继续执行中断请求，可从用量列表取 request_id，调用 `POST /v1/billing/requests/{request_id}/cancel` 释放预扣；正在执行或已完成的请求不能取消。此操作只退还服务次数，不是支付宝现金退款。
- 幂等响应正文普通请求保留 24 小时，会话请求保留 30 分钟；后续计费访问或用量查询清理过期正文，账本只保留请求哈希与扣次元数据。原幂等键过期后返回 410，不再扣费。该策略与短期记忆一起避免永久保存对话正文。
- 共享实例密钥兼容模式默认关闭。`TOKEN_MENTOR_ALLOW_LEGACY_KEY=true` 会绕过收费，仅供原内部调用迁移；Compose 固定关闭。

`MeteredService` 是 HTTP 和后续 MCP 适配器共用的计费入口。当前此轮提供 HTTP 接口，尚未新增入站 MCP Server。

## 交付验证

```powershell
uv run --extra api --extra dev --extra payments pytest -q
```

测试覆盖并发余额扣减、幂等重试、失败退款、中断释放、跨账户项目与订单隔离、Key 撤销、记忆隔离、真实 RSA2 验签、SDK 请求签名及响应验签。网关网络由测试替身替换；真实扫码交易仍需应用权限、沙箱账号和可访问的回调地址。
