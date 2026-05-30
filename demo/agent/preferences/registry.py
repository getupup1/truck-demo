"""偏好规则注册表：根据 DSL type 分发 progress 处理器。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from agent.preferences.off_day_rules import (
    MONTHLY_OFF_DAY_QUOTA,
    calculate_monthly_off_day_quota_progress,
)
from agent.preferences.distance_rules import DISTANCE_LIMIT, calculate_distance_limit_progress
from agent.preferences.rest_rules import (
    DAILY_CONTINUOUS_REST,
    DAILY_REST_WINDOW,
    calculate_daily_continuous_rest_progress,
    calculate_daily_rest_window_progress,
)
from agent.preferences.region_visit_rules import (
    REGION_VISIT_QUOTA,
    calculate_region_visit_quota_progress,
)

ProgressHandler = Callable[[dict[str, Any], dict[str, Any], int], dict[str, Any]]

_PROGRESS_HANDLERS: dict[str, ProgressHandler] = {
    DAILY_CONTINUOUS_REST: calculate_daily_continuous_rest_progress,
    DAILY_REST_WINDOW: calculate_daily_rest_window_progress,
    MONTHLY_OFF_DAY_QUOTA: calculate_monthly_off_day_quota_progress,
    DISTANCE_LIMIT: calculate_distance_limit_progress,
    REGION_VISIT_QUOTA: calculate_region_visit_quota_progress,
}


def supported_rule_types() -> tuple[str, ...]:
    """返回当前可以计算 progress 的规则类型。"""
    return tuple(_PROGRESS_HANDLERS)


def calculate_rule_progress(
    rule: dict[str, Any],
    trajectory_snapshot: dict[str, Any],
    now_minutes: int,
) -> dict[str, Any]:
    """根据原子 DSL 的 type 调用对应 progress 处理器。"""
    if not isinstance(rule, dict):
        raise TypeError("rule 必须为对象")
    rule_type = rule.get("type")
    handler = _PROGRESS_HANDLERS.get(str(rule_type))
    if handler is None:
        raise ValueError(f"未注册的偏好规则类型: {rule_type}")
    return handler(rule, trajectory_snapshot, now_minutes)
