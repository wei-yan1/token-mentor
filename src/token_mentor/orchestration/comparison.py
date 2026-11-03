"""Auditable five-stage model comparison and scenario recommendation engine."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, Iterable, Mapping
from hashlib import sha256

from token_mentor.constraints.recovery import MAX_NODE_ATTEMPTS, exclusive_run

from token_mentor.constraints.scoring import performance_tier
from token_mentor.models import BenchmarkEvidence, ModelQuote, _token_count
from token_mentor.state.storage import Catalog


@dataclass(frozen=True, slots=True)
class ComparisonRequest:
    task: str
    input_tokens: int
    output_tokens: int
    benchmark_id: str | None = None
    benchmark_version: str | None = None
    protocol: str = ""

    def __post_init__(self) -> None:
        if not self.task.strip():
            raise ValueError("comparison task 不能为空")
        _token_count(self.input_tokens, "input_tokens")
        _token_count(self.output_tokens, "output_tokens")
        if self.input_tokens + self.output_tokens <= 0:
            raise ValueError("input_tokens 和 output_tokens 不能同时为 0")
        if bool(self.benchmark_id) != bool(self.benchmark_version):
            raise ValueError("benchmark_id 和 benchmark_version 必须同时提供")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CandidateMetric:
    provider: str
    model: str
    request_cost_cny: float
    blended_cost_per_million: float
    benchmark_score: float | None
    relative_performance: float | None
    value_index: float | None
    tier: str
    comparable: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _find_evidence(quote: ModelQuote, evidence: Iterable[BenchmarkEvidence], request: ComparisonRequest) -> BenchmarkEvidence | None:
    rows = [x for x in evidence if x.provider == quote.provider and x.model == quote.model and x.task == request.task]
    if request.benchmark_id:
        rows = [x for x in rows if x.benchmark_id == request.benchmark_id and x.benchmark_version == request.benchmark_version and x.protocol == request.protocol]
    elif rows:
        # Without an explicit benchmark selector, use only one unambiguous cohort.
        cohorts = {(x.benchmark_id, x.benchmark_version, x.protocol) for x in rows}
        if len(cohorts) != 1:
            return None
    if not rows:
        return None
    return max(rows, key=lambda x: x.observed_at)


def quantify(quotes: Iterable[ModelQuote], evidence: Iterable[BenchmarkEvidence], request: ComparisonRequest) -> list[CandidateMetric]:
    quotes = list(quotes)
    evidence = list(evidence)
    if not quotes:
        return []
    input_tokens = _token_count(request.input_tokens, "input_tokens")
    output_tokens = _token_count(request.output_tokens, "output_tokens")
    total = input_tokens + output_tokens
    found = {id(q): _find_evidence(q, evidence, request) for q in quotes}
    eligible = [x for x in found.values() if x is not None]
    # A relative score is only meaningful when all evidence belongs to one exact cohort.
    cohort = {(x.benchmark_id, x.benchmark_version, x.task, x.protocol) for x in eligible}
    if len(cohort) != 1:
        found = {key: None for key in found}
        eligible = []
    baseline = max((x.normalized_score for x in eligible), default=None)
    rows: list[CandidateMetric] = []
    for quote in quotes:
        ev = found[id(quote)]
        request_cost = quote.estimated_cost(input_tokens, output_tokens)
        blended = request_cost * 1_000_000 / total
        relative = None if ev is None or baseline in (None, 0) else ev.normalized_score / baseline * 100
        value = None if relative is None or blended == 0 else relative / blended
        if ev is None:
            tier, reason = "未评级", "缺少同一任务、同一 benchmark 版本和协议的可比证据"
        else:
            tier, reason = performance_tier(relative or 0), "同一 benchmark cohort，可进行相对性能比较"
        rows.append(CandidateMetric(quote.provider, quote.model, request_cost, blended,
                                    None if ev is None else ev.score, relative, value, tier, ev is not None, reason))
    return rows


def analyze(metrics: Iterable[CandidateMetric]) -> dict[str, Any]:
    rows = list(metrics)
    comparable = [x for x in rows if x.comparable and x.relative_performance is not None]
    by_perf = sorted(comparable, key=lambda x: (-x.relative_performance, x.request_cost_cny, x.model))
    by_value = sorted(comparable, key=lambda x: (0 if x.request_cost_cny == 0 else 1, -(x.value_index or 0), x.request_cost_cny, x.model))
    dominated: list[dict[str, str]] = []
    for candidate in comparable:
        for other in comparable:
            if other.model == candidate.model and other.provider == candidate.provider:
                continue
            if other.relative_performance > candidate.relative_performance and other.request_cost_cny < candidate.request_cost_cny:
                dominated.append({"provider": candidate.provider, "model": candidate.model,
                                  "by_provider": other.provider, "by_model": other.model,
                                  "reason": "性能更高且按当前 token 比例请求成本更低"})
                break
    return {"performance": [x.to_dict() for x in by_perf], "value": [x.to_dict() for x in by_value],
            "dominated": dominated, "unrated": [x.to_dict() for x in rows if not x.comparable]}


def recommend(metrics: Iterable[CandidateMetric], analysis: Mapping[str, Any]) -> dict[str, Any]:
    rows = list(metrics)
    excluded = {(x["provider"], x["model"]) for x in analysis.get("dominated", [])}
    eligible = [x for x in rows if x.comparable and x.relative_performance is not None and (x.provider, x.model) not in excluded]
    def best(predicate, key):
        options = [x for x in eligible if predicate(x)]
        return min(options, key=key).to_dict() if options else None
    scenarios = {
        "极致性能": {"primary": best(lambda x: True, lambda x: (-x.relative_performance, x.request_cost_cny, x.model)),
                     "backup": best(lambda x: True, lambda x: (x.request_cost_cny, -x.relative_performance, x.model))},
        "日常主力": {"primary": best(lambda x: x.tier in ("旗舰", "主力"), lambda x: (0 if x.request_cost_cny == 0 else 1, -float(x.value_index or 0), x.request_cost_cny, x.model)),
                     "backup": best(lambda x: x.tier in ("旗舰", "主力"), lambda x: (x.request_cost_cny, x.model))},
        "批量轻量": {"primary": best(lambda x: x.tier == "轻量", lambda x: (x.request_cost_cny, x.model)),
                     "backup": best(lambda x: True, lambda x: (x.request_cost_cny, x.model))},
        "预算敏感": {"primary": best(lambda x: True, lambda x: (x.request_cost_cny, -x.relative_performance, x.model)),
                     "backup": best(lambda x: True, lambda x: (0 if x.request_cost_cny == 0 else 1, -float(x.value_index or 0), x.request_cost_cny, x.model))},
    }
    for choices in scenarios.values():
        if choices["primary"] == choices["backup"]:
            alternatives = [x for x in eligible if x.to_dict() != choices["primary"]]
            choices["backup"] = min(alternatives, key=lambda x: (x.request_cost_cny, x.model)).to_dict() if alternatives else None
    return {"scenarios": scenarios, "dominated": analysis.get("dominated", []),
            "notes": ["推荐只在同一 benchmark cohort 内比较；未评级模型不会被强行排序。", "价格按输入/输出 token 数量计算，改变比例会改变成本和性价比。"]}


NODE_ORDER = ("collect", "quantify", "analyze", "recommend", "assemble")


class ComparisonWorkflow:
    """Durable, idempotent five-node workflow backed by Catalog."""

    def __init__(self, catalog: Catalog) -> None:
        self.catalog = catalog

    def create(self, run_id: str, request: ComparisonRequest, quotes: Iterable[ModelQuote], evidence: Iterable[BenchmarkEvidence]) -> str:
        payload = {"request": request.to_dict(), "quotes": [x.to_dict() for x in quotes], "evidence": [x.to_dict() for x in evidence]}
        self.catalog.create_workflow_run(run_id, payload)
        self.catalog.set_workflow_snapshot(run_id, payload)
        return run_id

    def run(self, run_id: str, *, fail_node: str | None = None) -> dict[str, Any]:
        lock_name = sha256(run_id.encode("utf-8")).hexdigest() + ".lock"
        with exclusive_run(self.catalog.path.parent / (self.catalog.path.name + ".locks") / lock_name):
            return self._run(run_id, fail_node=fail_node)

    def _run(self, run_id: str, *, fail_node: str | None = None) -> dict[str, Any]:
        state = self.catalog.get_workflow_run(run_id)
        if state is None: raise ValueError("workflow run 不存在")
        if state["status"] == "completed": return state
        snapshot = state["snapshot_json"] or state["input_json"]
        request = ComparisonRequest(**snapshot["request"])
        quotes = [ModelQuote.from_dict(x) for x in snapshot["quotes"]]
        evidence = [BenchmarkEvidence.from_dict(x) for x in snapshot["evidence"]]
        done = {x["node"]: x["output"] for x in state["checkpoints"]}
        pending_node = next((x for x in NODE_ORDER if x not in done), NODE_ORDER[-1])
        attempts = sum(1 for event in state["events"] if event["node"] == pending_node and event["type"] == "started")
        if attempts >= MAX_NODE_ATTEMPTS:
            raise RuntimeError(f"节点 {pending_node} 已达到 {MAX_NODE_ATTEMPTS} 次执行上限（含首次），请修复数据后创建新工作流")
        try:
            for node in NODE_ORDER:
                if node in done: continue
                self.catalog.start_workflow_node(run_id, node)
                if fail_node == node: raise RuntimeError(f"测试注入失败：{node}")
                if node == "collect": output = {"quote_count": len(quotes), "evidence_count": len(evidence), "snapshot_at": datetime.now(UTC).isoformat()}
                elif node == "quantify": output = {"metrics": [x.to_dict() for x in quantify(quotes, evidence, request)]}
                elif node == "analyze": output = analyze([CandidateMetric(**x) for x in done["quantify"]["metrics"]])
                elif node == "recommend": output = recommend([CandidateMetric(**x) for x in done["quantify"]["metrics"]], done["analyze"])
                else:
                    analysis_data = done["analyze"]
                    recommendation_data = done["recommend"]
                    lines = ["# 模型对比推荐", "", f"- 任务：{request.task}",
                             f"- Token 假设：输入 {request.input_tokens:,} / 输出 {request.output_tokens:,}", "",
                             "## 性能排名", "", "| 厂商 | 模型 | 相对性能 | 档位 | 单次估算成本 (CNY) |", "|---|---|---:|---|---:|"]
                    for item in analysis_data["performance"]:
                        lines.append(f"| {item['provider']} | {item['model']} | {item['relative_performance']:.1f} | {item['tier']} | {item['request_cost_cny']:.6f} |")
                    lines.extend(["", "## 性价比排序", "", "| 排名 | 厂商 | 模型 | 性能/每百万 token 混合价 | 单次成本 (CNY) |", "|---:|---|---|---:|---:|"])
                    for index, item in enumerate(analysis_data["value"], 1):
                        value = "免费" if item["request_cost_cny"] == 0 else f"{item['value_index']:.4f}"
                        lines.append(f"| {index} | {item['provider']} | {item['model']} | {value} | {item['request_cost_cny']:.6f} |")
                    lines.extend(["", "## 场景建议", ""])
                    for scenario, choices in recommendation_data["scenarios"].items():
                        primary = choices["primary"]
                        lines.append(f"- **{scenario}**：" + (f"{primary['provider']} / {primary['model']}" if primary else "暂无具备可比 benchmark 的候选模型"))
                    lines.extend(["", "## 被支配候选", ""])
                    lines.extend([f"- {x['provider']} / {x['model']}：被 {x['by_provider']} / {x['by_model']} 严格支配" for x in analysis_data["dominated"]] or ["- 未发现严格支配关系。"])
                    if analysis_data["unrated"]:
                        lines.extend(["", "## 暂不评级", "", "存在未评级报价；它们没有与本次选择同任务、同版本、同协议的 benchmark 证据，故不混入性能排名。"])
                        lines.extend([f"- {x['provider']} / {x['model']}：{x['reason']}" for x in analysis_data["unrated"]])
                    lines.extend(["", "## 说明", "", "相对性能以本次可比候选的最高分为 100；成本按给定输入/输出 token 数计算。benchmark 只用于统一 cohort 内横比，样例与厂商定位均不当成实测。", ""])
                    output = {"run_id": run_id, "request": request.to_dict(), "analysis": analysis_data, "recommendation": recommendation_data, "markdown": "\n".join(lines), "generated_at": datetime.now(UTC).isoformat()}
                next_node = NODE_ORDER[NODE_ORDER.index(node)+1] if node != NODE_ORDER[-1] else "done"
                self.catalog.checkpoint_workflow_node(run_id, node, output, next_node, final=node == NODE_ORDER[-1])
                done[node] = output
            return self.catalog.get_workflow_run(run_id) or {}
        except Exception as exc:
            node = next((x for x in NODE_ORDER if x not in done), NODE_ORDER[-1])
            self.catalog.fail_workflow_node(run_id, node, str(exc))
            raise

    def status(self, run_id: str) -> dict[str, Any]:
        result = self.catalog.get_workflow_run(run_id)
        if result is None: raise ValueError("workflow run 不存在")
        return result

    def resume(self, run_id: str) -> dict[str, Any]:
        return self.run(run_id)
