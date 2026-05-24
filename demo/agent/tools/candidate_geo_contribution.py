"""Candidate-level visit contribution checks."""

from __future__ import annotations

from typing import Any

from agent.history.time_utils import coerce_float
from agent.tools.geo_checks import point_inside_target


def evaluate_candidate(brief: dict[str, Any], config: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any] | None:
    simulated_event = candidate.get("simulated_event") if isinstance(candidate.get("simulated_event"), dict) else {}
    points = []
    start = candidate.get("start") if isinstance(candidate.get("start"), dict) else {}
    position_after = simulated_event.get("position_after") if isinstance(simulated_event.get("position_after"), dict) else {}
    for label, value in (("接货位置", start), ("完成位置", position_after)):
        lat = coerce_float(value.get("lat"), float("nan"))
        lng = coerce_float(value.get("lng"), float("nan"))
        if lat == lat and lng == lng:
            points.append((label, lat, lng))
    if not points:
        return None
    checked_points: list[dict[str, Any]] = []
    matched_points: list[str] = []
    facts: list[str] = []
    for label, lat, lng in points:
        inside, fact = point_inside_target(config, lat, lng)
        checked_points.append({"label": label, "lat": round(lat, 6), "lng": round(lng, 6), "inside": inside})
        facts.append(f"{label}{fact}")
        if inside:
            matched_points.append(label)
    can_contribute = bool(matched_points)
    conclusion = "可以增加到访次数" if can_contribute else "不能增加到访次数"
    return {
        "pref_id": brief.get("pref_id"),
        "cargo_id": candidate.get("cargo_id"),
        "can_contribute": can_contribute,
        "checked_points": checked_points,
        "matched_points": matched_points,
        "summary": f"{conclusion}。{'; '.join(facts)}。",
    }
