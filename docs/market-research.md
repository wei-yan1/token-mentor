# 国内外模型市场巡检

## 运行范围

`refresh --scope domestic|foreign|all` 控制目标厂商范围。配置的 `targets` 每项包含 provider、地域、别名和官方域名。示例名单是可维护的起步清单，不代表市场完整覆盖；新增厂商只需加 target。新闻热词与厂商归属分开，像“牛来 / Ox Alpha”只作为 `watch_terms` 搜索，不凭新闻推测归属。

## 一个 24 小时周期的工作

1. 对阿里云百炼目录接口分页读取当前工作空间可用模型、能力、模态、上下文和部署方式；它不是全网产品目录，目录返回的价格字段不进入正式报价表。结构化价格 MCP 独立拉取最新报价并检查厂商和域名。
2. Tavily 对每个厂商搜索官方通用价格页、向量、多模态、语音、重排、3D、自部署模型资料和发布信息，官方域名通过 `include_domains` 限定；定价查询会提取 `targets[].pricing_urls` 配置的官方页，未配置时按搜索到的官方定价页补充提取。
3. 从已有报价和模型资料中选取最近未核验的模型，逐模型搜索；每厂商最多排队 100 个，按上次核验时间轮换，并在厂商间 round-robin 分配全局额度。
4. 按国内、海外分别搜索最近一周的行业新闻，并把结果存入新闻/模型待核验队列。
5. 保存每个数据源、厂商、模型的核查状态、发现数量、报价变化和失败信息。单个来源失败不阻断其他来源。

每次 refresh 对目录 API 分页、价格 MCP、Tavily Search/Extract、逐模型核验及新闻搜索共享**最多 100 次远程请求**。预算在所有 source 间共用；超出部分跳过并在 refresh JSON 的 `research_requests_skipped` 字段报告。新闻查询优先排队，模型核验按厂商轮转，避免某家厂商独占预算。Compose collector 默认间隔是 86,400 秒（24 小时）；也可由操作系统调度器每天调用一次 `refresh`。避免同时用多个定时器启动同一批采集任务。

## 信任级别和写入规则

- `quotes`：接受结构化报价 MCP；也会从 Tavily 抓取的官方页面正文中解析格式明确的 Markdown 价格表。页面必须匹配 target 的 `official_domains`，并能识别模型、输入价、输出价、币种和每百万 token 单位。不明确的文本、搜索摘要、第三方页面和单位无法确认的数据仍只进线索库。
- `targets[].pricing_urls`：可固定厂商自己的定价页。配置加载时会检查每个 URL 必须为 HTTPS，且主机位于该 target 的 `official_domains` 中。
- `aliyun_models`：调用百炼 `GET /api/v1/models`，需提供 `DASHSCOPE_API_KEY`、工作空间 ID 和地域 endpoint。能力标签映射到统一任务类别并保存为模型资料；该接口的价格字段会忽略，正式价格由独立价格源核验。
- `task_pricing`：按厂商搜索向量、多模态、语音、视频、重排、3D 和自部署模型的官方价格与说明。搜索摘要只作为线索，不直接进入正式报价。
- `model_profiles`：存模型描述、特长、上下文长度、模态、来源、地域、首次/最近观测时间。来自官方报价结构的数据可标为 verified；搜索摘要保留原链接并标为 `needs_review`。
- `market_leads`：新闻、价格页、未识别的新厂商/模型搜索结果。Tavily 的搜索摘要不会直接改写已验证报价。
- `market_checks`：每次按 source、厂商、模型记录 `verified`、`search_only`、`found`、`no_results` 或 `failed`。
- `price_changes`：保留旧价、新价、来源链接和观测时间。

国际报价按固定汇率接口 Frankfurter（基于欧洲央行参考汇率）换算为人民币用于跨厂商成本比较。每条报价保留原币、原币输入/输出价格、换算率、汇率日期和汇率接口地址；价格变更历史比较原币价格，避免只因汇率波动就被误报为厂商调整价格。Tavily 的摘要不会单独作为正式报价。

## Tavily 接入

本项目用 Tavily Search REST API，不把 WeKnora 插件二进制当成 MCP server。Search 和 Extract API 都通过 `Authorization: Bearer` 头接收 API key；本地 Compose 从 `.env` 读 `TAVILY_API_KEY` 并注入 collector。接口说明见 [Search API](https://docs.tavily.com/documentation/api-reference/endpoint/search) 和 [Extract API](https://docs.tavily.com/documentation/api-reference/endpoint/extract)。

项目配置只保存变量名。CLI 启动时从项目根目录 `.env` 读取 `KEY=VALUE`，已有进程环境变量优先。`.env` 已被 Git 忽略；密钥不需要也不应该写入 `sources.json`。

百炼目录使用账号工作空间 API，公开产品目录是另一类来源；目录和价格分开同步。Laya 是本地任务分类器：先把用户用途映射为统一任务类，编排层再按已同步的能力标签和正式报价排序。缺少可导入的 `laya` 包或本地模型时会使用规则分类器。

## 查询结果

```powershell
token-mentor targets --scope all
token-mentor models --scope foreign
token-mentor market-leads --scope domestic
token-mentor market-checks --limit 100
token-mentor price-changes --limit 100
```

API 也提供受 Bearer 保护的 `/v1/market/targets`、`/v1/market/models`、`/v1/market/leads`、`/v1/market/checks` 和 `/v1/market/price-changes`。
