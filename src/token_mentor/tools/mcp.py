from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
import re
from typing import Any
from urllib.parse import parse_qsl, urlparse
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from token_mentor.models import ModelQuote


PROTOCOL_VERSION = "2025-03-26"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
SENSITIVE_QUERY_KEYS = {"token", "api_key", "apikey", "access_token", "secret", "signature", "sig", "authorization", "key", "code"}


@dataclass(frozen=True, slots=True)
class McpSourceConfig:
    name: str
    endpoint: str
    tool: str
    mode: str = "pricing"
    enabled: bool = True
    transport: str = "mcp"
    scope: str = "all"
    category: str = "pricing"
    query_argument: str = "query"
    query_templates: dict[str, str] | None = None
    watch_terms: tuple[str, ...] = ()
    iterate_targets: bool = False
    max_model_queries: int = 100
    provider_argument: str = "provider"
    provider: str | None = None
    arguments: dict[str, Any] | None = None
    api_key_env: str | None = None
    auth_header: str = "Authorization"
    auth_scheme: str = "Bearer"
    timeout_seconds: float = 20.0

    @classmethod
    def from_dict(cls, item: dict[str, Any]) -> "McpSourceConfig":
        if not isinstance(item, dict):
            raise ValueError("MCP source 配置必须是 JSON 对象")
        name = str(item.get("name", "")).strip()
        endpoint = str(item.get("endpoint", "")).strip()
        tool = str(item.get("tool", "")).strip()
        if not name or not endpoint or not tool:
            raise ValueError("MCP source 必须配置 name、endpoint、tool")
        mode = str(item.get("mode", "pricing")).strip().lower()
        if mode not in {"pricing", "discovery"}:
            raise ValueError(f"{name}: mode 只能是 pricing 或 discovery")
        enabled = item.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError(f"{name}: enabled 必须是布尔值")
        transport = str(item.get("transport", "mcp")).strip().lower()
        if transport not in {"mcp", "tavily", "aliyun_models"}:
            raise ValueError(f"{name}: transport 只能是 mcp、tavily 或 aliyun_models")
        parsed_endpoint = urlparse(endpoint)
        if transport == "tavily" and (parsed_endpoint.hostname != "api.tavily.com" or parsed_endpoint.path.rstrip("/") != "/search"):
            raise ValueError(f"{name}: Tavily transport endpoint 必须是 https://api.tavily.com/search")
        local_hosts = {"localhost", "127.0.0.1", "::1"}
        if not parsed_endpoint.hostname:
            raise ValueError(f"{name}: MCP endpoint 必须包含主机名")
        if parsed_endpoint.username or parsed_endpoint.password:
            raise ValueError(f"{name}: 不要把凭据放进 MCP endpoint URL")
        if any(
            key.lower() in SENSITIVE_QUERY_KEYS
            for key, _ in parse_qsl(parsed_endpoint.query, keep_blank_values=True)
        ):
            raise ValueError(f"{name}: 不要把凭据放进 MCP endpoint URL")
        if parsed_endpoint.scheme != "https" and not (parsed_endpoint.scheme == "http" and parsed_endpoint.hostname in local_hosts):
            raise ValueError(f"{name}: 远程 MCP endpoint 必须使用 HTTPS")
        scope = str(item.get("scope", "all")).strip().lower()
        if scope not in {"domestic", "foreign", "all"}:
            raise ValueError(f"{name}: scope 只能是 domestic、foreign 或 all")
        category = str(item.get("category", "pricing" if mode == "pricing" else "news")).strip().lower()
        if category not in {"pricing", "news", "discovery", "verification", "model_catalog"}:
            raise ValueError(f"{name}: category 无效")
        query_argument = str(item.get("query_argument", "query")).strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", query_argument):
            raise ValueError(f"{name}: query_argument 格式无效")
        query_templates = item.get("query_templates", {})
        if not isinstance(query_templates, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in query_templates.items()):
            raise ValueError(f"{name}: query_templates 必须是字符串映射")
        watch_terms = item.get("watch_terms", [])
        if not isinstance(watch_terms, list) or any(not isinstance(term, str) for term in watch_terms):
            raise ValueError(f"{name}: watch_terms 必须是字符串数组")
        if len(watch_terms) > 100 or any(len(term) > 100 for term in watch_terms):
            raise ValueError(f"{name}: watch_terms 超出长度限制")
        iterate_targets = item.get("iterate_targets", False)
        if not isinstance(iterate_targets, bool):
            raise ValueError(f"{name}: iterate_targets 必须是布尔值")
        try:
            max_model_queries = int(item.get("max_model_queries", 100))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name}: max_model_queries 必须是整数") from exc
        if max_model_queries < 0 or max_model_queries > 2_000:
            raise ValueError(f"{name}: max_model_queries 必须在 0 到 2000 之间")
        provider_argument = str(item.get("provider_argument", "provider")).strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", provider_argument):
            raise ValueError(f"{name}: provider_argument 格式无效")
        arguments = item.get("arguments", {})
        if not isinstance(arguments, dict):
            raise ValueError(f"{name}: arguments 必须是 JSON 对象")
        auth_header = str(item.get("auth_header", "Authorization")).strip()
        if not re.fullmatch(r"[A-Za-z0-9-]{1,64}", auth_header):
            raise ValueError(f"{name}: auth_header 格式无效")
        auth_scheme = str(item.get("auth_scheme", "Bearer")).strip()
        if auth_scheme and not re.fullmatch(r"[A-Za-z0-9._~+/-]{1,64}", auth_scheme):
            raise ValueError(f"{name}: auth_scheme 格式无效")
        try:
            timeout = float(item.get("timeout_seconds", 20))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name}: timeout_seconds 必须是数字") from exc
        if not math.isfinite(timeout) or timeout < 0.1 or timeout > 300:
            raise ValueError(f"{name}: timeout_seconds 需在 0.1 到 300 之间")
        return cls(
            name=name,
            endpoint=endpoint,
            tool=tool,
            mode=mode,
            enabled=enabled,
            transport=transport,
            scope=scope,
            category=category,
            query_argument=query_argument,
            query_templates=query_templates,
            watch_terms=tuple(term.strip() for term in watch_terms if term.strip()),
            iterate_targets=iterate_targets,
            max_model_queries=max_model_queries,
            provider_argument=provider_argument,
            provider=str(item["provider"]).strip() if item.get("provider") else None,
            arguments=arguments,
            api_key_env=str(item["api_key_env"]).strip() if item.get("api_key_env") else None,
            auth_header=auth_header,
            auth_scheme=auth_scheme,
            timeout_seconds=timeout,
        )


class McpHttpClient:
    """Minimal MCP Streamable HTTP client using only the Python standard library."""

    def __init__(
        self,
        endpoint: str,
        *,
        token: str | None = None,
        auth_header: str = "Authorization",
        auth_scheme: str = "Bearer",
        timeout_seconds: float = 20.0,
    ) -> None:
        self.endpoint = endpoint
        self.token = token
        self.auth_header = auth_header
        self.auth_scheme = auth_scheme
        self.timeout_seconds = timeout_seconds
        self.session_id: str | None = None
        self._request_id = 0
        self._initialized = False

    def _post(self, payload: dict[str, Any], *, allow_empty: bool = False) -> dict[str, Any] | None:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
        }
        if self.token:
            headers[self.auth_header] = f"{self.auth_scheme} {self.token}".strip()
        if self.session_id:
            headers["MCP-Session-Id"] = self.session_id
        request = Request(
            self.endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                self.session_id = response.headers.get("MCP-Session-Id", self.session_id)
                raw_body = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw_body) > MAX_RESPONSE_BYTES:
                    raise RuntimeError("MCP 响应超过 4 MiB 上限")
                body = raw_body.decode("utf-8", errors="replace")
                content_type = response.headers.get("Content-Type", "")
        except HTTPError as exc:
            raise RuntimeError(f"MCP HTTP {exc.code}") from exc
        except (URLError, TimeoutError) as exc:
            raise RuntimeError(f"MCP endpoint 请求失败：{exc}") from exc
        if not body.strip():
            if allow_empty:
                return None
            raise RuntimeError("MCP endpoint 返回了空响应")
        if "text/event-stream" in content_type:
            messages = [line[6:].strip() for line in body.splitlines() if line.startswith("data: ")]
            for message in reversed(messages):
                if message and message != "[DONE]":
                    try:
                        return json.loads(message)
                    except json.JSONDecodeError:
                        continue
            raise RuntimeError("MCP SSE 响应中没有 JSON-RPC 消息")
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise RuntimeError("MCP endpoint 返回的不是 JSON-RPC 或 SSE") from exc

    def _rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._request_id += 1
        response = self._post({
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": method,
            "params": params or {},
        })
        assert response is not None
        if response.get("error"):
            error = response["error"]
            raise RuntimeError(f"MCP {method} 失败：{error.get('message', error)}")
        if response.get("id") != self._request_id:
            raise RuntimeError("MCP JSON-RPC 响应 ID 不匹配")
        return response

    def call_tool(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if not self._initialized:
            response = self._rpc("initialize", {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "token-mentor", "version": "0.1.0"},
            })
            result = response.get("result", {})
            if result.get("protocolVersion") != PROTOCOL_VERSION:
                raise RuntimeError(f"MCP 协议版本不兼容：{result.get('protocolVersion')}")
            self._post({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}, allow_empty=True)
            self._initialized = True
        response = self._rpc("tools/call", {"name": tool, "arguments": arguments})
        result = response.get("result", {})
        if result.get("isError"):
            raise RuntimeError(f"MCP tool {tool} 返回错误：{_brief_result(result)}")
        return result


def _brief_result(result: Any) -> str:
    try:
        return json.dumps(result, ensure_ascii=False)[:500]
    except (TypeError, ValueError):
        return str(result)[:500]


def _extract_quote_rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[Any] = []
    if "structuredContent" in result:
        candidates.append(result["structuredContent"])
    candidates.extend(block.get("text") for block in result.get("content", []) if isinstance(block, dict) and isinstance(block.get("text"), str))
    for candidate in candidates:
        if isinstance(candidate, str):
            try:
                candidate = json.loads(candidate)
            except json.JSONDecodeError:
                continue
        if isinstance(candidate, list) and all(isinstance(row, dict) for row in candidate):
            return candidate
        if isinstance(candidate, dict):
            for key in ("quotes", "models", "results", "data"):
                rows = candidate.get(key)
                if isinstance(rows, list) and all(isinstance(row, dict) for row in rows):
                    return rows
            if any(key in candidate for key in ("model", "model_id")):
                return [candidate]
    raise ValueError(
        "MCP tool 必须返回结构化 JSON 报价数组（quotes/model、输入输出单价、来源 URL、更新时间）；"
        "原始网页文本无法可靠地直接作为价格数据"
    )


class McpMarketSource:
    def __init__(self, config: McpSourceConfig) -> None:
        self.config = config

    def collect_records(self, arguments_override: dict[str, Any] | None = None, *, default_region: str | None = None) -> tuple[list[ModelQuote], list[dict[str, Any]]]:
        result = self.call(arguments_override)
        rows = _extract_quote_rows(result)
        inherited_region = default_region or (self.config.scope if self.config.scope in {"domestic", "foreign"} else "unknown")
        from token_mentor.tools.pricing import normalize_quote_currency

        normalized_rows = [normalize_quote_currency(row, timeout_seconds=self.config.timeout_seconds) for row in rows]
        quotes = [ModelQuote.from_payload(row, default_provider=self.config.provider, default_region=inherited_region)
                  for row in normalized_rows]
        return quotes, rows

    def collect(self, arguments_override: dict[str, Any] | None = None) -> list[ModelQuote]:
        return self.collect_records(arguments_override)[0]

    def discover(self, arguments_override: dict[str, Any] | None = None) -> list[dict[str, str]]:
        return _extract_discovery_rows(self.call(arguments_override))

    def call(self, arguments_override: dict[str, Any] | None = None) -> dict[str, Any]:
        token = None
        if self.config.api_key_env:
            token = os.environ.get(self.config.api_key_env)
            if not token:
                raise ValueError(f"未设置环境变量 {self.config.api_key_env}")
        client = McpHttpClient(
            self.config.endpoint,
            token=token,
            auth_header=self.config.auth_header,
            auth_scheme=self.config.auth_scheme,
            timeout_seconds=self.config.timeout_seconds,
        )
        arguments = dict(self.config.arguments or {})
        if arguments_override:
            arguments.update(arguments_override)
        return client.call_tool(self.config.tool, arguments)


def _extract_discovery_rows(result: dict[str, Any]) -> list[dict[str, str]]:
    candidates: list[Any] = []
    if "structuredContent" in result:
        candidates.append(result["structuredContent"])
    candidates.extend(
        block.get("text") for block in result.get("content", [])
        if isinstance(block, dict) and isinstance(block.get("text"), str)
    )
    for candidate in candidates:
        if isinstance(candidate, str):
            try:
                candidate = json.loads(candidate)
            except json.JSONDecodeError:
                parsed = _discovery_rows_from_text(candidate)
                if parsed:
                    return parsed
                continue
        if isinstance(candidate, dict):
            candidate = candidate.get("results", candidate.get("items", []))
        if not isinstance(candidate, list):
            continue
        rows = []
        for row in candidate:
            if not isinstance(row, dict):
                continue
            url = str(row.get("url", row.get("source_url", ""))).strip()
            title = str(row.get("title", row.get("name", ""))).strip()
            snippet = str(row.get("content", row.get("snippet", row.get("description", "")))).strip()
            if _safe_public_url(url) and (title or snippet):
                item = {"url": url, "title": title or url, "snippet": snippet}
                published = row.get("published_at", row.get("published_date"))
                if isinstance(published, str): item["published_at"] = published[:80]
                for key, aliases in {"provider": ("provider", "vendor"), "model": ("model", "model_id"),
                                     "description": ("description", "summary"), "strengths": ("strengths", "features", "specialties"),
                                     "modalities": ("modalities", "modalities_supported")}.items():
                    value = next((row.get(alias) for alias in aliases if row.get(alias) is not None), None)
                    if isinstance(value, str): item[key] = value[:5_000]
                    elif key == "strengths" and isinstance(value, list): item[key] = json.dumps([str(x)[:300] for x in value[:30]], ensure_ascii=False)
                rows.append(item)
        if rows:
            return rows
    raise ValueError("MCP 搜索结果缺少结构化的 title/url/snippet；发现线索不会直接作为已验证价格")


def _discovery_rows_from_text(text: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for line in text.splitlines():
        markdown = re.search(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", line)
        raw_url = re.search(r"https?://[^\s<>\]\[)]+", line)
        if markdown:
            title, url = markdown.group(1).strip(), markdown.group(2).strip().rstrip(".,;:")
        elif raw_url:
            url = raw_url.group(0).rstrip(".,;:")
            title = line.strip(" -*#\t")[:240] or url
        else:
            continue
        if title.lower().startswith("http"):
            title = url
        if _safe_public_url(url):
            rows.append({"title": title[:240], "url": url, "snippet": line.strip()[:4_000]})
    return rows


def _safe_public_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            return False
        return not any(key.lower() in SENSITIVE_QUERY_KEYS for key, _ in parse_qsl(parsed.query, keep_blank_values=True))
    except ValueError:
        return False

