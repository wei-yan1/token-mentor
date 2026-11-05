# Token 军师

面向 AI 任务成本与项目选型的轻量 Python 框架：扫描 README/Markdown/少量源码，结合人工用途说明生成项目画像；通过 MCP 收集有来源的模型价格和新模型线索；按任务、成本、能力、稳定性、速度生成可核验的 Markdown 推荐报告；把报价、反馈和报告缓存到本地 SQLite，供同一服务的多个用户复用。

当前仓库原先只有一个 PyCharm 示例脚本，没有项目说明或产品代码。此实现提供可运行的 MVP 骨架。定价源和搜索工具需使用你实际配置的阿里云、DeepSeek、Tavily MCP 服务地址与工具名；配置模板中的 URL 是占位符，框架不会假装已经连上厂商或填入未经核验的实时价格。

## 快速开始

需要 Python 3.11 或更高版本。

```powershell
python -m pip install -e .
token-mentor eval --cases evals/cases.json
token-mentor recommend --repo . --intent "AI 项目成本和模型推荐工具" --output reports/recommendation.md
```

首次运行推荐时价格库为空，会生成项目画像、数据库建议和空的模型报价表。先创建真实价格源配置：

```powershell
Copy-Item config/sources.example.json config/sources.json
# 编辑 config/sources.json：换成你信任的 MCP endpoint、实际 tool 名和查询参数。
$env:ALIYUN_MCP_TOKEN = "..."
$env:DEEPSEEK_MCP_TOKEN = "..."
$env:TAVILY_API_KEY = "..."
token-mentor refresh --config config/sources.json
token-mentor recommend --repo . --intent "AI 项目成本与模型选型" --output reports/recommendation.md
```

也可通过已核验的报价 JSON 导入数据，字段规范见 [报价数据格式](#报价数据格式)。密钥只通过环境变量传入；不要把密钥写进 JSON、提交到 Git 或放入报告。

## 功能

- `recommend`：优先读取用户指定的项目用途，再读 README、Markdown、Python 和配置文件，生成能力画像、分任务模型排名、估算成本、数据库/缓存建议及 Markdown 报告。
- `refresh`：按国内/国外/全部范围运行市场巡检；支持结构化报价 MCP、Tavily Search 与 Extract。每天核验官方定价页、已收录模型和近期新闻；符合明确表格格式的官方报价自动进入价格表，其他搜索结果进入待核验线索。默认循环间隔 24 小时。
- 阿里云百炼 `GET /api/v1/models` 结构化同步当前工作空间可用模型、能力标签、模态、上下文和部署方式；目录返回的价格不会写入正式报价表。官方目录与定价页分别维护。
- 三个可组合的 Python agent：`MarketResearchAgent`（定时采集）、`ProjectAdvisorAgent`（项目/用途分析与选型）、`EvaluationAgent`（本地测评）。`ProjectAdvisorAgent` 可用本地 Laya 分类任务，再按目录能力标签筛选候选，最后用可核验报价及现有评分规则排序；缺少 `laya.load` 时自动回退到规则分类。
- `import-prices`：导入手工核验或其他 MCP/ETL 整理的标准价格快照。
- `api`：可选 FastAPI 多用户服务；项目描述通过 API 提交，不允许客户端指定服务端文件路径。推荐结果由共享 SQLite 缓存复用。
- `feedback` / `feedback-list`：保存并复盘 1–5 分和原因；`eval`：运行自建的小型意图识别/排序测评集。
- SQLite WAL、事务、外键、查询索引、唯一快照键、缓存 TTL、数据过期提醒；单个 MCP 源失败不会丢弃其他来源的结果。

## 命令

```text
token-mentor recommend [--repo PATH] [--intent TEXT] [--weights ...] [--input-tokens N] [--output-tokens N]
token-mentor refresh [--config PATH] [--scope domestic|foreign|all] [--loop] [--interval-seconds N]
token-mentor import-prices --file prices.json
token-mentor import-benchmarks --file benchmarks.json
token-mentor compare --task coding --benchmark-id coding-suite --benchmark-version 1 --protocol public-v1
token-mentor workflow status RUN_ID
token-mentor workflow resume RUN_ID
token-mentor discoveries [--limit N]
token-mentor targets [--scope domestic|foreign|all]
token-mentor models [--scope domestic|foreign|all]
token-mentor market-leads [--scope domestic|foreign|all]
token-mentor market-checks [--limit N]
token-mentor price-changes [--limit N]
token-mentor feedback REPORT_ID --rating 1..5 [--note TEXT]
token-mentor feedback-list [--limit N]
token-mentor eval [--cases evals/cases.json]
token-mentor api [--host 127.0.0.1] [--port 8080]
```

`--weights` 接受 `cost=0.35,quality=0.4,stability=0.15,speed=0.1`。未配置能力、速度或稳定性实测数据时，该维度会被跳过并在报告中解释，不会补造分数。默认估算每次请求 4,000 输入 token、1,200 输出 token；这只是比较假设，可按实际任务改参数。

## MCP 数据源

复制 `config/sources.example.json`，将 `endpoint` 替换为 MCP 服务的 Streamable HTTP URL，将 `tool` 和 `arguments` 配成对应服务真实提供的工具和参数。当前 MCP 客户端使用 JSON-RPC 初始化与 `tools/call`；远程地址要求 HTTPS。`api_key_env` 指向本机/容器环境变量，默认通过 Bearer Authorization 发送，也可以设置 `auth_header` 与 `auth_scheme` 匹配服务商协议。搜索 MCP 可以返回结构化 JSON，也可以将结果以带标题/URL 的文本返回；发现线索仍需人工核价。

价格工具需返回一个 JSON 报价对象数组，或包含 `quotes` / `models` / `results` / `data` 的 JSON 对象。每条价格至少包含：厂商、模型、输入/输出每百万 token 人民币价格、原始来源 URL、ISO 8601 更新时间。来源链接必须是公开页面链接，不能带认证参数。能力分数 0–1 或 0–100，稳定性 0–100，延迟毫秒。只要 MCP 返回的是网页文本或搜索摘要，框架会拒收为价格。

```json
{
  "provider": "厂商名称",
  "model": "模型 ID",
  "currency": "CNY",
  "price_unit": "CNY/1M tokens",
  "input_price_per_million": 1.0,
  "output_price_per_million": 4.0,
  "source_url": "https://provider.example/pricing",
  "observed_at": "2026-10-27T08:00:00Z",
  "capabilities": {"coding": 0.82, "reasoning": 0.75},
  "stability": 98,
  "latency_ms": 720
}
```

完整离线导入格式为 `{"quotes": [ ... ]}`。benchmark 需单独导入，比较 cohort 必须匹配任务、benchmark ID/版本和协议，字段规范及工作流说明见 [比较数据与工作流](docs/benchmark-data.md)。模型新发现保存在 `discoveries` 表中，可运行 `token-mentor discoveries` 查看；核验官方价格后再转成报价记录。这样 Laya、牛来或后续出现的新模型可以先进入发现队列，再进入可比较价目表。

## HTTP 服务

短期对话记忆：`POST /v1/recommend` 同时传入 `user_id` 和 `session_id` 即启用；省略则独立处理。
每轮使用“上轮摘要 + 本轮要求”生成推荐，再以规则抽取压缩成最多 2000 字符的摘要，覆盖旧摘要；不另存完整聊天流水，不调用 LLM 做语义总结。
摘要保留当前要求与上一轮答复中的任务/首选模型要点，闲置 30 分钟后失效，下次访问记忆库时清除过期摘要。近期上下文会逐轮压缩丢弃，仅作为短期辅助，不保证保留每条历史约束。
调用 `DELETE /v1/conversations/{user_id}/{session_id}` 可清空会话摘要。会话使用的推荐缓存有效期也限制为最多 30 分钟。
现有 API 使用共享服务密钥，`user_id` 是可信接入端提供的隔离标识，并非独立用户鉴权；应由接入端绑定真实用户与会话。

```json
{"user_id":"alice","session_id":"project-a","intent":"再便宜一点","tasks":["speech_recognition"]}
```

```powershell
python -m pip install -e ".[api]"
$env:TOKEN_MENTOR_API_KEY = python -c "import secrets; print(secrets.token_urlsafe(32))"
token-mentor api --host 127.0.0.1 --port 8080
```

`GET /healthz` 提供健康状态；受保护接口：

- `POST /v1/recommend`：JSON 包含 `intent`，可选 `tasks`、`technologies`、`input_tokens`、`output_tokens` 和 `weights`；返回 `report_id`、缓存标记和 Markdown。
- `POST /v1/feedback`：JSON 包含 `report_id`、`rating` 和可选 `note`。
- `GET /v1/catalog` 与 `GET /v1/discoveries`：查看已采集报价和待核验发现。

业务接口使用 `Authorization: Bearer <TOKEN_MENTOR_API_KEY>`。生产部署应设置强随机密钥、TLS/反向代理、访问控制和备份。服务默认只监听 `127.0.0.1`；不要直接把无 TLS 的端口暴露到公网。不要在日志中输出 Authorization 请求头。

## 阿里云单机部署

提供 `Dockerfile` 和 `docker-compose.yml` 作为单机 ECS/容器的起点。每日采集器是默认服务；API 放在可选的 `api` profile 中，因此仅启用价格更新时不需要配置 API 密钥。两者共享持久化数据卷。

```powershell
Copy-Item .env.example .env
# 在 .env 中填写 TAVILY_API_KEY；config/sources.json 需包含已核验的官方定价页。
docker compose up -d collector
docker compose logs -f collector
```

采集容器启动后立即执行一轮，之后每 24 小时执行。检查数据库是否收到正式报价，可在主机运行 `token-mentor market-checks --limit 20` 和 `token-mentor recommend --repo . --intent "你的项目用途"`。只有官方页面抽取到可识别价格时，检查状态才会是 `verified` 并更新正式价格表；`search_only` 表示本轮搜到了线索但没有解析出正式报价，可查看采集器日志定位该厂商页面。

需要同时开放推荐 API 时，在 `.env` 设置至少 32 个字符的 `TOKEN_MENTOR_API_KEY`，然后运行 `docker compose --profile api up -d api`。API 默认仅绑定 `127.0.0.1`。

这个 MVP 使用 SQLite，适合单机 API/采集进程和多用户共享结果缓存。它不是跨多节点高可用数据层；若需多副本横向扩展，需把 `Catalog` 仓储替换为阿里云 RDS PostgreSQL，并通过对象存储或数据库备份保留快照。未在本仓库中伪称 SQLite 已支持多节点部署。

## 测评与改进

### 本地项目模型配置更新（简版）

使用独立 JSON 配置，例如 `{"provider":"厂商名","model":"当前模型ID","temperature":0.3}`。
命令中的 `my-project` 是你自己的项目 ID，`VERSION_ID` 是 propose 返回的版本 ID：

```powershell
token-mentor project-model register my-project --file model-config.json --task coding
token-mentor project-model propose my-project
token-mentor project-model status VERSION_ID
token-mentor project-model apply VERSION_ID
token-mentor project-model rollback VERSION_ID
```

登记配置路径及任务后，生成前后配置、报价快照及每次请求的成本差异。只有近 30 天内的报价、同任务已有能力评分不降、输入输出单价均不升且至少一项下降时才建议替换；无评分则拒绝推断。这里的评分是已导入的数据，需维护者保证可比较，不代表系统进行了真实模型测评。估算使用 4000 输入、1200 输出 token。

应用会检查原配置及报价快照是否变化，通过后原子替换 JSON 文件；额外字段保留。SQLite 保存版本与执行状态，进程重启后可查询、继续中断操作或回滚；外部改动不会被直接覆盖。此版本校验配置结构和报价，不进行真实 API 质量测试，不迁移厂商密钥/endpoint，也不会自动重启业务服务。仅用于本地配置文件，自动定时应用、订阅权益及业务健康检查仍待实现。

编辑 `evals/cases.json`，加入业务常见意图、预期任务类别、可核验报价样本和预期首选模型，再执行 `token-mentor eval`。当前测评覆盖任务识别与推荐排序，不会调用真实模型生成回答；生成质量、延迟和稳定性应通过你自己的模型 API 测评器/线上观测导入。反馈用于复盘，人工据此调整任务映射、测评集和权重；当前不会根据单条评分静默改写规则或自动训练模型。

反馈记录与评分透明，推荐每次带报告 ID 和成本假设。数据库建议仅是规则推断，最终应结合数据量、并发、合规、部署和灾备约束确认。

## 开发

```powershell
python -m pip install -e ".[dev]"
python -m unittest discover -s tests -v
python -m compileall -q src
```


## 六层代码组织

业务实现位于 `context`（上下文）、`tools`（工具）、`orchestration`（编排）、`state`（状态记忆）、`observability`（测评可观测性）、`constraints`（约束策略）。原模块路径保留兼容导入；CLI/API 使用正式模块。目录责任、依赖规则和待实现能力见 [系统架构](docs/architecture.md)。参考 DeepSeek Harness 的可组合能力拆分，保持 Python 核心精简。

## 国内外市场巡检

配置文件中维护厂商和官方域名列表，当前示例覆盖国内与海外常见厂商，并把“牛来 / Ox Alpha”作为新闻热词搜索，不预设其厂商归属。可用 `refresh --scope domestic`、`--scope foreign` 或 `--scope all` 控制范围。百炼结构化目录按页同步工作空间可用模型及能力；Tavily 按厂商搜索通用价格、向量、多模态、语音、重排、3D 和自部署模型的官方资料及新闻；也会逐条核验已入库模型。每次 refresh 对结构化目录/报价、厂商搜索、逐模型核验、页面提取和新闻搜索共享硬上限 100 次外部请求；模型核验按厂商轮转，按最近核验时间优先。

```powershell
Copy-Item config/sources.example.json config/sources.json
Copy-Item .env.example .env
# 在 .env 里填 TAVILY_API_KEY；sources.json 已含可用的官方定价页 URL，也可继续添加结构化价格 MCP
token-mentor refresh --config config/sources.json --scope all --loop --interval-seconds 86400
token-mentor market-leads --scope domestic
token-mentor market-checks
token-mentor price-changes
```

Tavily 搜索的新闻、价格线索和模型描述摘要会保留在待核验库；价格页面正文只有在含有明确的“每百万 token”单位、模型/输入/输出价格和可识别币种时才自动解析为报价，且必须来自该厂商配置的官方域名。已确认的定价页 URL 会直接提取，减少搜索结果变化造成的漏收。外币会按 Frankfurter/ECB 日汇率换算成人民币，同时保存原币价格、汇率、日期和汇率来源；无法消歧或不支持的页面仍只作为待核验线索。百炼目录能力只定义候选适用任务，价格仍由该厂商官方定价页或结构化价格源单独核验。详细状态定义见 [市场巡检](docs/market-research.md)。

百炼目录同步需在 `.env` 设置 `DASHSCOPE_API_KEY` 和 `DASHSCOPE_WORKSPACE_ID`，并按工作空间地域调整 `config/sources.json` endpoint。Laya 按本地 Python 环境的 `import laya` / `laya.load` 接口惰性加载，可通过 `TOKEN_MENTOR_DECISION_BACKEND=laya|laya_shadow|rule` 选择模式，模型与设备分别由 `TOKEN_MENTOR_LAYA_MODEL`、`TOKEN_MENTOR_LAYA_DEVICE` 配置。本项目不下载未知来源的 Laya 包或权重；若本机包不可用，分类器回退到规则判断。


### 私有缓存与本地项目分析

服务端 SQLite 保存模型目录、报价和推荐缓存；客户不获得数据库或全量目录访问。内部目录、市场巡检和工作流详情改用独立的 `TOKEN_MENTOR_OPERATOR_KEY`；不设置即禁止访问。

敏感项目运行 `token-mentor profile --repo .`，仅输出任务类别和技术栈，再调用 `/v1/recommend/profile`。愿意将用途描述交给服务判断时，调用 `/v1/recommend` 并显式设置 `allow_project_content: true`。会话可传 `user_id` / `session_id`，摘要按上下轮压缩，默认保存 30 分钟。详见 [API 合约](docs/api.md)。


### 客户项目与按次收费

已提供独立客户/项目 Key、次数包订单、原子扣次账本及支付宝扫码支付适配。默认关闭共享密钥免计费通道；客户每次成功生成推荐或比较报告扣 1 次，失败退回，幂等重试不重复收费。示例价格为 10 元 / 10 次，可通过服务端配置调整。

支付宝使用官方 Python SDK、RSA2 验签及幂等充值；默认沙箱。需要自行配置对应应用、商户、密钥文件和公网 HTTPS 回调，真实支付未在本仓库环境完成验证。安装 `.[api,payments]`，按 [计费与接入文档](docs/billing.md) 完成开户、建项目、购买和调用。当前无入站 MCP Server 或自动续费；客户登录与工作台已实现。


### 网页工作台

启动 API 后打开 `http://127.0.0.1:8080/`，即可使用紫蓝渐变玻璃卡片产品页、登录/注册页和项目工作台。支持账号密码与 QQ 邮箱验证码认证；API Key 仅用于程序调用服务，不能用于网页登录。登录后可以创建独立项目 API Key，并使用模型推荐、Markdown 下载、评价、密钥管理及支付宝充值。新注册账户从零次数开始，页面不展示内部模型和报价缓存。QQ SMTP 配置见 [网页部署与验收](docs/web.md)。

网页和 API 在同一服务内提供，无需单独运行前端构建。详见 [网页部署与验收](docs/web.md)。
