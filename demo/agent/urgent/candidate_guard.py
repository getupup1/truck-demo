"""Urgent candidate guards for planning_guard tasks."""

from __future__ import annotations

from typing import Any

from agent.history.time_utils import coerce_float, coerce_int, distance_to_minutes, haversine_km, parse_wall_time_minutes


def apply_urgent_candidate_guard(
    candidates: list[dict[str, Any]],
    urgent_task: dict[str, Any],
    *,
    speed_km_per_hour: float,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    guards = urgent_task.get("candidate_guards")
    if not isinstance(guards, list) or not guards:
        return candidates, {"input_count": len(candidates), "kept_count": len(candidates), "removed_count": 0}

    kept: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    for candidate in candidates:
        results = [_evaluate_guard(candidate, guard, speed_km_per_hour=speed_km_per_hour) for guard in guards]
        if all(result.get("safe") for result in results):
            kept.append(candidate)
        else:
            removed.append({"cargo_id": candidate.get("cargo_id"), "guard_results": results})
    return kept, {
        "input_count": len(candidates),
        "kept_count": len(kept),
        "removed_count": len(removed),
        "removed": removed,
    }


def _evaluate_guard(
    candidate: dict[str, Any],
    guard: dict[str, Any],
    *,
    speed_km_per_hour: float,
) -> dict[str, Any]:
    tool = str(guard.get("tool") or "").strip()
    if tool != "deadline_location_check":
        return {"tool": tool, "safe": False, "reason": "unsupported urgent guard"}
    config = guard.get("config") if isinstance(guard.get("config"), dict) else {}
    return _deadline_guard(candidate, config, speed_km_per_hour=speed_km_per_hour)


def _deadline_guard(
    candidate: dict[str, Any],
    config: dict[str, Any],
    *,
    speed_km_per_hour: float,
) -> dict[str, Any]:
    simulated_event = candidate.get("simulated_event") if isinstance(candidate.get("simulated_event"), dict) else {}
    finish_minutes = coerce_int(simulated_event.get("action_end_minutes"), -1)
    position_after = simulated_event.get("position_after") if isinstance(simulated_event.get("position_after"), dict) else {}
    center = config.get("center")
    deadline_minutes = parse_wall_time_minutes(config.get("deadline_wall_time"))
    if finish_minutes < 0 or deadline_minutes is None or not isinstance(center, list) or len(center) != 2:
        return {"tool": "deadline_location_check", "safe": False, "reason": "missing finish time, deadline, or target"}
    lat = coerce_float(position_after.get("lat"), float("nan"))
    lng = coerce_float(position_after.get("lng"), float("nan"))
    target_lat = coerce_float(center[0], float("nan"))
    target_lng = coerce_float(center[1], float("nan"))
    if lat != lat or lng != lng or target_lat != target_lat or target_lng != target_lng:
        return {"tool": "deadline_location_check", "safe": False, "reason": "invalid coordinates"}
    radius_km = max(0.0, coerce_float(config.get("radius_km"), 0.0))
    raw_distance_km = haversine_km(lat, lng, target_lat, target_lng)
    travel_distance_km = max(0.0, raw_distance_km - radius_km)
    travel_minutes = 0 if travel_distance_km <= 1e-6 else distance_to_minutes(travel_distance_km, speed_km_per_hour)
    arrival_minutes = finish_minutes + travel_minutes
    safe = arrival_minutes <= deadline_minutes
    return {
        "tool": "deadline_location_check",
        "safe": safe,
        "arrival_minutes": arrival_minutes,
        "deadline_minutes": deadline_minutes,
        "distance_to_target_km": round(raw_distance_km, 2),
        "travel_minutes": travel_minutes,
        "reason": "can reach urgent target before deadline" if safe else "cannot reach urgent target before deadline",
    }
