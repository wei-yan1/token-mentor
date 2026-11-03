# API 合约

服务通过 `token-mentor api` 启动。API key 从 `TOKEN_MENTOR_API_KEY` 读取，至少 32 个字符；除 `GET /healthz` 外，所有路由要求 `Authorization: Bearer <key>`。

## POST `/v1/recommend`

请求：

```json
{
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

返回最新报价快照或搜索发现列表。`/v1/discoveries?limit=50` 的 limit 会限制在 1–1000。

## HTTP 状态

- `200`：推荐/读取成功。
- `201`：反馈已保存。
- `401`：缺失或无效 Bearer key。
- `404`：反馈报告 ID 不存在或缓存已过期。
- `422`：请求参数或任务值错误。
- `503`：本地报价存储不可用。

API key 是共享实例级凭据，适合 MVP 内部服务；如需租户隔离、用户身份审计、配额/限流，应在生产 API Gateway 或后续身份层实现。服务不记录 key 内容。



## 五节点比较工作流

除原有 `/v1/recommend` 外，服务提供：

- `POST /v1/comparisons`：读取当前报价和 benchmark 快照，执行 `collect → quantify → analyze → recommend → assemble`，返回 `run_id` 和 Markdown。请求包含 `task`、`input_tokens`、`output_tokens`，可选 `benchmark_id`、`benchmark_version`、`protocol`。
- `GET /v1/workflows/{run_id}`：返回状态、当前节点、固定输入快照、检查点和事件。
- `POST /v1/workflows/{run_id}/resume`：从第一个未完成节点恢复，使用原始快照。

所有接口沿用 Bearer 认证。工作流只读当前报价，不替客户项目配置；配置差异、校验、应用和回滚是后续服务能力。
