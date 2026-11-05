from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import re
from string import Formatter
from typing import Any
from urllib.parse import urlparse

from token_mentor.models import ModelProfile, ModelQuote, ResearchTarget
from token_mentor.constraints.market import MAX_SEARCH_REQUESTS_PER_REFRESH
from token_mentor.tools.mcp import McpMarketSource, McpSourceConfig
from token_mentor.tools.tavily import TavilySearchSource
from token_mentor.tools.aliyun_models import AliyunModelCatalogSource
from token_mentor.tools.pricing import extract_markdown_quotes, normalize_quotes
from token_mentor.state.storage import Catalog

SCOPES = {"domestic", "foreign", "all"}
DEFAULT_MODEL_LANE_TERMS = (
    "text embedding vector model API pricing",
    "multimodal vision image video model API pricing",
    "speech recognition ASR transcription API pricing",
    "text to speech TTS voice model API pricing",
    "reranking reranker model API pricing",
    "3D generation model API pricing",
    "open weights self hosted inference deployment model",
)


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
        pricing_urls = row.get("pricing_urls", [])
        model_lanes = row.get("model_lanes", {})
        enabled = row.get("enabled", True)
        if (not isinstance(aliases, list) or not isinstance(domains, list) or
                not isinstance(pricing_urls, list) or any(not isinstance(url, str) for url in pricing_urls) or
                not isinstance(model_lanes, dict) or any(not isinstance(value, list) or any(not isinstance(term, str) for term in value) for value in model_lanes.values()) or
                not isinstance(enabled, bool)):
            raise ValueError("target aliases/official_domains/pricing_urls 必须是数组，model_lanes 必须是字符串数组映射，enabled 必须是布尔值")
        targets.append(ResearchTarget(provider=str(row.get("provider", "")), region=str(row.get("region", "")),
                                      aliases=tuple(aliases), official_domains=tuple(domains), enabled=enabled,
                                      pricing_urls=tuple(pricing_urls),
                                      model_lanes={str(key): tuple(value) for key, value in model_lanes.items()}))
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
              "task_terms": " ".join(term for terms in (target.model_lanes.values() if target and target.model_lanes else (DEFAULT_MODEL_LANE_TERMS,)) for term in terms),
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


def _native_quote_prices(quote: ModelQuote) -> tuple[str, float, float]:
    return (quote.source_currency,
            quote.source_input_price_per_million if quote.source_input_price_per_million is not None else quote.input_price_per_million,
            quote.source_output_price_per_million if quote.source_output_price_per_million is not None else quote.output_price_per_million)


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
                                   context_tokens=row.get("context_tokens", row.get("context_length")),
                                   task_lanes=tuple(row.get("task_lanes", ())),
                                   capability_tags=tuple(row.get("capability_tags", row.get("capabilities", ())) or ()),
                                   features=tuple(row.get("features", ()) or ())))
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
    research_requests_attempted = 0
    extraction_requests_skipped = 0
    news_regions_count = 2 if scope == "all" else 1
    news_reserve_remaining = 0
    for configured_source in configs:
        if not configured_source.enabled or configured_source.mode != "discovery" or not _source_in_scope(configured_source, scope):
            continue
        categories = configured_source.query_templates or {}
        news_count = sum(1 for category in ("market_news", "news") if category in categories)
        if not categories and configured_source.category in {"market_news", "news"}:
            news_count = 1
        news_reserve_remaining += news_count * news_regions_count
    news_reserve_remaining = min(news_reserve_remaining, MAX_SEARCH_REQUESTS_PER_REFRESH)
    for config in configs:
        if not config.enabled or not _source_in_scope(config, scope):
            continue
        source = (TavilySearchSource(config) if config.transport == "tavily"
                  else AliyunModelCatalogSource(config) if config.transport == "aliyun_models"
                  else McpMarketSource(config))
        if config.mode == "pricing":
            price_targets: list[ResearchTarget | None] = (
                [target for target in active_targets if config.scope == "all" or config.scope == target.region]
                if config.iterate_targets else [None]
            )
            if not price_targets and config.iterate_targets:
                continue
            for target in price_targets:
                if research_requests_attempted >= MAX_SEARCH_REQUESTS_PER_REFRESH - news_reserve_remaining:
                    search_queries_skipped += 1
                    continue
                search_queries_attempted += 1
                research_requests_attempted += 1
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
                        if previous and (_native_quote_prices(previous) != _native_quote_prices(item) or
                                         previous.cached_input_price_per_million != item.cached_input_price_per_million):
                            price_changes.append({"provider": item.provider, "model": item.model,
                                                  "old_input": _native_quote_prices(previous)[1], "new_input": _native_quote_prices(item)[1],
                                                  "old_output": _native_quote_prices(previous)[2], "new_output": _native_quote_prices(item)[2],
                                                  "currency": _native_quote_prices(item)[0],
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

        if config.category == "model_catalog" and config.transport == "aliyun_models":
            jobs.append(("model_catalog", None, "", None))
        elif templates and active_targets:
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
            is_news_query = category in {"market_news", "news"}
            available_limit = MAX_SEARCH_REQUESTS_PER_REFRESH if is_news_query else MAX_SEARCH_REQUESTS_PER_REFRESH - news_reserve_remaining
            request_reservation = max(1, int(getattr(source, "max_request_count", 1)))
            if research_requests_attempted + request_reservation > available_limit:
                search_queries_skipped += 1
                continue
            search_queries_attempted += 1
            research_requests_attempted += request_reservation
            if is_news_query and news_reserve_remaining:
                news_reserve_remaining -= 1
            try:
                override = {config.query_argument: query} if query else None
                if config.transport == "tavily":
                    override = {**(override or {}), "topic": "news" if category == "market_news" else "general"}
                    if category in {"pricing", "task_pricing", "model_verification"}:
                        override["include_raw_content"] = True
                    if category == "market_news": override["time_range"] = "week"
                    elif category == "model_news": override["time_range"] = "month"
                    if target and category in {"pricing", "task_pricing", "provider_research", "model_news", "model_verification"} and target.official_domains:
                        override["include_domains"] = list(target.official_domains)
                found = source.discover(override)
                actual_requests = int(getattr(source, "last_request_count", request_reservation))
                research_requests_attempted += actual_requests - request_reservation
                request_reservation = actual_requests
                extracted_quotes: list[ModelQuote] = []
                pricing_error: str | None = None
                latest_before: dict[tuple[str, str], ModelQuote] = {}
                if config.transport == "tavily" and category in {"pricing", "task_pricing", "model_verification"} and target:
                    candidates: list[dict[str, Any]] = []
                    for item in found:
                        if not _official_source(str(item.get("url", "")), target):
                            continue
                        page_content = item.get("raw_content") or item.get("snippet", "")
                        candidates.extend(extract_markdown_quotes(
                            page_content,
                            provider=target.provider,
                            source_url=str(item.get("url", "")),
                            region=target.region,
                        ))
                    if not candidates:
                        extract_urls = list(target.pricing_urls) if category == "pricing" else []
                    else:
                        extract_urls = []
                    if ((category == "task_pricing" or (category == "pricing" and not target.pricing_urls))
                            and not candidates):
                        extract_urls = [str(item.get("url", "")) for item in found
                            if _official_source(str(item.get("url", "")), target)
                            and re.search(r"pricing|price|billing|model-pricing|embedding|speech|audio|video|rerank|计费|价格|语音|向量|多模态|视频", f"{item.get('title', '')} {item.get('url', '')}", re.I)]
                        if category == "task_pricing":
                            configured = {url.casefold() for url in target.pricing_urls}
                            extract_urls = [url for url in extract_urls if url.casefold() not in configured]
                    extract_urls = list(dict.fromkeys(url for url in extract_urls if _official_source(url, target)))
                    if extract_urls and research_requests_attempted < MAX_SEARCH_REQUESTS_PER_REFRESH - news_reserve_remaining:
                        research_requests_attempted += 1
                        try:
                            extracted_pages = source.extract_pages(extract_urls[:3])
                            for page in extracted_pages:
                                candidates.extend(extract_markdown_quotes(page["raw_content"],
                                    provider=target.provider, source_url=page["url"], region=target.region))
                        except Exception as exc:
                            pricing_error = f"官方价格页提取失败：{str(exc)[:300]}"
                    elif extract_urls:
                        extraction_requests_skipped += 1
                    if category == "model_verification" and model_name:
                        candidates = [row for row in candidates
                                      if str(row.get("model", "")).casefold() == model_name.casefold()]
                    try:
                        extracted_quotes = normalize_quotes(candidates, timeout_seconds=config.timeout_seconds) if candidates else []
                        if found and not candidates:
                            pricing_error = pricing_error or "官方搜索结果未包含可识别的每百万 token 模型价格表"
                    except Exception as exc:
                        pricing_error = str(exc)[:500]
                    if extracted_quotes:
                        latest_before = {(item.provider, item.model): item for item in catalog.latest_quotes()}
                        price_changes = []
                        for item in extracted_quotes:
                            if not _official_source(item.source_url, target):
                                continue
                            previous = latest_before.get((item.provider, item.model))
                            if previous and (_native_quote_prices(previous) != _native_quote_prices(item) or
                                             previous.cached_input_price_per_million != item.cached_input_price_per_million):
                                price_changes.append({"provider": item.provider, "model": item.model,
                                    "old_input": _native_quote_prices(previous)[1], "new_input": _native_quote_prices(item)[1],
                                    "old_output": _native_quote_prices(previous)[2], "new_output": _native_quote_prices(item)[2],
                                    "currency": _native_quote_prices(item)[0],
                                    "source_url": item.source_url, "observed_at": item.observed_at})
                        extracted_quotes = [item for item in extracted_quotes if _official_source(item.source_url, target)]
                        changed_quotes = catalog.upsert_quotes(extracted_quotes)
                        for change in price_changes:
                            catalog.record_price_change(config.name, change)
                        quote_total += len(extracted_quotes)
                        changed_total += changed_quotes
                        price_change_total += len(price_changes)
                if category == "model_catalog":
                    profiles = [ModelProfile(
                        provider=str(row["provider"]), model=str(row["model"]), region=str(row.get("region", "domestic")),
                        description=str(row.get("description", "")), strengths=tuple(row.get("strengths", ())),
                        source_url=str(row["source_url"]), observed_at=str(row["observed_at"]),
                        source_kind="official", verification_status="verified",
                        modalities=tuple(row.get("modalities", ())), context_tokens=row.get("context_tokens"),
                        task_lanes=tuple(row.get("task_lanes", ())), capability_tags=tuple(row.get("capability_tags", ())),
                        features=tuple(row.get("features", ())),
                        price_details=row.get("price_details", []),
                    ) for row in found]
                    profile_count = catalog.upsert_model_profiles(profiles)
                    profiles_total += profile_count
                    catalog.record_market_check(config.name, "domestic", status="verified" if profile_count else "no_results",
                                                profile_count=profile_count, lead_count=0)
                    successes.append({"source": config.name, "category": category, "profiles": profile_count,
                                      "requests": actual_requests})
                    continue
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
                check_status = ("verified" if extracted_quotes else "search_only") if category in {"pricing", "task_pricing", "verification", "model_verification"} and count else "found" if count else "no_results"
                catalog.record_market_check(config.name, lead_scope, target_provider=target.provider if target else None,
                                            target_model=model_name, status=check_status, quote_count=len(extracted_quotes),
                                            profile_count=profile_count, lead_count=count,
                                            changed_count=sum(1 for item in extracted_quotes
                                                              if (item.provider, item.model) in latest_before and
                                                              (_native_quote_prices(latest_before[(item.provider, item.model)]) != _native_quote_prices(item))),
                                            error=pricing_error)
                successes.append({"source": config.name, "category": category, "scope": target.region if target else scope,
                                  "target": target.provider if target else None, "leads": count, "quotes": len(extracted_quotes),
                                  "profiles": profile_count, "pricing_error": pricing_error})
            except Exception as exc:
                actual_requests = int(getattr(source, "last_request_count", request_reservation))
                research_requests_attempted += actual_requests - request_reservation
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
            "research_requests_attempted": research_requests_attempted,
            "research_requests_skipped": search_queries_skipped + extraction_requests_skipped,
            "research_request_budget_remaining": MAX_SEARCH_REQUESTS_PER_REFRESH - research_requests_attempted,
            "search_budget_remaining": MAX_SEARCH_REQUESTS_PER_REFRESH - research_requests_attempted,
            "discoveries_received": discovery_total, "news_leads_received": news_total,
            "sources_ok": successes, "sources_failed": errors, "pruned_quotes": pruned[0]}

