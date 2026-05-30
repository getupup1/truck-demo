"""休息类偏好 DSL 与 progress 计算。"""

from __future__ import annotations

from typing import Any

from agent.cargo_simulator import simulation_minutes_to_wall_time

MINUTES_PER_DAY = 24 * 60
DAILY_CONTINUOUS_REST = "daily_continuous_rest"
DAILY_REST_WINDOW = "daily_rest_window"
SUPPORTED_REST_RULE_TYPES = (DAILY_CONTINUOUS_REST, DAILY_REST_WINDOW)


def build_daily_continuous_rest_rule(rule_id: str, min_minutes: int) -> dict[str, Any]:
    """构造“每天至少连续休息若干分钟”的原子 DSL。"""
    rule = {
        "rule_id": rule_id,
        "type": DAILY_CONTINUOUS_REST,
        "progress_kind": "daily",
        "active_period": None,
        "params": {"min_minutes": min_minutes},
    }
    validate_rest_rule(rule)
    return rule


def build_daily_rest_window_rule(rule_id: str, start: str, end: str) -> dict[str, Any]:
    """构造“每天固定时段停车休息”的原子 DSL。"""
    rule = {
        "rule_id": rule_id,
        "type": DAILY_REST_WINDOW,
        "progress_kind": "daily",
        "active_period": None,
        "params": {"window": {"start": start, "end": end}},
    }
    validate_rest_rule(rule)
    return rule


def calculate_daily_continuous_rest_progress(
    rule: dict[str, Any],
    trajectory_snapshot: dict[str, Any],
    now_minutes: int,
) -> dict[str, Any]:
    """计算当前自然日的连续休息完成度与最晚启动余量。"""
    validate_rest_rule(rule, expected_type=DAILY_CONTINUOUS_REST)
    now_minutes = _validate_now_minutes(now_minutes)
    min_minutes = int(rule["params"]["min_minutes"])
    day_index = now_minutes // MINUTES_PER_DAY
    day_start = day_index * MINUTES_PER_DAY
    day_end = day_start + MINUTES_PER_DAY
    intervals = _clip_intervals(_read_wait_intervals(trajectory_snapshot), day_start, now_minutes)
    longest_wait = max((end - start for start, end in intervals), default=0)
    current_wait = _tail_wait_minutes(intervals, now_minutes)
    satisfied = longest_wait >= min_minutes
    required_wait_now = 0 if satisfied else max(0, min_minutes - current_wait) if current_wait else min_minutes
    remaining_day_minutes = day_end - now_minutes
    slack_minutes = remaining_day_minutes - required_wait_now
    latest_rest_start = day_end - min_minutes
    return {
        "rule_id": rule["rule_id"],
        "type": DAILY_CONTINUOUS_REST,
        "progress_kind": "daily",
        "as_of_minutes": now_minutes,
        "as_of_time": simulation_minutes_to_wall_time(now_minutes),
        "day_index": day_index,
        "required_continuous_minutes": min_minutes,
        "longest_completed_wait_minutes": longest_wait,
        "current_continuous_wait_minutes": current_wait,
        "remaining_day_minutes": remaining_day_minutes,
        "slack_minutes": slack_minutes,
        "latest_rest_start_minutes": latest_rest_start,
        "latest_rest_start_time": simulation_minutes_to_wall_time(latest_rest_start),
        "satisfied": satisfied,
        "forced_now": not satisfied and slack_minutes <= 0,
    }


def calculate_daily_rest_window_progress(
    rule: dict[str, Any],
    trajectory_snapshot: dict[str, Any],
    now_minutes: int,
) -> dict[str, Any]:
    """计算当前或下一个固定休息窗口的完成状态。"""
    validate_rest_rule(rule, expected_type=DAILY_REST_WINDOW)
    now_minutes = _validate_now_minutes(now_minutes)
    window = rule["params"]["window"]
    start_of_day = _parse_clock_minutes(window["start"])
    end_of_day = _parse_clock_minutes(window["end"])
    window_start, window_end, window_state = _actionable_window(now_minutes, start_of_day, end_of_day)
    elapsed_end = min(max(now_minutes, window_start), window_end)
    intervals = _clip_intervals(_read_wait_intervals(trajectory_snapshot), window_start, elapsed_end)
    rested_minutes = sum(end - start for start, end in intervals)
    elapsed_minutes = elapsed_end - window_start
    missing_elapsed_minutes = max(0, elapsed_minutes - rested_minutes)
    is_active = window_state == "active"
    minutes_until_window_start = max(0, window_start - now_minutes)
    remaining_window_minutes = max(0, window_end - now_minutes) if is_active else window_end - window_start
    can_still_satisfy_window = is_active and missing_elapsed_minutes == 0
    return {
        "rule_id": rule["rule_id"],
        "type": DAILY_REST_WINDOW,
        "progress_kind": "daily",
        "as_of_minutes": now_minutes,
        "as_of_time": simulation_minutes_to_wall_time(now_minutes),
        "window_instance": {
            "start_minutes": window_start,
            "start_time": simulation_minutes_to_wall_time(window_start),
            "end_minutes": window_end,
            "end_time": simulation_minutes_to_wall_time(window_end),
        },
        "minutes_until_window_start": minutes_until_window_start,
        "required_minutes": window_end - window_start,
        "rested_minutes": rested_minutes,
        "missing_elapsed_minutes": missing_elapsed_minutes,
        "remaining_window_minutes": remaining_window_minutes,
        "latest_safe_busy_end_minutes": window_start,
        "latest_safe_busy_end_time": simulation_minutes_to_wall_time(window_start),
        "recommended_wait_minutes": remaining_window_minutes if can_still_satisfy_window else 0,
        "forced_now": can_still_satisfy_window,
    }


def validate_rest_rule(rule: dict[str, Any], expected_type: str | None = None) -> None:
    """校验当前已实现的两类休息原子 DSL。"""
    if not isinstance(rule, dict):
        raise TypeError("rule 必须为对象")
    rule_id = rule.get("rule_id")
    if not isinstance(rule_id, str) or not rule_id.strip():
        raise ValueError("rule.rule_id 必须为非空字符串")
    rule_type = rule.get("type")
    if rule_type not in SUPPORTED_REST_RULE_TYPES:
        raise ValueError(f"暂不支持休息规则类型: {rule_type}")
    if expected_type is not None and rule_type != expected_type:
        raise ValueError(f"规则类型应为 {expected_type}，实际为 {rule_type}")
    if rule.get("progress_kind") != "daily":
        raise ValueError("休息规则 progress_kind 必须为 daily")
    if rule.get("active_period") is not None:
        raise ValueError("当前休息规则暂不支持 active_period")
    params = rule.get("params")
    if not isinstance(params, dict):
        raise TypeError("rule.params 必须为对象")
    if rule_type == DAILY_CONTINUOUS_REST:
        min_minutes = params.get("min_minutes")
        if not isinstance(min_minutes, int) or isinstance(min_minutes, bool) or min_minutes <= 0:
            raise ValueError("daily_continuous_rest.params.min_minutes 必须为正整数")
        if min_minutes > MINUTES_PER_DAY:
            raise ValueError("daily_continuous_rest.params.min_minutes 不能超过 1440")
    if rule_type == DAILY_REST_WINDOW:
        window = params.get("window")
        if not isinstance(window, dict):
            raise TypeError("daily_rest_window.params.window 必须为对象")
        _parse_clock_minutes(window.get("start"))
        _parse_clock_minutes(window.get("end"))


def _validate_now_minutes(now_minutes: int) -> int:
    if not isinstance(now_minutes, int) or isinstance(now_minutes, bool) or now_minutes < 0:
        raise ValueError("now_minutes 必须为非负整数")
    return now_minutes


def _parse_clock_minutes(value: Any) -> int:
    if not isinstance(value, str):
        raise ValueError("时间必须使用 HH:MM 字符串")
    parts = value.split(":")
    if len(parts) != 2 or any(not part.isdigit() for part in parts):
        raise ValueError("时间必须使用 HH:MM 字符串")
    hour, minute = (int(part) for part in parts)
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError("时间必须位于 00:00 到 23:59")
    return hour * 60 + minute


def _read_wait_intervals(trajectory_snapshot: dict[str, Any]) -> list[tuple[int, int]]:
    if not isinstance(trajectory_snapshot, dict):
        raise TypeError("trajectory_snapshot 必须为对象")
    by_day = trajectory_snapshot.get("wait_intervals_by_day", {})
    if not isinstance(by_day, dict):
        raise TypeError("trajectory_snapshot.wait_intervals_by_day 必须为对象")
    intervals: list[tuple[int, int]] = []
    for day_intervals in by_day.values():
        if not isinstance(day_intervals, list):
            raise TypeError("每天的 wait_intervals 必须为数组")
        for interval in day_intervals:
            if not isinstance(interval, dict):
                raise TypeError("wait interval 必须为对象")
            start = int(interval["start_minutes"])
            end = int(interval["end_minutes"])
            if start < 0 or end <= start:
                raise ValueError("wait interval 时间范围无效")
            intervals.append((start, end))
    return _merge_intervals(intervals)


def _merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[list[int]] = []
    for start, end in sorted(intervals):
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return [(start, end) for start, end in merged]


def _clip_intervals(intervals: list[tuple[int, int]], start: int, end: int) -> list[tuple[int, int]]:
    clipped = [(max(item_start, start), min(item_end, end)) for item_start, item_end in intervals]
    return _merge_intervals([(item_start, item_end) for item_start, item_end in clipped if item_end > item_start])


def _tail_wait_minutes(intervals: list[tuple[int, int]], now_minutes: int) -> int:
    for start, end in reversed(intervals):
        if end == now_minutes:
            return end - start
        if end < now_minutes:
            break
    return 0


def _actionable_window(now_minutes: int, start_of_day: int, end_of_day: int) -> tuple[int, int, str]:
    duration = end_of_day - start_of_day
    if duration <= 0:
        duration += MINUTES_PER_DAY
    day_index = now_minutes // MINUTES_PER_DAY
    windows = [
        (anchor_day * MINUTES_PER_DAY + start_of_day, anchor_day * MINUTES_PER_DAY + start_of_day + duration)
        for anchor_day in range(day_index - 1, day_index + 2)
    ]
    for start, end in windows:
        if start <= now_minutes < end:
            return start, end, "active"
    upcoming = [(start, end) for start, end in windows if start >= now_minutes]
    if not upcoming:
        start = (day_index + 2) * MINUTES_PER_DAY + start_of_day
        return start, start + duration, "upcoming"
    start, end = min(upcoming)
    return start, end, "upcoming"
