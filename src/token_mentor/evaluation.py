from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .analysis import infer_tasks
from .models import ModelQuote
from .ranking import DEFAULT_WEIGHTS, rank_quotes


def run_evaluation(path: str | Path) -> dict[str, Any]:
    cases_path = Path(path).expanduser().resolve()
    try:
        payload = json.loads(cases_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取测评集 {cases_path}: {exc}") from exc
    cases = payload.get("cases") if isinstance(payload, dict) else None
    if not isinstance(cases, list) or not cases:
        raise ValueError("测评集需要非空 cases 数组")
    results = []
    for case in cases:
        if not isinstance(case, dict) or not case.get("id") or not case.get("intent"):
            raise ValueError("每个测评项都需要 id 和 intent")
        if not isinstance(case.get("models", []), list):
            raise ValueError(f"测评项 {case['id']} 的 models 必须是数组")
        if not isinstance(case.get("weights", DEFAULT_WEIGHTS), dict):
            raise ValueError(f"测评项 {case['id']} 的 weights 必须是对象")
        inferred = infer_tasks(str(case["intent"]))
        expected_task = str(case.get("expected_task", ""))
        if expected_task not in {"coding", "reasoning", "translation", "summarization", "rewrite", "general"}:
            raise ValueError(f"测评项 {case['id']} 的 expected_task 无效")
        quotes = [ModelQuote.from_payload(row) for row in case.get("models", [])]
        weights = case.get("weights", DEFAULT_WEIGHTS)
        ranked, omitted = rank_quotes(
            quotes,
            capability=expected_task,
            input_tokens=int(case.get("input_tokens", 4_000)),
            output_tokens=int(case.get("output_tokens", 1_200)),
            weights=weights,
        )
        actual = ranked[0].quote.model if ranked else None
        task_ok = expected_task in inferred
        model_ok = actual == case.get("expected_top_model")
        results.append({
            "id": case["id"],
            "passed": task_ok and model_ok,
            "task_ok": task_ok,
            "model_ok": model_ok,
            "inferred_tasks": list(inferred),
            "expected_task": expected_task,
            "expected_top_model": case.get("expected_top_model"),
            "actual_top_model": actual,
            "omitted_metrics": list(omitted),
        })
    passed = sum(1 for result in results if result["passed"])
    return {"passed": passed, "total": len(results), "pass_rate": passed / len(results), "cases": results}

