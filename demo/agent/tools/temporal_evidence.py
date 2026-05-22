"""Temporal preference evidence and action option helpers."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from agent.history.time_utils import (
    SIMULATION_EPOCH,
    coerce_float,
    coerce_int,
    distance_to_minutes,
    format_simulation_time,
    haversine_km,
)


DAY_MINUTES = 1440
DEFAULT_WINDOW_LEAD_MINUTES = 60


def attach_candidate_temporal_evidence(
    candidates: list[dict[str, Any]],
    preference_context: dict[str, Any],
    *,
    speed_km_per_hour: float,
) -> list[dict[str, Any]]:
    """Attach time-window and deadline evidence to filtered cargo candidates."""
    briefs = _briefs_with_tools(preference_context)
    if not briefs:
        return candidates

    enhanced: list[dict[str, Any]] = []
    for candidate in candidates:
        item = dict(candidate)
        tool_evidence: dict[str, Any] = dict(item.get("tool_evidence") or {})
        time_window_results: list[dict[str, Any]] = []
        deadline_results: list[dict[str, Any]] = []
        for brief in briefs:
            tools = brief.get("tools") if isinstance(brief.get("tools"), dict) else {}
            time_config = tools.get("time_window_check")
            if isinstance(time_config, dict):
                result = _time_window_result_for_event(
                    brief=brief,
                    config=time_config,
                    event=item.get("simulated_event"),
                    option_id=str(item.get("cargo_id") or ""),
                    action="take_order",
                )
                if result is not None:
                    result["cargo_id"] = item.get("cargo_id")
                    time_window_results.append(result)
            deadline_config = tools.get("deadline_location_check")
            if isinstance(deadline_config, dict):
                result = _deadline_result_for_candidate(
                    brief=brief,
                    config=deadline_config,
                    candidate=item,
                    speed_km_per_hour=speed_km_per_hour,
                )
                if result is not None:
                    deadline_results.append(result)
        if time_window_results:
            tool_evidence["time_window_check_result"] = time_window_results
        if deadline_results:
            tool_evidence["deadline_location_check_result"] = deadline_results
        if tool_evidence:
            item["tool_evidence"] = tool_evidence
        enhanced.append(item)
    return enhanced


def attach_reposition_time_window_evidence(
    reposition_options: list[dict[str, Any]],
    preference_context: dict[str, Any],
) -> list[dict[str, Any]]:
    """Attach time-window evidence to generated reposition options."""
    briefs = _briefs_with_tools(preference_context)
    if not briefs:
        return reposition_options

    enhanced: list[dict[str, Any]] = []
    for option in reposition_options:
        item = dict(option)
        tool_evidence: dict[str, Any] = dict(item.get("tool_evidence") or {})
        time_window_results: list[dict[str, Any]] = []
        for brief in briefs:
            tools = brief.get("tools") if isinstance(brief.get("tools"), dict) else {}
            time_config = tools.get("time_window_check")
            if not isinstance(time_config, dict):
                continue
            result = _time_window_result_for_event(
                brief=brief,
                config=time_config,
                event=item,
                option_id=str(item.get("option_id") or ""),
                action="reposition",
            )
            if result is not None:
                time_window_results.append(result)
        if time_window_results:
            tool_evidence["time_window_check_result"] = time_window_results
        if tool_evidence:
            item["tool_evidence"] = tool_evidence
        enhanced.append(item)
    return enhanced


def build_deadline_reposition_options(
    *,
    preference_context: dict[str, Any],
    scored_candidates: list[dict[str, Any]],
    status: dict[str, Any],
    speed_km_per_hour: float,
) -> list[dict[str, Any]]:
    """Build controlled reposition options for deadline-location preferences."""
    current_lat = coerce_float(status.get("current_lat"), float("nan"))
    current_lng = coerce_float(status.get("current_lng"), float("nan"))
    current_minutes = coerce_int(status.get("simulation_progress_minutes"))
    if current_lat != current_lat or current_lng != current_lng:
        return []

    options: list[dict[str, Any]] = []
    for brief in _briefs_with_tools(preference_context):
        tools = brief.get("tools") if isinstance(brief.get("tools"), dict) else {}
        config = tools.get("deadline_location_check")
        if not isinstance(config, dict) or _any_candidate_can_reach_deadline(scored_candidates, str(brief.get("pref_id") or "")):
            continue
        center = config.get("center")
        if not isinstance(center, list) or len(center) != 2:
            continue
        target_lat = coerce_float(center[0], float("nan"))
        target_lng = coerce_float(center[1], float("nan"))
        if target_lat != target_lat or target_lng != target_lng:
            continue
        distance_km = haversine_km(current_lat, current_lng, target_lat, target_lng)
        estimated_minutes = distance_to_minutes(distance_km, speed_km_per_hour)
        end_minutes = current_minutes + estimated_minutes
        deadline_minutes = _next_time_of_day_minutes(current_minutes, str(config.get("deadline_time") or ""))
        can_reach = deadline_minutes is not None and end_minutes <= deadline_minutes
        reason = (
            f"所有候选订单都无法确认满足该期限地点偏好，当前直接reposition预计"
            f"{format_simulation_time(end_minutes)}到达目标点。"
        )
        options.append(
            make_reposition_option(
                option_id=f"deadline_reposition_{brief.get('pref_id')}",
                pref_id=str(brief.get("pref_id") or ""),
                target_lat=target_lat,
                target_lng=target_lng,
                status=status,
                distance_km=distance_km,
                estimated_minutes=estimated_minutes,
                reason=reason,
                tool_origin="deadline_location_check",
                extra={
                    "deadline_location_check": {
                        "pref_id": brief.get("pref_id"),
                        "source_text": brief.get("source_text"),
                        "target": {
                            "center": config.get("center"),
                            "radius_km": config.get("radius_km"),
                        },
                        "deadline_time": config.get("deadline_time"),
                        "deadline_minutes": deadline_minutes,
                        "deadline_wall_time": format_simulation_time(deadline_minutes),
                        "arrival_if_go_to_target_minutes": end_minutes,
                        "arrival_if_go_to_target_time": format_simulation_time(end_minutes),
                        "can_reach_deadline": can_reach,
                        "summary": reason,
                    }
                },
            )
        )
    return options


def build_preference_wait_options(
    *,
    preference_context: dict[str, Any],
    history_summary: dict[str, Any],
    status: dict[str, Any],
    event_log: list[dict[str, Any]] | None = None,
    lead_minutes: int = DEFAULT_WINDOW_LEAD_MINUTES,
) -> list[dict[str, Any]]:
    """Build preference-driven wait options."""
    options: list[dict[str, Any]] = []
    current_minutes = coerce_int(status.get("simulation_progress_minutes"))
    today = history_summary.get("today") if isinstance(history_summary.get("today"), dict) else {}
    month = history_summary.get("month") if isinstance(history_summary.get("month"), dict) else {}
    current_wait_streak = _current_wait_streak_minutes(event_log, current_minutes)
    if current_wait_streak is None:
        current_wait_streak = coerce_int(today.get("current_wait_streak_minutes"))

    for brief in _briefs_with_tools(preference_context):
        tools = brief.get("tools") if isinstance(brief.get("tools"), dict) else {}
        config = tools.get("wait_generation")
        if not isinstance(config, dict):
            continue
        pref_id = str(brief.get("pref_id") or "")

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
            window = _current_or_next_window(current_minutes, fixed_window)
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


def make_wait_option(
    *,
    duration_minutes: int,
    status: dict[str, Any] | None,
    pref_id: str | None = None,
    source: str = "default_wait",
    reason: str | None = None,
) -> dict[str, Any]:
    duration = max(1, int(duration_minutes))
    start_minutes = coerce_int((status or {}).get("simulation_progress_minutes"))
    end_minutes = start_minutes + duration
    lat = coerce_float((status or {}).get("current_lat"))
    lng = coerce_float((status or {}).get("current_lng"))
    position = {"lat": lat, "lng": lng}
    option = {
        "option_id": f"{source}_{duration}" if not pref_id else f"{source}_{pref_id}_{duration}",
        "action": "wait",
        "params": {"duration_minutes": duration},
        "action_start_minutes": start_minutes,
        "action_start_time": (status or {}).get("simulation_wall_time") or format_simulation_time(start_minutes),
        "action_end_minutes": end_minutes,
        "action_end_time": format_simulation_time(end_minutes),
        "position": position,
        "position_before": position,
        "position_after": position,
        "source": source,
        "reason": reason or f"等待 {duration} 分钟",
        "metrics": {"duration_minutes": duration},
    }
    if pref_id:
        option["pref_id"] = pref_id
    return option


def make_reposition_option(
    *,
    option_id: str,
    pref_id: str,
    target_lat: float,
    target_lng: float,
    status: dict[str, Any],
    distance_km: float,
    estimated_minutes: int,
    reason: str | None,
    tool_origin: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    start_minutes = coerce_int(status.get("simulation_progress_minutes"))
    end_minutes = start_minutes + int(estimated_minutes)
    current_lat = coerce_float(status.get("current_lat"))
    current_lng = coerce_float(status.get("current_lng"))
    position_before = {"lat": current_lat, "lng": current_lng}
    position_after = {"lat": round(float(target_lat), 6), "lng": round(float(target_lng), 6)}
    option = {
        "option_id": option_id,
        "action": "reposition",
        "params": {"latitude": position_after["lat"], "longitude": position_after["lng"]},
        "action_start_minutes": start_minutes,
        "action_start_time": status.get("simulation_wall_time") or format_simulation_time(start_minutes),
        "action_end_minutes": end_minutes,
        "action_end_time": format_simulation_time(end_minutes),
        "position_before": position_before,
        "position_after": position_after,
        "distance_km": round(float(distance_km), 2),
        "estimated_minutes": int(estimated_minutes),
        "pref_id": pref_id,
        "reason": reason,
        "tool_origin": tool_origin,
        "metrics": {
            "distance_km": round(float(distance_km), 2),
            "estimated_minutes": int(estimated_minutes),
        },
    }
    if extra:
        option.update(extra)
    return option


def _deadline_result_for_candidate(
    *,
    brief: dict[str, Any],
    config: dict[str, Any],
    candidate: dict[str, Any],
    speed_km_per_hour: float,
) -> dict[str, Any] | None:
    simulated_event = candidate.get("simulated_event") if isinstance(candidate.get("simulated_event"), dict) else {}
    finish_minutes = coerce_int(simulated_event.get("action_end_minutes"), -1)
    start_minutes = coerce_int(simulated_event.get("action_start_minutes"), -1)
    if finish_minutes < 0 or start_minutes < 0:
        return None
    position_after = simulated_event.get("position_after") if isinstance(simulated_event.get("position_after"), dict) else {}
    center = config.get("center")
    if not isinstance(center, list) or len(center) != 2:
        return None
    lat = coerce_float(position_after.get("lat"), float("nan"))
    lng = coerce_float(position_after.get("lng"), float("nan"))
    target_lat = coerce_float(center[0], float("nan"))
    target_lng = coerce_float(center[1], float("nan"))
    if lat != lat or lng != lng or target_lat != target_lat or target_lng != target_lng:
        return None
    distance_km = haversine_km(lat, lng, target_lat, target_lng)
    travel_minutes = distance_to_minutes(distance_km, speed_km_per_hour)
    arrival_minutes = finish_minutes + travel_minutes
    deadline_minutes = _next_time_of_day_minutes(start_minutes, str(config.get("deadline_time") or ""))
    if deadline_minutes is None:
        return None
    can_reach = arrival_minutes <= deadline_minutes
    return {
        "pref_id": brief.get("pref_id"),
        "cargo_id": candidate.get("cargo_id"),
        "can_reach_deadline": can_reach,
        "deadline_minutes": deadline_minutes,
        "deadline_time": format_simulation_time(deadline_minutes),
        "arrival_if_go_to_target_minutes": arrival_minutes,
        "arrival_if_go_to_target_time": format_simulation_time(arrival_minutes),
        "travel_to_target_minutes": travel_minutes,
        "distance_to_target_km": round(distance_km, 2),
        "summary": (
            f"{'可达' if can_reach else '不可达'}。该订单{format_simulation_time(finish_minutes)}完成后，"
            f"从完成位置到目标点还需{travel_minutes}分钟，预计{format_simulation_time(arrival_minutes)}到达，"
            f"期限为{format_simulation_time(deadline_minutes)}。"
        ),
    }


def _time_window_result_for_event(
    *,
    brief: dict[str, Any],
    config: dict[str, Any],
    event: Any,
    option_id: str,
    action: str,
) -> dict[str, Any] | None:
    if not isinstance(event, dict):
        return None
    start_minutes = coerce_int(event.get("action_start_minutes"), -1)
    end_minutes = coerce_int(event.get("action_end_minutes"), -1)
    if start_minutes < 0 or end_minutes <= start_minutes:
        return None
    overlap_minutes = _overlap_fixed_window(start_minutes, end_minutes, config)
    overlaps = overlap_minutes > 0
    return {
        "pref_id": brief.get("pref_id"),
        "option_id": option_id,
        "action": action,
        "overlaps_forbidden_window": overlaps,
        "overlap_minutes": overlap_minutes,
        "window": {
            "start": config.get("start"),
            "end": config.get("end"),
            "cross_day": bool(config.get("cross_day")),
        },
        "action_start_time": format_simulation_time(start_minutes),
        "action_end_time": format_simulation_time(end_minutes),
        "summary": (
            f"模拟{action}区间{format_simulation_time(start_minutes)}至{format_simulation_time(end_minutes)}"
            f"{'与固定休息窗口重叠' + str(overlap_minutes) + '分钟' if overlaps else '不与固定休息窗口重叠'}。"
        ),
    }


def _overlap_fixed_window(start_minutes: int, end_minutes: int, config: dict[str, Any]) -> int:
    start_tod = _time_to_minutes(config.get("start"))
    end_tod = _time_to_minutes(config.get("end"))
    if start_tod is None or end_tod is None:
        return 0
    cross_day = bool(config.get("cross_day")) or end_tod <= start_tod
    total = 0
    first_day = start_minutes // DAY_MINUTES - 1
    last_day = end_minutes // DAY_MINUTES + 1
    for day in range(first_day, last_day + 1):
        window_start = day * DAY_MINUTES + start_tod
        window_end = (day + 1) * DAY_MINUTES + end_tod if cross_day else day * DAY_MINUTES + end_tod
        total += max(0, min(end_minutes, window_end) - max(start_minutes, window_start))
    return total


def _current_or_next_window(current_minutes: int, config: dict[str, Any]) -> tuple[int, int, bool] | None:
    start_tod = _time_to_minutes(config.get("start"))
    end_tod = _time_to_minutes(config.get("end"))
    if start_tod is None or end_tod is None:
        return None
    cross_day = bool(config.get("cross_day")) or end_tod <= start_tod
    current_day = current_minutes // DAY_MINUTES
    for day in range(current_day - 1, current_day + 3):
        window_start = day * DAY_MINUTES + start_tod
        window_end = (day + 1) * DAY_MINUTES + end_tod if cross_day else day * DAY_MINUTES + end_tod
        if window_start <= current_minutes < window_end:
            return window_start, window_end, True
        if current_minutes < window_start:
            return window_start, window_end, False
    return None


def _next_time_of_day_minutes(current_minutes: int, hhmm: str) -> int | None:
    minute_of_day = _time_to_minutes(hhmm)
    if minute_of_day is None:
        return None
    candidate = (current_minutes // DAY_MINUTES) * DAY_MINUTES + minute_of_day
    if candidate < current_minutes:
        candidate += DAY_MINUTES
    return candidate


def _time_to_minutes(value: Any) -> int | None:
    if not isinstance(value, str) or ":" not in value:
        return None
    hour_text, minute_text = value.split(":", 1)
    try:
        hour = int(hour_text)
        minute = int(minute_text[:2])
    except ValueError:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour * 60 + minute


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


def _any_candidate_can_reach_deadline(candidates: list[dict[str, Any]], pref_id: str) -> bool:
    for candidate in candidates:
        evidence = candidate.get("tool_evidence") if isinstance(candidate.get("tool_evidence"), dict) else {}
        results = evidence.get("deadline_location_check_result") if isinstance(evidence, dict) else []
        if not isinstance(results, list):
            continue
        for result in results:
            if isinstance(result, dict) and str(result.get("pref_id") or "") == pref_id and result.get("can_reach_deadline"):
                return True
    return False


def _briefs_with_tools(preference_context: dict[str, Any]) -> list[dict[str, Any]]:
    brief = preference_context.get("preference_brief")
    if not isinstance(brief, list):
        return []
    return [item for item in brief if isinstance(item, dict) and isinstance(item.get("tools"), dict) and item.get("tools")]
