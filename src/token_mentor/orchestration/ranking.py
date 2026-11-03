from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from token_mentor.models import ModelQuote


from token_mentor.constraints.scoring import DEFAULT_WEIGHTS, normalize_weights, parse_weights


@dataclass(frozen=True, slots=True)
class RankedQuote:
    quote: ModelQuote
    estimated_cost_cny: float
    score: float
    axis_scores: dict[str, float]



def rank_quotes(
    quotes: Sequence[ModelQuote],
    *,
    capability: str,
    input_tokens: int,
    output_tokens: int,
    weights: Mapping[str, float] | None = None,
) -> tuple[list[RankedQuote], tuple[str, ...]]:
    if not quotes:
        return [], ()
    configured = normalize_weights(weights)
    costs = [quote.estimated_cost(input_tokens, output_tokens) for quote in quotes]
    minimum, maximum = min(costs), max(costs)
    raw: dict[str, list[float | None]] = {axis: [] for axis in DEFAULT_WEIGHTS}
    for quote, cost in zip(quotes, costs):
        raw["cost"].append(1.0 if maximum == minimum else (maximum - cost) / (maximum - minimum))
        quality = quote.capabilities.get("quality") if capability == "quality" else quote.capabilities.get(capability)
        raw["quality"].append(quality)
        raw["stability"].append(None if quote.stability is None else quote.stability / 100.0)
        raw["speed"].append(None if quote.latency_ms is None else quote.latency_ms)

    normalized: dict[str, list[float | None]] = {}
    for axis, values in raw.items():
        if axis == "speed":
            latencies = [value for value in values if value is not None]
            fastest = min(latencies) if latencies else None
            normalized[axis] = [
                (fastest / value if value is not None and fastest is not None else None)
                for value in values
            ]
        else:
            normalized[axis] = values

    active = tuple(
        axis for axis, values in normalized.items()
        if configured.get(axis, 0.0) > 0 and all(value is not None for value in values)
    )
    omitted = tuple(
        axis for axis, values in normalized.items()
        if configured.get(axis, 0.0) > 0 and axis not in active
    )
    weight_sum = sum(configured[axis] for axis in active)
    if weight_sum <= 0:
        active = ("cost",)
        configured = {axis: 1.0 if axis == "cost" else 0.0 for axis in DEFAULT_WEIGHTS}
        weight_sum = 1.0

    ranked: list[RankedQuote] = []
    for index, quote in enumerate(quotes):
        axis_scores = {
            axis: float(normalized[axis][index])
            for axis in active
            if normalized[axis][index] is not None
        }
        score = sum(axis_scores[axis] * configured[axis] for axis in active) / weight_sum
        ranked.append(RankedQuote(quote, costs[index], score, axis_scores))
    ranked.sort(key=lambda item: (-item.score, item.estimated_cost_cny, item.quote.provider.lower(), item.quote.model.lower()))
    return ranked, omitted

