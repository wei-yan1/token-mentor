from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
import math
import re
from typing import Any
from urllib.parse import parse_qsl, urlparse


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("时间必须包含时区，例如 2026-10-27T09:00:00Z")
    return parsed.astimezone(UTC)


def _finite_number(value: Any, name: str, *, minimum: float = 0.0) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} 必须是数字")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必须是数字") from exc
    if not math.isfinite(number) or number < minimum:
        raise ValueError(f"{name} 必须是大于等于 {minimum} 的有限数字")
    return number


def _token_count(value: Any, name: str, *, allow_zero: bool = True) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} 必须是整数 token 数")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必须是整数 token 数") from exc
    if not math.isfinite(number) or not number.is_integer() or number < (0 if allow_zero else 1):
        raise ValueError(f"{name} 必须是{'>= 0' if allow_zero else '> 0'}的整数 token 数")
    return int(number)


def _string_tuple(value: Any, name: str, *, maximum: int) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, (list, tuple, set)):
        values = list(value)
    else:
        raise ValueError(f"{name} 必须是字符串或字符串数组")
    return tuple(str(item).strip()[:300] for item in values if str(item).strip())[:maximum]


def _first(payload: dict[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in payload and payload[name] is not None:
            return payload[name]
    return default


@dataclass(frozen=True, slots=True)
class ModelQuote:
    """One sourced price observation. Prices are CNY per one million tokens."""

    provider: str
    model: str
    input_price_per_million: float
    output_price_per_million: float
    source_url: str
    observed_at: str
    currency: str = "CNY"
    context_tokens: int | None = None
    cached_input_price_per_million: float | None = None
    capabilities: dict[str, float] = field(default_factory=dict)
    stability: float | None = None
    latency_ms: float | None = None
    region: str = "unknown"
    description: str = ""
    strengths: tuple[str, ...] = ()
    modalities: tuple[str, ...] = ()
    source_currency: str = "CNY"
    source_input_price_per_million: float | None = None
    source_output_price_per_million: float | None = None
    fx_rate_to_cny: float = 1.0
    fx_source_url: str | None = None
    fx_observed_at: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.provider, str) or not isinstance(self.model, str) or not self.provider.strip() or not self.model.strip():
            raise ValueError("provider 和 model 不能为空")
        if not isinstance(self.currency, str) or self.currency.upper() != "CNY":
            raise ValueError("当前版本只接受人民币报价；请先规范换算并保留原始来源")
        input_price = _finite_number(self.input_price_per_million, "input_price_per_million")
        output_price = _finite_number(self.output_price_per_million, "output_price_per_million")
        object.__setattr__(self, "input_price_per_million", input_price)
        object.__setattr__(self, "output_price_per_million", output_price)
        if self.cached_input_price_per_million is not None:
            object.__setattr__(
                self,
                "cached_input_price_per_million",
                _finite_number(self.cached_input_price_per_million, "cached_input_price_per_million"),
            )
        if self.stability is not None:
            stability = _finite_number(self.stability, "stability")
            if stability > 100:
                raise ValueError("stability 取值范围是 0 到 100")
            object.__setattr__(self, "stability", stability)
        if self.latency_ms is not None:
            object.__setattr__(self, "latency_ms", _finite_number(self.latency_ms, "latency_ms", minimum=0.001))
        if self.context_tokens is not None:
            object.__setattr__(self, "context_tokens", _token_count(self.context_tokens, "context_tokens", allow_zero=False))
        if not isinstance(self.source_url, str) or not isinstance(self.observed_at, str):
            raise ValueError("source_url 和 observed_at 必须是字符串")
        source_url = self.source_url.strip()
        object.__setattr__(self, "source_url", source_url)
        parsed_url = urlparse(source_url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname:
            raise ValueError("source_url 必须是可核验的 http/https 链接")
        if parsed_url.username or parsed_url.password or any(
            name.lower() in {"token", "api_key", "apikey", "access_token", "secret", "signature", "sig", "authorization", "key", "code"}
            for name, _ in parse_qsl(parsed_url.query, keep_blank_values=True)
        ):
            raise ValueError("source_url 不能包含认证凭据；请使用不带密钥的公开页面链接")
        if any(ord(char) < 32 for char in source_url):
            raise ValueError("source_url 不能包含控制字符")
        normalized_time = parse_time(self.observed_at).isoformat(timespec="seconds").replace("+00:00", "Z")
        object.__setattr__(self, "observed_at", normalized_time)
        clean: dict[str, float] = {}
        if not isinstance(self.capabilities, dict):
            raise ValueError("capabilities 必须是对象")
        for key, raw in self.capabilities.items():
            name = str(key).strip().lower()
            value = _finite_number(raw, f"capabilities.{name}")
            if not name or value > 100:
                raise ValueError("能力评分需使用 0 到 100 范围")
            clean[name] = value / 100.0 if value > 1 else value
        object.__setattr__(self, "capabilities", clean)
        object.__setattr__(self, "currency", "CNY")
        source_currency = str(self.source_currency or "CNY").upper()
        if not re.fullmatch(r"[A-Z]{3}", source_currency):
            raise ValueError("source_currency 必须是 ISO 4217 货币代码")
        object.__setattr__(self, "source_currency", source_currency)
        for field_name in ("source_input_price_per_million", "source_output_price_per_million"):
            raw_price = getattr(self, field_name)
            if raw_price is not None:
                object.__setattr__(self, field_name, _finite_number(raw_price, field_name))
        rate = _finite_number(self.fx_rate_to_cny, "fx_rate_to_cny", minimum=0.0000001)
        object.__setattr__(self, "fx_rate_to_cny", rate)
        if self.fx_source_url is not None:
            fx_url = str(self.fx_source_url).strip()
            parsed_fx_url = urlparse(fx_url)
            if parsed_fx_url.scheme != "https" or not parsed_fx_url.hostname:
                raise ValueError("fx_source_url 必须是公开 HTTPS 链接")
            object.__setattr__(self, "fx_source_url", fx_url)
        if self.fx_observed_at is not None:
            object.__setattr__(self, "fx_observed_at", parse_time(self.fx_observed_at).isoformat(timespec="seconds").replace("+00:00", "Z"))
        if self.region not in {"domestic", "foreign", "all", "unknown"}:
            raise ValueError("region 必须是 domestic、foreign、all 或 unknown")
        object.__setattr__(self, "description", str(self.description or "").strip()[:5_000])
        object.__setattr__(self, "strengths", _string_tuple(self.strengths, "strengths", maximum=30))
        object.__setattr__(self, "modalities", _string_tuple(self.modalities, "modalities", maximum=20))

    @classmethod
    def from_payload(
        cls,
        payload: dict[str, Any],
        *,
        default_provider: str | None = None,
        default_region: str | None = None,
    ) -> "ModelQuote":
        if not isinstance(payload, dict):
            raise ValueError("报价必须是 JSON 对象")
        provider = _first(payload, "provider", "vendor", default=default_provider)
        model = _first(payload, "model", "model_id", "name")
        source_url = _first(payload, "source_url", "url")
        observed_at = _first(payload, "observed_at", "updated_at", "checked_at")
        if not all(isinstance(value, str) and value.strip() for value in (provider, model, source_url, observed_at)):
            raise ValueError("每条报价都必须包含 provider、model、source_url、observed_at")
        currency = str(_first(payload, "currency", default="CNY")).upper()
        unit = str(_first(payload, "price_unit", "unit", default="CNY/1M tokens")).lower()
        if not any(token in unit for token in ("1m", "million", "百万")):
            raise ValueError("只接受按百万 token 计价的规范数据")
        input_price = _first(payload, "input_price_per_million", "input_price", "prompt_price")
        output_price = _first(payload, "output_price_per_million", "output_price", "completion_price")
        if input_price is None or output_price is None:
            raise ValueError("报价需包含输入和输出 token 单价")
        context = _first(payload, "context_tokens", "context_length")
        capabilities = _first(payload, "capabilities", "quality_scores", default={})
        if not isinstance(capabilities, dict):
            raise ValueError("capabilities 必须是对象，例如 {\"coding\": 0.9}")
        return cls(
            provider=provider.strip(),
            model=model.strip(),
            input_price_per_million=_finite_number(input_price, "input_price_per_million"),
            output_price_per_million=_finite_number(output_price, "output_price_per_million"),
            source_url=source_url.strip(),
            observed_at=observed_at.strip(),
            currency=currency,
            source_currency=str(_first(payload, "source_currency", default=currency)).upper(),
            source_input_price_per_million=(
                _finite_number(_first(payload, "source_input_price_per_million"), "source_input_price_per_million")
                if _first(payload, "source_input_price_per_million") is not None else None
            ),
            source_output_price_per_million=(
                _finite_number(_first(payload, "source_output_price_per_million"), "source_output_price_per_million")
                if _first(payload, "source_output_price_per_million") is not None else None
            ),
            fx_rate_to_cny=_finite_number(_first(payload, "fx_rate_to_cny", default=1.0), "fx_rate_to_cny", minimum=0.0000001),
            fx_source_url=_first(payload, "fx_source_url"),
            fx_observed_at=_first(payload, "fx_observed_at"),
        context_tokens=_token_count(context, "context_tokens", allow_zero=False) if context is not None else None,
            cached_input_price_per_million=(
                _finite_number(_first(payload, "cached_input_price_per_million", "cached_input_price"), "cached_input_price_per_million")
                if _first(payload, "cached_input_price_per_million", "cached_input_price") is not None
                else None
            ),
            capabilities={str(key): _finite_number(value, f"capabilities.{key}") for key, value in capabilities.items()},
            stability=(
                _finite_number(_first(payload, "stability", "reliability"), "stability")
                if _first(payload, "stability", "reliability") is not None
                else None
            ),
            latency_ms=(
                _finite_number(_first(payload, "latency_ms", "speed_ms"), "latency_ms", minimum=0.001)
                if _first(payload, "latency_ms", "speed_ms") is not None
                else None
            ),
            region=str(_first(payload, "region", "market", default=default_region or "unknown")),
            description=str(_first(payload, "description", "summary", default=""))[:5_000],
            strengths=_string_tuple(_first(payload, "strengths", "specialties", "features", default=[]), "strengths", maximum=30),
            modalities=_string_tuple(_first(payload, "modalities", "modalities_supported", default=[]), "modalities", maximum=20),
        )

    def estimated_cost(self, input_tokens: int, output_tokens: int) -> float:
        input_tokens = _token_count(input_tokens, "input_tokens")
        output_tokens = _token_count(output_tokens, "output_tokens")
        return (
            input_tokens * self.input_price_per_million
            + output_tokens * self.output_price_per_million
        ) / 1_000_000

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ModelQuote":
        normalized = dict(payload)
        normalized["strengths"] = _string_tuple(normalized.get("strengths", ()), "strengths", maximum=30)
        normalized["modalities"] = _string_tuple(normalized.get("modalities", ()), "modalities", maximum=20)
        return cls(**normalized)


@dataclass(frozen=True, slots=True)
class ModelProfile:
    """Descriptive model metadata kept separately from numeric price observations."""

    provider: str
    model: str
    region: str
    description: str
    strengths: tuple[str, ...]
    source_url: str
    observed_at: str
    source_kind: str = "official"
    verification_status: str = "unverified"
    modalities: tuple[str, ...] = ()
    context_tokens: int | None = None
    task_lanes: tuple[str, ...] = ()
    capability_tags: tuple[str, ...] = ()
    features: tuple[str, ...] = ()
    price_details: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.provider.strip() or not self.model.strip():
            raise ValueError("model profile 必须包含 provider 和 model")
        if self.region not in {"domestic", "foreign", "unknown"}:
            raise ValueError("model profile region 必须是 domestic、foreign 或 unknown")
        if not self.description.strip() and not self.strengths:
            raise ValueError("model profile 至少需要 description 或 strengths")
        if self.source_kind not in {"official", "news", "community", "aggregator"}:
            raise ValueError("model profile source_kind 无效")
        if self.verification_status not in {"verified", "unverified", "needs_review"}:
            raise ValueError("model profile verification_status 无效")
        parsed = urlparse(self.source_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("model profile source_url 必须是公开 http(s) URL")
        if parsed.username or parsed.password:
            raise ValueError("model profile source_url 不能包含 URL 凭据")
        object.__setattr__(self, "description", self.description.strip()[:5000])
        object.__setattr__(self, "strengths", _string_tuple(self.strengths, "strengths", maximum=30))
        object.__setattr__(self, "modalities", _string_tuple(self.modalities, "modalities", maximum=20))
        object.__setattr__(self, "task_lanes", _string_tuple(self.task_lanes, "task_lanes", maximum=30))
        object.__setattr__(self, "capability_tags", _string_tuple(self.capability_tags, "capability_tags", maximum=50))
        object.__setattr__(self, "features", _string_tuple(self.features, "features", maximum=50))
        if self.context_tokens is not None:
            object.__setattr__(self, "context_tokens", _token_count(self.context_tokens, "context_tokens", allow_zero=False))
        object.__setattr__(self, "observed_at", parse_time(self.observed_at).isoformat(timespec="seconds").replace("+00:00", "Z"))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ResearchTarget:
    """A provider/model search target used for periodic verification."""

    provider: str
    region: str
    aliases: tuple[str, ...] = ()
    official_domains: tuple[str, ...] = ()
    enabled: bool = True
    pricing_urls: tuple[str, ...] = ()
    model_lanes: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.provider.strip():
            raise ValueError("research target provider 不能为空")
        if self.region not in {"domestic", "foreign"}:
            raise ValueError("research target region 必须是 domestic 或 foreign")
        object.__setattr__(self, "aliases", tuple(str(x).strip() for x in self.aliases if str(x).strip())[:30])
        object.__setattr__(self, "official_domains", tuple(str(x).strip().lower() for x in self.official_domains if str(x).strip())[:20])
        checked_urls = []
        for value in self.pricing_urls:
            url = str(value).strip()
            parsed = urlparse(url)
            host = (parsed.hostname or "").lower()
            if parsed.scheme != "https" or not host or not any(host == domain or host.endswith("." + domain)
                                                                for domain in self.official_domains):
                raise ValueError("pricing_urls 必须使用 HTTPS 且域名位于 official_domains 中")
            checked_urls.append(url)
        object.__setattr__(self, "pricing_urls", tuple(checked_urls[:10]))
        lanes: dict[str, tuple[str, ...]] = {}
        for lane, terms in self.model_lanes.items():
            name = str(lane).strip().lower()
            if not name or not isinstance(terms, (list, tuple)):
                raise ValueError("model_lanes 必须是字符串到词条数组的映射")
            clean_terms = tuple(str(term).strip()[:100] for term in terms if str(term).strip())
            if clean_terms:
                lanes[name] = clean_terms[:30]
        object.__setattr__(self, "model_lanes", lanes)


@dataclass(frozen=True, slots=True)
class BenchmarkEvidence:
    """Published or internal benchmark observation with an explicit comparison protocol."""

    provider: str
    model: str
    benchmark_id: str
    benchmark_version: str
    task: str
    score: float
    max_score: float = 100.0
    source_url: str = ""
    observed_at: str = ""
    protocol: str = ""

    def __post_init__(self) -> None:
        for key in ("provider", "model", "benchmark_id", "benchmark_version", "task", "source_url", "observed_at", "protocol"):
            value = getattr(self, key)
            if not isinstance(value, str) or (key != "protocol" and not value.strip()):
                raise ValueError(f"benchmark {key} 必须是非空字符串")
        score = _finite_number(self.score, "benchmark score")
        maximum = _finite_number(self.max_score, "benchmark max_score", minimum=0.000001)
        if score > maximum:
            raise ValueError("benchmark score 必须位于 0 和 max_score 之间")
        url = urlparse(self.source_url)
        if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password:
            raise ValueError("benchmark source_url 必须是公开 http(s) URL")
        object.__setattr__(self, "score", score)
        object.__setattr__(self, "max_score", maximum)
        object.__setattr__(self, "observed_at", parse_time(self.observed_at).isoformat().replace("+00:00", "Z"))

    @property
    def normalized_score(self) -> float:
        return float(self.score) / float(self.max_score)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "BenchmarkEvidence":
        return cls(**payload)


@dataclass(frozen=True, slots=True)
class ProjectAnalysis:
    root: str
    summary: str
    evidence: tuple[str, ...]
    tasks: tuple[str, ...]
    technologies: tuple[str, ...]
    content_fingerprint: str

