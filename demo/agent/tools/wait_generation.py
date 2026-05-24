"""Preference-driven wait option generation."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from agent.history.time_utils import SIMULATION_EPOCH, coerce_float, coerce_int
from agent.tools.time_window_checks import DAY_MINUTES, current_or_next_window
from agent.tools.wait_factory import make_wait_option


DEFAULT_WINDOW_LEAD_MINUTES = 60


def build_action_options(
    *,
    preference_context: dict[str, Any],
    tool_plan: dict[str, dict[str, Any]],
    context: dict[str, Any] | None = None,
    history_summary: dict[str, Any],
    status: dict[str, Any],
    event_log: list[dict[str, Any]] | None = None,
    lead_minutes: int = DEFAULT_WINDOW_LEAD_MINUTES,
) -> list[dict[str, Any]]:
    options: list[dict[str, Any]] = []
    current_minutes = coerce_int(status.get("simulation_progress_minutes"))
    today = history_summary.get("today") if isinstance(history_summary.get("today"), dict) else {}
    month = history_summary.get("month") if isinstance(history_summary.get("month"), dict) else {}
    current_wait_streak = _current_wait_streak_minutes(event_log, current_minutes)
    if current_wait_streak is None:
        current_wait_streak = coerce_int(today.get("current_wait_streak_minutes"))

    for pref_id, tools in tool_plan.items():
        config = tools.get("wait_generation")
        if not isinstance(config, dict):
            continue

        continuous = config.get("continuous_rest")
        if isinstance(continuous, dict):
            if bool(continuous.get("weekdays_only")) and not _is_weekday(current_minutes):
                continue
            required_minutes = int(round(coerce_float(continuous.get("hours")) * 60))
            longest_wait = coerce_int(today.get("longest_wait_minutes"))
            current_streak = current_wait_streak
            if required_minutes > 0 and longest_wait < required_minutes and current_streak < required_minutes:
                duration = required_minutes - current_streak
                options.append(
                    make_wait_option(
                        duration_minutes=duration,
                        status=status,
                        pref_id=pref_id,
                        source="continuous_rest_wait",
                        reason=f"今日最长连续休息{longest_wait}分钟，尚未达到{required_minutes}分钟；继续wait {duration}分钟可补足。",
                    )
                )

        fixed_window = config.get("fixed_rest_window")
        if isinstance(fixed_window, dict):
            window = current_or_next_window(current_minutes, fixed_window)
            if window is not None:
                window_start, window_end, in_window = window
                minutes_until_start = window_start - current_minutes
                if in_window or 0 <= minutes_until_start <= int(lead_minutes):
                    duration = max(0, window_end - current_minutes)
                    if duration > 0:
                        current_streak = current_wait_streak
                        needed_streak = max(0, current_minutes - window_start)
                        if in_window and current_streak >= needed_streak:
                            detail = "从窗口开始到现在持续处于wait，继续休息到窗口结束可继续满足固定窗口休息偏好。"
                        elif in_window:
                            detail = "当前已在固定休息窗口内，但窗口开始后发生过非wait动作；现在wait只能减少后续违反。"
                        else:
                            detail = "距离固定休息窗口开始不超过60分钟，提前wait到窗口结束可满足该窗口休息偏好。"
                        options.append(
                            make_wait_option(
                                duration_minutes=duration,
                                status=status,
                                pref_id=pref_id,
                                source="fixed_rest_window_wait",
                                reason=detail,
                            )
                        )

        monthly_rest = config.get("monthly_rest_days")
        if isinstance(monthly_rest, dict):
            required_days = coerce_int(monthly_rest.get("days"))
            fully_idle_days = coerce_int(month.get("fully_idle_days"))
            if required_days > 0 and fully_idle_days < required_days and _today_still_idle(today):
                next_day_start = ((current_minutes // DAY_MINUTES) + 1) * DAY_MINUTES
                duration = max(0, next_day_start - current_minutes)
                if duration > 0:
                    options.append(
                        make_wait_option(
                            duration_minutes=duration,
                            status=status,
                            pref_id=pref_id,
                            source="monthly_rest_day_wait",
                            reason=(
                                f"月内完全休息日已完成{fully_idle_days}天，目标{required_days}天；"
                                "今天尚未接单或reposition，wait到日末可把今天保留为完全休息日。"
                            ),
                        )
                    )
    return options


def build_preference_wait_options(
    *,
    preference_context: dict[str, Any],
    tool_plan: dict[str, dict[str, Any]],
    history_summary: dict[str, Any],
    status: dict[str, Any],
    event_log: list[dict[str, Any]] | None = None,
    lead_minutes: int = DEFAULT_WINDOW_LEAD_MINUTES,
) -> list[dict[str, Any]]:
    return build_action_options(
        preference_context=preference_context,
        tool_plan=tool_plan,
        context=None,
        history_summary=history_summary,
        status=status,
        event_log=event_log,
        lead_minutes=lead_minutes,
    )


def _today_still_idle(today: dict[str, Any]) -> bool:
    return (
        coerce_int(today.get("active_minutes")) == 0
        and coerce_int(today.get("successful_take_order_count")) == 0
        and coerce_int(today.get("failed_take_order_count")) == 0
    )


def _is_weekday(current_minutes: int) -> bool:
    return (SIMULATION_EPOCH + timedelta(minutes=int(current_minutes))).weekday() < 5


def _current_wait_streak_minutes(event_log: list[dict[str, Any]] | None, current_minutes: int) -> int | None:
    if event_log is None:
        return None
    intervals: list[tuple[int, int]] = []
    for event in event_log:
        if event.get("action") != "wait":
            continue
        start = coerce_int(event.get("action_start_minutes"), -1)
        end = coerce_int(event.get("action_end_minutes"), -1)
        if end > start:
            intervals.append((start, end))
    if not intervals:
        return 0
    merged = _merge_intervals(intervals)
    for start, end in reversed(merged):
        if end == current_minutes:
            return end - start
        if end < current_minutes:
            break
    return 0


def _merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(intervals):
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    return merged
