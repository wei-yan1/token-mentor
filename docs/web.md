# 产品页面与项目工作台

网页由现有 FastAPI 服务托管，无需单独运行 Node、前端构建器或第二套部署服务。

```powershell
uv sync --extra api --extra payments
uv run --extra api --extra payments token-mentor api --host 127.0.0.1 --port 8080
```

打开：

- `http://127.0.0.1:8080/`：产品首页。
- `http://127.0.0.1:8080/login`：账号密码登录、QQ 邮箱验证码登录与注册。
- `http://127.0.0.1:8080/app`：项目工作台。

## 设计与部署

遵循本次页面规格：背景从 `#667eea` 渐变至 `#764ba2`，卡片使用 `rgba(255,255,255,0.15)`、12px 背景模糊、20% 白色边框和 16px 圆角。桌面三列，平板两列，手机一列；白色文字以透明度区分层级。首页保留参考图中的宽松导航、居中标题与直接进入产品的行动入口。

`src/token_mentor/web/` 内为 HTML、CSS 与原生 ES modules；不使用外部字体/CDN。设计 token 位于 `web/tokens.css`，根目录 `tokens.css` 是便于复用的导出副本。静态资源随 Python wheel 一起打包，Compose 原有 API 服务直接提供网页。

## 真实功能

| 页面 | 连接的能力 |
|---|---|
| 登录与注册 | 用户名/QQ 邮箱 + 密码、QQ 邮箱验证码、8 小时 HttpOnly 会话、退出 |
| 工作台 | 真实项目数量、剩余次数、服务端次数包价格、最近用量 |
| 项目管理 | 创建项目、切换当前项目、一次性展示项目 Key |
| 模型推荐 | 本地任务类别、JSON 画像导入、授权云端分析、用量与权重 |
| 推荐结果 | 安全文本化渲染、下载 Markdown、评分反馈 |
| 连续会话 | 按认证账户/项目隔离的短期记忆，支持清除 |
| API Key | 签发账户或项目密钥、查看元数据、撤销 |
| 用量与充值 | 支付宝订单、服务端二维码、核对到账、调用状态、释放中断预扣 |

新注册账户余额为零；页面不填演示用量，不提供内部报价目录或缓存浏览接口。支付配置缺失会显示后端返回的状态，订单保留，可在配置完成后继续支付。支付宝真实环境仍按 [计费接入文档](billing.md) 配置。

## 登录与数据

注册要求用户名（3–32 位字母、数字或下划线）、密码和一个 `@qq.com` QQ 邮箱。发送至该邮箱的 6 位一次性验证码验证邮箱所有权后才能建户。已注册账户可以用用户名或已绑定邮箱 + 密码登录，也可以选择 QQ 邮箱验证码登录。**API Key 只能给程序调用服务，不能登录网页。** 已有老版本通过 API Key 建立的浏览器会话会在升级时注销。用户登录后可以在项目工作台创建或撤销 API Key。

数据库新增 `web_users`、`web_sessions`、`web_auth_attempts` 和 `email_verification_codes`。密码使用带随机盐的 scrypt；会话令牌和验证码均只存哈希。验证码 10 分钟过期、60 秒重发冷却、最多 5 次校验，IP 登录与发码尝试按窗口限流。QQ 邮箱发送使用标准库 SMTP SSL，验证码不会在 HTTP 响应、应用日志或页面中返回。

Cookie 为 HttpOnly、SameSite=Lax，默认 8 小时到期；HTTPS 请求自动设置 Secure，也可通过 `TOKEN_MENTOR_COOKIE_SECURE=true` 配置。基于会话的写请求检查同源和 `X-Requested-With`，Bearer API 调用沿用原契约。当前没有密码找回、账户改绑邮箱或第三方 OAuth。

在 QQ 邮箱设置中启用 SMTP 服务并生成**授权码**（不是 QQ 邮箱网页登录密码），通过服务运行环境配置 `TOKEN_MENTOR_SMTP_USERNAME` 与 `TOKEN_MENTOR_SMTP_PASSWORD`。默认使用 `smtp.qq.com:465` SSL，也可以配置 SMTP host/port。未配置时发送验证码接口返回 503，测试或本地开发可注入 mailer；服务绝不把验证码降级为返回给用户或写入数据库明文。

浏览器不在 localStorage 保存 API Key 或密码。sessionStorage 仅保存所选项目、会话 ID 和用于重试的请求哈希/幂等键，不保存业务描述；报告只保留在当前页面内存中。登出清空当前浏览器的界面状态。用户输入、项目名与服务报告通过 DOM 文本节点渲染，避免把结果当 HTML 执行。

## 验证

接口测试：

```powershell
uv run --extra api --extra dev --extra payments pytest -q
```

真实浏览器验收：

```powershell
uv run --extra api --extra dev --extra browser python tests/browser_web.py
```

浏览器测试优先使用本机 Edge；无 Edge 时先运行 `uv run --extra browser playwright install chromium`。测试使用临时数据库、测试邮件注入与模拟支付网关，不碰生产账户、模型数据或真实支付。覆盖 QQ 邮箱验证码注册、账号密码登录、API Key 不能网页登录、创建项目、充值和推荐等操作。会在 `reports/ui/` 输出截图，并检查 320 / 375 / 414 / 768 / 1440 像素布局与 JavaScript 异常。
