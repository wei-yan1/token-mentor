"""Conservative extraction and currency normalization for public pricing tables."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
import json
import re
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from token_mentor.models import ModelQuote
from token_mentor.tools.mcp import MAX_RESPONSE_BYTES, _safe_public_url


_PRICE_RE = re.compile(r"(?<![A-Za-z0-9_.])(?:[$¥￥€£]\s*)?([0-9]+(?:,[0-9]{3})*(?:\.[0-9]+)?)(?![A-Za-z0-9_.])")
_MODEL_FAMILIES = ("gpt", "claude", "gemini", "qwen", "deepseek", "llama", "mistral", "codestral", "devstral", "glm", "kimi", "ernie", "doubao", "hunyuan", "command", "nova", "jamba", "grok", "nemotron", "minimax", "chatglm", "phi", "yi-")
_MODEL_FAMILY_RE = re.compile("(?i)(" + "|".join(re.escape(item) for item in sorted(_MODEL_FAMILIES, key=len, reverse=True)) + r")")
_FX_CACHE: dict[str, tuple[datetime, Decimal, str, str]] = {}
_FX_CACHE_TTL = timedelta(hours=20)
_FX_BASE = "https://api.frankfurter.dev/v1/latest"


def _table_rows(text: str) -> list[tuple[int, list[str]]]:
    rows: list[tuple[int, list[str]]] = []
    for line_number, line in enumerate(text.splitlines()):
        line = line.strip()
        if line.count("|") < 2:
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) >= 3:
            rows.append((line_number, cells))
    return rows


def _is_separator(cells: list[str]) -> bool:
    return bool(cells) and all(re.fullmatch(r":?-+:?", cell.replace(" ", "")) for cell in cells)


def _currency(text: str, cells: list[str]) -> str | None:
    material = f"{text} {' '.join(cells)}"
    if re.search(r"[$＄]|\bUSD\b|\bUS dollars?\b|美元", material, re.I):
        return "USD"
    if re.search(r"[¥￥]|\bCNY\b|\bRMB\b|人民币|元", material, re.I):
        return "CNY"
    if re.search(r"[€]|\bEUR\b|euros?", material, re.I):
        return "EUR"
    if re.search(r"[£]|\bGBP\b|pounds?", material, re.I):
        return "GBP"
    return None


def _price(cell: str) -> Decimal | None:
    normalized = re.sub(r"<[^>]*>", "", cell).replace("\\$", "$").strip()
    if normalized.casefold() in {"free", "免费"}:
        return Decimal(0)
    if not re.fullmatch(r"\s*(?:[$¥￥€£]\s*)?[0-9]+(?:,[0-9]{3})*(?:\.[0-9]+)?\s*(?:(?:USD|CNY|RMB|EUR|GBP|元|美元)\s*)?(?:/\s*(?:1\s*M|million)\s*tokens?)?\s*", normalized, re.I):
        return None
    matches = list(_PRICE_RE.finditer(normalized))
    if not matches:
        return None
    if len(matches) != 1:
        return None
    try:
        value = Decimal(matches[0].group(1).replace(",", ""))
    except InvalidOperation:
        return None
    return value if value.is_finite() and value >= 0 else None


def _looks_like_model_name(value: str) -> bool:
    model = re.sub(r"\s*\[[^\]]*\]\([^)]*\).*", "", value).strip()
    model = re.sub(r"[`*_]", "", model).strip()
    if not model or len(model) > 100 or len(model.split()) > 6:
        return False
    if re.search(r"[\[\]{}<>;,!?=|]", model):
        return False
    if not re.fullmatch(r"[\w\u3400-\u9fff .:/+()\-]+", model, re.UNICODE):
        return False
    normalized = model.casefold()
    return (any(char.isdigit() for char in normalized)
            or any(family in normalized for family in _MODEL_FAMILIES)
            or "-" in normalized)


def _model_name(value: str) -> str:
    match = _MODEL_FAMILY_RE.search(value)
    if match:
        value = value[match.start():]
    model = re.sub(r"\s*\[[^\]]*\]\([^)]*\).*", "", value).strip()
    model = re.sub(r"\s+\[[^\]]*\].*", "", model).strip()
    model = re.sub(r"[`*_]", "", model).strip()
    return re.sub(r"\s*\(\d+\)$", "", model).strip()


def _extract_deepseek_quotes(content: str, *, source_url: str, observed_at: str, region: str) -> list[dict[str, Any]]:
    indexed = _table_rows(content)
    model_columns: list[tuple[int, str]] = []
    for _, cells in indexed:
        candidates = [(index, _model_name(cell)) for index, cell in enumerate(cells)
                      if re.search(r"deepseek[-_][\w.-]+", cell, re.I)]
        if len(candidates) >= 2:
            model_columns = candidates
            break
    if not model_columns:
        return []
    prices: dict[str, dict[str, Decimal]] = {model: {} for _, model in model_columns}
    active_kind: str | None = None
    for _, cells in indexed:
        row_text = " ".join(cells).casefold().replace(" ", "")
        if "百万tokens输入" in row_text or "milliontokensinput" in row_text:
            active_kind = "input_cached" if "缓存命中" in row_text or "cachehit" in row_text else "input"
        elif "百万tokens输出" in row_text or "milliontokensoutput" in row_text:
            active_kind = "output"
        if not active_kind or "空闲时段" in row_text or "off-peak" in row_text:
            continue
        if "高峰时段" not in row_text and "peak" not in row_text:
            continue
        kind = active_kind
        value_cells = cells[-len(model_columns):]
        for (_, model), cell in zip(model_columns, value_cells):
            amount = _price(cell)
            if amount is not None:
                prices[model][kind] = amount
        active_kind = None
    result = []
    for model, values in prices.items():
        if "input" not in values or "output" not in values:
            continue
        row: dict[str, Any] = {
            "provider": "DeepSeek", "model": model,
            "input_price_per_million": float(values["input"]),
            "output_price_per_million": float(values["output"]),
            "currency": "CNY", "price_unit": "CNY/1M tokens",
            "source_url": source_url, "observed_at": observed_at,
            "region": region,
        }
        if "input_cached" in values:
            row["cached_input_price_per_million"] = float(values["input_cached"])
        result.append(row)
    return result


def _scheduled_usd_price(cell: str, *, as_of: datetime.date) -> Decimal | None:
    matches = list(re.finditer(r"\$\s*([0-9]+(?:,[0-9]{3})*(?:\.[0-9]+)?)", cell))
    if not matches:
        return None
    if len(matches) == 1 and not re.search(r"\b(?:through|starting)\s+[A-Z][a-z]+\s+\d{1,2},\s+\d{4}", cell):
        return Decimal(matches[0].group(1).replace(",", ""))
    for index, match in enumerate(matches):
        next_start = matches[index + 1].start() if index + 1 < len(matches) else len(cell)
        segment = cell[match.end():next_start]
        through = re.search(r"through\s+([A-Z][a-z]+\s+\d{1,2},\s+\d{4})", segment)
        starting = re.search(r"starting\s+([A-Z][a-z]+\s+\d{1,2},\s+\d{4})", segment)
        try:
            if through and as_of <= datetime.strptime(through.group(1), "%B %d, %Y").date():
                return Decimal(match.group(1).replace(",", ""))
            if starting and as_of >= datetime.strptime(starting.group(1), "%B %d, %Y").date():
                return Decimal(match.group(1).replace(",", ""))
        except ValueError:
            continue
    return None


def _extract_google_quotes(content: str, *, source_url: str, observed_at: str, region: str) -> list[dict[str, Any]]:
    lines = content.splitlines()
    observed = datetime.fromisoformat(observed_at.replace("Z", "+00:00")).date()
    quotes: dict[str, dict[str, Any]] = {}
    i = 0
    while i < len(lines):
        header_line = lines[i].strip()
        if not header_line.startswith("|") or "paid tier" not in header_line.casefold() or "per 1m tokens" not in header_line.casefold():
            i += 1
            continue
        code_matches = list(re.finditer(r"\[`([^`]+)`\]\(https://ai\.google\.dev/gemini-api/docs/models/[^)]+\)", "\n".join(lines[max(0, i - 40):i]), re.I))
        model = code_matches[-1].group(1).strip() if code_matches else ""
        if not model or not _looks_like_model_name(model):
            i += 1
            continue
        input_price: Decimal | None = None
        output_price: Decimal | None = None
        j = i + 1
        while j < len(lines) and lines[j].strip().startswith("|"):
            cells = [cell.strip() for cell in lines[j].strip().strip("|").split("|")]
            if len(cells) >= 3:
                label = re.sub(r"[*_`\[\]]", "", cells[0]).casefold()
                price_text = cells[2]
                if label.startswith("input price"):
                    input_price = _scheduled_usd_price(price_text, as_of=observed)
                elif label.startswith("output price"):
                    output_price = _scheduled_usd_price(price_text, as_of=observed)
            j += 1
        if input_price is not None and output_price is not None:
            key = model.casefold()
            quotes.setdefault(key, {
                "provider": "Google", "model": model,
                "input_price_per_million": float(input_price),
                "output_price_per_million": float(output_price),
                "currency": "USD", "price_unit": "USD/1M tokens",
                "source_url": source_url, "observed_at": observed_at, "region": region,
            })
        i = j
    return list(quotes.values())


def extract_markdown_quotes(
    content: str,
    *,
    provider: str,
    source_url: str,
    region: str,
    observed_at: str | None = None,
) -> list[dict[str, Any]]:
    """Extract only markdown tables with explicit per-million-token units and columns."""
    if not isinstance(content, str) or not content.strip() or not _safe_public_url(source_url):
        return []
    lines = content.splitlines()
    indexed = _table_rows(content)
    quotes: dict[str, dict[str, Any]] = {}
    i = 0
    while i < len(indexed) - 2:
        header_line, headers = indexed[i]
        header_text = " ".join(headers).casefold()
        if indexed[i + 1][0] != header_line + 1 or not _is_separator(indexed[i + 1][1]) or len(indexed[i + 1][1]) != len(headers):
            i += 1
            continue
        model_columns = [index for index, value in enumerate(headers) if re.search(r"model|模型", value, re.I)]
        input_columns = [index for index, value in enumerate(headers) if re.search(r"\b(input|prompt)\b|输入", value, re.I)]
        output_columns = [index for index, value in enumerate(headers) if re.search(r"\b(output|completion)\b|输出", value, re.I)]
        if len(model_columns) != 1 or len(input_columns) != 1 or len(output_columns) != 1:
            i += 1
            continue
        table_end = i + 2
        while table_end < len(indexed) and indexed[table_end][0] == indexed[table_end - 1][0] + 1 and not _is_separator(indexed[table_end][1]):
            table_end += 1
        start = max(0, header_line - 6)
        end = min(len(lines), indexed[table_end - 1][0] + 1)
        context_lines = "\n".join(lines[start:end])
        unit_context = f"{header_text} {context_lines}"
        per_million = r"(?:per\s+(?:1\s*)?(?:million|1\s*m)|(?:1\s*m|million)\s*tokens?|每百万\s*token|百万\s*token|/\s*1\s*m\s*tokens?)"
        if re.search(r"(?:per\s+(?:1\s*k|1000)|/\s*(?:1\s*k|1000))\s*tokens?", header_text, re.I) or not (re.search(per_million, unit_context, re.I) or re.search(per_million, content, re.I)):
            i = table_end
            continue
        for _, cells in indexed[i + 2:table_end]:
            if len(cells) != len(headers):
                continue
            model = _model_name(cells[model_columns[0]])
            input_cell, output_cell = cells[input_columns[0]], cells[output_columns[0]]
            if re.search(r"(?:/\s*(?:1\s*k|1000)|per\s+(?:1\s*k|1000))\s*tokens?", f"{input_cell} {output_cell}", re.I):
                continue
            if not _looks_like_model_name(model):
                continue
            input_price, output_price = _price(input_cell), _price(output_cell)
            if input_price is None or output_price is None:
                continue
            currency = _currency(" ".join((input_cell, output_cell, headers[input_columns[0]], headers[output_columns[0]])), [input_cell, output_cell])
            if currency is None:
                currency = _currency(context_lines, [header_text])
            if currency is None:
                continue
            key = model.casefold()
            quotes[key] = {
                "provider": provider,
                "model": model,
                "input_price_per_million": float(input_price),
                "output_price_per_million": float(output_price),
                "currency": currency,
                "price_unit": f"{currency}/1M tokens",
                "source_url": source_url,
                "observed_at": observed_at or datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
                "region": region,
            }
        i = table_end
    grouped: list[dict[str, Any]] = []
    hostname = urlparse(source_url).hostname or ""
    captured_at = observed_at or datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    if hostname.endswith("deepseek.com"):
        grouped = _extract_deepseek_quotes(content, source_url=source_url,
            observed_at=captured_at,
            region=region)
    elif hostname == "ai.google.dev":
        grouped = _extract_google_quotes(content, source_url=source_url, observed_at=captured_at, region=region)
    for row in grouped:
        quotes[row["model"].casefold()] = row
    return list(quotes.values())


def _fetch_cny_rate(currency: str, *, timeout_seconds: float = 12.0) -> tuple[Decimal, str, str]:
    currency = currency.upper()
    if currency == "CNY":
        return Decimal(1), "", datetime.now(UTC).date().isoformat()
    if currency not in {"USD", "EUR", "GBP", "JPY", "CAD", "AUD", "CHF", "NZD", "SGD", "HKD", "KRW", "INR", "BRL", "MXN", "SEK", "NOK", "DKK", "PLN", "CZK", "HUF", "TRY", "THB", "IDR", "MYR", "PHP", "ILS", "ZAR", "ISK", "RON", "BGN"}:
        raise ValueError(f"暂不支持自动换算 {currency} 报价")
    now = datetime.now(UTC)
    cached = _FX_CACHE.get(currency)
    if cached and now - cached[0] < _FX_CACHE_TTL:
        return cached[1], cached[2], cached[3]
    query = urlencode({"base": currency, "symbols": "CNY"})
    endpoint = f"{_FX_BASE}?{query}"
    try:
        with urlopen(Request(endpoint, headers={"Accept": "application/json"}), timeout=timeout_seconds) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        raise RuntimeError(f"汇率服务 HTTP {exc.code}") from exc
    except (URLError, TimeoutError) as exc:
        raise RuntimeError(f"汇率服务请求失败：{type(exc).__name__}") from exc
    if len(raw) > MAX_RESPONSE_BYTES:
        raise RuntimeError("汇率服务响应过大")
    try:
        payload = json.loads(raw.decode("utf-8"))
        rate = Decimal(str(payload["rates"]["CNY"]))
        rate_date = str(payload["date"])
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, InvalidOperation) as exc:
        raise RuntimeError("汇率服务返回的数据格式无效") from exc
    if not rate.is_finite() or rate <= 0 or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", rate_date):
        raise RuntimeError("汇率服务返回了无效汇率或日期")
    _FX_CACHE[currency] = (now, rate, endpoint, rate_date)
    return rate, endpoint, rate_date


def normalize_quote_currency(payload: dict[str, Any], *, timeout_seconds: float = 12.0) -> dict[str, Any]:
    """Convert a structured official quote to CNY while preserving the original quote and FX evidence."""
    currency = str(payload.get("currency", "CNY")).upper()
    if currency == "CNY":
        return payload
    input_price = Decimal(str(payload["input_price_per_million"]))
    output_price = Decimal(str(payload["output_price_per_million"]))
    rate, fx_url, rate_date = _fetch_cny_rate(currency, timeout_seconds=timeout_seconds)
    converted = dict(payload)
    converted.update({
        "input_price_per_million": float(input_price * rate),
        "output_price_per_million": float(output_price * rate),
        "currency": "CNY",
        "price_unit": "CNY/1M tokens",
        "source_currency": currency,
        "source_input_price_per_million": float(input_price),
        "source_output_price_per_million": float(output_price),
        "fx_rate_to_cny": float(rate),
        "fx_source_url": fx_url,
        "fx_observed_at": f"{rate_date}T00:00:00Z",
    })
    return converted


def normalize_quotes(rows: list[dict[str, Any]], *, timeout_seconds: float = 12.0) -> list[ModelQuote]:
    return [ModelQuote.from_payload(normalize_quote_currency(row, timeout_seconds=timeout_seconds)) for row in rows]
