"""Factory for reposition action options."""

from __future__ import annotations

from typing import Any

from agent.history.time_utils import coerce_float, coerce_int, format_simulation_time


def make_reposition_option(
    *,
    option_id: str,
    pref_id: str,
    target_lat: float,
    target_lng: float,
    status: dict[str, Any],
    distance_km: float,
    estimated_minutes: int,
    reason: str | None,
    tool_origin: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    start_minutes = coerce_int(status.get("simulation_progress_minutes"))
    end_minutes = start_minutes + int(estimated_minutes)
    current_lat = coerce_float(status.get("current_lat"))
    current_lng = coerce_float(status.get("current_lng"))
    position_before = {"lat": current_lat, "lng": current_lng}
    position_after = {"lat": round(float(target_lat), 6), "lng": round(float(target_lng), 6)}
    option = {
        "option_id": option_id,
        "action": "reposition",
        "params": {"latitude": position_after["lat"], "longitude": position_after["lng"]},
        "action_start_minutes": start_minutes,
        "action_start_time": status.get("simulation_wall_time") or format_simulation_time(start_minutes),
        "action_end_minutes": end_minutes,
        "action_end_time": format_simulation_time(end_minutes),
        "position_before": position_before,
        "position_after": position_after,
        "distance_km": round(float(distance_km), 2),
        "estimated_minutes": int(estimated_minutes),
        "pref_id": pref_id,
        "reason": reason,
        "tool_origin": tool_origin,
        "metrics": {
            "distance_km": round(float(distance_km), 2),
            "estimated_minutes": int(estimated_minutes),
        },
    }
    if extra:
        option.update(extra)
    return option
