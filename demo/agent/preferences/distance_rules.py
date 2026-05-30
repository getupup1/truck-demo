"""距离限制偏好 DSL 与累计空驶进度。"""

from __future__ import annotations

from typing import Any

from agent.cargo_simulator import simulation_minutes_to_wall_time

DISTANCE_LIMIT = "distance_limit"
PICKUP_DEADHEAD_KM = "pickup_deadhead_km"
HAUL_DISTANCE_KM = "haul_distance_km"
MONTHLY_DEADHEAD_KM = "monthly_deadhead_km"
_PROGRESS_KIND_BY_METRIC = {
    PICKUP_DEADHEAD_KM: "none",
    HAUL_DISTANCE_KM: "none",
    MONTHLY_DEADHEAD_KM: "cumulative",
}


def build_distance_limit_rule(
    rule_id: str,
    metric: str,
    max_km: float,
) -> dict[str, Any]:
    """构造一条原子距离限制规则。"""
    rule = {
        "rule_id": rule_id,
        "type": DISTANCE_LIMIT,
        "progress_kind": _PROGRESS_KIND_BY_METRIC.get(metric),
        "active_period": None,
        "params": {
            "metric": metric,
            "max_km": max_km,
        },
    }
    validate_distance_limit_rule(rule)
    return rule


def calculate_distance_limit_progress(
    rule: dict[str, Any],
    trajectory_snapshot: dict[str, Any],
    now_minutes: int,
) -> dict[str, Any]:
    """根据已完成动作计算月度累计空驶里程。"""
    validate_distance_limit_rule(rule)
    if rule["progress_kind"] != "cumulative":
        raise ValueError("只有累计距离限制规则才有运行时进度")
    now_minutes = _validate_now_minutes(now_minutes)
    events = _read_events(trajectory_snapshot)
    max_km = float(rule["params"]["max_km"])
    current_km = round(sum(_event_deadhead_km(event) for event in events), 2)
    return {
        "rule_id": rule["rule_id"],
        "type": DISTANCE_LIMIT,
        "progress_kind": "cumulative",
        "as_of_minutes": now_minutes,
        "as_of_time": simulation_minutes_to_wall_time(now_minutes),
        "metric": MONTHLY_DEADHEAD_KM,
        "max_km": max_km,
        "current_km": current_km,
        "remaining_free_km": round(max(0.0, max_km - current_km), 2),
        "exceeded_km": round(max(0.0, current_km - max_km), 2),
    }


def validate_distance_limit_rule(rule: dict[str, Any]) -> None:
    """校验一条原子距离限制 DSL 规则。"""
    if not isinstance(rule, dict):
        raise TypeError("rule 必须为对象")
    rule_id = rule.get("rule_id")
    if not isinstance(rule_id, str) or not rule_id.strip():
        raise ValueError("rule.rule_id 必须为非空字符串")
    if rule.get("type") != DISTANCE_LIMIT:
        raise ValueError(f"规则类型必须为 {DISTANCE_LIMIT}")
    params = rule.get("params")
    if not isinstance(params, dict):
        raise TypeError("rule.params 必须为对象")
    metric = params.get("metric")
    if metric not in _PROGRESS_KIND_BY_METRIC:
        raise ValueError("distance_limit.params.metric 不支持该指标")
    if rule.get("progress_kind") != _PROGRESS_KIND_BY_METRIC[metric]:
        raise ValueError("distance_limit.progress_kind 与 params.metric 不匹配")
    if rule.get("active_period") is not None:
        raise ValueError("当前 distance_limit 暂不支持 active_period")
    max_km = params.get("max_km")
    if not isinstance(max_km, (int, float)) or isinstance(max_km, bool) or max_km < 0:
        raise ValueError("distance_limit.params.max_km 必须为非负数")


def _validate_now_minutes(now_minutes: int) -> int:
    if not isinstance(now_minutes, int) or isinstance(now_minutes, bool) or now_minutes < 0:
        raise ValueError("now_minutes 必须为非负整数")
    return now_minutes


def _read_events(trajectory_snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(trajectory_snapshot, dict):
        raise TypeError("trajectory_snapshot 必须为对象")
    events = trajectory_snapshot.get("events", [])
    if not isinstance(events, list) or any(not isinstance(event, dict) for event in events):
        raise TypeError("trajectory_snapshot.events 必须为对象数组")
    return events


def _event_deadhead_km(event: dict[str, Any]) -> float:
    metrics = event.get("metrics", {})
    if not isinstance(metrics, dict):
        raise TypeError("轨迹事件 metrics 必须为对象")
    action = str(event.get("action", "")).strip().lower()
    if action == "take_order":
        return _distance(metrics.get(PICKUP_DEADHEAD_KM, 0.0))
    if action == "reposition":
        return _distance(metrics.get("distance_km", 0.0))
    return 0.0


def _distance(value: Any) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
        raise ValueError("轨迹距离指标必须为非负数")
    return float(value)
