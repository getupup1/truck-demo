"""月度区域访问额度 DSL 与 progress 计算。"""

from __future__ import annotations

import math
from typing import Any

from agent.cargo_simulator import haversine_km, simulation_minutes_to_wall_time
from agent.preferences.region_rules import validate_region

MINUTES_PER_DAY = 24 * 60
SIMULATION_MONTH_DAYS = 31
REGION_VISIT_QUOTA = "region_visit_quota"
_SUPPORTED_EVENTS = {"position_arrival", "cargo_touch"}
_SUPPORTED_COUNT_BY = {"distinct_day", "occurrence"}


def build_region_visit_quota_rule(
    rule_id: str,
    region: dict[str, Any],
    event: str,
    count_by: str,
    min_count: int,
) -> dict[str, Any]:
    """构造“月内至少访问目标区域若干次或若干天”的原子 DSL。"""
    rule = {
        "rule_id": rule_id,
        "type": REGION_VISIT_QUOTA,
        "progress_kind": "monthly_quota",
        "active_period": None,
        "params": {
            "region": dict(region),
            "event": event,
            "count_by": count_by,
            "min_count": min_count,
        },
    }
    validate_region_visit_quota_rule(rule)
    return rule


def calculate_region_visit_quota_progress(
    rule: dict[str, Any],
    trajectory_snapshot: dict[str, Any],
    now_minutes: int,
) -> dict[str, Any]:
    """根据轨迹事实计算月度区域访问额度。"""
    validate_region_visit_quota_rule(rule)
    now_minutes = _validate_now_minutes(now_minutes)
    events = _read_events(trajectory_snapshot)
    params = rule["params"]
    matched_days: set[int] = set()
    matched_occurrences = 0
    for event in events:
        visit_days = _matching_visit_days(event, params["event"], params["region"], now_minutes)
        matched_days.update(visit_days)
        matched_occurrences += len(visit_days)

    count_by = params["count_by"]
    completed_count = len(matched_days) if count_by == "distinct_day" else matched_occurrences
    required_count = int(params["min_count"])
    remaining_count = max(0, required_count - completed_count)
    month_end_minutes = SIMULATION_MONTH_DAYS * MINUTES_PER_DAY
    remaining_days = math.ceil(max(0, month_end_minutes - now_minutes) / MINUTES_PER_DAY)
    return {
        "rule_id": rule["rule_id"],
        "type": REGION_VISIT_QUOTA,
        "progress_kind": "monthly_quota",
        "as_of_minutes": now_minutes,
        "as_of_time": simulation_minutes_to_wall_time(now_minutes),
        "required_count": required_count,
        "completed_count": completed_count,
        "remaining_count": remaining_count,
        "remaining_days": remaining_days,
        "slack_days": remaining_days - remaining_count if count_by == "distinct_day" else None,
        "satisfied": remaining_count == 0,
    }


def validate_region_visit_quota_rule(rule: dict[str, Any]) -> None:
    """校验区域访问额度原子 DSL。"""
    if not isinstance(rule, dict):
        raise TypeError("rule 必须为对象")
    rule_id = rule.get("rule_id")
    if not isinstance(rule_id, str) or not rule_id.strip():
        raise ValueError("rule.rule_id 必须为非空字符串")
    if rule.get("type") != REGION_VISIT_QUOTA:
        raise ValueError(f"规则类型必须为 {REGION_VISIT_QUOTA}")
    if rule.get("progress_kind") != "monthly_quota":
        raise ValueError("region_visit_quota progress_kind 必须为 monthly_quota")
    if rule.get("active_period") is not None:
        raise ValueError("当前 region_visit_quota 暂不支持 active_period")
    params = rule.get("params")
    if not isinstance(params, dict):
        raise TypeError("rule.params 必须为对象")
    validate_region(params.get("region"))
    if params.get("event") not in _SUPPORTED_EVENTS:
        raise ValueError("region_visit_quota.params.event 必须为 position_arrival 或 cargo_touch")
    if params.get("count_by") not in _SUPPORTED_COUNT_BY:
        raise ValueError("region_visit_quota.params.count_by 必须为 distinct_day 或 occurrence")
    min_count = params.get("min_count")
    if not isinstance(min_count, int) or isinstance(min_count, bool) or min_count <= 0:
        raise ValueError("region_visit_quota.params.min_count 必须为正整数")


def _validate_now_minutes(now_minutes: int) -> int:
    month_end_minutes = SIMULATION_MONTH_DAYS * MINUTES_PER_DAY
    if (
        not isinstance(now_minutes, int)
        or isinstance(now_minutes, bool)
        or not 0 <= now_minutes <= month_end_minutes
    ):
        raise ValueError("now_minutes 必须位于当前仿真自然月内")
    return now_minutes


def _read_events(trajectory_snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(trajectory_snapshot, dict):
        raise TypeError("trajectory_snapshot 必须为对象")
    events = trajectory_snapshot.get("events", [])
    if not isinstance(events, list) or any(not isinstance(event, dict) for event in events):
        raise TypeError("trajectory_snapshot.events 必须为对象数组")
    return events


def _matching_visit_days(
    event: dict[str, Any],
    visit_event: str,
    region: dict[str, Any],
    now_minutes: int,
) -> list[int]:
    points = (
        _position_arrival_points(event)
        if visit_event == "position_arrival"
        else _cargo_touch_points(event)
    )
    return [
        at_minutes // MINUTES_PER_DAY
        for point, at_minutes in points
        if 0 <= at_minutes <= now_minutes and _point_in_region(point, region)
    ]


def _position_arrival_points(event: dict[str, Any]) -> list[tuple[dict[str, Any], int]]:
    action = str(event.get("action", "")).strip().lower()
    if action not in {"take_order", "reposition"}:
        return []
    return [(_read_point(event.get("position_after")), int(event["action_end_minutes"]))]


def _cargo_touch_points(event: dict[str, Any]) -> list[tuple[dict[str, Any], int]]:
    if str(event.get("action", "")).strip().lower() != "take_order" or event.get("accepted") is not True:
        return []
    raw_points = event.get("cargo_touch_points", [])
    if raw_points:
        if not isinstance(raw_points, list):
            raise TypeError("轨迹事件 cargo_touch_points 必须为数组")
        return [
            (_read_point(point), int(point.get("at_minutes", event["action_end_minutes"])))
            for point in raw_points
        ]
    return [(_read_point(event.get("position_after")), int(event["action_end_minutes"]))]


def _point_in_region(point: dict[str, Any], region: dict[str, Any]) -> bool:
    kind = region["kind"]
    if kind == "named":
        names = point.get("region_names", [])
        return isinstance(names, list) and region["name"] in names
    if kind == "bbox":
        return (
            float(region["lat_min"]) <= point["lat"] <= float(region["lat_max"])
            and float(region["lng_min"]) <= point["lng"] <= float(region["lng_max"])
        )
    return (
        haversine_km(point["lat"], point["lng"], float(region["lat"]), float(region["lng"]))
        <= float(region["radius_km"])
    )


def _read_point(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise TypeError("轨迹访问位置必须为对象")
    point: dict[str, Any] = {"lat": float(raw["lat"]), "lng": float(raw["lng"])}
    if "region_names" in raw:
        names = raw["region_names"]
        if not isinstance(names, list) or any(not isinstance(name, str) for name in names):
            raise TypeError("轨迹访问位置 region_names 必须为字符串数组")
        point["region_names"] = list(names)
    return point
