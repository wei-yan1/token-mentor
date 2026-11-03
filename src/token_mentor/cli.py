from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from token_mentor.orchestration.agents import EvaluationAgent, MarketResearchAgent, ProjectAdvisorAgent
from token_mentor.orchestration.collector import load_source_configs
from .models import BenchmarkEvidence, ModelQuote
from token_mentor.orchestration.ranking import parse_weights
from token_mentor.orchestration.service import RecommendationService
from token_mentor.state.storage import Catalog
from token_mentor.orchestration.comparison import ComparisonRequest, ComparisonWorkflow


DEFAULT_DB = ".token-mentor/catalog.sqlite3"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="token-mentor", description="项目感知的模型价格与选型助手")
    sub = parser.add_subparsers(dest="command", required=True)

    refresh = sub.add_parser("refresh", help="从配置的 MCP 数据源采集报价和新模型线索")
    refresh.add_argument("--config", default="config/sources.json")
    refresh.add_argument("--db", default=DEFAULT_DB)
    refresh.add_argument("--loop", action="store_true", help="持续定时运行；生产环境建议由系统调度器托管")
    refresh.add_argument("--interval-seconds", type=int, default=21_600)

    recommend = sub.add_parser("recommend", help="分析项目并生成 Markdown 推荐报告")
    recommend.add_argument("--repo", default=".", help="项目目录；默认扫描当前项目")
    recommend.add_argument("--intent", help="手动补充或直接描述项目用途")
    recommend.add_argument("--db", default=DEFAULT_DB)
    recommend.add_argument("--output", default="reports/recommendation.md")
    recommend.add_argument("--weights", help="cost、quality、stability、speed 权重")
    recommend.add_argument("--input-tokens", type=int, default=4_000)
    recommend.add_argument("--output-tokens", type=int, default=1_200)
    recommend.add_argument("--cache-ttl-seconds", type=int, default=86_400)
    recommend.add_argument("--freshness-days", type=int, default=30)

    importer = sub.add_parser("import-prices", help="导入符合规范的 JSON 报价，适合离线/手工校验")
    importer.add_argument("--file", required=True)
    importer.add_argument("--db", default=DEFAULT_DB)

    benchmarks = sub.add_parser("import-benchmarks", help="导入可溯源的 benchmark 证据 JSON")
    benchmarks.add_argument("--file", required=True)
    benchmarks.add_argument("--db", default=DEFAULT_DB)

    compare = sub.add_parser("compare", help="启动可恢复的五节点模型对比工作流")
    compare.add_argument("--task", required=True)
    compare.add_argument("--input-tokens", type=int, default=4_000)
    compare.add_argument("--output-tokens", type=int, default=1_200)
    compare.add_argument("--benchmark-id")
    compare.add_argument("--benchmark-version")
    compare.add_argument("--protocol", default="")
    compare.add_argument("--run-id")
    compare.add_argument("--db", default=DEFAULT_DB)
    compare.add_argument("--output", default="reports/comparison.md")

    workflow = sub.add_parser("workflow", help="查看或恢复已持久化的对比工作流")
    workflow.add_argument("action", choices=("status", "resume"))
    workflow.add_argument("run_id")
    workflow.add_argument("--db", default=DEFAULT_DB)

    feedback = sub.add_parser("feedback", help="记录对推荐报告的评分和原因")
    feedback.add_argument("report_id")
    feedback.add_argument("--rating", required=True, type=int, choices=range(1, 6))
    feedback.add_argument("--note", default="")
    feedback.add_argument("--db", default=DEFAULT_DB)

    feedback_list = sub.add_parser("feedback-list", help="查看近期用户反馈，支持人工复盘和规则更新")
    feedback_list.add_argument("--db", default=DEFAULT_DB)
    feedback_list.add_argument("--limit", type=int, default=100)

    evaluation = sub.add_parser("eval", help="运行本地小型离线测评集")
    evaluation.add_argument("--cases", default="evals/cases.json")

    discoveries = sub.add_parser("discoveries", help="查看 MCP 搜索发现的新模型/价格来源线索")
    discoveries.add_argument("--db", default=DEFAULT_DB)
    discoveries.add_argument("--limit", type=int, default=50)

    api = sub.add_parser("api", help="启动带 Bearer API key 认证的多用户 HTTP 服务")
    api.add_argument("--host", default="127.0.0.1")
    api.add_argument("--port", type=int, default=8080)
    api.add_argument("--db", default=None)

    return parser


def _refresh(args: argparse.Namespace) -> int:
    catalog = Catalog(args.db)
    configs = load_source_configs(args.config)
    agent = MarketResearchAgent(catalog, configs)
    if args.loop:
        try:
            agent.run_forever(
                interval_seconds=args.interval_seconds,
                on_result=lambda result: print(json.dumps(result, ensure_ascii=False, indent=2)),
            )
        except KeyboardInterrupt:
            return 0
        return 0
    result = agent.run_once()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["quotes_received"] or result["discoveries_received"] else 1


def _recommend(args: argparse.Namespace) -> int:
    catalog = Catalog(args.db)
    service = RecommendationService(
        catalog,
        cache_ttl_seconds=args.cache_ttl_seconds,
        freshness_days=args.freshness_days,
    )
    result = ProjectAdvisorAgent(service).from_repository(
        args.repo,
        intent=args.intent,
        input_tokens=args.input_tokens,
        output_tokens=args.output_tokens,
        weights=parse_weights(args.weights),
    )
    output = Path(args.output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(result.markdown, encoding="utf-8")
    state = "命中共享缓存" if result.cached else "新生成"
    print(f"报告已{state}：{output}\n报告 ID：{result.report_id}")
    if not catalog.latest_quotes():
        print("提示：目前报价库为空。先配置 MCP 来源运行 refresh，或用 import-prices 导入规范报价。", file=sys.stderr)
    return 0


def _import_prices(args: argparse.Namespace) -> int:
    path = Path(args.file).expanduser().resolve()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取报价文件 {path}: {exc}") from exc
    rows = payload.get("quotes") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not rows:
        raise ValueError("报价文件格式应为非空 {\"quotes\": [...]} 数组")
    quotes = [ModelQuote.from_payload(row) for row in rows]
    changed = Catalog(args.db).upsert_quotes(quotes)
    print(f"导入 {len(quotes)} 条报价；新增或更新 {changed} 条。")
    return 0


def _import_benchmarks(args: argparse.Namespace) -> int:
    path = Path(args.file).expanduser().resolve()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取 benchmark 文件 {path}: {exc}") from exc
    rows = payload.get("benchmarks") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not rows:
        raise ValueError("benchmark 文件格式应为非空 {\"benchmarks\": [...]} 数组")
    evidence = [BenchmarkEvidence.from_dict(row) for row in rows]
    changed = Catalog(args.db).upsert_benchmarks(evidence)
    print(f"导入 {len(evidence)} 条 benchmark 证据；新增或更新 {changed} 条。")
    return 0

def _compare(args: argparse.Namespace) -> int:
    catalog = Catalog(args.db)
    quotes = catalog.latest_quotes()
    if not quotes:
        raise ValueError("报价库为空；先运行 refresh 或 import-prices")
    request = ComparisonRequest(args.task, args.input_tokens, args.output_tokens, args.benchmark_id, args.benchmark_version, args.protocol)
    import uuid
    run_id = args.run_id or uuid.uuid4().hex[:16]
    workflow = ComparisonWorkflow(catalog)
    workflow.create(run_id, request, quotes, catalog.benchmark_snapshot())
    result = workflow.run(run_id)
    report = result["result_json"]["markdown"]
    output = Path(args.output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(report, encoding="utf-8")
    print(f"对比工作流完成：{output}\nRun ID：{run_id}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "refresh":
            return _refresh(args)
        if args.command == "recommend":
            return _recommend(args)
        if args.command == "import-prices":
            return _import_prices(args)
        if args.command == "import-benchmarks":
            return _import_benchmarks(args)
        if args.command == "compare":
            return _compare(args)
        if args.command == "workflow":
            workflow_agent = ComparisonWorkflow(Catalog(args.db))
            result = workflow_agent.status(args.run_id) if args.action == "status" else workflow_agent.resume(args.run_id)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["status"] == "completed" else 2
        if args.command == "feedback":
            Catalog(args.db).record_feedback(args.report_id, args.rating, args.note)
            print("反馈已记录，可纳入下一轮规则和权重复核。")
            return 0
        if args.command == "feedback-list":
            print(json.dumps(Catalog(args.db).list_feedback(args.limit), ensure_ascii=False, indent=2))
            return 0
        if args.command == "eval":
            result = EvaluationAgent().run(args.cases)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["passed"] == result["total"] else 2
        if args.command == "discoveries":
            print(json.dumps(Catalog(args.db).list_discoveries(args.limit), ensure_ascii=False, indent=2))
            return 0
        if args.command == "api":
            from .api import create_app

            try:
                import uvicorn
            except ImportError as exc:
                raise RuntimeError("HTTP 服务依赖尚未安装，请执行 pip install -e '.[api]'") from exc
            uvicorn.run(create_app(db_path=args.db), host=args.host, port=args.port, workers=1)
            return 0
    except (ValueError, OSError, RuntimeError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

