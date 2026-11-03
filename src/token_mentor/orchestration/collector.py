from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
import json
import os
from pathlib import Path
from string import Formatter
from typing import Any
from urllib.parse import urlparse

from token_mentor.models import ModelProfile, ModelQuote, ResearchTarget
from token_mentor.constraints.market import MAX_SEARCH_REQUESTS_PER_REFRESH
from token_mentor.tools.mcp import McpMarketSource, McpSourceConfig
from token_mentor.tools.tavily import TavilySearchSource
from token_mentor.state.storage import Catalog

SCOPES = {"domestic", "foreign", "all"}


def load_collection_config(path: str | Path) -> tuple[list[McpSourceConfig], list[ResearchTarget]]:
    config_path = Path(path).expanduser().resolve()
    try:
        payload: Any = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取市场研究配置 {config_path}: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("sources"), list):
        raise ValueError("MCP 配置格式应为 {\"sources\": [...], \"targets\": [...]} ")
    if not all(isinstance(item, dict) for item in payload["sources"]):
        raise ValueError("sources 中每项必须是 JSON 对象")
    configs = [McpSourceConfig.from_dict(item) for item in payload["sources"]]
    if not configs:
        raise ValueError("MCP 配置中至少需要一个 source")
    names = [config.name for config in configs]
    if len(names) != len(set(names)):
        raise ValueError("MCP source name 不能重复")
    raw_targets = payload.get("targets", [])
    if not isinstance(raw_targets, list) or not all(isinstance(row, dict) for row in raw_targets):
        raise ValueError("targets 必须是对象数组")
    targets = []
    for row in raw_targets:
        aliases = row.get("aliases", [])
        domains = row.get("official_domains", [])
        enabled = row.get("enabled", True)
        if not isinstance(aliases, list) or not isinstance(domains, list) or not isinstance(enabled, bool):
            raise ValueError("target aliases/official_domains 必须是数组，enabled 必须是布尔值")
        targets.append(ResearchTarget(provider=str(row.get("provider", "")), region=str(row.get("region", "")),
                                      aliases=tuple(aliases), official_domains=tuple(domains), enabled=enabled))
    if len({(x.provider.casefold(), x.region) for x in targets}) != len(targets):
        raise ValueError("targets 中 provider + region 不能重复")
    return configs, targets


def load_source_configs(path: str | Path) -> list[McpSourceConfig]:
    return load_collection_config(path)[0]


def load_research_targets(path: str | Path) -> list[ResearchTarget]:
    return load_collection_config(path)[1]


def _target_in_scope(target: ResearchTarget, scope: str) -> bool:
    return scope == "all" or target.region == scope


def _source_in_scope(source: McpSourceConfig, scope: str) -> bool:
    return scope == "all" or source.scope in {"all", scope}


def _format_query(template: str, target: ResearchTarget | None, scope: str, model: str = "", watch_terms: tuple[str, ...] = ()) -> str:
    now = datetime.now(UTC).strftime("%Y-%m-%d")
    values = {"provider": target.provider if target else "",
              "aliases": " ".join(target.aliases) if target else "",
              "domains": " ".join(f"site:{domain}" for domain in target.official_domains) if target else "",
              "model": model, "watch_terms": " ".join(watch_terms),
              "region": target.region if target else scope, "date": now}
    try:
        return template.format_map(values).strip()
    except (KeyError, ValueError) as exc:
        raise ValueError(f"query template 格式错误：{exc}") from exc


def _official_source(url: str, target: ResearchTarget | None) -> bool:
    if not target or not target.official_domains:
        return False
    hostname = (urlparse(url).hostname or "").lower()
    return any(hostname == domain or hostname.endswith("." + domain) for domain in target.official_domains)


def _profiles_from_rows(rows: list[dict[str, Any]], targets: list[ResearchTarget], observed_at: str, *, default_provider: str | None = None, default_region: str = "unknown") -> list[ModelProfile]:
    result = []
    for row in rows:
        provider = str(row.get("provider", row.get("vendor", default_provider or ""))).strip()
        model = str(row.get("model", row.get("model_id", row.get("name", "")))).strip()
        url = str(row.get("source_url", row.get("url", ""))).strip()
        description = str(row.get("description", row.get("summary", ""))).strip()
        strengths = row.get("strengths", row.get("features", row.get("specialties", [])))
        if isinstance(strengths, str):
            try: strengths = json.loads(strengths)
            except json.JSONDecodeError: strengths = [strengths]
        if isinstance(strengths, tuple): strengths = list(strengths)
        if not isinstance(strengths, list): strengths = []
        if not provider or not model or not url:
            continue
        missing_description = not description and not strengths
        if missing_description:
            description = "来源未提供模型描述或特长"
        target = next((t for t in targets if provider.casefold() in {t.provider.casefold(), *(alias.casefold() for alias in t.aliases)}), None)
        region = target.region if target else default_region if default_region in {"domestic", "foreign"} else "unknown"
        official = _official_source(url, target)
        result.append(ModelProfile(provider=provider, model=model, region=region, description=description or str(row.get("description", "")),
                                   strengths=tuple(str(x) for x in strengths), source_url=url, observed_at=str(row.get("observed_at", observed_at)),
                                   source_kind="official" if official else "aggregator",
                                   verification_status="verified" if official and not missing_description else "needs_review",
                                   modalities=row.get("modalities", row.get("modalities_supported", ())) or (),
                                   context_tokens=row.get("context_tokens", row.get("context_length"))))
    return result


def collect_once(catalog: Catalog, configs: list[McpSourceConfig], *, scope: str = "all", targets: list[ResearchTarget] | None = None) -> dict[str, Any]:
    if scope not in SCOPES:
        raise ValueError("scope 只能是 domestic、foreign 或 all")
    targets = targets or []
    catalog.upsert_research_targets(targets)
    active_targets = [target for target in targets if target.enabled and _target_in_scope(target, scope)]
    quote_total = changed_total = price_change_total = profiles_total = discovery_total = news_total = 0
    successes: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    search_queries_attempted = 0
    search_queries_skipped = 0
    for config in configs:
        if not config.enabled or not _source_in_scope(config, scope):
            continue
        source = TavilySearchSource(config) if config.transport == "tavily" else McpMarketSource(config)
        if config.mode == "pricing":
            price_targets: list[ResearchTarget | None] = (
                [target for target in active_targets if config.scope == "all" or config.scope == target.region]
                if config.iterate_targets else [None]
            )
            if not price_targets and config.iterate_targets:
                continue
            for target in price_targets:
                if search_queries_attempted >= MAX_SEARCH_REQUESTS_PER_REFRESH:
                    search_queries_skipped += 1
                    continue
                search_queries_attempted += 1
                try:
                    override: dict[str, Any] = {}
                    if target:
                        override[config.provider_argument] = target.provider
                        if config.query_templates and "pricing" in config.query_templates:
                            override[config.query_argument] = _format_query(config.query_templates["pricing"], target, scope)
                    if override or config.iterate_targets:
                        observed, raw_rows = source.collect_records(override or None, default_region=target.region if target else None)
                    else:
                        observed = source.collect()
                        raw_rows = [quote.to_dict() for quote in observed]
                    if not observed:
                        raise ValueError("MCP tool 返回了空报价列表")
                    if target:
                        accepted_providers = {target.provider.casefold(), *(alias.casefold() for alias in target.aliases)}
                        mismatches = [quote.provider for quote in observed if quote.provider.casefold() not in accepted_providers]
                        if mismatches:
                            raise ValueError(f"请求厂商 {target.provider}，但数据源返回其他厂商报价；拒绝错绑")
                        unverified_urls = [quote.source_url for quote in observed if not _official_source(quote.source_url, target)]
                        if unverified_urls:
                            raise ValueError(f"{target.provider} 报价来源不在配置的官方域名内，拒绝自动更新；需核对 official_domains")
                    latest_before = {(item.provider, item.model): item for item in catalog.latest_quotes()}
                    price_changes = []
                    for item in observed:
                        previous = latest_before.get((item.provider, item.model))
                        if previous and (previous.input_price_per_million != item.input_price_per_million or
                                         previous.output_price_per_million != item.output_price_per_million or
                                         previous.cached_input_price_per_million != item.cached_input_price_per_million):
                            price_changes.append({"provider": item.provider, "model": item.model,
                                                  "old_input": previous.input_price_per_million, "new_input": item.input_price_per_million,
                                                  "old_output": previous.output_price_per_million, "new_output": item.output_price_per_million,
                                                  "source_url": item.source_url, "observed_at": item.observed_at})
                    changed = catalog.upsert_quotes(observed)
                    for change in price_changes: catalog.record_price_change(config.name, change)
                    price_change_total += len(price_changes)
                    observed_at = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
                    profiles = _profiles_from_rows(raw_rows, targets, observed_at, default_provider=target.provider if target else config.provider,
                                                   default_region=target.region if target else config.scope)
                    profile_count = catalog.upsert_model_profiles(profiles)
                    quote_total += len(observed); changed_total += changed; profiles_total += profile_count
                    target_name = target.provider if target else config.provider
                    if target_name is None and len({x.provider for x in observed}) == 1:
                        target_name = observed[0].provider
                    catalog.record_market_check(config.name, scope, target_provider=target_name, status="verified",
                                                quote_count=len(observed), profile_count=profile_count, changed_count=len(price_changes))
                    successes.append({"source": config.name, "category": "pricing", "quotes": len(observed),
                                      "changed": changed, "price_changes": len(price_changes), "profiles": profile_count, "target": target_name})
                except Exception as exc:
                    message = str(exc)
                    if config.api_key_env and os.environ.get(config.api_key_env):
                        message = message.replace(os.environ[config.api_key_env], "[REDACTED]")
                    target_name = target.provider if target else config.provider
                    catalog.record_market_check(config.name, scope, target_provider=target_name, status="failed", error=message[:1000])
                    errors.append({"source": config.name, "target": target_name, "error": message[:1000]})
            continue

        templates = config.query_templates or {}
        jobs: list[tuple[str, ResearchTarget | None, str, str | None]] = []
        news_categories = [name for name in ("market_news", "news") if name in templates]
        news_regions = ("domestic", "foreign") if scope == "all" else (scope,)
        for category in news_categories:
            for news_region in news_regions:
                jobs.append((category, None, _format_query(templates[category], None, news_region, watch_terms=config.watch_terms), None))

        if templates and active_targets:
            for category, template in templates.items():
                if category in {"market_news", "news", "model_verification"}:
                    continue
                for target in active_targets:
                    jobs.append((category, target, _format_query(template, target, scope, watch_terms=config.watch_terms), None))

            known_quotes = catalog.latest_quotes()
            known_profiles = catalog.list_model_profiles(limit=2_000)
            verification_times = catalog.model_verification_times(config.name)
            per_target_models: dict[str, list[tuple[str, str, ResearchTarget]]] = {}
            for target in active_targets:
                provider_names = {target.provider.casefold(), *(alias.casefold() for alias in target.aliases)}
                models = {(quote.provider, quote.model) for quote in known_quotes
                          if quote.provider.casefold() in provider_names}
                models.update((str(profile["provider"]), str(profile["model"])) for profile in known_profiles
                              if str(profile.get("provider", "")).casefold() in provider_names)
                selected = sorted(models, key=lambda item: (verification_times.get((target.provider, item[1]), ""), item[1]))[:config.max_model_queries]
                per_target_models[target.provider] = [(model_provider, model_name, target) for model_provider, model_name in selected]

            # Round-robin across providers so a large catalog at one vendor cannot consume the whole allowance.
            round_index = 0
            while any(round_index < len(rows) for rows in per_target_models.values()):
                for target in active_targets:
                    rows = per_target_models[target.provider]
                    if round_index < len(rows):
                        model_provider, model_name, model_target = rows[round_index]
                        template = templates.get("model_verification")
                        if template:
                            jobs.append(("model_verification", model_target,
                                         _format_query(template, model_target, scope, model_name, config.watch_terms), model_name))
                round_index += 1
        elif templates and not active_targets and not news_categories:
            for category, template in templates.items():
                jobs.append((category, None, _format_query(template, None, scope, watch_terms=config.watch_terms), None))
        elif not templates:
            jobs.append((config.category, None, "", None))

        for category, target, query, model_name in jobs:
            if search_queries_attempted >= MAX_SEARCH_REQUESTS_PER_REFRESH:
                search_queries_skipped += 1
                continue
            search_queries_attempted += 1
            try:
                override = {config.query_argument: query} if query else None
                if config.transport == "tavily":
                    override = {**(override or {}), "topic": "news" if category == "market_news" else "general"}
                    if category == "market_news": override["time_range"] = "week"
                    elif category == "model_news": override["time_range"] = "month"
                    if target and category in {"pricing", "provider_research", "model_news", "model_verification"} and target.official_domains:
                        override["include_domains"] = list(target.official_domains)
                found = source.discover(override)
                annotated = []
                for item in found:
                    annotated.append({**item, "provider": target.provider if target else item.get("provider", ""),
                                      "model": model_name or item.get("model", ""),
                                      "region": target.region if target else ("domestic" if "domestic" in query.lower() else "foreign" if "foreign" in query.lower() else scope),
                    "category": category, "query": query})
                lead_scope = target.region if target else ("domestic" if "domestic" in query.lower() else "foreign" if "foreign" in query.lower() else scope)
                lead_source_name = f"{config.name}/{category}/{lead_scope}/{target.provider if target else 'market'}/{model_name or 'general'}"
                catalog.upsert_market_leads(lead_source_name, annotated, scope=lead_scope, category=category,
                                            provider=target.provider if target else None)
                catalog.upsert_discoveries(lead_source_name, annotated)
                count = len(annotated); discovery_total += count
                if category in {"market_news", "news"}: news_total += count
                profiles = _profiles_from_rows(found, targets, datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
                                               default_provider=target.provider if target else None,
                                               default_region=target.region if target else scope)
                if target and model_name:
                    for lead in annotated:
                        snippet = str(lead.get("snippet", "")).strip()
                        if snippet:
                            profiles.append(ModelProfile(provider=target.provider, model=model_name, region=target.region,
                                description=snippet, strengths=(), modalities=(), source_url=str(lead["url"]),
                                observed_at=datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
                                source_kind="official" if _official_source(str(lead["url"]), target) else "aggregator",
                                verification_status="needs_review"))
                profile_count = catalog.upsert_model_profiles(profiles)
                profiles_total += profile_count
                check_status = "search_only" if category in {"pricing", "verification", "model_verification"} and count else "found" if count else "no_results"
                catalog.record_market_check(config.name, lead_scope, target_provider=target.provider if target else None,
                                            target_model=model_name, status=check_status, profile_count=profile_count, lead_count=count)
                successes.append({"source": config.name, "category": category, "scope": target.region if target else scope,
                                  "target": target.provider if target else None, "leads": count, "profiles": profile_count})
            except Exception as exc:
                message = str(exc)
                if config.api_key_env and os.environ.get(config.api_key_env):
                    message = message.replace(os.environ[config.api_key_env], "[REDACTED]")
                catalog.record_market_check(config.name, scope, target_provider=target.provider if target else None, target_model=model_name,
                                            status="failed", error=message[:1000])
                errors.append({"source": config.name, "target": target.provider if target else None,
                               "model": model_name, "category": category, "error": message[:1000]})
    pruned = catalog.prune()
    return {"scope": scope, "targets_checked": len({str(x["target"]) for x in successes if x.get("target")}),
            "quotes_received": quote_total, "quotes_changed": changed_total, "price_changes": price_change_total,
            "profiles_updated": profiles_total,
            "search_budget": MAX_SEARCH_REQUESTS_PER_REFRESH,
            "search_queries_attempted": search_queries_attempted,
            "search_queries_skipped": search_queries_skipped,
            "research_requests_attempted": search_queries_attempted,
            "research_requests_skipped": search_queries_skipped,
            "research_request_budget_remaining": MAX_SEARCH_REQUESTS_PER_REFRESH - search_queries_attempted,
            "discoveries_received": discovery_total, "news_leads_received": news_total,
            "sources_ok": successes, "sources_failed": errors, "pruned_quotes": pruned[0]}

