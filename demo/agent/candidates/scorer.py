"""Score judged cargo candidates for final action selection."""

from __future__ import annotations

from typing import Any

from agent.history.time_utils import coerce_float


DEFAULT_DISTANCE_COST_PER_KM = 1.5


def score_candidates(
    candidates: list[dict[str, Any]],
    *,
    distance_cost_per_km: float = DEFAULT_DISTANCE_COST_PER_KM,
) -> list[dict[str, Any]]:
    scored: list[dict[str, Any]] = []
    for candidate in candidates:
        item = dict(candidate)
        gross_income = coerce_float(item.get("gross_income"))
        pickup_km = coerce_float(item.get("pickup_deadhead_km"))
        haul_km = coerce_float(item.get("haul_distance_km"))
        distance_cost = (pickup_km + haul_km) * float(distance_cost_per_km)
        evaluation = item.get("preference_evaluation") if isinstance(item.get("preference_evaluation"), dict) else {}
        preference_penalty = coerce_float(evaluation.get("preference_penalty")) if isinstance(evaluation, dict) else 0.0
        preference_penalty = max(0.0, preference_penalty)
        estimated_total_minutes = max(1.0, coerce_float(item.get("estimated_total_minutes"), 1.0))
        net_income = gross_income - distance_cost - preference_penalty
        score = net_income / estimated_total_minutes
        item["gross_income"] = round(gross_income, 2)
        item["distance_cost"] = round(distance_cost, 2)
        item["preference_penalty"] = round(preference_penalty, 2)
        item["net_income"] = round(net_income, 2)
        item["score"] = round(score, 4)
        scored.append(item)
    scored.sort(key=lambda item: (coerce_float(item.get("score")), coerce_float(item.get("gross_income"))), reverse=True)
    return scored
