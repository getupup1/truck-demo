"""Build compact event logs, summaries, and slices from decision history."""

from __future__ import annotations

import re
from typing import Any

from agent.history.time_utils import coerce_float, coerce_int, format_simulation_time, parse_wall_time_minutes


def build_event_log(history_resp: dict[str, Any]) -> list[dict[str, Any]]:
    records = history_resp.get("records") if isinstance(history_resp, dict) else []
    if not isinstance(records, list):
        return []

    events: list[dict[str, Any]] = []
    previous_step_end = 0
    for record in records:
        if not isinstance(record, dict):
            continue
        action = record.get("action")
        result = record.get("result")
        if not isinstance(action, dict) or not isinstance(result, dict):
            continue
        query_scan_cost = coerce_int(record.get("query_scan_cost_minutes"), -1)
        action_exec_cost = coerce_int(record.get("action_exec_cost_minutes"), -1)
        step_end = coerce_int(result.get("simulation_progress_minutes"), -1)
        if min(query_scan_cost, action_exec_cost, step_end) < 0:
            continue

        action_start = previous_step_end + query_scan_cost
        action_end = action_start + action_exec_cost
        position_before = record.get("position_before") if isinstance(record.get("position_before"), dict) else {}
        position_after = record.get("position_after") if isinstance(record.get("position_after"), dict) else {}
        params = action.get("params") if isinstance(action.get("params"), dict) else {}
        action_name = str(action.get("action", "")).strip().lower()

        events.append(
            {
                "step": record.get("step"),
                "action": action_name,
                "params": params,
                "action_start_minutes": action_start,
                "action_start_time": format_simulation_time(action_start),
                "action_end_minutes": action_end,
                "action_end_time": format_simulation_time(action_end),
                "position_before": {
                    "lat": coerce_float(position_before.get("lat")),
                    "lng": coerce_float(position_before.get("lng")),
                },
                "position_after": {
                    "lat": coerce_float(position_after.get("lat")),
                    "lng": coerce_float(position_after.get("lng")),
                },
                "result": {
                    "accepted": result.get("accepted"),
                    "detail": result.get("detail"),
                    "cargo_id": result.get("cargo_id") or params.get("cargo_id"),
                    "pickup_deadhead_km": result.get("pickup_deadhead_km"),
                    "haul_distance_km": result.get("haul_distance_km"),
                    "distance_km": result.get("distance_km"),
                    "simulation_progress_minutes": result.get("simulation_progress_minutes"),
                    "simulation_wall_time": result.get("simulation_wall_time"),
                },
            }
        )
        previous_step_end = step_end
    return events


def build_history_summary(event_log: list[dict[str, Any]], current_minutes: int) -> dict[str, Any]:
    """Common history indicators used as compact LLM context."""
    current_day = max(0, int(current_minutes) // 1440)
    completed_days = list(range(current_day))

    today_events = [event for event in event_log if _event_start_minutes(event) // 1440 == current_day]
    today_take_orders = [event for event in today_events if event.get("action") == "take_order"]
    today_success = [event for event in today_take_orders if bool(_event_result(event).get("accepted", False))]
    today_failed = [event for event in today_take_orders if not bool(_event_result(event).get("accepted", False))]
    today_wait_intervals = _wait_intervals(event_log, day=current_day)

    success_orders = [
        event
        for event in event_log
        if event.get("action") == "take_order" and bool(_event_result(event).get("accepted", False))
    ]
    failed_orders = [
        event
        for event in event_log
        if event.get("action") == "take_order" and not bool(_event_result(event).get("accepted", False))
    ]
    failure_reasons: dict[str, int] = {}
    for event in failed_orders:
        detail = str(_event_result(event).get("detail") or "unknown")
        failure_reasons[detail] = failure_reasons.get(detail, 0) + 1

    accepted_days = {_event_start_minutes(event) // 1440 for event in success_orders}
    active_by_completed_day = _active_minutes_by_day(event_log, completed_days)
    no_order_days = sum(1 for day in completed_days if day not in accepted_days)
    fully_idle_days = sum(1 for day in completed_days if active_by_completed_day.get(day, 0) == 0)
    month_wait_intervals = _wait_intervals(event_log)
    longest_wait = _longest_merged_span(month_wait_intervals)
    today_distance = _distance_metrics(event_log, day=current_day)
    month_distance = _distance_metrics(event_log)

    return {
        "total_steps": len(event_log),
        "today": {
            "day_index": current_day,
            "successful_take_order_count": len(today_success),
            "failed_take_order_count": len(today_failed),
            "longest_wait_minutes": _longest_merged_span(today_wait_intervals),
            "current_wait_streak_minutes": _current_wait_streak_minutes(today_wait_intervals, int(current_minutes)),
            "active_minutes": _active_minutes_by_day(event_log, [current_day]).get(current_day, 0),
            **today_distance,
        },
        "month": {
            "successful_take_order_count": len(success_orders),
            "failed_take_order_count": len(failed_orders),
            "failure_reasons": failure_reasons,
            "completed_day_count": len(completed_days),
            "no_order_days": no_order_days,
            "fully_idle_days": fully_idle_days,
            "longest_wait_minutes": longest_wait,
            "longest_wait_hours": round(longest_wait / 60.0, 2),
            **month_distance,
        },
    }


class HistorySliceBuilder:
    """Select event slices requested by preference brief history requirements."""

    def build(
        self,
        *,
        event_log: list[dict[str, Any]],
        current_minutes: int,
        preference_context: dict[str, Any],
    ) -> dict[str, Any]:
        history_summary = build_history_summary(event_log, current_minutes)
        needs = self._history_needs(preference_context)
        if not needs:
            return {"history_summary": history_summary, "history_slice": None}

        period_start, period_end, reason = self._resolve_period(needs, current_minutes, event_log)
        events = _slice_events(event_log, period_start, period_end)
        return {
            "history_summary": history_summary,
            "history_slice": {
                "period": {
                    "start_time": format_simulation_time(period_start),
                    "end_time": format_simulation_time(period_end),
                    "reason": reason,
                },
                "events": events,
            },
        }

    @staticmethod
    def _history_needs(preference_context: dict[str, Any]) -> list[str]:
        needs: list[str] = []
        brief = preference_context.get("preference_brief")
        if isinstance(brief, list):
            for item in brief:
                if isinstance(item, dict):
                    value = item.get("needs_history")
                    if isinstance(value, str) and value.strip():
                        needs.append(value.strip())
                    elif value is True:
                        needs.append("full event_log requested by preference_brief")
        return needs

    @staticmethod
    def _resolve_period(
        needs: list[str],
        current_minutes: int,
        event_log: list[dict[str, Any]],
    ) -> tuple[int, int, str]:
        joined = "；".join(needs)
        text = joined.lower()
        current_day_start = (int(current_minutes) // 1440) * 1440

        explicit_period = _parse_explicit_wall_time_period(joined)
        if explicit_period is not None:
            period_start, period_end = explicit_period
            return period_start, min(period_end, int(current_minutes)), joined
        if any(token in text for token in ("当前自然日", "当天", "今日", "今天", "current day", "today")):
            return current_day_start, int(current_minutes), joined
        if any(token in text for token in ("本月", "当前月", "自然月", "月度", "current month", "month")):
            return 0, int(current_minutes), joined
        if any(token in text for token in ("最近", "recent", "last")):
            recent = event_log[-20:]
            if not recent:
                return int(current_minutes), int(current_minutes), joined
            return _event_start_minutes(recent[0]), int(current_minutes), joined
        if any(token in text for token in ("完整", "全量", "full", "无法解析")):
            return _full_period(event_log, current_minutes, joined)
        return _full_period(event_log, current_minutes, f"无法解析 history need，保守返回完整历史：{joined}")


def _full_period(event_log: list[dict[str, Any]], current_minutes: int, reason: str) -> tuple[int, int, str]:
    if not event_log:
        return 0, int(current_minutes), reason
    return min(_event_start_minutes(event) for event in event_log), int(current_minutes), reason


def _parse_explicit_wall_time_period(text: str) -> tuple[int, int] | None:
    start_match = re.search(r"start_simulation_wall_time\s*=\s*([0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2})", text)
    end_match = re.search(r"end_simulation_wall_time\s*=\s*([0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2})", text)
    if start_match is None or end_match is None:
        return None
    start_minutes = parse_wall_time_minutes(start_match.group(1))
    end_minutes = parse_wall_time_minutes(end_match.group(1))
    if start_minutes is None or end_minutes is None:
        return None
    if end_minutes < start_minutes:
        return None
    return start_minutes, end_minutes


def _slice_events(event_log: list[dict[str, Any]], start_minutes: int, end_minutes: int) -> list[dict[str, Any]]:
    return [
        event
        for event in event_log
        if _event_end_minutes(event) > start_minutes and _event_start_minutes(event) <= end_minutes
    ]


def _event_result(event: dict[str, Any]) -> dict[str, Any]:
    result = event.get("result")
    return result if isinstance(result, dict) else {}


def _event_start_minutes(event: dict[str, Any]) -> int:
    return coerce_int(event.get("action_start_minutes"))


def _event_end_minutes(event: dict[str, Any]) -> int:
    return coerce_int(event.get("action_end_minutes"), _event_start_minutes(event))


def _iter_day_segments(start_min: int, end_min: int) -> list[tuple[int, int]]:
    if end_min <= start_min:
        return []
    out: list[tuple[int, int]] = []
    cur = start_min
    while cur < end_min:
        day_idx = cur // 1440
        day_end = (day_idx + 1) * 1440
        seg_end = min(day_end, end_min)
        out.append((day_idx, seg_end - cur))
        cur = seg_end
    return out


def _merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if not intervals:
        return []
    merged = [sorted(intervals)[0]]
    for start, end in sorted(intervals)[1:]:
        last_start, last_end = merged[-1]
        if start > last_end:
            merged.append((start, end))
        else:
            merged[-1] = (last_start, max(last_end, end))
    return merged


def _longest_merged_span(intervals: list[tuple[int, int]]) -> int:
    return max((end - start for start, end in _merge_intervals(intervals)), default=0)


def _current_wait_streak_minutes(intervals: list[tuple[int, int]], current_minutes: int) -> int:
    for start, end in reversed(_merge_intervals(intervals)):
        if end == current_minutes:
            return end - start
        if end < current_minutes:
            break
    return 0


def _wait_intervals(event_log: list[dict[str, Any]], *, day: int | None = None) -> list[tuple[int, int]]:
    intervals: list[tuple[int, int]] = []
    day_start = day * 1440 if day is not None else None
    day_end = day_start + 1440 if day_start is not None else None
    for event in event_log:
        if event.get("action") != "wait":
            continue
        start = _event_start_minutes(event)
        end = _event_end_minutes(event)
        if day_start is not None and day_end is not None:
            start = max(start, day_start)
            end = min(end, day_end)
        if end > start:
            intervals.append((start, end))
    return intervals


def _active_minutes_by_day(event_log: list[dict[str, Any]], days: list[int]) -> dict[int, int]:
    active = {day: 0 for day in days}
    for event in event_log:
        if event.get("action") not in {"take_order", "reposition"}:
            continue
        for day, minutes in _iter_day_segments(_event_start_minutes(event), _event_end_minutes(event)):
            if day in active:
                active[day] += minutes
    return active


def _distance_metrics(event_log: list[dict[str, Any]], *, day: int | None = None) -> dict[str, float]:
    pickup_deadhead_km = 0.0
    reposition_km = 0.0
    haul_distance_km = 0.0
    failed_take_order_deadhead_km = 0.0

    for event in event_log:
        if day is not None and _event_start_minutes(event) // 1440 != day:
            continue
        result = _event_result(event)
        action = event.get("action")
        if action == "reposition":
            reposition_km += coerce_float(result.get("distance_km"))
        elif action == "take_order":
            if bool(result.get("accepted", False)):
                pickup_deadhead_km += coerce_float(result.get("pickup_deadhead_km"))
                haul_distance_km += coerce_float(result.get("haul_distance_km"))
            else:
                failed_take_order_deadhead_km += coerce_float(result.get("pickup_deadhead_km"))

    deadhead_km = pickup_deadhead_km + reposition_km
    total_driving_km = pickup_deadhead_km + reposition_km + haul_distance_km + failed_take_order_deadhead_km
    return {
        "deadhead_km": round(deadhead_km, 2),
        "pickup_deadhead_km": round(pickup_deadhead_km, 2),
        "reposition_km": round(reposition_km, 2),
        "haul_distance_km": round(haul_distance_km, 2),
        "failed_take_order_deadhead_km": round(failed_take_order_deadhead_km, 2),
        "total_driving_km": round(total_driving_km, 2),
    }
