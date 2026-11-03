"""Deterministic scoring configuration and validation, independent of I/O."""
from __future__ import annotations

import math
from typing import Mapping

DEFAULT_WEIGHTS: dict[str, float] = {
    "cost": 0.35,
    "quality": 0.40,
    "stability": 0.15,
    "speed": 0.10,
}


def normalize_weights(weights: Mapping[str, float] | None) -> dict[str, float]:
    if weights is None:
        return dict(DEFAULT_WEIGHTS)
    result: dict[str, float] = {axis: 0.0 for axis in DEFAULT_WEIGHTS}
    for raw_key, raw_weight in weights.items():
        key = str(raw_key).strip().lower()
        if key not in DEFAULT_WEIGHTS:
            raise ValueError(f"不支持的评分维度：{key}")
        if isinstance(raw_weight, bool):
            raise ValueError(f"维度 {key} 的权重必须是有限非负数字")
        try:
            weight = float(raw_weight)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"维度 {key} 的权重必须是有限非负数字") from exc
        if not math.isfinite(weight) or weight < 0:
            raise ValueError(f"维度 {key} 的权重必须是有限非负数字")
        result[key] = weight
    if sum(result.values()) <= 0:
        raise ValueError("至少一个维度的权重大于 0")
    return result


def parse_weights(value: str | None) -> dict[str, float]:
    if not value:
        return dict(DEFAULT_WEIGHTS)
    result: dict[str, float] = {}
    aliases = {"latency": "speed", "reliability": "stability", "price": "cost"}
    for item in value.split(","):
        if "=" not in item:
            raise ValueError("权重格式应为 cost=0.35,quality=0.4,stability=0.15,speed=0.1")
        key, raw = item.split("=", 1)
        key = aliases.get(key.strip().lower(), key.strip().lower())
        if key not in DEFAULT_WEIGHTS:
            raise ValueError(f"不支持的评分维度：{key}")
        try:
            weight = float(raw)
        except ValueError as exc:
            raise ValueError(f"维度 {key} 的权重不是数字") from exc
        if not math.isfinite(weight) or weight < 0:
            raise ValueError("权重必须是有限非负数字")
        result[key] = weight
    return normalize_weights(result)



# Relative performance score tiers are labels for a cohort, not universal quality claims.
def performance_tier(relative_score: float) -> str:
    if relative_score >= 85:
        return "旗舰"
    if relative_score >= 65:
        return "主力"
    return "轻量"
