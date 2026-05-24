"""Build final action options from scored cargo candidates."""

from __future__ import annotations

from typing import Any

from agent.history.time_utils import coerce_float, coerce_int
from agent.tools.wait_factory import make_wait_option


DEFAULT_WAIT_MINUTES = (10, 30)


class ActionOptionBuilder:
    def __init__(self, *, top_k: int = 10, wait_minutes: tuple[int, ...] = DEFAULT_WAIT_MINUTES) -> None:
        self._top_k = max(0, int(top_k))
        self._wait_minutes = tuple(int(v) for v in wait_minutes if int(v) > 0)

    def build(
        self,
        scored_candidates: list[dict[str, Any]],
        *,
        status: dict[str, Any] | None = None,
        history_geo_summary: list[dict[str, Any]] | None = None,
        tool_context: dict[str, Any] | None = None,
        extra_wait_options: list[dict[str, Any]] | None = None,
        reposition_options: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        eligible_orders = [candidate for candidate in scored_candidates if coerce_float(candidate.get("net_income"), 0.0) >= 0]
        order_options = [self._order_option(candidate) for candidate in eligible_orders[: self._top_k]]
        wait_options = self._merge_wait_options(
            [
                make_wait_option(
                    duration_minutes=minutes,
                    status=status,
                    source="default_wait",
                    reason=f"默认等待 {minutes} 分钟",
                )
                for minutes in self._wait_minutes
            ],
            extra_wait_options or [],
        )
        merged_tool_context = dict(tool_context or {})
        if history_geo_summary is not None:
            merged_tool_context["history_geo_summary"] = history_geo_summary
        else:
            merged_tool_context.setdefault("history_geo_summary", [])
        return {
            "orders": order_options,
            "wait_options": wait_options,
            "reposition_options": reposition_options or [],
            "tool_context": merged_tool_context,
        }

    @staticmethod
    def _order_option(candidate: dict[str, Any]) -> dict[str, Any]:
        return {
            "option_id": f"order_{candidate.get('cargo_id')}",
            "action": "take_order",
            "params": {"cargo_id": str(candidate.get("cargo_id") or "")},
            "score": candidate.get("score"),
            "gross_income": candidate.get("gross_income"),
            "net_income": candidate.get("net_income"),
            "distance_cost": candidate.get("distance_cost"),
            "preference_penalty": candidate.get("preference_penalty"),
            "preference_evaluation": candidate.get("preference_evaluation"),
            "tool_evidence": candidate.get("tool_evidence", {}),
            "candidate": {
                "cargo_id": candidate.get("cargo_id"),
                "cargo_name": candidate.get("cargo_name"),
                "start": candidate.get("start"),
                "end": candidate.get("end"),
                "pickup_deadhead_km": candidate.get("pickup_deadhead_km"),
                "pickup_minutes": candidate.get("pickup_minutes"),
                "haul_distance_km": candidate.get("haul_distance_km"),
                "arrival_time": candidate.get("arrival_time"),
                "waiting_minutes": candidate.get("waiting_minutes"),
                "finish_time": candidate.get("finish_time"),
                "estimated_total_minutes": candidate.get("estimated_total_minutes"),
            },
        }

    @staticmethod
    def _merge_wait_options(
        default_wait_options: list[dict[str, Any]],
        extra_wait_options: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        by_duration: dict[int, dict[str, Any]] = {}
        order: list[int] = []
        for option in default_wait_options + extra_wait_options:
            params = option.get("params") if isinstance(option.get("params"), dict) else {}
            duration = coerce_int(params.get("duration_minutes"), -1)
            if duration <= 0:
                continue
            if duration not in by_duration:
                order.append(duration)
                by_duration[duration] = dict(option)
                continue
            existing = by_duration[duration]
            reasons = list(existing.get("preference_reasons") or [])
            if existing.get("reason") and existing.get("source") != "default_wait":
                reasons.append(existing.get("reason"))
            if option.get("reason"):
                reasons.append(option.get("reason"))
            merged = dict(existing)
            if option.get("source") != "default_wait":
                merged.update(option)
            if reasons:
                deduped_reasons = list(dict.fromkeys(str(reason) for reason in reasons if reason))
                merged["preference_reasons"] = deduped_reasons
                merged["reason"] = " | ".join(deduped_reasons)
            by_duration[duration] = merged
        return [by_duration[duration] for duration in sorted(order)]
