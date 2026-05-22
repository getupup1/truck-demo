"""Enhance query_cargo results with deterministic simulated take-order events."""

from __future__ import annotations

from typing import Any

from agent.history.time_utils import (
    coerce_float,
    coerce_int,
    format_simulation_time,
    haversine_km,
    parse_wall_time_minutes,
    pickup_distance_to_minutes,
)


def _load_window_minutes(cargo: dict[str, Any]) -> tuple[int, int] | None:
    raw = cargo.get("load_time")
    if raw is None:
        return None
    if not isinstance(raw, list) or len(raw) != 2:
        return None
    start = parse_wall_time_minutes(raw[0])
    end = parse_wall_time_minutes(raw[1])
    if start is None or end is None or end < start:
        return None
    return start, end


def enhance_cargo_candidates(
    items: list[dict[str, Any]],
    status_after_scan: dict[str, Any],
    *,
    speed_km_per_hour: float = 60.0,
    simulation_horizon_minutes: int | None = None,
    limit: int = 30,
) -> list[dict[str, Any]]:
    """Build candidate features using only API-visible cargo and driver status."""
    current_lat = coerce_float(status_after_scan.get("current_lat"))
    current_lng = coerce_float(status_after_scan.get("current_lng"))
    t0_minutes = coerce_int(status_after_scan.get("simulation_progress_minutes"))
    t0_time = status_after_scan.get("simulation_wall_time") or format_simulation_time(t0_minutes)

    enhanced: list[dict[str, Any]] = []
    for item in items[: max(0, int(limit))]:
        cargo = item.get("cargo")
        if not isinstance(cargo, dict):
            continue
        start = cargo.get("start") if isinstance(cargo.get("start"), dict) else {}
        end = cargo.get("end") if isinstance(cargo.get("end"), dict) else {}
        start_lat = coerce_float(start.get("lat"), current_lat)
        start_lng = coerce_float(start.get("lng"), current_lng)
        end_lat = coerce_float(end.get("lat"), start_lat)
        end_lng = coerce_float(end.get("lng"), start_lng)

        pickup_deadhead_km = coerce_float(item.get("distance_km"), -1.0)
        if pickup_deadhead_km < 0:
            pickup_deadhead_km = haversine_km(current_lat, current_lng, start_lat, start_lng)
        pickup_minutes = pickup_distance_to_minutes(pickup_deadhead_km, speed_km_per_hour)
        arrival_minutes = t0_minutes + pickup_minutes

        load_window = _load_window_minutes(cargo)
        load_time_missed = False
        waiting_minutes = 0
        ready_minutes = arrival_minutes
        if load_window is not None:
            load_start, load_end = load_window
            load_time_missed = arrival_minutes > load_end
            if not load_time_missed:
                waiting_minutes = max(0, load_start - arrival_minutes)
                ready_minutes = arrival_minutes + waiting_minutes

        cost_time_minutes = coerce_int(cargo.get("cost_time_minutes"))
        if load_time_missed:
            finish_minutes: int | None = None
            estimated_total_minutes: int | None = None
        else:
            finish_minutes = ready_minutes + cost_time_minutes
            estimated_total_minutes = pickup_minutes + waiting_minutes + cost_time_minutes

        remove_minutes = parse_wall_time_minutes(cargo.get("remove_time"))
        expired_after_scan = remove_minutes is not None and remove_minutes <= t0_minutes
        income_eligible = (
            None
            if simulation_horizon_minutes is None or finish_minutes is None
            else finish_minutes <= simulation_horizon_minutes
        )
        haul_distance_km = haversine_km(start_lat, start_lng, end_lat, end_lng)
        cargo_id = str(cargo.get("cargo_id", "")).strip()

        candidate = {
            "cargo_id": cargo_id,
            "cargo_name": cargo.get("cargo_name"),
            "gross_income": cargo.get("price"),
            "cost_time_minutes": cost_time_minutes,
            "remove_time": cargo.get("remove_time"),
            "remove_minutes": remove_minutes,
            "load_time": cargo.get("load_time"),
            "start": cargo.get("start"),
            "end": cargo.get("end"),
            "pickup_deadhead_km": round(float(pickup_deadhead_km), 2),
            "pickup_minutes": pickup_minutes,
            "haul_distance_km": round(float(haul_distance_km), 2),
            "arrival_minutes": arrival_minutes,
            "arrival_time": format_simulation_time(arrival_minutes),
            "waiting_minutes": waiting_minutes,
            "finish_minutes": finish_minutes,
            "finish_time": format_simulation_time(finish_minutes),
            "estimated_total_minutes": estimated_total_minutes,
            "load_time_missed": load_time_missed,
            "expired_after_scan": expired_after_scan,
            "income_eligible": income_eligible,
        }
        candidate["simulated_event"] = {
            "action": "take_order",
            "simulation_status": "simulated_failure" if load_time_missed else "simulated_success",
            "params": {"cargo_id": cargo_id},
            "action_start_minutes": t0_minutes,
            "action_start_time": t0_time,
            "action_end_minutes": finish_minutes,
            "action_end_time": format_simulation_time(finish_minutes),
            "position_before": {"lat": current_lat, "lng": current_lng},
            "position_after": {"lat": end_lat, "lng": end_lng},
            "cargo": {
                "cargo_id": cargo_id,
                "cargo_name": cargo.get("cargo_name"),
                "gross_income": cargo.get("price"),
                "load_time": cargo.get("load_time"),
                "remove_time": cargo.get("remove_time"),
                "start": cargo.get("start"),
                "end": cargo.get("end"),
                "cost_time_minutes": cost_time_minutes,
            },
            "metrics": {
                "pickup_deadhead_km": round(float(pickup_deadhead_km), 2),
                "pickup_minutes": pickup_minutes,
                "waiting_minutes": waiting_minutes,
                "haul_distance_km": round(float(haul_distance_km), 2),
                "estimated_total_minutes": estimated_total_minutes,
                "income_eligible": income_eligible,
            },
            "failure_flags": {
                "expired_after_scan": expired_after_scan,
                "load_time_missed": load_time_missed,
                "invalid_estimated_total_minutes": not isinstance(estimated_total_minutes, int)
                or estimated_total_minutes <= 0,
            },
        }
        enhanced.append(candidate)
    return enhanced
