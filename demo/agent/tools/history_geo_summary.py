"""History-level geographic visit summaries and related reposition options."""

from __future__ import annotations

import calendar
import re
from datetime import timedelta
from typing import Any

from agent.history.time_utils import SIMULATION_EPOCH, coerce_float, coerce_int, distance_to_minutes, haversine_km
from agent.tools.geo_checks import point_inside_target
from agent.tools.reposition_factory import make_reposition_option


def build_context(
    *,
    preference_context: dict[str, Any],
    tool_plan: dict[str, dict[str, Any]],
    event_log: list[dict[str, Any]],
    current_minutes: int,
) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    current_day = int(current_minutes) // 1440
    brief_by_id = _brief_by_pref_id(preference_context)
    for pref_id, tools in tool_plan.items():
        history_config = tools.get("history_geo_summary")
        if not isinstance(history_config, dict):
            continue
        target_config = _paired_visit_target(tools)
        if target_config is None:
            continue
        brief = brief_by_id.get(pref_id, {"pref_id": pref_id})
        visited_days = set()
        for event in event_log:
            if event.get("action") not in {"take_order", "reposition", "wait"}:
                continue
            position_after = event.get("position_after") if isinstance(event.get("position_after"), dict) else {}
            lat = coerce_float(position_after.get("lat"), float("nan"))
            lng = coerce_float(position_after.get("lng"), float("nan"))
            if lat != lat or lng != lng:
                continue
            inside, _fact = point_inside_target(target_config, lat, lng)
            if inside:
                event_day = coerce_int(event.get("action_end_minutes"), coerce_int(event.get("action_start_minutes"))) // 1440
                visited_days.add(event_day)

        required = _required_visit_count(history_config)
        current_count = len(visited_days)
        remaining = max(0, required - current_count)
        visited_today = current_day in visited_days
        summaries.append(
            {
                "pref_id": pref_id,
                "source_text": brief.get("source_text"),
                "visited_today": visited_today,
                "current_visit_count": current_count,
                "required_visit_count": required,
                "remaining_visit_count": remaining,
                "days_until_deadline": _days_until_month_end(current_minutes),
                "period": str(history_config.get("period") or "current_month"),
                "summary": (
                    f"本月已到访{current_count}个不同自然日，还差{remaining}天。"
                    f"{'今天已到访；同日多次不重复计数。' if visited_today else '今天尚未到访；可优先选择能到达目标点的订单或reposition。'}"
                ),
            }
        )
    return summaries


def build_action_options(
    *,
    preference_context: dict[str, Any],
    tool_plan: dict[str, dict[str, Any]],
    context: dict[str, Any],
    history_geo_summary: list[dict[str, Any]],
    scored_candidates: list[dict[str, Any]],
    status: dict[str, Any],
    speed_km_per_hour: float,
) -> list[dict[str, Any]]:
    options: list[dict[str, Any]] = []
    current_lat = coerce_float(status.get("current_lat"), float("nan"))
    current_lng = coerce_float(status.get("current_lng"), float("nan"))
    if current_lat != current_lat or current_lng != current_lng:
        return options

    contributing_pref_ids = _candidate_contributing_pref_ids(scored_candidates)
    for item in history_geo_summary:
        pref_id = str(item.get("pref_id") or "")
        if not pref_id or pref_id in contributing_pref_ids:
            continue
        if bool(item.get("visited_today")) or coerce_int(item.get("remaining_visit_count")) <= 0:
            continue
        target_config = _paired_visit_target(tool_plan.get(pref_id, {}))
        target_lat_lng = _target_reposition_point(target_config or {})
        if target_lat_lng is None:
            continue
        target_lat, target_lng = target_lat_lng
        distance_km = haversine_km(current_lat, current_lng, target_lat, target_lng)
        estimated_minutes = distance_to_minutes(distance_km, speed_km_per_hour)
        options.append(
            make_reposition_option(
                option_id=f"reposition_{pref_id}",
                pref_id=pref_id,
                target_lat=target_lat,
                target_lng=target_lng,
                status=status,
                distance_km=distance_km,
                estimated_minutes=estimated_minutes,
                reason=item.get("summary"),
                tool_origin="history_geo_summary",
                extra={"history_geo_summary": item},
            )
        )
    return options


def build_history_geo_summary(
    *,
    preference_context: dict[str, Any],
    tool_plan: dict[str, dict[str, Any]],
    event_log: list[dict[str, Any]],
    current_minutes: int,
) -> list[dict[str, Any]]:
    return build_context(
        preference_context=preference_context,
        tool_plan=tool_plan,
        event_log=event_log,
        current_minutes=current_minutes,
    )


def build_geo_reposition_options(
    *,
    history_geo_summary: list[dict[str, Any]],
    scored_candidates: list[dict[str, Any]],
    status: dict[str, Any],
    speed_km_per_hour: float,
    preference_context: dict[str, Any] | None = None,
    tool_plan: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    if tool_plan is None:
        tool_plan = _legacy_tool_plan_from_summary(history_geo_summary)
    return build_action_options(
        preference_context=preference_context or {},
        tool_plan=tool_plan,
        context={"history_geo_summary": history_geo_summary},
        history_geo_summary=history_geo_summary,
        scored_candidates=scored_candidates,
        status=status,
        speed_km_per_hour=speed_km_per_hour,
    )


def _brief_by_pref_id(preference_context: dict[str, Any]) -> dict[str, dict[str, Any]]:
    brief = preference_context.get("preference_brief")
    if not isinstance(brief, list):
        return {}
    return {str(item.get("pref_id") or ""): item for item in brief if isinstance(item, dict)}


def _paired_visit_target(tools: dict[str, Any]) -> dict[str, Any] | None:
    history_config = tools.get("history_geo_summary") if isinstance(tools.get("history_geo_summary"), dict) else {}
    if _has_geo_target(history_config):
        return history_config
    contribution_config = tools.get("candidate_geo_contribution")
    if isinstance(contribution_config, dict) and _has_geo_target(contribution_config):
        return contribution_config
    return None


def _has_geo_target(config: dict[str, Any]) -> bool:
    center = config.get("center")
    if isinstance(center, list) and len(center) == 2 and config.get("radius_km") is not None:
        return True
    return isinstance(config.get("lat_range"), list) and isinstance(config.get("lng_range"), list)


def _required_visit_count(config: dict[str, Any]) -> int:
    value = coerce_int(config.get("required_visit_count"), 0)
    if value > 0:
        return value
    reason = str(config.get("reason") or "")
    match = re.search(r"required_visit_count\s*=\s*(\d+)", reason)
    if match:
        return max(1, int(match.group(1)))
    return 1


def _days_until_month_end(current_minutes: int) -> int:
    current_dt = SIMULATION_EPOCH + timedelta(minutes=int(current_minutes))
    last_day = calendar.monthrange(current_dt.year, current_dt.month)[1]
    return max(0, last_day - current_dt.day)


def _target_public_shape(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "center": config.get("center"),
        "radius_km": config.get("radius_km"),
        "lat_range": config.get("lat_range"),
        "lng_range": config.get("lng_range"),
    }


def _target_reposition_point(target: dict[str, Any]) -> tuple[float, float] | None:
    center = target.get("center")
    if isinstance(center, list) and len(center) == 2:
        return coerce_float(center[0]), coerce_float(center[1])
    lat_range = target.get("lat_range")
    lng_range = target.get("lng_range")
    if isinstance(lat_range, list) and len(lat_range) == 2 and isinstance(lng_range, list) and len(lng_range) == 2:
        return (coerce_float(lat_range[0]) + coerce_float(lat_range[1])) / 2.0, (
            coerce_float(lng_range[0]) + coerce_float(lng_range[1])
        ) / 2.0
    return None


def _legacy_tool_plan_from_summary(history_geo_summary: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    plan: dict[str, dict[str, Any]] = {}
    for item in history_geo_summary:
        if not isinstance(item, dict):
            continue
        pref_id = str(item.get("pref_id") or "")
        target = item.get("target")
        if pref_id and isinstance(target, dict):
            plan[pref_id] = {"history_geo_summary": target, "candidate_geo_contribution": target}
    return plan


def _candidate_contributing_pref_ids(candidates: list[dict[str, Any]]) -> set[str]:
    pref_ids: set[str] = set()
    for candidate in candidates:
        evidence = candidate.get("tool_evidence") if isinstance(candidate.get("tool_evidence"), dict) else {}
        results = evidence.get("candidate_geo_contribution_result") if isinstance(evidence, dict) else []
        if not isinstance(results, list):
            continue
        for result in results:
            if isinstance(result, dict) and result.get("can_contribute"):
                pref_ids.add(str(result.get("pref_id") or ""))
    return {pref_id for pref_id in pref_ids if pref_id}
