"""Fixed time-window overlap checks for simulated actions."""

from __future__ import annotations

from typing import Any

from agent.history.time_utils import coerce_int, format_simulation_time


DAY_MINUTES = 1440


def evaluate_candidate(brief: dict[str, Any], config: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any] | None:
    result = evaluate_event(
        brief=brief,
        config=config,
        event=candidate.get("simulated_event"),
        option_id=str(candidate.get("cargo_id") or ""),
        action="take_order",
    )
    if result is not None:
        result["cargo_id"] = candidate.get("cargo_id")
    return result


def evaluate_event(
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
    overlap_minutes = overlap_fixed_window(start_minutes, end_minutes, config)
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


def attach_reposition_time_window_evidence(
    reposition_options: list[dict[str, Any]],
    *,
    preference_context: dict[str, Any],
    tool_plan: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    if not reposition_options or not tool_plan:
        return reposition_options
    brief_by_id = _brief_by_pref_id(preference_context)
    enhanced: list[dict[str, Any]] = []
    for option in reposition_options:
        item = dict(option)
        tool_evidence: dict[str, Any] = dict(item.get("tool_evidence") or {})
        results: list[dict[str, Any]] = []
        for pref_id, tools in tool_plan.items():
            config = tools.get("time_window_check")
            if not isinstance(config, dict):
                continue
            result = evaluate_event(
                brief=brief_by_id.get(pref_id, {"pref_id": pref_id}),
                config=config,
                event=item,
                option_id=str(item.get("option_id") or ""),
                action="reposition",
            )
            if result is not None:
                results.append(result)
        if results:
            tool_evidence["time_window_check_result"] = results
        if tool_evidence:
            item["tool_evidence"] = tool_evidence
        enhanced.append(item)
    return enhanced


def annotate_action_options(
    options: list[dict[str, Any]],
    *,
    preference_context: dict[str, Any],
    tool_plan: dict[str, dict[str, Any]],
    context: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    return attach_reposition_time_window_evidence(
        options,
        preference_context=preference_context,
        tool_plan=tool_plan,
    )


def overlap_fixed_window(start_minutes: int, end_minutes: int, config: dict[str, Any]) -> int:
    start_tod = time_to_minutes(config.get("start"))
    end_tod = time_to_minutes(config.get("end"))
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


def current_or_next_window(current_minutes: int, config: dict[str, Any]) -> tuple[int, int, bool] | None:
    start_tod = time_to_minutes(config.get("start"))
    end_tod = time_to_minutes(config.get("end"))
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


def next_time_of_day_minutes(current_minutes: int, hhmm: str) -> int | None:
    minute_of_day = time_to_minutes(hhmm)
    if minute_of_day is None:
        return None
    candidate = (current_minutes // DAY_MINUTES) * DAY_MINUTES + minute_of_day
    if candidate < current_minutes:
        candidate += DAY_MINUTES
    return candidate


def time_to_minutes(value: Any) -> int | None:
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


def _brief_by_pref_id(preference_context: dict[str, Any]) -> dict[str, dict[str, Any]]:
    brief = preference_context.get("preference_brief")
    if not isinstance(brief, list):
        return {}
    return {str(item.get("pref_id") or ""): item for item in brief if isinstance(item, dict)}
