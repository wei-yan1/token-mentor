from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .models import ModelQuote
from .sources.mcp import McpMarketSource, McpSourceConfig
from .storage import Catalog


def load_source_configs(path: str | Path) -> list[McpSourceConfig]:
    config_path = Path(path).expanduser().resolve()
    try:
        payload: Any = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取 MCP 配置 {config_path}: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("sources"), list):
        raise ValueError("MCP 配置格式应为 {\"sources\": [...]} ")
    if not all(isinstance(item, dict) for item in payload["sources"]):
        raise ValueError("sources 中每项必须是 JSON 对象")
    configs = [McpSourceConfig.from_dict(item) for item in payload["sources"]]
    if not configs:
        raise ValueError("MCP 配置中至少需要一个 source")
    names = [config.name for config in configs]
    if len(names) != len(set(names)):
        raise ValueError("MCP source name 不能重复")
    return configs


def collect_once(catalog: Catalog, configs: list[McpSourceConfig]) -> dict[str, Any]:
    quotes: list[ModelQuote] = []
    successes: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    discovery_count = 0
    for config in configs:
        try:
            if config.mode == "discovery":
                candidates = McpMarketSource(config).discover()
                if not candidates:
                    raise ValueError("MCP tool 没有返回搜索线索")
                catalog.upsert_discoveries(config.name, candidates)
                discovery_count += len(candidates)
                successes.append({"source": config.name, "quotes": 0, "discoveries": len(candidates)})
                continue
            observed = McpMarketSource(config).collect()
            if not observed:
                raise ValueError("MCP tool 返回了空报价列表")
            quotes.extend(observed)
            successes.append({"source": config.name, "quotes": len(observed)})
        except Exception as exc:  # one broken source must not discard other market data
            message = str(exc)
            if config.api_key_env:
                secret = os.environ.get(config.api_key_env)
                if secret:
                    message = message.replace(secret, "[REDACTED]")
            errors.append({"source": config.name, "error": message[:1_000]})
    if quotes:
        changed = catalog.upsert_quotes(quotes)
    else:
        changed = 0
    pruned = catalog.prune()
    return {
        "quotes_received": len(quotes),
        "discoveries_received": discovery_count,
        "quotes_changed": changed,
        "sources_ok": successes,
        "sources_failed": errors,
        "pruned_quotes": pruned[0],
    }

