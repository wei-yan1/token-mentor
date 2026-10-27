# 系统架构

## 数据流

```text
官方价目 / MCP 报价工具 ──> 价格快照校验 ──┐
Tavily / MCP 搜索工具 ───> 新模型发现队列 ──┼─> SQLite catalog ─> task ranker ─> Markdown / API
README / 源码 / 用户用途 ─> Project Advisor ─┘                  └─> cache + feedback
                                                                  └─> offline eval gate
```

搜索线索和价格快照是分开的数据类型。新模型先记录标题、URL、摘要和发现时间；只有获得标准报价且来源经核验后才进入候选排名。

## 包结构

| 模块 | 责任 |
|---|---|
| `analysis.py` | 本地项目文件白名单扫描、用途/任务/技术线索识别；限制扫描深度与文件数 |
| `agents.py` | 定时市场研究、项目选型、离线测评三个可组合的 agent 对象 |
| `sources/mcp.py` | MCP Streamable HTTP JSON-RPC 客户端，价格/搜索工具结果解析 |
| `models.py` | 价格与项目画像类型、输入校验和单次费用计算 |
| `storage.py` | SQLite WAL、报价快照、共享报告缓存、反馈和发现线索 |
| `ranking.py` | 可配置权重，成本/能力/稳定/延迟评分与缺项处理 |
| `service.py` | 画像、价格快照、权重的缓存键与报告生成编排 |
| `report.py` | Markdown 推荐、引用源、数据库建议和数据限制 |
| `api.py` | 可选 FastAPI service；Bearer API key 保护业务接口 |
| `evaluation.py` | 自建 JSON 小测评集的任务分类与排序回归 |

## 缓存与一致性

价格按 `(provider, model, source_url, observed_at)` 幂等 upsert。报告键为项目画像 fingerprint、任务、技术、token 估算、评分权重、报价快照 fingerprint、新鲜度窗口的 SHA-256。任一输入变化都会自然形成新的缓存键；旧报告按 TTL 过期。反馈按报告缓存 ID 关联。

SQLite WAL 适合单机部署；连接逐次打开/提交/关闭，设置 busy timeout 与外键。单机共享 Docker volume 可供 API 和采集进程复用；多个机器写同一 SQLite 文件不是支持的高可用模式。

## 关键取舍

- 不调用外部 LLM 来读项目源码；第一版推断透明、可离线运行，也减少源码离开本机的风险。
- 能力分数、稳定率、延迟由数据源提供，报告要求运营方标明来源属性；系统不把厂商宣传标成自测。
- HTTP 服务不接受用户指定服务端路径，只接收用户显式提交的项目用途和标签。
- MCP 连接器支持 Streamable HTTP；stdio MCP server 和其他传输可通过适配器后续扩展。

