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
- `refresh`：连接 Streamable HTTP MCP 服务；`pricing` 模式只接受结构化、可溯源价格，`discovery` 模式将 Tavily 等搜索结果存入线索列表，不把搜索摘要当价格。
- 三个可组合的 Python agent：`MarketResearchAgent`（定时采集）、`ProjectAdvisorAgent`（项目/用途分析与选型）、`EvaluationAgent`（本地测评）。推荐决策当前是确定性、可审计规则；不依赖隐藏的 LLM 判断。
- `import-prices`：导入手工核验或其他 MCP/ETL 整理的标准价格快照。
- `api`：可选 FastAPI 多用户服务；项目描述通过 API 提交，不允许客户端指定服务端文件路径。推荐结果由共享 SQLite 缓存复用。
- `feedback` / `feedback-list`：保存并复盘 1–5 分和原因；`eval`：运行自建的小型意图识别/排序测评集。
- SQLite WAL、事务、外键、查询索引、唯一快照键、缓存 TTL、数据过期提醒；单个 MCP 源失败不会丢弃其他来源的结果。

## 命令

```text
token-mentor recommend [--repo PATH] [--intent TEXT] [--weights ...] [--input-tokens N] [--output-tokens N]
token-mentor refresh [--config PATH] [--loop] [--interval-seconds N]
token-mentor import-prices --file prices.json
token-mentor import-benchmarks --file benchmarks.json
token-mentor compare --task coding --benchmark-id coding-suite --benchmark-version 1 --protocol public-v1
token-mentor workflow status RUN_ID
token-mentor workflow resume RUN_ID
token-mentor discoveries [--limit N]
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

提供 `Dockerfile` 和 `docker-compose.yml` 作为单机 ECS/容器的起点。创建本地 `config/sources.json` 并准备环境变量后，在单机上运行 API 和定时采集容器，共享持久化数据卷。服务/API、采集循环及调度器分离；采集任务可按需用容器重启策略监督。

这个 MVP 使用 SQLite，适合单机 API/采集进程和多用户共享结果缓存。它不是跨多节点高可用数据层；若需多副本横向扩展，需把 `Catalog` 仓储替换为阿里云 RDS PostgreSQL，并通过对象存储或数据库备份保留快照。未在本仓库中伪称 SQLite 已支持多节点部署。

## 测评与改进

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
