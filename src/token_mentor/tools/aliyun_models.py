"""Alibaba Cloud Model Studio workspace model directory adapter."""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from token_mentor.tools.mcp import MAX_RESPONSE_BYTES, McpSourceConfig

MODEL_CATALOG_DOC = "https://help.aliyun.com/en/model-studio/list-models"
PROVIDER_NAMES = {
    "qwen": "阿里云",
    "qwen-domain-model": "阿里云",
    "aliyun-bailian": "阿里云",
    "alibaba-cloud-modelstudio": "阿里云",
    "deepseek": "DeepSeek",
    "zhipu-ai": "智谱AI",
    "moonshot-ai": "月之暗面",
    "mini-max": "MiniMax",
    "xiaomi": "小米",
}
CAPABILITY_LANES = {
    "TG": ("general", "coding", "rewrite", "summarization", "translation"),
    "REASONING": ("reasoning", "coding"),
    "VU": ("multimodal", "video_understanding"),
    "IG": ("image_generation",),
    "VG": ("video_generation",),
    "ASR": ("speech_recognition",),
    "TTS": ("speech_generation",),
    "TR": ("embeddings",),
    "ME": ("embeddings", "multimodal"),
    "MULTIMODAL-OMNI": ("multimodal", "speech_recognition", "speech_generation"),
    "REALTIME-OMNI": ("multimodal", "speech_recognition", "speech_generation"),
    "REALTIME-TEXT-TO-SPEECH": ("speech_generation",),
    "REALTIME-ASR": ("speech_recognition",),
    "REALTIME-AUDIO-TRANSLATE": ("speech_recognition", "translation"),
    "3D-GENERATION": ("3d_generation",),
    "REALTIME-CHATTING": ("multimodal", "general"),
}


def _rows(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], int | None]:
    data: Any = payload.get("output", payload.get("data", payload))
    if isinstance(data, dict):
        rows = next((data[key] for key in ("models", "items", "results", "list") if isinstance(data.get(key), list)), [])
        total = data.get("total", data.get("total_count"))
    elif isinstance(data, list):
        rows, total = data, payload.get("total", payload.get("total_count"))
    else:
        rows, total = [], None
    try:
        normalized_total = int(total) if total is not None else None
    except (ValueError, TypeError):
        normalized_total = None
    return [row for row in rows if isinstance(row, dict)], normalized_total


def _strings(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if isinstance(value, list):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return ()


def normalize_model(row: dict[str, Any], *, observed_at: str) -> dict[str, Any] | None:
    model = str(row.get("model_id", row.get("model", row.get("id", "")))).strip()
    if not model:
        return None
    provider_raw = str(row.get("provider", row.get("model_provider", "qwen"))).strip()
    provider = PROVIDER_NAMES.get(provider_raw.casefold(), provider_raw.replace("-", " ").title())
    capabilities = _strings(row.get("capabilities", row.get("modalities", [])))
    features = _strings(row.get("features", []))
    lanes = tuple(dict.fromkeys(lane for tag in capabilities for lane in CAPABILITY_LANES.get(tag.upper(), ())))
    deployment = _strings(row.get("deployment_methods", row.get("deployment_modes", [])))
    supports = _strings(row.get("supports", []))
    if any("deploy" in value.casefold() or "self" in value.casefold() for value in (*deployment, *supports)):
        lanes = tuple(dict.fromkeys((*lanes, "self_hosted")))
    context_window = row.get("context_window", row.get("context_length", (row.get("model_info") or {}).get("context_window")))
    try:
        context_tokens = int(context_window) if context_window is not None else None
        if context_tokens is not None and context_tokens <= 0:
            context_tokens = None
    except (ValueError, TypeError):
        context_tokens = None
    description = str(row.get("description", row.get("model_description", ""))).strip()
    strengths = tuple(dict.fromkeys((*capabilities, *features)))
    return {
        "provider": provider,
        "model": model,
        "region": "domestic",
        "description": description or f"Model Studio catalog model; capabilities: {', '.join(capabilities) or 'not provided'}",
        "strengths": strengths or ("Model Studio workspace catalog",),
        "modalities": capabilities,
        "capability_tags": capabilities,
        "features": features,
        "price_details": row.get("prices", []),
        "task_lanes": lanes,
        "context_tokens": context_tokens,
        "source_url": MODEL_CATALOG_DOC,
        "observed_at": observed_at,
        "source_kind": "official",
        "verification_status": "verified",
    }


class AliyunModelCatalogSource:
    def __init__(self, config: McpSourceConfig) -> None:
        if config.transport != "aliyun_models":
            raise ValueError("AliyunModelCatalogSource requires transport=aliyun_models")
        self.config = config
        self.max_request_count = min(max(int((config.arguments or {}).get("max_pages", 20)), 1), 20)
        self.last_request_count = 0

    def _request_page(self, page_no: int, page_size: int, query_args: dict[str, Any]) -> dict[str, Any]:
        token_env = self.config.api_key_env or "DASHSCOPE_API_KEY"
        token = os.environ.get(token_env)
        if not token:
            raise ValueError(f"未设置环境变量 {token_env}")
        endpoint = self.config.endpoint
        workspace_env = str((self.config.arguments or {}).get("workspace_id_env", "DASHSCOPE_WORKSPACE_ID"))
        workspace = os.environ.get(workspace_env, "").strip()
        if "{workspace_id}" in endpoint:
            if not workspace or not workspace.replace("-", "").isalnum():
                raise ValueError(f"未设置有效的环境变量 {workspace_env}")
            endpoint = endpoint.replace("{workspace_id}", workspace)
        arguments = dict(query_args)
        arguments.pop("workspace_id_env", None)
        arguments.pop("max_pages", None)
        arguments.update({"page_no": page_no, "page_size": page_size})
        url = f"{endpoint}?{urlencode(arguments, doseq=True)}"
        headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}
        if workspace:
            headers["WorkspaceId"] = workspace
        request = Request(url, headers=headers)
        try:
            with urlopen(request, timeout=self.config.timeout_seconds) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            raise RuntimeError(f"Model Studio model catalog HTTP {exc.code}") from exc
        except (URLError, TimeoutError) as exc:
            raise RuntimeError(f"Model Studio model catalog request failed: {type(exc).__name__}") from exc
        if len(raw) > MAX_RESPONSE_BYTES:
            raise RuntimeError("Model Studio model catalog response exceeds 4 MiB")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Model Studio model catalog returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise TypeError("Model Studio model catalog response must be a JSON object")
        return payload

    def discover(self, arguments_override: dict[str, Any] | None = None) -> list[dict[str, str]]:
        arguments = dict(self.config.arguments or {})
        arguments.update(arguments_override or {})
        page_size = min(max(int(arguments.get("page_size", 100)), 1), 100)
        max_pages = min(max(int(arguments.get("max_pages", 20)), 1), 20)
        self.max_request_count = max_pages
        self.last_request_count = 0
        observed_at = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
        result: list[dict[str, str]] = []
        for page_no in range(1, max_pages + 1):
            self.last_request_count += 1
            payload = self._request_page(page_no, page_size, arguments)
            rows, total = _rows(payload)
            result.extend(row for raw in rows if (row := normalize_model(raw, observed_at=observed_at)) is not None)
            if not rows or len(rows) < page_size or (total is not None and len(result) >= total):
                break
        return result
