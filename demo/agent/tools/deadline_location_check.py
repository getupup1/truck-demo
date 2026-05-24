"""Deadline-location evidence and controlled reposition generation."""

from __future__ import annotations

from typing import Any

from agent.history.time_utils import coerce_float, coerce_int, distance_to_minutes, format_simulation_time, haversine_km
from agent.tools.reposition_factory import make_reposition_option
from agent.tools.time_window_checks import next_time_of_day_minutes


def evaluate_candidate(
    brief: dict[str, Any],
    config: dict[str, Any],
    candidate: dict[str, Any],
    *,
    speed_km_per_hour: float,
) -> dict[str, Any] | None:
    simulated_event = candidate.get("simulated_event") if isinstance(candidate.get("simulated_event"), dict) else {}
    finish_minutes = coerce_int(simulated_event.get("action_end_minutes"), -1)
    start_minutes = coerce_int(simulated_event.get("action_start_minutes"), -1)
    if finish_minutes < 0 or start_minutes < 0:
        return None
    position_after = simulated_event.get("position_after") if isinstance(simulated_event.get("position_after"), dict) else {}
    center = config.get("center")
    if not isinstance(center, list) or len(center) != 2:
        return None
    lat = coerce_float(position_after.get("lat"), float("nan"))
    lng = coerce_float(position_after.get("lng"), float("nan"))
    target_lat = coerce_float(center[0], float("nan"))
    target_lng = coerce_float(center[1], float("nan"))
    if lat != lat or lng != lng or target_lat != target_lat or target_lng != target_lng:
        return None
    distance_km = haversine_km(lat, lng, target_lat, target_lng)
    travel_minutes = distance_to_minutes(distance_km, speed_km_per_hour)
    arrival_minutes = finish_minutes + travel_minutes
    deadline_minutes = next_time_of_day_minutes(start_minutes, str(config.get("deadline_time") or ""))
    if deadline_minutes is None:
        return None
    can_reach = arrival_minutes <= deadline_minutes
    return {
        "pref_id": brief.get("pref_id"),
        "cargo_id": candidate.get("cargo_id"),
        "can_reach_deadline": can_reach,
        "deadline_minutes": deadline_minutes,
        "deadline_time": format_simulation_time(deadline_minutes),
        "arrival_if_go_to_target_minutes": arrival_minutes,
        "arrival_if_go_to_target_time": format_simulation_time(arrival_minutes),
        "travel_to_target_minutes": travel_minutes,
        "distance_to_target_km": round(distance_km, 2),
        "summary": (
            f"{'可达' if can_reach else '不可达'}。该订单{format_simulation_time(finish_minutes)}完成后，"
            f"从完成位置到目标点还需{travel_minutes}分钟，预计{format_simulation_time(arrival_minutes)}到达，"
            f"期限为{format_simulation_time(deadline_minutes)}。"
        ),
    }


def build_action_options(
    *,
    preference_context: dict[str, Any],
    tool_plan: dict[str, dict[str, Any]],
    context: dict[str, Any] | None = None,
    scored_candidates: list[dict[str, Any]],
    status: dict[str, Any],
    speed_km_per_hour: float,
) -> list[dict[str, Any]]:
    current_lat = coerce_float(status.get("current_lat"), float("nan"))
    current_lng = coerce_float(status.get("current_lng"), float("nan"))
    current_minutes = coerce_int(status.get("simulation_progress_minutes"))
    if current_lat != current_lat or current_lng != current_lng:
        return []

    brief_by_id = _brief_by_pref_id(preference_context)
    options: list[dict[str, Any]] = []
    for pref_id, tools in tool_plan.items():
        config = tools.get("deadline_location_check")
        if not isinstance(config, dict) or _any_candidate_can_reach_deadline(scored_candidates, pref_id):
            continue
        center = config.get("center")
        if not isinstance(center, list) or len(center) != 2:
            continue
        target_lat = coerce_float(center[0], float("nan"))
        target_lng = coerce_float(center[1], float("nan"))
        if target_lat != target_lat or target_lng != target_lng:
            continue
        distance_km = haversine_km(current_lat, current_lng, target_lat, target_lng)
        estimated_minutes = distance_to_minutes(distance_km, speed_km_per_hour)
        end_minutes = current_minutes + estimated_minutes
        deadline_minutes = next_time_of_day_minutes(current_minutes, str(config.get("deadline_time") or ""))
        can_reach = deadline_minutes is not None and end_minutes <= deadline_minutes
        brief = brief_by_id.get(pref_id, {"pref_id": pref_id})
        reason = (
            f"所有候选订单都无法确认满足该期限地点偏好，当前直接reposition预计"
            f"{format_simulation_time(end_minutes)}到达目标点。"
        )
        options.append(
            make_reposition_option(
                option_id=f"deadline_reposition_{pref_id}",
                pref_id=pref_id,
                target_lat=target_lat,
                target_lng=target_lng,
                status=status,
                distance_km=distance_km,
                estimated_minutes=estimated_minutes,
                reason=reason,
                tool_origin="deadline_location_check",
                extra={
                    "deadline_location_check": {
                        "pref_id": pref_id,
                        "source_text": brief.get("source_text"),
                        "target": {"center": config.get("center"), "radius_km": config.get("radius_km")},
                        "deadline_time": config.get("deadline_time"),
                        "deadline_minutes": deadline_minutes,
                        "deadline_wall_time": format_simulation_time(deadline_minutes),
                        "arrival_if_go_to_target_minutes": end_minutes,
                        "arrival_if_go_to_target_time": format_simulation_time(end_minutes),
                        "can_reach_deadline": can_reach,
                        "summary": reason,
                    }
                },
            )
        )
    return options


def build_deadline_reposition_options(
    *,
    preference_context: dict[str, Any],
    tool_plan: dict[str, dict[str, Any]],
    scored_candidates: list[dict[str, Any]],
    status: dict[str, Any],
    speed_km_per_hour: float,
) -> list[dict[str, Any]]:
    return build_action_options(
        preference_context=preference_context,
        tool_plan=tool_plan,
        context=None,
        scored_candidates=scored_candidates,
        status=status,
        speed_km_per_hour=speed_km_per_hour,
    )


def _any_candidate_can_reach_deadline(candidates: list[dict[str, Any]], pref_id: str) -> bool:
    for candidate in candidates:
        evidence = candidate.get("tool_evidence") if isinstance(candidate.get("tool_evidence"), dict) else {}
        results = evidence.get("deadline_location_check_result") if isinstance(evidence, dict) else []
        if not isinstance(results, list):
            continue
        for result in results:
            if isinstance(result, dict) and str(result.get("pref_id") or "") == pref_id and result.get("can_reach_deadline"):
                return True
    return False


def _brief_by_pref_id(preference_context: dict[str, Any]) -> dict[str, dict[str, Any]]:
    brief = preference_context.get("preference_brief")
    if not isinstance(brief, list):
        return {}
    return {str(item.get("pref_id") or ""): item for item in brief if isinstance(item, dict)}
