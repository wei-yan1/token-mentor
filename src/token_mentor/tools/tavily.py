"""Direct Tavily Search API adapter used by the daily market research job."""
from __future__ import annotations

import json
import os
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import urlparse

from token_mentor.tools.mcp import MAX_RESPONSE_BYTES, McpSourceConfig, _safe_public_url
from token_mentor.tools.pricing import extract_markdown_quotes, normalize_quotes


class TavilySearchSource:
    def __init__(self, config: McpSourceConfig) -> None:
        if config.transport != "tavily":
            raise ValueError("TavilySearchSource 需要 transport=tavily")
        self.config = config

    def _search(self, arguments_override: dict[str, Any] | None = None, *, raw_content: bool = False) -> list[dict[str, str]]:
        token = os.environ.get(self.config.api_key_env or "")
        if not token:
            raise ValueError(f"未设置环境变量 {self.config.api_key_env or 'TAVILY_API_KEY'}")
        arguments = dict(self.config.arguments or {})
        if arguments_override:
            arguments.update(arguments_override)
        if raw_content:
            arguments["include_raw_content"] = True
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
            row = {"url": url, "title": title[:240], "snippet": snippet[:4_000],
                   "published_at": str(item.get("published_date", ""))[:80]}
            extracted = str(item.get("raw_content", "")).strip()
            if extracted:
                row["raw_content"] = extracted[:200_000]
            rows.append(row)
        return rows

    def discover(self, arguments_override: dict[str, Any] | None = None) -> list[dict[str, str]]:
        raw = bool((arguments_override or {}).get("include_raw_content"))
        return self._search(arguments_override, raw_content=raw)

    def extract_pages(self, urls: list[str]) -> list[dict[str, str]]:
        """Fetch clean markdown for a small set of already-discovered official pages."""
        token = os.environ.get(self.config.api_key_env or "")
        if not token:
            raise ValueError(f"未设置环境变量 {self.config.api_key_env or 'TAVILY_API_KEY'}")
        unique_urls = list(dict.fromkeys(url for url in urls
            if _safe_public_url(url) and urlparse(url).scheme == "https"))[:5]
        if not unique_urls:
            return []
        endpoint = "https://api.tavily.com/extract"
        request = Request(endpoint, data=json.dumps({"urls": unique_urls, "extract_depth": "advanced",
            "format": "markdown"}).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "application/json",
                     "Authorization": f"Bearer {token}"}, method="POST")
        try:
            with urlopen(request, timeout=self.config.timeout_seconds) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            raise RuntimeError(f"Tavily Extract API HTTP {exc.code}") from exc
        except (URLError, TimeoutError) as exc:
            raise RuntimeError(f"Tavily Extract API 请求失败：{type(exc).__name__}") from exc
        if len(raw) > MAX_RESPONSE_BYTES:
            raise RuntimeError("Tavily Extract API 响应超过 4 MiB 上限")
        try:
            result = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Tavily Extract API 返回无效 JSON") from exc
        if not isinstance(result, dict) or not isinstance(result.get("results"), list):
            raise RuntimeError("Tavily Extract API 返回缺少 results 数组")
        rows = []
        for item in result["results"]:
            if not isinstance(item, dict):
                continue
            url, content = str(item.get("url", "")).strip(), str(item.get("raw_content", "")).strip()
            if url in unique_urls and _safe_public_url(url) and content:
                rows.append({"url": url, "raw_content": content[:200_000]})
        return rows

    @staticmethod
    def _matches_domains(url: str, domains: Any) -> bool:
        hostname = (urlparse(url).hostname or "").lower()
        return any(
            isinstance(domain, str)
            and (hostname == domain.lower() or hostname.endswith("." + domain.lower()))
            for domain in domains or []
        )

    def collect_records(
        self,
        arguments_override: dict[str, Any] | None = None,
        *,
        default_region: str | None = None,
    ) -> tuple[list[Any], list[dict[str, Any]]]:
        if self.config.mode != "pricing":
            raise ValueError("Tavily 仅在 mode=pricing 时可转换官方价格表")
        arguments_override = dict(arguments_override or {})
        provider = str(arguments_override.pop("provider", self.config.provider or "")).strip()
        region = default_region or (self.config.scope if self.config.scope in {"domestic", "foreign"} else "unknown")
        domains = arguments_override.get("include_domains", [])
        found = self._search(arguments_override, raw_content=True)
        official = [row for row in found if self._matches_domains(str(row["url"]), domains)] if domains else []
        raw_quotes: list[dict[str, Any]] = []
        for row in official:
            page = row.get("raw_content") or row.get("snippet", "")
            raw_quotes.extend(extract_markdown_quotes(
                page,
                provider=provider,
                source_url=str(row["url"]),
                region=region,
            ))
        return normalize_quotes(raw_quotes, timeout_seconds=self.config.timeout_seconds), found
