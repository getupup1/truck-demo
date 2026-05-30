"""月度整天休息额度 DSL 与 progress 计算。"""

from __future__ import annotations

from typing import Any

from agent.cargo_simulator import simulation_minutes_to_wall_time

MINUTES_PER_DAY = 24 * 60
SIMULATION_MONTH_DAYS = 31
MONTHLY_OFF_DAY_QUOTA = "monthly_off_day_quota"
_SUPPORTED_BLOCKED_ACTIONS = {"take_order", "reposition"}


def build_monthly_off_day_quota_rule(
    rule_id: str,
    min_days: int,
    blocked_actions: list[str],
) -> dict[str, Any]:
    """构造“自然月内至少保留若干整天休息”的原子 DSL。"""
    rule = {
        "rule_id": rule_id,
        "type": MONTHLY_OFF_DAY_QUOTA,
        "progress_kind": "monthly_quota",
        "active_period": None,
        "params": {
            "min_days": min_days,
            "blocked_actions": list(blocked_actions),
        },
    }
    validate_monthly_off_day_quota_rule(rule)
    return rule


def calculate_monthly_off_day_quota_progress(
    rule: dict[str, Any],
    trajectory_snapshot: dict[str, Any],
    now_minutes: int,
) -> dict[str, Any]:
    """计算月度整天休息额度、剩余日和动态松弛度。"""
    validate_monthly_off_day_quota_rule(rule)
    now_minutes = _validate_now_minutes(now_minutes)
    events = _read_events(trajectory_snapshot)
    min_days = int(rule["params"]["min_days"])
    blocked_actions = set(rule["params"]["blocked_actions"])
    elapsed_days = min(now_minutes // MINUTES_PER_DAY, SIMULATION_MONTH_DAYS)
    current_day_index = elapsed_days if elapsed_days < SIMULATION_MONTH_DAYS else None
    completed_off_day_indexes = [
        day_index
        for day_index in range(elapsed_days)
        if not _day_has_blocked_action(events, day_index, blocked_actions)
    ]
    current_day_eligible = (
        not _day_has_blocked_action(events, current_day_index, blocked_actions)
        if current_day_index is not None
        else False
    )
    future_days = max(0, SIMULATION_MONTH_DAYS - elapsed_days - (1 if current_day_index is not None else 0))
    remaining_eligible_days = future_days + (1 if current_day_eligible else 0)
    remaining_required_days = max(0, min_days - len(completed_off_day_indexes))
    slack_days = remaining_eligible_days - remaining_required_days
    forced_now = remaining_required_days > 0 and current_day_eligible and slack_days <= 0
    return {
        "rule_id": rule["rule_id"],
        "type": MONTHLY_OFF_DAY_QUOTA,
        "progress_kind": "monthly_quota",
        "as_of_minutes": now_minutes,
        "as_of_time": simulation_minutes_to_wall_time(now_minutes),
        "required_days": min_days,
        "completed_off_days": len(completed_off_day_indexes),
        "remaining_required_days": remaining_required_days,
        "remaining_eligible_days": remaining_eligible_days,
        "slack_days": slack_days,
        "current_day_eligible": current_day_eligible,
        "forced_now": forced_now,
    }


def validate_monthly_off_day_quota_rule(rule: dict[str, Any]) -> None:
    """校验月度整天休息额度原子 DSL。"""
    if not isinstance(rule, dict):
        raise TypeError("rule 必须为对象")
    rule_id = rule.get("rule_id")
    if not isinstance(rule_id, str) or not rule_id.strip():
        raise ValueError("rule.rule_id 必须为非空字符串")
    if rule.get("type") != MONTHLY_OFF_DAY_QUOTA:
        raise ValueError(f"规则类型应为 {MONTHLY_OFF_DAY_QUOTA}")
    if rule.get("progress_kind") != "monthly_quota":
        raise ValueError("monthly_off_day_quota progress_kind 必须为 monthly_quota")
    if rule.get("active_period") is not None:
        raise ValueError("当前 monthly_off_day_quota 暂不支持 active_period")
    params = rule.get("params")
    if not isinstance(params, dict):
        raise TypeError("rule.params 必须为对象")
    min_days = params.get("min_days")
    if not isinstance(min_days, int) or isinstance(min_days, bool) or not 1 <= min_days <= SIMULATION_MONTH_DAYS:
        raise ValueError("monthly_off_day_quota.params.min_days 必须为 1 到 31 的整数")
    blocked_actions = params.get("blocked_actions")
    if not isinstance(blocked_actions, list) or not blocked_actions:
        raise ValueError("monthly_off_day_quota.params.blocked_actions 必须为非空数组")
    if any(not isinstance(action, str) or action not in _SUPPORTED_BLOCKED_ACTIONS for action in blocked_actions):
        raise ValueError("monthly_off_day_quota.params.blocked_actions 包含不支持的动作")
    if len(set(blocked_actions)) != len(blocked_actions):
        raise ValueError("monthly_off_day_quota.params.blocked_actions 不能重复")


def _validate_now_minutes(now_minutes: int) -> int:
    month_end = SIMULATION_MONTH_DAYS * MINUTES_PER_DAY
    if not isinstance(now_minutes, int) or isinstance(now_minutes, bool) or not 0 <= now_minutes <= month_end:
        raise ValueError("now_minutes 必须位于当前仿真自然月内")
    return now_minutes


def _read_events(trajectory_snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(trajectory_snapshot, dict):
        raise TypeError("trajectory_snapshot 必须为对象")
    events = trajectory_snapshot.get("events", [])
    if not isinstance(events, list) or any(not isinstance(event, dict) for event in events):
        raise TypeError("trajectory_snapshot.events 必须为对象数组")
    return events


def _day_has_blocked_action(
    events: list[dict[str, Any]],
    day_index: int,
    blocked_actions: set[str],
) -> bool:
    day_start = day_index * MINUTES_PER_DAY
    day_end = day_start + MINUTES_PER_DAY
    for event in events:
        if str(event.get("action", "")).strip().lower() not in blocked_actions:
            continue
        action_start = int(event["action_start_minutes"])
        action_end = int(event["action_end_minutes"])
        if max(action_start, day_start) < min(action_end, day_end):
            return True
    return False
