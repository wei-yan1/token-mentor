from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from token_mentor.models import BenchmarkEvidence, ModelQuote
from token_mentor.orchestration.comparison import ComparisonRequest, ComparisonWorkflow, quantify
from token_mentor.state.storage import Catalog


def make_quote(model: str, input_price: float, output_price: float) -> ModelQuote:
    return ModelQuote.from_payload({
        "provider": "Fixture", "model": model,
        "input_price_per_million": input_price, "output_price_per_million": output_price,
        "price_unit": "CNY/1M tokens", "source_url": f"https://example.invalid/{model}",
        "observed_at": "2026-11-03T00:00:00Z",
    })


def evidence(model: str, score: float, version: str = "1") -> BenchmarkEvidence:
    return BenchmarkEvidence("Fixture", model, "coding-suite", version, "coding", score, 100,
                             f"https://example.invalid/{model}/bench", "2026-11-03T00:00:00Z", "public-protocol")


class ComparisonTests(unittest.TestCase):
    def test_cost_uses_requested_input_output_mix(self):
        a, b = make_quote("a", 0.1, 20), make_quote("b", 5, 5)
        ev = [evidence("a", 80), evidence("b", 90)]
        input_heavy = ComparisonRequest("coding", 900, 100, "coding-suite", "1", "public-protocol")
        output_heavy = ComparisonRequest("coding", 100, 900, "coding-suite", "1", "public-protocol")
        first = {x.model: x for x in quantify([a, b], ev, input_heavy)}
        second = {x.model: x for x in quantify([a, b], ev, output_heavy)}
        self.assertLess(first["a"].request_cost_cny, first["b"].request_cost_cny)
        self.assertGreater(second["a"].request_cost_cny, second["b"].request_cost_cny)
        self.assertEqual(max(x.relative_performance for x in first.values()), 100)

    def test_dominated_candidate_and_recommendation_are_reported(self):
        from token_mentor.orchestration.comparison import analyze, recommend
        quotes = [make_quote("better-cheaper", 1, 1), make_quote("worse-costlier", 4, 4), make_quote("tradeoff", 0.2, 0.2)]
        ev = [evidence("better-cheaper", 90), evidence("worse-costlier", 70), evidence("tradeoff", 60)]
        metrics = quantify(quotes, ev, ComparisonRequest("coding", 100, 100, "coding-suite", "1", "public-protocol"))
        result = analyze(metrics)
        self.assertEqual([(x["model"], x["by_model"]) for x in result["dominated"]], [("worse-costlier", "better-cheaper")])
        scenarios = recommend(metrics, result)["scenarios"]
        self.assertNotEqual(scenarios["极致性能"]["primary"]["model"], "worse-costlier")
        self.assertNotEqual(scenarios["极致性能"]["primary"], scenarios["极致性能"]["backup"])

    def test_unmatched_benchmark_version_is_not_ranked(self):
        request = ComparisonRequest("coding", 100, 100, "coding-suite", "2", "public-protocol")
        rows = quantify([make_quote("a", 1, 1)], [evidence("a", 99, "1")], request)
        self.assertFalse(rows[0].comparable)
        self.assertIsNone(rows[0].relative_performance)

    def test_failure_resumes_from_persisted_checkpoints_and_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            catalog = Catalog(Path(directory) / "catalog.sqlite3")
            quote = make_quote("a", 1, 1)
            ev = evidence("a", 88)
            catalog.upsert_quotes([quote])
            catalog.upsert_benchmarks([ev])
            workflow = ComparisonWorkflow(catalog)
            request = ComparisonRequest("coding", 1000, 300, "coding-suite", "1", "public-protocol")
            workflow.create("run-1", request, catalog.latest_quotes(), catalog.benchmark_snapshot())
            with self.assertRaisesRegex(RuntimeError, "analyze"):
                workflow.run("run-1", fail_node="analyze")
            failed = workflow.status("run-1")
            self.assertEqual(failed["status"], "failed")
            self.assertEqual([x["node"] for x in failed["checkpoints"]], ["collect", "quantify"])
            catalog.upsert_quotes([make_quote("a", 100, 100)])
            complete = workflow.resume("run-1")
            self.assertEqual(complete["status"], "completed")
            self.assertEqual([x["node"] for x in complete["checkpoints"]], ["collect", "quantify", "analyze", "recommend", "assemble"])
            costs = complete["checkpoints"][1]["output"]["metrics"][0]["request_cost_cny"]
            self.assertAlmostEqual(costs, quote.estimated_cost(1000, 300))
            self.assertEqual(sum(1 for e in complete["events"] if e["type"] == "completed" and e["node"] == "collect"), 1)
            self.assertIn("# 模型对比推荐", complete["result_json"]["markdown"])

    def test_retry_limit_is_enforced_including_crashed_or_failed_attempts(self):
        with tempfile.TemporaryDirectory() as directory:
            catalog = Catalog(Path(directory) / "catalog.sqlite3")
            workflow = ComparisonWorkflow(catalog)
            workflow.create("run-limit", ComparisonRequest("coding", 1, 1), [make_quote("a", 1, 1)], [])
            for _ in range(4):
                with self.assertRaises(RuntimeError):
                    workflow.run("run-limit", fail_node="collect")
            with self.assertRaisesRegex(RuntimeError, "执行上限"):
                workflow.resume("run-limit")
            state = workflow.status("run-limit")
            self.assertEqual(sum(1 for x in state["events"] if x["type"] == "failed"), 4)
            self.assertEqual(sum(1 for x in state["events"] if x["type"] == "started" and x["node"] == "collect"), 4)


if __name__ == "__main__":
    unittest.main()
