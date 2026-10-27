# 价格数据与来源合同

## 规范报价对象

最小字段：

| 字段 | 规则 |
|---|---|
| `provider`、`model` | 非空名称/厂商 ID |
| `input_price_per_million` | CNY / 1,000,000 输入 token，有限且非负 |
| `output_price_per_million` | CNY / 1,000,000 输出 token，有限且非负 |
| `currency` | 当前版本只接收 CNY |
| `price_unit` | 包含 `1M`、`million` 或 `百万` 的单位 |
| `source_url` | HTTP(S) 公共页面；不能含 userinfo 或 token/api_key 等认证 query |
| `observed_at` | 带时区 ISO-8601 时间，存储前转换到 UTC |
| `capabilities` | 可选对象，能力名到 0–1 或 0–100 的数值 |
| `stability` | 可选 0–100；需注明厂商数据或自测语义 |
| `latency_ms` | 可选正数，毫秒；测评协议应一致 |

价格快照示例见 [README](../README.md#报价数据格式)。输入/输出分别计价。成本估算公式：

`(input_tokens × input_price_per_million + output_tokens × output_price_per_million) / 1,000,000`

不自动折算缓存 token、百万 token 以外的阶梯价格、免费配额、批量折扣、上下文窗口溢价、部署费用或税费。导入前需把报价转换到可比较的有效单价，且保留可公开复核的原始链接。

## MCP 响应

价格 MCP 必须返回结构化 JSON。可直接返回对象数组，或在 `quotes`、`models`、`results`、`data` 字段内返回数组。每个模型至少要满足规范报价对象必填字段。结果若只有自然语言网页摘要，采集器拒绝写入报价库。

Discovery MCP 可返回结构化 `results` 数组，也可返回含标题和 URL 的文本。发现记录仅用于人工核价，不能进入模型排行榜。

## 校验与保留

- MCP 远程 endpoint 用 HTTPS，token 通过配置的 HTTP 认证头环境变量传递；禁止把凭据放进 URL。
- 单个报价记录不超过响应字节上限；超时、源错误、结构无效都记为该 source 失败，不影响成功源。
- 同厂商同模型读取最新的观测快照；历史价格保留 90 天，超过推荐新鲜度阈值的价格会警示。
- 所有价格修改都带观测时间与来源链接，报告缓存随报价快照 fingerprint 自动失效。

