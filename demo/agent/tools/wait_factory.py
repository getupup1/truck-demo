"""Factory for wait action options."""

from __future__ import annotations

from typing import Any

from agent.history.time_utils import coerce_float, coerce_int, format_simulation_time


def make_wait_option(
    *,
    duration_minutes: int,
    status: dict[str, Any] | None,
    pref_id: str | None = None,
    source: str = "default_wait",
    reason: str | None = None,
) -> dict[str, Any]:
    duration = max(1, int(duration_minutes))
    start_minutes = coerce_int((status or {}).get("simulation_progress_minutes"))
    end_minutes = start_minutes + duration
    lat = coerce_float((status or {}).get("current_lat"))
    lng = coerce_float((status or {}).get("current_lng"))
    position = {"lat": lat, "lng": lng}
    option = {
        "option_id": f"{source}_{duration}" if not pref_id else f"{source}_{pref_id}_{duration}",
        "action": "wait",
        "params": {"duration_minutes": duration},
        "action_start_minutes": start_minutes,
        "action_start_time": (status or {}).get("simulation_wall_time") or format_simulation_time(start_minutes),
        "action_end_minutes": end_minutes,
        "action_end_time": format_simulation_time(end_minutes),
        "position": position,
        "position_before": position,
        "position_after": position,
        "source": source,
        "reason": reason or f"等待 {duration} 分钟",
        "metrics": {"duration_minutes": duration},
    }
    if pref_id:
        option["pref_id"] = pref_id
    return option
