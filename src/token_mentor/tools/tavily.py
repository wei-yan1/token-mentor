"""Direct Tavily Search API adapter used by the daily market research job."""
from __future__ import annotations

import json
import os
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from token_mentor.tools.mcp import MAX_RESPONSE_BYTES, McpSourceConfig, _safe_public_url


class TavilySearchSource:
    def __init__(self, config: McpSourceConfig) -> None:
        if config.transport != "tavily" or config.mode != "discovery":
            raise ValueError("TavilySearchSource 需要 transport=tavily 的 discovery 配置")
        self.config = config

    def discover(self, arguments_override: dict[str, Any] | None = None) -> list[dict[str, str]]:
        token = os.environ.get(self.config.api_key_env or "")
        if not token:
            raise ValueError(f"未设置环境变量 {self.config.api_key_env or 'TAVILY_API_KEY'}")
        arguments = dict(self.config.arguments or {})
        if arguments_override:
            arguments.update(arguments_override)
        query = arguments.get(self.config.query_argument)
        if not isinstance(query, str) or not query.strip():
            raise ValueError("Tavily Search query 不能为空")
        payload = {key: value for key, value in arguments.items() if key not in {"scope", "provider"}}
        request = Request(self.config.endpoint, data=json.dumps(payload).encode("utf-8"),
                          headers={"Content-Type": "application/json", "Accept": "application/json",
                                   "Authorization": f"Bearer {token}"}, method="POST")
        try:
            with urlopen(request, timeout=self.config.timeout_seconds) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            # Do not include Tavily response bodies: providers can echo request metadata.
            raise RuntimeError(f"Tavily Search API HTTP {exc.code}") from exc
        except (URLError, TimeoutError) as exc:
            raise RuntimeError(f"Tavily Search API 请求失败：{type(exc).__name__}") from exc
        if len(raw) > MAX_RESPONSE_BYTES:
            raise RuntimeError("Tavily Search API 响应超过 4 MiB 上限")
        try:
            result = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Tavily Search API 返回无效 JSON") from exc
        if not isinstance(result, dict) or not isinstance(result.get("results"), list):
            raise RuntimeError("Tavily Search API 返回缺少 results 数组")
        rows: list[dict[str, str]] = []
        for item in result["results"]:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url", "")).strip()
            title = str(item.get("title", "")).strip()
            snippet = str(item.get("content", item.get("raw_content", ""))).strip()
            if not _safe_public_url(url) or not (title or snippet):
                continue
            rows.append({"url": url, "title": title[:240], "snippet": snippet[:4_000],
                         "published_at": str(item.get("published_date", ""))[:80]})
        return rows
