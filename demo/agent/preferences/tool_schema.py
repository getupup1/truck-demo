"""Registry and validation helpers for optional preference tools."""

from __future__ import annotations

import re
from typing import Any

from agent.history.time_utils import coerce_float


TOOL_REGISTRY: dict[str, dict[str, Any]] = {
    "geo_checks": {
        "stage": ("candidate",),
        "result_key": "geo_checks_result",
        "module": "agent.tools.geo_checks",
    },
    "candidate_geo_contribution": {
        "stage": ("candidate",),
        "result_key": "candidate_geo_contribution_result",
        "module": "agent.tools.candidate_geo_contribution",
    },
    "history_geo_summary": {
        "stage": ("context", "action"),
        "result_key": "history_geo_summary",
        "module": "agent.tools.history_geo_summary",
    },
    "time_window_check": {
        "stage": ("candidate",),
        "result_key": "time_window_check_result",
        "module": "agent.tools.time_window_checks",
    },
    "deadline_location_check": {
        "stage": ("candidate", "action"),
        "result_key": "deadline_location_check_result",
        "module": "agent.tools.deadline_location_check",
    },
    "wait_generation": {
        "stage": ("action",),
        "result_key": "wait_options",
        "module": "agent.tools.wait_generation",
    },
}

TOOL_NAMES = tuple(TOOL_REGISTRY.keys())


def default_tool_flags() -> list[str]:
    return []


def normalize_tool_flags(value: Any) -> list[str]:
    enabled: list[str] = []
    if isinstance(value, (list, tuple, set)):
        for item in value:
            name = str(item).strip()
            if name in TOOL_NAMES and name not in enabled:
                enabled.append(name)
        return enabled
    if isinstance(value, str):
        name = value.strip()
        return [name] if name in TOOL_NAMES else []
    if isinstance(value, dict):
        for name in TOOL_NAMES:
            item = value.get(name)
            if isinstance(item, bool) and item and name not in enabled:
                enabled.append(name)
            elif isinstance(item, dict) and bool(item.get("enabled", True)) and name not in enabled:
                enabled.append(name)
    return enabled


def enabled_tools_for_brief(brief: dict[str, Any]) -> list[str]:
    return normalize_tool_flags(brief.get("tools"))


def enabled_tool_names(preference_context: dict[str, Any]) -> list[str]:
    enabled: list[str] = []
    brief = preference_context.get("preference_brief")
    if not isinstance(brief, list):
        return enabled
    for item in brief:
        if not isinstance(item, dict):
            continue
        for name in enabled_tools_for_brief(item):
            if name not in enabled:
                enabled.append(name)
    return enabled


def stage_tools(stage: str) -> list[str]:
    return [name for name, schema in TOOL_REGISTRY.items() if stage in schema.get("stage", ())]


def result_key(tool_name: str) -> str:
    schema = TOOL_REGISTRY.get(tool_name) or {}
    return str(schema.get("result_key") or f"{tool_name}_result")


def module_path(tool_name: str) -> str:
    schema = TOOL_REGISTRY.get(tool_name) or {}
    return str(schema.get("module") or "")


def normalize_tool_plan(raw: Any, preference_context: dict[str, Any]) -> dict[str, dict[str, Any]]:
    patches = _extract_plan_items(raw)
    allowed_by_pref_id = _allowed_tools_by_pref_id(preference_context)
    plan: dict[str, dict[str, Any]] = {}
    for patch in patches:
        pref_id = str(patch.get("pref_id") or "").strip()
        if not pref_id:
            continue
        raw_tools = patch.get("tools") if isinstance(patch.get("tools"), dict) else {}
        allowed = allowed_by_pref_id.get(pref_id, set())
        normalized_tools: dict[str, Any] = {}
        for name in TOOL_NAMES:
            if name not in allowed:
                continue
            config = raw_tools.get(name)
            if not isinstance(config, dict):
                continue
            normalized = normalize_tool_config(name, config)
            if normalized:
                normalized_tools[name] = normalized
        if normalized_tools:
            plan[pref_id] = normalized_tools
    return plan


def normalize_tool_config(name: str, raw: dict[str, Any]) -> dict[str, Any]:
    if name == "history_geo_summary":
        config = {
            "reason": str(raw.get("reason") or "").strip(),
            "required_visit_count": _positive_int(raw.get("required_visit_count")),
            "period": str(raw.get("period") or "current_month").strip() or "current_month",
            "count_unit": str(raw.get("count_unit") or "distinct_day").strip() or "distinct_day",
        }
        target = _normalize_geo_target(raw)
        config.update(target)
        return {key: value for key, value in config.items() if value is not None and value != ""}

    if name == "time_window_check":
        start = _time_hhmm(raw.get("start") or raw.get("start_time"))
        end = _time_hhmm(raw.get("end") or raw.get("end_time"))
        if start is None or end is None:
            return {}
        return {
            "start": start,
            "end": end,
            "cross_day": _bool_or_default(raw.get("cross_day"), _infer_cross_day(start, end)),
            "reason": str(raw.get("reason") or "").strip(),
        }

    if name == "deadline_location_check":
        center = _lat_lng_pair(raw.get("center"))
        radius = _positive_float(raw.get("radius_km")) or 1.0
        deadline_time = _time_hhmm(raw.get("deadline_time") or raw.get("deadline") or raw.get("time"))
        if center is None or deadline_time is None:
            return {}
        return {
            "center": center,
            "radius_km": radius,
            "deadline_time": deadline_time,
            "reason": str(raw.get("reason") or "").strip(),
        }

    if name == "wait_generation":
        continuous_rest = _normalize_continuous_rest(raw.get("continuous_rest"))
        fixed_rest_window = _normalize_fixed_window(raw.get("fixed_rest_window"))
        monthly_rest_days = _normalize_monthly_rest_days(raw.get("monthly_rest_days"))
        config = {
            "continuous_rest": continuous_rest,
            "fixed_rest_window": fixed_rest_window,
            "monthly_rest_days": monthly_rest_days,
            "reason": str(raw.get("reason") or "").strip(),
        }
        return {key: value for key, value in config.items() if value is not None and value != ""}

    relation = str(raw.get("relation") or "").strip()
    if name == "candidate_geo_contribution":
        relation = "must_visit"
    elif relation not in {"forbidden_inside", "must_inside"}:
        return {}

    target = _normalize_geo_target(raw)
    has_circle = target.get("center") is not None and target.get("radius_km") is not None
    has_range = target.get("lat_range") is not None and target.get("lng_range") is not None
    if has_circle == has_range:
        return {}
    return {
        "relation": relation,
        "center": target.get("center"),
        "radius_km": target.get("radius_km"),
        "lat_range": target.get("lat_range"),
        "lng_range": target.get("lng_range"),
        "reason": str(raw.get("reason") or "").strip(),
    }


def configured_context_to_tool_plan(preference_context: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Compatibility helper for old callers that still store configs under brief.tools."""
    plan: dict[str, dict[str, Any]] = {}
    brief = preference_context.get("preference_brief")
    if not isinstance(brief, list):
        return plan
    for item in brief:
        if not isinstance(item, dict):
            continue
        pref_id = str(item.get("pref_id") or "").strip()
        tools = item.get("tools") if isinstance(item.get("tools"), dict) else {}
        configured: dict[str, Any] = {}
        for name in TOOL_NAMES:
            raw_config = tools.get(name)
            if isinstance(raw_config, dict):
                normalized = normalize_tool_config(name, raw_config)
                if normalized:
                    configured[name] = normalized
        if pref_id and configured:
            plan[pref_id] = configured
    return plan


def _extract_plan_items(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, dict):
        return []
    value = raw.get("tool_plan")
    if isinstance(value, dict):
        return [
            {"pref_id": pref_id, "tools": tools}
            for pref_id, tools in value.items()
            if isinstance(tools, dict)
        ]
    for key in ("preference_brief", "tool_updates", "preferences"):
        value = raw.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    if raw.get("pref_id") is not None:
        return [raw]
    return []


def _allowed_tools_by_pref_id(preference_context: dict[str, Any]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    brief = preference_context.get("preference_brief")
    if not isinstance(brief, list):
        return out
    for item in brief:
        if not isinstance(item, dict):
            continue
        pref_id = str(item.get("pref_id") or "").strip()
        if pref_id:
            out[pref_id] = set(enabled_tools_for_brief(item))
    return out


def _normalize_geo_target(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "center": _lat_lng_pair(raw.get("center")),
        "radius_km": _positive_float(raw.get("radius_km")),
        "lat_range": _range_pair(raw.get("lat_range")),
        "lng_range": _range_pair(raw.get("lng_range")),
    }


def _lat_lng_pair(value: Any) -> list[float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    lat = coerce_float(value[0], float("nan"))
    lng = coerce_float(value[1], float("nan"))
    if lat != lat or lng != lng:
        return None
    return [round(lat, 6), round(lng, 6)]


def _range_pair(value: Any) -> list[float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    low = coerce_float(value[0], float("nan"))
    high = coerce_float(value[1], float("nan"))
    if low != low or high != high:
        return None
    return [round(min(low, high), 6), round(max(low, high), 6)]


def _positive_float(value: Any) -> float | None:
    number = coerce_float(value, -1.0)
    if number <= 0 and isinstance(value, str):
        match = re.search(r"-?\d+(?:\.\d+)?", value)
        if match:
            number = coerce_float(match.group(0), -1.0)
    return round(number, 6) if number > 0 else None


def _positive_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        if not isinstance(value, str):
            return None
        match = re.search(r"\d+", value)
        if match is None:
            return None
        number = int(match.group(0))
    return number if number > 0 else None


def _time_hhmm(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    match = re.search(r"(\d{1,2}):(\d{2})(?::\d{2})?", text)
    if match is None:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return f"{hour:02d}:{minute:02d}"


def _infer_cross_day(start: str, end: str) -> bool:
    return _minute_of_day(end) <= _minute_of_day(start)


def _minute_of_day(value: str) -> int:
    hour, minute = value.split(":")
    return int(hour) * 60 + int(minute)


def _bool_or_default(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"true", "yes", "1", "是", "跨天"}:
            return True
        if text in {"false", "no", "0", "否", "不跨天"}:
            return False
    return default


def _normalize_continuous_rest(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    hours = _positive_float(value.get("hours") or value.get("hour"))
    if hours is None:
        return None
    return {"hours": hours, "weekdays_only": _bool_or_default(value.get("weekdays_only"), False)}


def _normalize_fixed_window(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    start = _time_hhmm(value.get("start") or value.get("start_time"))
    end = _time_hhmm(value.get("end") or value.get("end_time"))
    if start is None or end is None:
        return None
    return {"start": start, "end": end, "cross_day": _bool_or_default(value.get("cross_day"), _infer_cross_day(start, end))}


def _normalize_monthly_rest_days(value: Any) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    days = _positive_int(value.get("days") or value.get("day_count"))
    if days is None:
        return None
    return {"days": days}
