from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Mapping

from token_mentor.context.analysis import analyze_project
from token_mentor.models import ProjectAnalysis, _token_count
from token_mentor.orchestration.ranking import RankedQuote, normalize_weights, rank_quotes
from token_mentor.orchestration.report import TASK_CAPABILITIES, TASK_TITLES, _md_escape, _md_url, database_recommendations, render_report
from token_mentor.state.storage import Catalog


@dataclass(frozen=True, slots=True)
class RecommendationResult:
    report_id: str
    markdown: str
    cached: bool


class RecommendationService:
    def __init__(self, catalog: Catalog, *, cache_ttl_seconds: int = 86_400, freshness_days: int = 30) -> None:
        if cache_ttl_seconds <= 0 or freshness_days <= 0:
            raise ValueError("cache TTL 和报价新鲜度必须大于 0")
        self.catalog = catalog
        self.cache_ttl_seconds = cache_ttl_seconds
        self.freshness_days = freshness_days

    def recommend_project(
        self,
        project_root: str | Path,
        *,
        intent: str | None = None,
        input_tokens: int = 4_000,
        output_tokens: int = 1_200,
        weights: Mapping[str, float] | None = None,
    ) -> RecommendationResult:
        project = analyze_project(project_root, intent=intent)
        return self.recommend_profile(
            project,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            weights=weights,
        )

    def recommend_profile(
        self,
        project: ProjectAnalysis,
        *,
        input_tokens: int = 4_000,
        output_tokens: int = 1_200,
        weights: Mapping[str, float] | None = None,
    ) -> RecommendationResult:
        input_tokens = _token_count(input_tokens, "input_tokens")
        output_tokens = _token_count(output_tokens, "output_tokens")
        if input_tokens + output_tokens <= 0:
            raise ValueError("估算 token 需至少有一个输入或输出 token")
        configured = normalize_weights(weights)
        quotes = self.catalog.latest_quotes()
        quote_hash = self.catalog.quote_fingerprint(quotes)
        key_material = {
            "fingerprint": project.content_fingerprint,
            "tasks": project.tasks,
            "technologies": project.technologies,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "weights": configured,
            "quote_hash": quote_hash,
            "freshness_days": self.freshness_days,
        }
        cache_key = hashlib.sha256(json.dumps(key_material, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
        report_id = cache_key[:16]
        cached = self.catalog.get_cached_report(cache_key)
        if cached is not None:
            return RecommendationResult(report_id, cached, True)

        per_task: list[dict[str, object]] = []
        omitted_axes: set[str] = set()
        for task in project.tasks:
            capability_options = TASK_CAPABILITIES.get(task, (task, "quality"))
            capability = next(
                (name for name in capability_options if any(name in quote.capabilities for quote in quotes)),
                "quality",
            )
            ranked, omitted = rank_quotes(
                quotes,
                capability=capability,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                weights=configured,
            )
            omitted_axes.update(omitted)
            per_task.append({"task": task, "title": TASK_TITLES.get(task, task), "ranked": ranked})

        freshness = self._freshness_warning(quotes)
        markdown = render_report(
            report_id=report_id,
            project=project,
            tasks=per_task,
            databases=database_recommendations(project),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            weights=configured,
            omitted_metrics=tuple(sorted(omitted_axes)),
            freshness_days=self.freshness_days,
            catalog_count=len(quotes),
        )
        if freshness:
            insertion = "\n".join(f"- {warning}" for warning in freshness)
            markdown = markdown.replace("## 报价可信度\n\n", f"## 报价可信度\n\n{insertion}\n")
        self.catalog.cache_report(cache_key, markdown, ttl_seconds=self.cache_ttl_seconds)
        return RecommendationResult(report_id, markdown, False)

    def _freshness_warning(self, quotes: list) -> list[str]:
        from datetime import UTC, datetime

        now = datetime.now(UTC)
        warnings = []
        for quote in quotes:
            observed = datetime.fromisoformat(quote.observed_at.replace("Z", "+00:00"))
            age = (now - observed.astimezone(UTC)).days
            if age > self.freshness_days:
                warnings.append(
                    f"⚠️ {_md_escape(quote.provider)}/{_md_escape(quote.model)} 报价已 {age} 天未更新 "
                    f"（来源：[查看]({_md_url(quote.source_url)})）。"
                )
        return warnings[:20]


def profile_from_text(intent: str, technologies: list[str] | None = None, tasks: list[str] | None = None) -> ProjectAnalysis:
    from hashlib import sha256

    from token_mentor.context.analysis import infer_tasks

    clean_intent = intent.strip()
    if not clean_intent:
        raise ValueError("intent 不能为空")
    supplied_tasks = tuple(dict.fromkeys(task.strip().lower() for task in (tasks or []) if task.strip()))
    allowed = set(TASK_TITLES)
    unknown = set(supplied_tasks) - allowed
    if unknown:
        raise ValueError(f"不支持的任务类别：{', '.join(sorted(unknown))}")
    final_tasks = supplied_tasks or infer_tasks(clean_intent)
    clean_tech = tuple(dict.fromkeys(item.strip() for item in (technologies or []) if item.strip()))
    return ProjectAnalysis(
        root="用户描述",
        summary=clean_intent,
        evidence=("用户手动输入项目用途",),
        tasks=final_tasks,
        technologies=clean_tech,
        content_fingerprint=sha256(json.dumps({"intent": clean_intent, "tasks": final_tasks, "technologies": clean_tech}, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest(),
    )

