"""Candidate-level geographic range checks."""

from __future__ import annotations

from typing import Any

from agent.history.time_utils import coerce_float, haversine_km


def evaluate_candidate(brief: dict[str, Any], config: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any] | None:
    points = candidate_key_points(candidate)
    if not points:
        return None
    relation = str(config.get("relation") or "")
    point_facts: list[str] = []
    violating_points: list[str] = []
    for label, lat, lng in points:
        inside, fact = point_inside_target(config, lat, lng)
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


def candidate_key_points(candidate: dict[str, Any]) -> list[tuple[str, float, float]]:
    points: list[tuple[str, float, float]] = []
    simulated_event = candidate.get("simulated_event") if isinstance(candidate.get("simulated_event"), dict) else {}
    for label, value in (
        ("当前位置", simulated_event.get("position_before")),
        ("装货点", candidate.get("start") if isinstance(candidate.get("start"), dict) else {}),
        ("卸货点", candidate.get("end") if isinstance(candidate.get("end"), dict) else {}),
        ("完成位置", simulated_event.get("position_after")),
    ):
        if not isinstance(value, dict):
            continue
        lat = coerce_float(value.get("lat"), float("nan"))
        lng = coerce_float(value.get("lng"), float("nan"))
        if lat == lat and lng == lng:
            points.append((label, lat, lng))
    return points


def point_inside_target(config: dict[str, Any], lat: float, lng: float) -> tuple[bool, str]:
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
