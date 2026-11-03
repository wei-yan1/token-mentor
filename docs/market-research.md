# 国内外模型市场巡检

## 运行范围

`refresh --scope domestic|foreign|all` 控制目标厂商范围。配置的 `targets` 每项包含 provider、地域、别名和官方域名。示例名单是可维护的起步清单，不代表市场完整覆盖；新增厂商只需加 target。新闻热词与厂商归属分开，像“牛来 / Ox Alpha”只作为 `watch_terms` 搜索，不凭新闻推测归属。

## 一个 24 小时周期的工作

1. 对配置了结构化价格 MCP 的厂商拉取最新模型报价，检查结果厂商和来源域名；记录价格变化历史。
2. Tavily 对每个厂商搜索官方价格页和模型发布信息，官方域名通过 `include_domains` 限定。
3. 从已有报价和模型资料中选取最近未核验的模型，逐模型搜索；每厂商最多排队 100 个，按上次核验时间轮换，并在厂商间 round-robin 分配全局额度。
4. 按国内、海外分别搜索最近一周的行业新闻，并把结果存入新闻/模型待核验队列。
5. 保存每个数据源、厂商、模型的核查状态、发现数量、报价变化和失败信息。单个来源失败不阻断其他来源。

每次 refresh 对所有已启用的价格 MCP、厂商搜索、逐模型核验及新闻搜索共享**最多 100 次远程请求**。预算在所有 source 间共用；超出部分跳过并在 refresh JSON 的 `research_requests_skipped` 字段报告。新闻查询优先排队，模型核验按厂商轮转，避免某家厂商独占预算。Compose collector 默认间隔是 86,400 秒（24 小时）；也可由操作系统调度器每天调用一次 `refresh`。避免同时用多个定时器启动同一批采集任务。

## 信任级别和写入规则

- `quotes`：只接受结构化报价 MCP。启用 target 级价格源时，返回厂商必须匹配 target，报价链接域名必须匹配 target 的 `official_domains`；不通过就不写价格表。
- `model_profiles`：存模型描述、特长、上下文长度、模态、来源、地域、首次/最近观测时间。来自官方报价结构的数据可标为 verified；搜索摘要保留原链接并标为 `needs_review`。
- `market_leads`：新闻、价格页、未识别的新厂商/模型搜索结果。Tavily 的搜索摘要不会直接改写已验证报价。
- `market_checks`：每次按 source、厂商、模型记录 `verified`、`search_only`、`found`、`no_results` 或 `failed`。
- `price_changes`：保留旧价、新价、来源链接和观测时间。

国际报价的币种需要在接入的结构化价格源中先换算成人民币并提供可靠汇率来源；当前核心 `ModelQuote` 只接受 CNY，因此 Tavily 发现的美元价格会留在线索中，不会冒充已完成换算。

## Tavily 接入

本项目用 Tavily Search REST API，不把 WeKnora 插件二进制当成 MCP server。Search API 通过 `Authorization: Bearer` 头接收 API key；本地 Compose 从 `.env` 读 `TAVILY_API_KEY` 并注入 collector。Tavily 的 [Search API 文档](https://docs.tavily.com/documentation/api-reference/endpoint/search)列明 Search endpoint、Bearer 认证、地域域名筛选、时间范围及搜索深度参数。生产应用适合直接 API 接入；团队共享则可使用 Tavily MCP。

项目配置只保存变量名。CLI 启动时从项目根目录 `.env` 读取 `KEY=VALUE`，已有进程环境变量优先。`.env` 已被 Git 忽略；密钥不需要也不应该写入 `sources.json`。

## 查询结果

```powershell
token-mentor targets --scope all
token-mentor models --scope foreign
token-mentor market-leads --scope domestic
token-mentor market-checks --limit 100
token-mentor price-changes --limit 100
```

API 也提供受 Bearer 保护的 `/v1/market/targets`、`/v1/market/models`、`/v1/market/leads`、`/v1/market/checks` 和 `/v1/market/price-changes`。
