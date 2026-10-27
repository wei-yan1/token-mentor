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

from ..models import ModelQuote


PROTOCOL_VERSION = "2025-03-26"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
SENSITIVE_QUERY_KEYS = {"token", "api_key", "apikey", "access_token", "secret", "signature", "sig", "authorization", "key", "code"}


@dataclass(frozen=True, slots=True)
class McpSourceConfig:
    name: str
    endpoint: str
    tool: str
    mode: str = "pricing"
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
        parsed_endpoint = urlparse(endpoint)
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

    def collect(self) -> list[ModelQuote]:
        result = self.call()
        return [
            ModelQuote.from_payload(row, default_provider=self.config.provider)
            for row in _extract_quote_rows(result)
        ]

    def discover(self) -> list[dict[str, str]]:
        return _extract_discovery_rows(self.call())

    def call(self) -> dict[str, Any]:
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
        return client.call_tool(self.config.tool, self.config.arguments or {})


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
                rows.append({"url": url, "title": title or url, "snippet": snippet})
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

