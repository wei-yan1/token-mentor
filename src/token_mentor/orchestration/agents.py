from __future__ import annotations

from collections.abc import Callable, Mapping
import threading
from pathlib import Path
from typing import Any

from token_mentor.orchestration.collector import collect_once
from token_mentor.observability.evaluation import run_evaluation
from token_mentor.models import ProjectAnalysis, ResearchTarget
from token_mentor.orchestration.service import RecommendationResult, RecommendationService, profile_from_text
from token_mentor.tools.mcp import McpSourceConfig
from token_mentor.state.storage import Catalog


class MarketResearchAgent:
    """Scheduled, auditable MCP market-data collection agent."""

    def __init__(self, catalog: Catalog, sources: list[McpSourceConfig], targets: list[ResearchTarget] | None = None) -> None:
        self.catalog = catalog
        self.sources = tuple(sources)
        self.targets = tuple(targets or ())

    def run_once(self, *, scope: str = "all") -> dict[str, Any]:
        return collect_once(self.catalog, list(self.sources), scope=scope, targets=list(self.targets))

    def run_forever(
        self,
        *,
        interval_seconds: int = 86_400,
        scope: str = "all",
        stop_event: threading.Event | None = None,
        on_result: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        if interval_seconds < 30000:
            raise ValueError("定时采集间隔至少为 30000 秒")
        stopper = stop_event or threading.Event()
        while not stopper.is_set():
            result = self.run_once(scope=scope)
            if on_result is not None:
                on_result(result)
            if stopper.wait(interval_seconds):
                break


class ProjectAdvisorAgent:
    """Deterministic task-to-model/database advisor backed by auditable pricing data."""

    def __init__(self, service: RecommendationService) -> None:
        self.service = service

    def from_repository(
        self,
        project_root: str | Path,
        *,
        intent: str | None = None,
        input_tokens: int = 4_000,
        output_tokens: int = 1_200,
        weights: Mapping[str, float] | None = None,
    ) -> RecommendationResult:
        return self.service.recommend_project(
            project_root,
            intent=intent,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            weights=weights,
        )

    def from_intent(
        self,
        intent: str,
        *,
        technologies: list[str] | None = None,
        tasks: list[str] | None = None,
        input_tokens: int = 4_000,
        output_tokens: int = 1_200,
        weights: Mapping[str, float] | None = None,
    ) -> RecommendationResult:
        profile: ProjectAnalysis = profile_from_text(intent, technologies, tasks)
        return self.service.recommend_profile(
            profile,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            weights=weights,
        )


class EvaluationAgent:
    """Offline gate for human-curated cases and the current recommendation policy."""

    def run(self, cases_path: str | Path) -> dict[str, Any]:
        return run_evaluation(cases_path)

