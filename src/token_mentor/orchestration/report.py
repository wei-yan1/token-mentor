from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import quote as url_quote

from token_mentor.models import ProjectAnalysis
from token_mentor.orchestration.ranking import RankedQuote


TASK_TITLES = {
    "coding": "编码与代码分析",
    "reasoning": "规划与复杂推理",
    "translation": "翻译与多语言",
    "summarization": "摘要与报告",
    "rewrite": "改写与文案",
    "embeddings": "文本/多模态向量化",
    "speech_recognition": "语音识别与转写",
    "speech_generation": "语音合成",
    "video_generation": "视频生成",
    "video_understanding": "视频理解",
    "image_generation": "图像生成",
    "multimodal": "多模态与视觉理解",
    "reranking": "检索重排",
    "3d_generation": "3D 内容生成",
    "self_hosted": "本地/私有化部署",
    "general": "通用问答",
}

TASK_CAPABILITIES = {
    "coding": ("coding", "code"),
    "reasoning": ("reasoning", "analysis", "agent"),
    "translation": ("translation", "language"),
    "summarization": ("summarization", "summary"),
    "rewrite": ("writing", "rewrite"),
    "general": ("general", "quality"),
    "embeddings": ("embeddings", "text_embedding", "multimodal_embedding"),
    "speech_recognition": ("speech_recognition", "asr"),
    "speech_generation": ("speech_generation", "tts"),
    "video_generation": ("video_generation",),
    "video_understanding": ("video_understanding", "multimodal"),
    "image_generation": ("image_generation",),
    "multimodal": ("multimodal", "visual_understanding"),
    "reranking": ("reranking",),
    "3d_generation": ("3d_generation",),
    "self_hosted": ("self_hosted",),
}


def capabilities_for_task(task: str) -> tuple[str, ...]:
    return TASK_CAPABILITIES.get(task, (task, "quality"))


def render_report(
    *,
    report_id: str,
    project: ProjectAnalysis,
    tasks: list[dict[str, object]],
    databases: list[dict[str, str]],
    input_tokens: int,
    output_tokens: int,
    weights: dict[str, float],
    omitted_metrics: tuple[str, ...],
    freshness_days: int,
    catalog_count: int,
) -> str:
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "# Token 军师｜项目模型与基础设施推荐",
        "",
        f"> 报告 ID：`{report_id}` · 生成时间：{now} · 报价目录：{catalog_count} 个模型",
        "",
        "## 项目画像",
        "",
        f"- **用途**：{_md_escape(project.summary)}",
        f"- **推荐任务**：{', '.join(TASK_TITLES.get(task, task) for task in project.tasks)}",
        f"- **技术线索**：{_md_escape(', '.join(project.technologies)) if project.technologies else '未识别；可使用 --intent 补充'}",
        f"- **依据**：{'；'.join(project.evidence)}",
        "",
        "## 模型推荐",
        "",
        f"成本估算使用每次输入 {input_tokens:,}、输出 {output_tokens:,} token。当前权重：成本 {weights.get('cost', 0):g} / 能力 {weights.get('quality', 0):g} / 稳定性 {weights.get('stability', 0):g} / 速度 {weights.get('speed', 0):g}。",
        "候选模型的各项分数列在表中，综合分按可用维度和权重计算；某维度缺数据时会省略该维度并重新归一权重。所有请求的评分指标缺失时退回按估算成本排序。",
        "",
    ]
    if omitted_metrics:
        display = {"quality": "能力/质量", "stability": "稳定性", "speed": "速度"}
        lines.append(
            "部分维度没有完整、可比较的数据，本次未计入："
            + "、".join(display.get(axis, axis) for axis in omitted_metrics)
            + "。"
        )
        lines.append("")
    if not tasks:
        lines.extend(["当前报价库为空，尚不能计算模型排序。先配置 Tavily 搜索和官方定价页、价格 MCP，或运行 `import-prices` 导入规范报价。", ""])
    fx_evidence: dict[tuple[str, str], tuple[str, str, float]] = {}
    for task in tasks:
        title = str(task["title"])
        ranked = task["ranked"]
        lines.extend([f"### {title}", ""])
        if task.get("lane_note"):
            lines.extend([str(task["lane_note"]), ""])
        if not isinstance(ranked, list) or not ranked:
            lines.extend(["暂无可用报价。", ""])
            continue
        for candidate in ranked:
            if isinstance(candidate, RankedQuote) and candidate.quote.source_currency != "CNY" and candidate.quote.fx_source_url:
                quote = candidate.quote
                fx_evidence[(quote.source_currency, quote.fx_observed_at or "")] = (
                    quote.source_currency, quote.fx_source_url, quote.fx_rate_to_cny)
        lines.append("| 排名 | 模型 | 估算单次成本 | 综合分 | 成本/能力/稳定/速度 | 输入 / 输出单价（元/百万 token） | 报价时间 | 来源 |")
        lines.append("|---:|---|---:|---:|---|---:|---|---|")
        for index, item in enumerate(ranked[:5], start=1):
            assert isinstance(item, RankedQuote)
            q = item.quote
            source = f"[{_md_escape(q.provider)}]({_md_url(q.source_url)})"
            axes = " / ".join(
                f"{item.axis_scores[axis]:.2f}" if axis in item.axis_scores else "—"
                for axis in ("cost", "quality", "stability", "speed")
            )
            lines.append(
                f"| {index} | {_md_escape(q.model)} | ¥{item.estimated_cost_cny:.6f} | {item.score:.3f} | {axes} "
                f"| ¥{q.input_price_per_million:g} / ¥{q.output_price_per_million:g} "
                f"| {q.observed_at} | {source} |"
            )
        best = ranked[0]
        assert isinstance(best, RankedQuote)
        lines.append("")
        lines.append(
            f"**首选：{_md_escape(best.quote.provider)} / {_md_escape(best.quote.model)}。** "
            f"它在当前权重下综合分最高（{best.score:.3f}），估算费用约 ¥{best.estimated_cost_cny:.6f}。"
            + "表中成本分是相对候选模型的反向成本归一化，能力/稳定分使用导入值，速度分按候选中最快延迟归一化；‘—’表示缺少可比较数据。"
        )
        lines.append("")

    lines.extend(["## 数据库与缓存建议", ""])
    for recommendation in databases:
        lines.append(f"- **{recommendation['name']}**：{recommendation['reason']}")
    lines.extend([
        "",
        "## 报价可信度",
        "",
        f"- 每条报价记录保留来源 URL、抓取时间、输入/输出价格和计价单位。超过 {freshness_days} 天的报价会标为过期，仍保留以供核对。",
        "- 排序和成本使用人民币/百万 token 标准价；外币原价、折算率、汇率日期和汇率来源随报价保存。成本估算不含缓存折扣、上下文溢价、批量折扣、网络/部署和税费。",
        *[f"- {currency}/CNY 汇率 {rate:g}（{rate_date[:10] or '日期未提供'}），来源：[Frankfurter](<{_md_url(url)}>)。"
          for (_, rate_date), (currency, url, rate) in sorted(fx_evidence.items())],
        "- 能力、稳定性和延迟采用数据源提供的值；导入前应标明它们来自厂商资料还是自有测评，框架不会自动认定为自有实测结论。",
        "- 速度和稳定性应由自有测评集持续压测后导入；厂商宣传材料不自动视为实测结论。",
        "",
        "## 推荐依据与限制",
        "",
        "- 项目用途优先来自用户输入，其次来自 README/Markdown 和有限源码线索；自动分析只生成候选任务，建议先核对画像。",
        "- 排序使用本次报价库中的相对分数，不代表跨供应商的绝对能力结论；缺少的数据维度会明确省略。",
        "- 当前数据库建议是基于项目线索的启发式建议，需结合并发、数据量、部署和一致性要求复核。",
        "",
        "## 反馈与测评",
        "",
        f"可用 `token-mentor feedback {report_id} --rating 1..5 --note \"原因\"` 记录效果；反馈会进入本地评估记录，供人工更新权重/规则。",
        "",
    ])
    return "\n".join(lines)


def _md_escape(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("\r", " ")
        .replace("\n", " ")
        .replace("|", "\\|")
        .replace("`", "\\`")
        .replace("[", "\\[")
        .replace("]", "\\]")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _md_url(value: str) -> str:
    return url_quote(value, safe=":/?#[]@!$&'*+,;=%")


def database_recommendations(project: ProjectAnalysis) -> list[dict[str, str]]:
    tech = {item.lower() for item in project.technologies}
    tasks = set(project.tasks)
    if "postgresql" in tech:
        primary = {
            "name": "PostgreSQL",
            "reason": "项目已使用 PostgreSQL；保留现有数据库通常比迁移到新产品更稳妥。优先确认连接池、备份恢复和索引策略。",
        }
    elif "sqlite" in tech:
        primary = {
            "name": "SQLite（轻量部署）/ PostgreSQL（多用户服务）",
            "reason": "检测到 SQLite。单机原型、嵌入式和低并发场景可继续使用；若要多实例写入或正式多租户服务，建议切到托管 PostgreSQL。",
        }
    else:
        primary = {
            "name": "PostgreSQL",
            "reason": "在需求尚不明确时，托管 PostgreSQL 是通用的事务型默认选择；提供关系约束、JSON 字段与成熟备份工具，先按实际并发和可用性要求选实例规格。",
        }
    recommendations = [primary]
    if "redis" in tech:
        recommendations.append({
            "name": "Redis（已检测到）",
            "reason": "保留现有 Redis 用于短期缓存/队列即可；不要把唯一持久数据放入缓存。",
        })
    elif tasks.intersection({"general", "reasoning", "coding"}):
        recommendations.append({
            "name": "Redis（按需）",
            "reason": "模型推荐结果、限流或异步任务规模达到需要共享高速缓存时再加；单实例早期可先使用本框架的 SQLite 结果缓存。",
        })
    return recommendations

