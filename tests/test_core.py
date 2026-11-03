from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from importlib.util import find_spec

from token_mentor.analysis import analyze_project
from token_mentor.collector import collect_once, load_source_configs
from token_mentor.cli import main as cli_main
from token_mentor.models import ModelQuote
from token_mentor.ranking import rank_quotes
from token_mentor.service import RecommendationService
from token_mentor.sources.mcp import McpSourceConfig, _extract_discovery_rows
from token_mentor.storage import Catalog


def quote(model: str, *, price: float = 1.0, observed_at: str | None = None, **kwargs: object) -> ModelQuote:
    return ModelQuote.from_payload({
        "provider": "Fixture",
        "model": model,
        "input_price_per_million": price,
        "output_price_per_million": price * 2,
        "price_unit": "CNY/1M tokens",
        "source_url": f"https://example.invalid/{model}",
        "observed_at": observed_at or "2026-10-27T00:00:00Z",
        **kwargs,
    })


class QuoteValidationTests(unittest.TestCase):
    def test_normalizes_timezone_and_calculates_estimated_cost(self) -> None:
        item = quote("m", observed_at="2026-10-27T08:00:00+08:00")
        self.assertEqual(item.observed_at, "2026-10-27T00:00:00Z")
        self.assertAlmostEqual(item.estimated_cost(1_000_000, 500_000), 2.0)

    def test_rejects_non_cny_and_non_million_units(self) -> None:
        with self.assertRaisesRegex(ValueError, "人民币"):
            quote("m", currency="USD")
        with self.assertRaisesRegex(ValueError, "百万 token"):
            ModelQuote.from_payload({
                "provider": "Fixture", "model": "m", "input_price": 1, "output_price": 2,
                "price_unit": "CNY/1K tokens", "source_url": "https://example.invalid/m",
                "observed_at": "2026-10-27T00:00:00Z",
            })

    def test_rejects_fractional_token_counts_and_nonfinite_weights(self) -> None:
        with self.assertRaisesRegex(ValueError, "整数 token"):
            quote("m").estimated_cost(1.5, 20)
        with self.assertRaisesRegex(ValueError, "有限"):
            rank_quotes(
                [quote("m")], capability="quality", input_tokens=1, output_tokens=1,
                weights={"cost": float("nan")},
            )


class RankingTests(unittest.TestCase):
    def test_omits_quality_when_not_all_quotes_have_it(self) -> None:
        quotes = [quote("a", price=1, capabilities={"coding": 0.9}), quote("b", price=2)]
        ranked, omitted = rank_quotes(
            quotes, capability="coding", input_tokens=100, output_tokens=100,
            weights={"cost": 0.5, "quality": 0.5, "stability": 0, "speed": 0},
        )
        self.assertEqual(ranked[0].quote.model, "a")
        self.assertIn("quality", omitted)
        self.assertNotIn("quality", ranked[0].axis_scores)


class ProjectAnalysisTests(unittest.TestCase):
    def test_reads_readme_first_and_ignores_virtual_environments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "README.md").write_text("A Python backend API for coding.", encoding="utf-8")
            (root / "app.py").write_text("def main(): pass", encoding="utf-8")
            (root / ".venv").mkdir()
            (root / ".venv" / "hidden.py").write_text("translation", encoding="utf-8")
            result = analyze_project(root)
            self.assertIn("README", " ".join(result.evidence))
            self.assertIn("coding", result.tasks)
            self.assertNotIn("translation", result.tasks)

    def test_scan_skips_secrets_and_manual_intent_takes_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "README.md").write_text("Python backend source", encoding="utf-8")
            (root / ".env").write_text("SECRET=translation", encoding="utf-8")
            result = analyze_project(root, intent="中英文档翻译")
            self.assertEqual(result.tasks, ("translation",))
            self.assertNotIn("SECRET", result.summary)


class CatalogAndServiceTests(unittest.TestCase):
    def test_cache_is_shared_and_quote_update_changes_recommendation_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            catalog = Catalog(Path(tmp) / "catalog.sqlite3")
            catalog.upsert_quotes([quote("cheap", price=1), quote("costly", price=5)])
            service = RecommendationService(catalog)
            first = service.recommend_project(tmp, intent="摘要报告", input_tokens=100, output_tokens=50)
            cached = service.recommend_project(tmp, intent="摘要报告", input_tokens=100, output_tokens=50)
            self.assertFalse(first.cached)
            self.assertTrue(cached.cached)
            self.assertEqual(first.markdown, cached.markdown)
            catalog.upsert_quotes([quote("cheap", price=9, observed_at="2026-10-27T01:00:00Z")])
            updated = service.recommend_project(tmp, intent="摘要报告", input_tokens=100, output_tokens=50)
            self.assertFalse(updated.cached)

    def test_feedback_requires_a_known_report_and_rating(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            catalog = Catalog(Path(tmp) / "catalog.sqlite3")
            catalog.upsert_quotes([quote("m")])
            result = RecommendationService(catalog).recommend_project(tmp, intent="聊天", input_tokens=1, output_tokens=1)
            catalog.record_feedback(result.report_id, 5, "good")
            self.assertEqual(catalog.list_feedback()[0]["rating"], 5)
            with self.assertRaises(ValueError):
                catalog.record_feedback("no-such-report-id", 5)

    def test_cli_imports_prices_and_writes_markdown_recommendation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            project.mkdir()
            (project / "README.md").write_text("Product creates long-form summaries.", encoding="utf-8")
            prices = root / "prices.json"
            prices.write_text(json.dumps({"quotes": [
                quote("summary-small", price=0.5, capabilities={"summarization": 0.8}).to_dict(),
                quote("summary-large", price=4.0, capabilities={"summarization": 0.95}).to_dict(),
            ]}), encoding="utf-8")
            db_path = root / "catalog.sqlite3"
            report_path = root / "reports" / "recommendation.md"
            self.assertEqual(cli_main(["import-prices", "--file", str(prices), "--db", str(db_path)]), 0)
            self.assertEqual(cli_main([
                "recommend", "--repo", str(project), "--intent", "长文摘要",
                "--db", str(db_path), "--output", str(report_path),
            ]), 0)
            report = report_path.read_text(encoding="utf-8")
            self.assertIn("summary-small", report)
            self.assertIn("首选", report)
            self.assertIn("报价可信度", report)


class SourceTests(unittest.TestCase):
    def test_source_validation_and_text_discovery(self) -> None:
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            McpSourceConfig.from_dict({"name": "bad", "endpoint": "http://example.com/mcp", "tool": "search"})
        with self.assertRaisesRegex(ValueError, "不要把凭据"):
            McpSourceConfig.from_dict({"name": "bad", "endpoint": "https://example.com/mcp?api_key=secret", "tool": "search"})
        rows = _extract_discovery_rows({"content": [{"type": "text", "text": "- [New Model](https://example.org/pricing): pricing updated"}]})
        self.assertEqual(rows[0]["title"], "New Model")
        self.assertEqual(rows[0]["url"], "https://example.org/pricing")
        with self.assertRaisesRegex(ValueError, "凭据"):
            quote("m", source_url="https://example.org/pricing?api_key=not-for-sharing")

    def test_example_mcp_config_is_valid_and_keeps_search_as_discovery(self) -> None:
        sources = load_source_configs("config/sources.example.json")
        self.assertEqual(len(sources), 3)
        self.assertEqual(sources[-1].mode, "discovery")

    def test_one_source_failure_keeps_successful_source_quotes(self) -> None:
        good = McpSourceConfig.from_dict({
            "name": "good", "endpoint": "https://example.invalid/mcp", "tool": "prices", "provider": "Fixture"
        })
        bad = McpSourceConfig.from_dict({
            "name": "bad", "endpoint": "https://example.invalid/mcp", "tool": "prices", "provider": "Fixture"
        })
        with tempfile.TemporaryDirectory() as tmp:
            catalog = Catalog(Path(tmp) / "catalog.sqlite3")
            observed = [quote("m")]
            def collect(source: object) -> list[ModelQuote]:
                if source.config.name == "bad":
                    raise RuntimeError("offline")
                return observed
            with patch("token_mentor.collector.McpMarketSource.collect", collect):
                result = collect_once(catalog, [good, bad])
            self.assertEqual(result["quotes_received"], 1)
            self.assertEqual(len(result["sources_failed"]), 1)
            self.assertEqual(len(catalog.latest_quotes()), 1)


@unittest.skipUnless(find_spec("fastapi") and find_spec("httpx"), "install api and dev extras to run API integration checks")
class ApiIntegrationTests(unittest.TestCase):
    def test_api_auth_recommend_and_feedback(self) -> None:
        from fastapi.testclient import TestClient

        from token_mentor.api import create_app

        with tempfile.TemporaryDirectory() as tmp:
            key = "test-api-key-which-is-at-least-32-characters-long"
            client = TestClient(create_app(str(Path(tmp) / "catalog.sqlite3"), key))
            self.assertEqual(client.get("/healthz").status_code, 200)
            unauthenticated = client.post("/v1/recommend", json={"intent": "project"})
            self.assertEqual(unauthenticated.status_code, 401)
            response = client.post(
                "/v1/recommend",
                headers={"Authorization": f"Bearer {key}"},
                json={"intent": "中英文本地化翻译", "tasks": ["translation"]},
            )
            self.assertEqual(response.status_code, 200, response.text)
            body = response.json()
            self.assertIn("报告 ID", body["markdown"])
            catalog = Catalog(Path(tmp) / "catalog.sqlite3")
            catalog.upsert_quotes([quote("api-model")])
            comparison = client.post(
                "/v1/comparisons",
                headers={"Authorization": f"Bearer {key}"},
                json={"task": "coding", "input_tokens": 200, "output_tokens": 100},
            )
            self.assertEqual(comparison.status_code, 200, comparison.text)
            run_id = comparison.json()["run_id"]
            status = client.get(f"/v1/workflows/{run_id}", headers={"Authorization": f"Bearer {key}"})
            self.assertEqual(status.status_code, 200, status.text)
            self.assertEqual(status.json()["status"], "completed")
            resumed = client.post(f"/v1/workflows/{run_id}/resume", headers={"Authorization": f"Bearer {key}"})
            self.assertEqual(resumed.status_code, 200, resumed.text)
            feedback = client.post(
                "/v1/feedback",
                headers={"Authorization": f"Bearer {key}"},
                json={"report_id": body["report_id"], "rating": 4, "note": "usable"},
            )
            self.assertEqual(feedback.status_code, 201, feedback.text)


if __name__ == "__main__":
    unittest.main()
