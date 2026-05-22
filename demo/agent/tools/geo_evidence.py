"""Geographic preference evidence helpers."""

from __future__ import annotations

import calendar
import re
from datetime import timedelta
from typing import Any

from agent.history.time_utils import (
    SIMULATION_EPOCH,
    coerce_float,
    coerce_int,
    distance_to_minutes,
    haversine_km,
)
from agent.tools.temporal_evidence import make_reposition_option


def attach_candidate_geo_evidence(
    candidates: list[dict[str, Any]],
    preference_context: dict[str, Any],
) -> list[dict[str, Any]]:
    """Attach geo tool evidence to each filtered candidate."""
    briefs = _briefs_with_tools(preference_context)
    if not briefs:
        return candidates

    enhanced: list[dict[str, Any]] = []
    for candidate in candidates:
        item = dict(candidate)
        tool_evidence: dict[str, Any] = dict(item.get("tool_evidence") or {})
        geo_results: list[dict[str, Any]] = []
        contribution_results: list[dict[str, Any]] = []
        for brief in briefs:
            tools = brief.get("tools") if isinstance(brief.get("tools"), dict) else {}
            geo_config = tools.get("geo_checks")
            if isinstance(geo_config, dict):
                result = _geo_check_candidate(brief, geo_config, item)
                if result is not None:
                    geo_results.append(result)
            contribution_config = tools.get("candidate_geo_contribution")
            if isinstance(contribution_config, dict):
                result = _candidate_geo_contribution(brief, contribution_config, item)
                if result is not None:
                    contribution_results.append(result)
        if geo_results:
            tool_evidence["geo_checks_result"] = geo_results
        if contribution_results:
            tool_evidence["candidate_geo_contribution_result"] = contribution_results
        if tool_evidence:
            item["tool_evidence"] = tool_evidence
        enhanced.append(item)
    return enhanced


def build_history_geo_summary(
    *,
    preference_context: dict[str, Any],
    event_log: list[dict[str, Any]],
    current_minutes: int,
) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    current_day = int(current_minutes) // 1440
    for brief in _briefs_with_tools(preference_context):
        tools = brief.get("tools") if isinstance(brief.get("tools"), dict) else {}
        history_config = tools.get("history_geo_summary")
        if not isinstance(history_config, dict):
            continue
        target_config = _paired_visit_target(tools)
        if target_config is None:
            continue

        visited_days = set()
        matched_steps: list[Any] = []
        for event in event_log:
            if event.get("action") not in {"take_order", "reposition", "wait"}:
                continue
            position_after = event.get("position_after") if isinstance(event.get("position_after"), dict) else {}
            lat = coerce_float(position_after.get("lat"), float("nan"))
            lng = coerce_float(position_after.get("lng"), float("nan"))
            if lat != lat or lng != lng:
                continue
            inside, _fact = _point_inside_target(target_config, lat, lng)
            if inside:
                event_day = coerce_int(event.get("action_end_minutes"), coerce_int(event.get("action_start_minutes"))) // 1440
                visited_days.add(event_day)
                matched_steps.append(event.get("step"))

        required = _required_visit_count(history_config)
        current_count = len(visited_days)
        remaining = max(0, required - current_count)
        visited_today = current_day in visited_days
        summary = {
            "pref_id": brief.get("pref_id"),
            "source_text": brief.get("source_text"),
            "visited_today": visited_today,
            "current_visit_count": current_count,
            "required_visit_count": required,
            "remaining_visit_count": remaining,
            "days_until_deadline": _days_until_month_end(current_minutes),
            "period": str(history_config.get("period") or "current_month"),
            "matched_history_events": matched_steps,
            "target": _target_public_shape(target_config),
            "summary": (
                f"本月已到访{current_count}个不同自然日，还差{remaining}天。"
                f"{'今天已到访；同日多次不重复计数。' if visited_today else '今天尚未到访；可优先选择能到达目标点的订单或reposition。'}"
            ),
        }
        summaries.append(summary)
    return summaries


def build_geo_reposition_options(
    *,
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
        target = item.get("target") if isinstance(item.get("target"), dict) else {}
        target_lat_lng = _target_reposition_point(target)
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


def _briefs_with_tools(preference_context: dict[str, Any]) -> list[dict[str, Any]]:
    brief = preference_context.get("preference_brief")
    if not isinstance(brief, list):
        return []
    return [item for item in brief if isinstance(item, dict) and isinstance(item.get("tools"), dict) and item.get("tools")]


def _geo_check_candidate(
    brief: dict[str, Any],
    config: dict[str, Any],
    candidate: dict[str, Any],
) -> dict[str, Any] | None:
    points = _candidate_key_points(candidate)
    if not points:
        return None
    relation = str(config.get("relation") or "")
    point_facts: list[str] = []
    violating_points: list[str] = []
    for label, lat, lng in points:
        inside, fact = _point_inside_target(config, lat, lng)
        point_facts.append(f"{label}{fact}")
        if relation == "forbidden_inside" and inside:
            violating_points.append(label)
        elif relation == "must_inside" and not inside:
            violating_points.append(label)
    violates = bool(violating_points)
    conclusion = "违反" if violates else "未违反"
    return {
        "pref_id": brief.get("pref_id"),
        "cargo_id": candidate.get("cargo_id"),
        "violates": violates,
        "summary": f"{conclusion}。{'; '.join(point_facts)}。",
    }


def _candidate_geo_contribution(
    brief: dict[str, Any],
    config: dict[str, Any],
    candidate: dict[str, Any],
) -> dict[str, Any] | None:
    simulated_event = candidate.get("simulated_event") if isinstance(candidate.get("simulated_event"), dict) else {}
    position_after = simulated_event.get("position_after") if isinstance(simulated_event.get("position_after"), dict) else {}
    lat = coerce_float(position_after.get("lat"), float("nan"))
    lng = coerce_float(position_after.get("lng"), float("nan"))
    if lat != lat or lng != lng:
        return None
    can_contribute, fact = _point_inside_target(config, lat, lng)
    conclusion = "可以增加到访次数" if can_contribute else "不能增加到访次数"
    return {
        "pref_id": brief.get("pref_id"),
        "cargo_id": candidate.get("cargo_id"),
        "can_contribute": can_contribute,
        "summary": f"{conclusion}。订单完成位置{fact}。",
    }


def _candidate_key_points(candidate: dict[str, Any]) -> list[tuple[str, float, float]]:
    points: list[tuple[str, float, float]] = []
    simulated_event = candidate.get("simulated_event") if isinstance(candidate.get("simulated_event"), dict) else {}
    for label, value in (
        ("当前位置", simulated_event.get("position_before")),
        ("装货点", (candidate.get("start") if isinstance(candidate.get("start"), dict) else {})),
        ("卸货点", (candidate.get("end") if isinstance(candidate.get("end"), dict) else {})),
        ("完成位置", simulated_event.get("position_after")),
    ):
        if not isinstance(value, dict):
            continue
        lat = coerce_float(value.get("lat"), float("nan"))
        lng = coerce_float(value.get("lng"), float("nan"))
        if lat == lat and lng == lng:
            points.append((label, lat, lng))
    return points


def _point_inside_target(config: dict[str, Any], lat: float, lng: float) -> tuple[bool, str]:
    center = config.get("center")
    radius = config.get("radius_km")
    if isinstance(center, list) and len(center) == 2 and radius is not None:
        center_lat = coerce_float(center[0], float("nan"))
        center_lng = coerce_float(center[1], float("nan"))
        radius_km = coerce_float(radius, -1.0)
        distance = haversine_km(lat, lng, center_lat, center_lng)
        inside = distance <= radius_km
        return inside, f"距离目标圆心{round(distance, 2)}km，半径{round(radius_km, 2)}km，位置={'in' if inside else 'not in'}"

    lat_range = config.get("lat_range")
    lng_range = config.get("lng_range")
    if isinstance(lat_range, list) and len(lat_range) == 2 and isinstance(lng_range, list) and len(lng_range) == 2:
        lat_min, lat_max = coerce_float(lat_range[0]), coerce_float(lat_range[1])
        lng_min, lng_max = coerce_float(lng_range[0]), coerce_float(lng_range[1])
        inside = lat_min <= lat <= lat_max and lng_min <= lng <= lng_max
        return inside, f"坐标({round(lat, 6)},{round(lng, 6)})相对经纬度范围位置={'in' if inside else 'not in'}"

    return False, "缺少可计算的目标区域"


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
