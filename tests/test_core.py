from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from decimal import Decimal
from unittest.mock import patch
from importlib.util import find_spec
import os

from token_mentor.analysis import analyze_project
from token_mentor.orchestration.collector import collect_once, load_collection_config, load_source_configs, _format_query
from token_mentor.orchestration.decision import TaskIntentDecisionAgent
from token_mentor.cli import main as cli_main, _load_env_file
from token_mentor.models import ModelProfile, ModelQuote, ProjectAnalysis, ResearchTarget
from token_mentor.context.task_intent import TaskIntent
from token_mentor.tools.aliyun_models import AliyunModelCatalogSource, normalize_model
from token_mentor.tools.tavily import TavilySearchSource
from token_mentor.tools.pricing import extract_markdown_quotes, normalize_quote_currency
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


class EnvironmentFileTests(unittest.TestCase):
    def test_loads_dotenv_without_overriding_process_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text("# comment\nTAVILY_API_KEY='local-value'\nPARENT_VALUE=file-value\nBAD LINE\n", encoding="utf-8")
            with patch.dict(os.environ, {"PARENT_VALUE": "process-value"}, clear=True):
                _load_env_file(env_path)
                self.assertEqual(os.environ["TAVILY_API_KEY"], "local-value")
                self.assertEqual(os.environ["PARENT_VALUE"], "process-value")


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


class TaskIntentTests(unittest.TestCase):
    def test_laya_is_task_classifier_and_rule_lanes_are_retained(self) -> None:
        class Classifier:
            def classify(self, message: str) -> TaskIntent:
                return TaskIntent("reasoning", ("reasoning",), 0.9, "判断需要分析", "laya")

        with patch.dict(os.environ, {"TOKEN_MENTOR_DECISION_BACKEND": "laya"}):
            decision = TaskIntentDecisionAgent(Classifier()).classify("给文本做 embeddings 向量化")
        self.assertEqual(decision.primary_task, "reasoning")
        self.assertEqual(decision.tasks, ("reasoning", "embeddings"))

    def test_missing_laya_falls_back_to_specialized_task_rules(self) -> None:
        class MissingClassifier:
            def classify(self, message: str) -> TaskIntent:
                raise ModuleNotFoundError("laya")

        with patch.dict(os.environ, {"TOKEN_MENTOR_DECISION_BACKEND": "laya"}):
            decision = TaskIntentDecisionAgent(MissingClassifier()).classify("把客户语音识别并转成文字")
        self.assertEqual(decision.source, "rule_fallback")
        self.assertEqual(decision.primary_task, "speech_recognition")

    def test_recommendation_uses_catalog_lane_before_cost_ranking(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            catalog = Catalog(Path(tmp) / "catalog.sqlite3")
            catalog.upsert_quotes([quote("embed-model", price=2, capabilities={"embeddings": 0.9}),
                                   quote("cheap-chat", price=0.01)])
            catalog.upsert_model_profiles([ModelProfile("Fixture", "embed-model", "domestic", "embedding model",
                ("text embeddings",), "https://example.invalid/models", "2026-11-04T00:00:00Z",
                task_lanes=("embeddings",), capability_tags=("TR",))])
            project = ProjectAnalysis("fixture", "文本向量化", (), ("embeddings",), (), "fingerprint")
            report = RecommendationService(catalog).recommend_profile(project).markdown
        self.assertIn("embed-model", report)
        self.assertNotIn("cheap-chat", report)


class AliyunModelCatalogTests(unittest.TestCase):
    def test_catalog_normalization_maps_lanes_and_ignores_returned_prices(self) -> None:
        profile = normalize_model({"model_id": "text-embedding-v4", "provider": "qwen",
            "description": "Multilingual embedding model", "capabilities": ["TR", "ME"],
            "features": ["batch"], "context_window": 8192,
            "input_price": 999, "output_price": 999}, observed_at="2026-11-04T00:00:00Z")
        self.assertEqual(profile["provider"], "阿里云")
        self.assertEqual(profile["task_lanes"], ("embeddings", "multimodal"))
        self.assertEqual(profile["context_tokens"], 8192)
        self.assertNotIn("input_price", profile)

    def test_workspace_catalog_request_is_authenticated_and_paged(self) -> None:
        config = McpSourceConfig.from_dict({"name":"aliyun-model-catalog", "mode":"discovery",
            "transport":"aliyun_models", "scope":"domestic", "category":"model_catalog",
            "endpoint":"https://{workspace_id}.cn-beijing.maas.aliyuncs.com/api/v1/models",
            "tool":"list_models", "api_key_env":"DASHSCOPE_API_KEY",
            "arguments":{"workspace_id_env":"DASHSCOPE_WORKSPACE_ID", "page_size":2, "max_pages":3}})
        response = BytesIO(json.dumps({"data":{"models":[{"model_id":"qwen-embedding-v4", "provider":"qwen",
            "capabilities":["TR"]}], "total":1}}).encode())
        with patch.dict(os.environ, {"DASHSCOPE_API_KEY":"test-key", "DASHSCOPE_WORKSPACE_ID":"workspace-123"}), \
             patch("token_mentor.tools.aliyun_models.urlopen", return_value=response) as mocked:
            rows = AliyunModelCatalogSource(config).discover()
        request = mocked.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer test-key")
        self.assertIn("workspace-123.cn-beijing", request.full_url)
        self.assertIn("page_no=1", request.full_url)
        self.assertEqual(rows[0]["task_lanes"], ("embeddings",))

    def test_collector_imports_catalog_profiles_but_never_catalog_prices(self) -> None:
        source = McpSourceConfig.from_dict({"name":"aliyun-model-catalog", "mode":"discovery",
            "transport":"aliyun_models", "scope":"domestic", "category":"model_catalog",
            "endpoint":"https://{workspace_id}.cn-beijing.maas.aliyuncs.com/api/v1/models",
            "tool":"list_models", "api_key_env":"DASHSCOPE_API_KEY",
            "arguments":{"max_pages":1}})
        row = normalize_model({"model_id":"qwen3-embedding", "provider":"qwen", "capabilities":["TR"],
                               "input_price":0.001}, observed_at="2026-11-04T00:00:00Z")
        with tempfile.TemporaryDirectory() as tmp:
            catalog = Catalog(Path(tmp) / "catalog.sqlite3")
            with patch("token_mentor.collector.AliyunModelCatalogSource.discover", return_value=[row]):
                result = collect_once(catalog, [source], scope="domestic")
            self.assertEqual(result["profiles_updated"], 1)
            self.assertEqual(catalog.latest_quotes(), [])
            profile = catalog.list_model_profiles()[0]
            self.assertEqual(profile["task_lanes"], ["embeddings"])


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
        self.assertEqual(len(sources), 4)
        search = next(source for source in sources if source.name == "tavily-market-research")
        self.assertEqual(search.mode, "discovery")
        self.assertEqual(search.transport, "tavily")
        self.assertIn("market_news", search.query_templates)
        self.assertIn("牛来", search.watch_terms)
        self.assertEqual(search.max_model_queries, 100)
        self.assertTrue(search.enabled)
        self.assertFalse(next(x for x in sources if x.name == "aliyun-pricing").enabled)
        self.assertEqual(next(x for x in sources if x.name == "aliyun-model-catalog").category, "model_catalog")

    def test_region_scope_queries_known_models_and_news(self) -> None:
        config = McpSourceConfig.from_dict({
            "name": "search", "mode": "discovery", "endpoint": "https://example.invalid/mcp", "tool": "search",
            "query_templates": {"pricing": "{provider} {domains} pricing",
                                "model_verification": "{provider} {model} official pricing {domains}",
                                "market_news": "LLM launch news {region} {date}"},
        })
        domestic = ResearchTarget("DomesticAI", "domestic", ("D-AI",), ("domestic.example",))
        foreign = ResearchTarget("ForeignAI", "foreign", (), ("foreign.example",))
        known = ModelQuote.from_payload({**quote("D-1").to_dict(), "provider": "DomesticAI", "region": "domestic"})
        seen = []
        def discover(source: object, override: dict[str, str] | None = None) -> list[dict[str, str]]:
            seen.append(override["query"] if override else "default")
            return [{"title": "lead", "url": f"https://result.example/{len(seen)}", "snippet": "candidate"}]
        with tempfile.TemporaryDirectory() as tmp:
            catalog = Catalog(Path(tmp) / "catalog.sqlite3")
            catalog.upsert_quotes([known])
            with patch("token_mentor.collector.McpMarketSource.discover", discover):
                result = collect_once(catalog, [config], scope="domestic", targets=[domestic, foreign])
            self.assertEqual(result["discoveries_received"], 3)
            self.assertEqual(result["news_leads_received"], 1)
            self.assertEqual(result["targets_checked"], 1)
            self.assertTrue(any("DomesticAI" in query and "D-1" in query for query in seen))
            self.assertFalse(any("ForeignAI" in query for query in seen))
            self.assertEqual(len(catalog.list_market_leads(scope="domestic")), 3)
            checks = catalog.list_market_checks()
            self.assertEqual(sum(1 for row in checks if row["status"] == "search_only"), 2)

    def test_search_budget_caps_all_sources_at_100_and_spreads_across_vendors(self) -> None:
        templates = {"provider_research":"{provider} official models pricing",
                     "model_verification":"{provider} {model} official model page",
                     "market_news":"LLM launch news {region} {date}"}
        sources = [McpSourceConfig.from_dict({"name": f"search-{index}", "mode":"discovery",
            "endpoint":"https://example.invalid/mcp", "tool":"search", "query_templates":templates})
            for index in range(2)]
        targets = [ResearchTarget(f"Vendor-{index:02}", "domestic" if index % 2 == 0 else "foreign")
                   for index in range(80)]
        quotes = [ModelQuote.from_payload({"provider":target.provider, "model":f"model-{index:02}",
            "input_price_per_million":1, "output_price_per_million":2, "price_unit":"CNY/1M tokens",
            "source_url":f"https://example.invalid/{index}", "observed_at":"2026-11-03T00:00:00Z",
            "region":target.region}) for index,target in enumerate(targets)]
        calls=[]
        def discover(source: object, override: dict[str, str] | None = None) -> list[dict[str, str]]:
            calls.append((source.config.name, override["query"]))
            return [{"title":"result", "url":f"https://results.example/{len(calls)}", "snippet":"evidence"}]
        with tempfile.TemporaryDirectory() as tmp:
            catalog=Catalog(Path(tmp)/"catalog.sqlite3")
            catalog.upsert_quotes(quotes)
            with patch("token_mentor.collector.McpMarketSource.discover", discover):
                result=collect_once(catalog,sources,scope="all",targets=targets)
        self.assertEqual(len(calls),100)
        self.assertEqual(result["search_queries_attempted"],100)
        self.assertEqual(result["search_queries_skipped"],224)
        self.assertEqual(result["search_budget_remaining"],0)
        self.assertEqual(result["news_leads_received"],4)
        provider_queries={query.split()[0] for source,query in calls if "official models pricing" in query}
        self.assertEqual(provider_queries,{target.provider for target in targets})
        checked_models=sum(1 for source,query in calls if "official model page" in query)
        self.assertEqual(checked_models,16)

    def test_global_budget_counts_price_catalog_and_reserves_news_calls(self) -> None:
        pricing=McpSourceConfig.from_dict({"name":"price-catalog","mode":"pricing","scope":"all",
            "iterate_targets":True,"endpoint":"https://catalog.example/mcp","tool":"prices"})
        news=McpSourceConfig.from_dict({"name":"news-search","mode":"discovery","scope":"all",
            "endpoint":"https://search.example/mcp","tool":"search",
            "query_templates":{"market_news":"LLM headlines {region}"}})
        targets=[ResearchTarget(f"Vendor-{i:03}","domestic" if i%2==0 else "foreign",(),(f"vendor-{i:03}.example",)) for i in range(100)]
        price_calls=[]; news_calls=[]
        def fetch_prices(source: object, override: dict[str,str] | None=None, *, default_region: str | None=None):
            provider=override["provider"]; price_calls.append(provider)
            index=int(provider.rsplit("-",1)[1]);domain=f"vendor-{index:03}.example"
            item=ModelQuote.from_payload({"provider":provider,"model":"catalog-model","input_price_per_million":1,
                "output_price_per_million":2,"price_unit":"CNY/1M tokens","source_url":f"https://{domain}/pricing",
                "observed_at":"2026-11-03T00:00:00Z","region":default_region})
            return [item],[item.to_dict()]
        def fetch_news(source: object, override: dict[str,str] | None=None):
            news_calls.append(override["query"])
            return [{"title":"headline","url":f"https://news.example/{len(news_calls)}","snippet":"lead"}]
        with tempfile.TemporaryDirectory() as tmp:
            catalog=Catalog(Path(tmp)/"catalog.sqlite3")
            with patch("token_mentor.collector.McpMarketSource.collect_records",fetch_prices), \
                 patch("token_mentor.collector.McpMarketSource.discover",fetch_news):
                result=collect_once(catalog,[pricing,news],scope="all",targets=targets)
        self.assertEqual(result["research_requests_attempted"],100)
        self.assertEqual(result["research_requests_skipped"],2)
        self.assertEqual(len(price_calls),98)
        self.assertEqual(len(news_calls),2)
        self.assertEqual(result["news_leads_received"],2)

    def test_tavily_source_sends_runtime_key_only_in_authorization_header(self) -> None:
        config = McpSourceConfig.from_dict({"name":"tavily", "mode":"discovery", "transport":"tavily",
            "endpoint":"https://api.tavily.com/search", "tool":"tavily_search", "api_key_env":"TAVILY_API_KEY",
            "arguments":{"query":"default", "search_depth":"basic"}})
        source = TavilySearchSource(config)
        response = BytesIO(json.dumps({"results":[{"title":"official pricing", "url":"https://official.example/pricing",
                                                 "content":"Input: $1", "published_date":"2026-11-02"}]}).encode())
        with patch.dict("os.environ", {"TAVILY_API_KEY":"runtime-secret"}), patch("token_mentor.tools.tavily.urlopen", return_value=response) as mocked:
            rows = source.discover({"query":"OpenAI official pricing"})
        request = mocked.call_args.args[0]
        body = json.loads(request.data.decode())
        self.assertEqual(request.get_header("Authorization"), "Bearer runtime-secret")
        self.assertNotIn("api_key", body)
        self.assertEqual(rows[0]["published_at"], "2026-11-02")
        self.assertNotIn("runtime-secret", json.dumps(rows))

    def test_tavily_extract_reads_only_safe_public_urls_and_keeps_key_in_header(self) -> None:
        config=McpSourceConfig.from_dict({"name":"tavily", "mode":"discovery", "transport":"tavily",
            "endpoint":"https://api.tavily.com/search", "tool":"tavily_search", "api_key_env":"TAVILY_API_KEY"})
        source=TavilySearchSource(config)
        response=BytesIO(json.dumps({"results":[{"url":"https://official.example/pricing","raw_content":"| Model | Input | Output |"}]}).encode())
        with patch.dict("os.environ",{"TAVILY_API_KEY":"runtime-secret"}), patch("token_mentor.tools.tavily.urlopen",return_value=response) as mocked:
            rows=source.extract_pages(["https://official.example/pricing","http://plain.example/pricing"])
        request=mocked.call_args.args[0]
        body=json.loads(request.data.decode())
        self.assertEqual(body["urls"],["https://official.example/pricing"])
        self.assertEqual(request.get_header("Authorization"),"Bearer runtime-secret")
        self.assertEqual(rows[0]["raw_content"],"| Model | Input | Output |")
        self.assertNotIn("runtime-secret",json.dumps(rows))

    def test_explicit_official_markdown_pricing_table_is_parsed_and_converted(self) -> None:
        content = """# Pricing\n\nPrices per 1M tokens (USD).\n\n| Model | Input | Output |\n| --- | ---: | ---: |\n| `fixture-chat` | $0.25 | $1.25 |\n"""
        rows = extract_markdown_quotes(content, provider="FixtureCo", source_url="https://fixture.example/pricing",
                                       region="foreign", observed_at="2026-11-04T00:00:00Z")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["currency"], "USD")
        with patch("token_mentor.tools.pricing._fetch_cny_rate", return_value=(Decimal("2.0"), "https://api.frankfurter.dev/v1/latest?base=USD&symbols=CNY", "2026-11-03")):
            converted = normalize_quote_currency(rows[0])
        model_quote = ModelQuote.from_payload(converted)
        self.assertEqual(model_quote.input_price_per_million, 0.5)
        self.assertEqual(model_quote.output_price_per_million, 2.5)
        self.assertEqual(model_quote.source_currency, "USD")
        self.assertEqual(model_quote.source_input_price_per_million, 0.25)
        self.assertEqual(model_quote.fx_rate_to_cny, 2.0)

    def test_pricing_parser_rejects_ambiguous_units_and_non_table_snippets(self) -> None:
        content = """| Model | Input | Output |\n| --- | --- | --- |\n| fixture-chat | $0.25 / 1K tokens | $1.25 / 1K tokens |\n"""
        self.assertEqual(extract_markdown_quotes(content, provider="FixtureCo",
                         source_url="https://fixture.example/pricing", region="foreign"), [])
        prose_table = """Prices per 1M tokens\n| Model | Input | Output |\n| --- | --- | --- |\n| Charged as regular tokens per model pricing | $3.8 | $3.8 |\n| Computer use | $0.20 | $0.40 |\n"""
        self.assertEqual(extract_markdown_quotes(prose_table, provider="FixtureCo",
                         source_url="https://fixture.example/pricing", region="foreign"), [])

    def test_deepseek_tiered_official_price_table_uses_peak_and_keeps_cached_input(self) -> None:
        content = """# 模型价格\n|  |  |  |  |  |\n| --- | --- | --- | --- | --- |\n| 模型 | | | deepseek-flash(1) | deepseek-v4-pro |\n| --- | --- | --- | --- | --- |\n| 价格 | 百万tokens输入（缓存命中） | 空闲时段 | 0.02元 | 0.15元 |\n| 高峰时段 | 0.04元 | 0.30元 |\n| 百万tokens输入（缓存未命中） | 空闲时段 | 1元 | 4.5元 |\n| 高峰时段 | 2元 | 9.0元 |\n| 百万tokens输出 | 空闲时段 | 4元 | 13.5元 |\n| 高峰时段 | 8元 | 27.0元 |\n"""
        rows=extract_markdown_quotes(content,provider="DeepSeek",source_url="https://api-docs.deepseek.com/zh-cn/quick_start/pricing",region="domestic")
        self.assertEqual({row["model"] for row in rows},{"deepseek-flash","deepseek-v4-pro"})
        flash=next(row for row in rows if row["model"]=="deepseek-flash")
        self.assertEqual(flash["input_price_per_million"],2.0)
        self.assertEqual(flash["cached_input_price_per_million"],0.04)
        self.assertEqual(flash["output_price_per_million"],8.0)

    def test_google_model_sections_select_the_price_active_on_observation_date(self) -> None:
        content="""## Gemini 4 Argon\n_[`gemini-4-argon`](https://ai.google.dev/gemini-api/docs/models/gemini-4-argon)_\n|  | Free Tier | Paid Tier, per 1M tokens in USD |\n| --- | --- | --- |\n| Input price | Not available | $2.00 through November 30, 2026. $4.00 starting December 1, 2026. |\n| Output price (including thinking tokens) | Not available | $10.00 through November 30, 2026. $20.00 starting December 1, 2026. |\n"""
        rows=extract_markdown_quotes(content,provider="Google",source_url="https://ai.google.dev/gemini-api/docs/pricing",region="foreign",observed_at="2026-11-04T00:00:00Z")
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]["model"],"gemini-4-argon")
        self.assertEqual((rows[0]["input_price_per_million"],rows[0]["output_price_per_million"]),(2.0,10.0))

    def test_tavily_official_pricing_tables_update_the_formal_catalog(self) -> None:
        source = McpSourceConfig.from_dict({"name":"tavily","mode":"discovery","transport":"tavily",
            "endpoint":"https://api.tavily.com/search","tool":"tavily_search","api_key_env":"TAVILY_API_KEY",
            "query_templates":{"pricing":"{provider} official pricing {domains}"}})
        target = ResearchTarget("FixtureCo", "foreign", (), ("fixture.example",))
        page = """# Pricing\n\nPrices per 1M tokens (USD).\n\n| Model | Input | Output |\n| --- | ---: | ---: |\n| fixture-chat | $0.25 | $1.25 |\n"""
        row = {"url":"https://fixture.example/pricing", "title":"Pricing", "snippet":"official prices",
               "raw_content":page, "published_at":"2026-11-03"}
        with tempfile.TemporaryDirectory() as tmp:
            catalog=Catalog(Path(tmp)/"catalog.sqlite3")
            with patch("token_mentor.collector.TavilySearchSource.discover", return_value=[row]), \
                 patch("token_mentor.tools.pricing._fetch_cny_rate", return_value=(Decimal("2.0"), "https://api.frankfurter.dev/v1/latest?base=USD&symbols=CNY", "2026-11-03")):
                result=collect_once(catalog,[source],scope="foreign",targets=[target])
            self.assertEqual(result["quotes_received"],1)
            self.assertEqual(result["sources_failed"],[])
            saved=catalog.latest_quotes()[0]
            self.assertEqual(saved.provider,"FixtureCo")
            self.assertEqual(saved.input_price_per_million,0.5)
            self.assertEqual(saved.source_currency,"USD")
            self.assertEqual(catalog.list_market_checks()[0]["status"],"verified")
            self.assertEqual(catalog.list_market_checks()[0]["quote_count"],1)

    def test_tavily_pricing_uses_configured_official_page_when_search_snippet_has_no_table(self) -> None:
        source = McpSourceConfig.from_dict({"name":"tavily","mode":"discovery","transport":"tavily",
            "endpoint":"https://api.tavily.com/search","tool":"tavily_search","api_key_env":"TAVILY_API_KEY",
            "query_templates":{"pricing":"{provider} official pricing {domains}"}})
        target = ResearchTarget("FixtureCo", "foreign", (), ("fixture.example",), True,
                                ("https://fixture.example/pricing",))
        page = """Prices per 1M tokens (USD).\n| Model | Input | Output |\n| --- | ---: | ---: |\n| fixture-chat | $0.25 | $1.25 |\n"""
        with tempfile.TemporaryDirectory() as tmp:
            catalog=Catalog(Path(tmp)/"catalog.sqlite3")
            with patch("token_mentor.collector.TavilySearchSource.discover", return_value=[{"url":"https://fixture.example/search",
                    "title":"Pricing", "snippet":"see official page"}]), \
                 patch("token_mentor.collector.TavilySearchSource.extract_pages", return_value=[{"url":"https://fixture.example/pricing",
                    "raw_content":page}]), \
                 patch("token_mentor.tools.pricing._fetch_cny_rate", return_value=(Decimal("2.0"), "https://api.frankfurter.dev/v1/latest?base=USD&symbols=CNY", "2026-11-03")):
                result=collect_once(catalog,[source],scope="foreign",targets=[target])
            self.assertEqual(result["quotes_received"],1)
            self.assertEqual(catalog.latest_quotes()[0].model,"fixture-chat")

    def test_model_verification_imports_only_the_requested_model_from_a_full_price_page(self) -> None:
        source=McpSourceConfig.from_dict({"name":"tavily","mode":"discovery","transport":"tavily",
            "endpoint":"https://api.tavily.com/search","tool":"tavily_search","api_key_env":"TAVILY_API_KEY",
            "query_templates":{"model_verification":"{provider} {model} official pricing {domains}"}})
        target=ResearchTarget("Google","foreign",(),("ai.google.dev",))
        known=[ModelQuote.from_payload({"provider":"Google","model":model,"input_price_per_million":1,
            "output_price_per_million":2,"price_unit":"CNY/1M tokens","source_url":"https://ai.google.dev/gemini-api/docs/pricing",
            "observed_at":"2026-11-03T00:00:00Z","region":"foreign"}) for model in ("gemini-4-argon","gemini-3.8-flash")]
        content="""## Gemini 4 Argon\n_[`gemini-4-argon`](https://ai.google.dev/gemini-api/docs/models/gemini-4-argon)_\n|  | Free Tier | Paid Tier, per 1M tokens in USD |\n| --- | --- | --- |\n| Input price | Not available | $2.00 |\n| Output price | Not available | $10.00 |\n## Gemini 3.8 Flash\n_[`gemini-3.8-flash`](https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash)_\n|  | Free Tier | Paid Tier, per 1M tokens in USD |\n| --- | --- | --- |\n| Input price | Not available | $0.75 |\n| Output price | Not available | $3.75 |\n"""
        with tempfile.TemporaryDirectory() as tmp:
            catalog=Catalog(Path(tmp)/"catalog.sqlite3"); catalog.upsert_quotes(known)
            with patch("token_mentor.collector.TavilySearchSource.discover",return_value=[{"url":"https://ai.google.dev/gemini-api/docs/pricing",
                    "title":"Pricing", "snippet":"official prices", "raw_content":content}]), \
                 patch("token_mentor.tools.pricing._fetch_cny_rate",return_value=(Decimal("7.5"),"https://api.frankfurter.dev/v1/latest?base=USD&symbols=CNY","2026-11-03")):
                result=collect_once(catalog,[source],scope="foreign",targets=[target])
            self.assertEqual(result["quotes_received"],2)
            self.assertEqual(sorted(item["quotes"] for item in result["sources_ok"]),[1,1])

    def test_daily_price_catalog_sync_records_changes_and_model_profiles(self) -> None:
        target = ResearchTarget("FixtureCo", "foreign", ("Fixture",), ("fixture.example",))
        source = McpSourceConfig.from_dict({"name":"catalog", "mode":"pricing", "scope":"all",
            "iterate_targets":True, "endpoint":"https://example.invalid/mcp", "tool":"prices"})
        old = ModelQuote.from_payload({**quote("m", price=1).to_dict(), "provider":"FixtureCo", "region":"foreign"})
        new = ModelQuote.from_payload({**quote("m", price=2, observed_at="2026-11-03T00:00:00Z").to_dict(),
                                       "provider":"FixtureCo", "region":"foreign", "description":"Coding and long-context model",
                                       "strengths":["coding", "agents"], "source_url":"https://fixture.example/pricing"})
        with tempfile.TemporaryDirectory() as tmp:
            catalog = Catalog(Path(tmp) / "catalog.sqlite3")
            catalog.upsert_quotes([old])
            with patch("token_mentor.collector.McpMarketSource.collect_records", return_value=([new],[new.to_dict()])):
                result = collect_once(catalog, [source], scope="foreign", targets=[target])
            self.assertEqual(result["quotes_changed"], 1)
            self.assertEqual(result["profiles_updated"], 1)
            changes = catalog.list_price_changes()
            self.assertEqual(changes[0]["new_input"], 2)
            profiles = catalog.list_model_profiles(region="foreign")
            self.assertEqual(profiles[0]["description"], "Coding and long-context model")
            self.assertEqual(profiles[0]["strengths"], ["coding", "agents"])

    def test_target_price_import_rejects_non_official_source_urls(self) -> None:
        target = ResearchTarget("FixtureCo", "foreign", (), ("fixture.example",))
        source = McpSourceConfig.from_dict({"name":"catalog", "mode":"pricing", "scope":"all",
            "iterate_targets":True, "endpoint":"https://example.invalid/mcp", "tool":"prices"})
        aggregator_quote = ModelQuote.from_payload({**quote("m").to_dict(), "provider":"FixtureCo",
            "source_url":"https://aggregator.example/fixture/prices"})
        with tempfile.TemporaryDirectory() as tmp:
            catalog = Catalog(Path(tmp) / "catalog.sqlite3")
            with patch("token_mentor.collector.McpMarketSource.collect_records", return_value=([aggregator_quote],[aggregator_quote.to_dict()])):
                result = collect_once(catalog, [source], scope="foreign", targets=[target])
            self.assertEqual(result["quotes_received"], 0)
            self.assertEqual(len(result["sources_failed"]), 1)
            self.assertEqual(catalog.latest_quotes(), [])

    def test_targets_and_direct_tavily_source_config_parse(self) -> None:
        sources, targets = load_collection_config("config/sources.example.json")
        self.assertEqual(len(targets), 17)
        self.assertEqual({x.region for x in targets}, {"domestic", "foreign"})
        tavily = next(x for x in sources if x.transport == "tavily")
        self.assertEqual(tavily.endpoint, "https://api.tavily.com/search")
        self.assertIn("model_verification", tavily.query_templates)
        self.assertEqual(tavily.max_model_queries, 100)
        self.assertIn("https://api-docs.deepseek.com/zh-cn/quick_start/pricing",
                      next(target for target in targets if target.provider == "DeepSeek").pricing_urls)

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
            self.assertEqual(client.get("/v1/market/models", headers={"Authorization": f"Bearer {key}"}).status_code, 200)
            self.assertEqual(client.get("/v1/market/leads?scope=domestic", headers={"Authorization": f"Bearer {key}"}).status_code, 200)
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
