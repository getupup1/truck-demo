"""区域限制类偏好 DSL。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

REGION_POLICY = "region_policy"
_SUPPORTED_POLICIES = {"stay_inside", "stay_outside"}
_SUPPORTED_REGION_KINDS = {"named", "bbox", "circle"}
_WALL_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def build_region_policy_rule(
    rule_id: str,
    policy: str,
    region: dict[str, Any],
    active_period: dict[str, str] | None = None,
) -> dict[str, Any]:
    """构造区域内活动或区域禁入原子 DSL。"""
    rule = {
        "rule_id": rule_id,
        "type": REGION_POLICY,
        "progress_kind": "none",
        "active_period": dict(active_period) if active_period is not None else None,
        "params": {
            "policy": policy,
            "region": dict(region),
        },
    }
    validate_region_policy_rule(rule)
    return rule


def validate_region_policy_rule(rule: dict[str, Any]) -> None:
    """校验区域限制原子 DSL。"""
    if not isinstance(rule, dict):
        raise TypeError("rule 必须为对象")
    rule_id = rule.get("rule_id")
    if not isinstance(rule_id, str) or not rule_id.strip():
        raise ValueError("rule.rule_id 必须为非空字符串")
    if rule.get("type") != REGION_POLICY:
        raise ValueError(f"规则类型应为 {REGION_POLICY}")
    if rule.get("progress_kind") != "none":
        raise ValueError("region_policy progress_kind 必须为 none")
    _validate_active_period(rule.get("active_period"))
    params = rule.get("params")
    if not isinstance(params, dict):
        raise TypeError("rule.params 必须为对象")
    if params.get("policy") not in _SUPPORTED_POLICIES:
        raise ValueError("region_policy.params.policy 必须为 stay_inside 或 stay_outside")
    validate_region(params.get("region"))


def _validate_active_period(active_period: Any) -> None:
    if active_period is None:
        return
    if not isinstance(active_period, dict):
        raise TypeError("region_policy.active_period 必须为对象或 null")
    start = _parse_wall_time(active_period.get("start"), "active_period.start")
    end = _parse_wall_time(active_period.get("end"), "active_period.end")
    if end < start:
        raise ValueError("region_policy.active_period.end 不能早于 start")


def validate_region(region: Any) -> None:
    """校验通用区域结构。"""
    if not isinstance(region, dict):
        raise TypeError("region_policy.params.region 必须为对象")
    kind = region.get("kind")
    if kind not in _SUPPORTED_REGION_KINDS:
        raise ValueError("region_policy.params.region.kind 必须为 named、bbox 或 circle")
    if kind == "named":
        name = region.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("named region.name 必须为非空字符串")
    if kind == "bbox":
        lat_min = _latitude(region.get("lat_min"), "bbox.lat_min")
        lat_max = _latitude(region.get("lat_max"), "bbox.lat_max")
        lng_min = _longitude(region.get("lng_min"), "bbox.lng_min")
        lng_max = _longitude(region.get("lng_max"), "bbox.lng_max")
        if lat_min >= lat_max or lng_min >= lng_max:
            raise ValueError("bbox 最小经纬度必须小于最大经纬度")
    if kind == "circle":
        _latitude(region.get("lat"), "circle.lat")
        _longitude(region.get("lng"), "circle.lng")
        radius_km = region.get("radius_km")
        if not isinstance(radius_km, (int, float)) or isinstance(radius_km, bool) or radius_km <= 0:
            raise ValueError("circle.radius_km 必须为正数")


def _parse_wall_time(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field} 必须使用 YYYY-MM-DD HH:MM:SS 字符串")
    try:
        return datetime.strptime(value, _WALL_TIME_FORMAT)
    except ValueError as exc:
        raise ValueError(f"{field} 必须使用 YYYY-MM-DD HH:MM:SS 字符串") from exc


def _latitude(value: Any, field: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not -90 <= value <= 90:
        raise ValueError(f"{field} 必须位于 -90 到 90")
    return float(value)


def _longitude(value: Any, field: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not -180 <= value <= 180:
        raise ValueError(f"{field} 必须位于 -180 到 180")
    return float(value)
