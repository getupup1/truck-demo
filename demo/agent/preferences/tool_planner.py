"""Second-pass LLM planner for optional preference evidence tools."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from copy import deepcopy
from typing import Any

from agent.history.json_utils import extract_model_json_object
from agent.history.time_utils import coerce_float


TOOL_NAMES = (
    "geo_checks",
    "candidate_geo_contribution",
    "history_geo_summary",
    "time_window_check",
    "deadline_location_check",
    "wait_generation",
)


class PreferenceToolPlanner:
    """Attach optional deterministic tool configs to preference briefs."""

    def __init__(self, model_chat_completion: Callable[[dict[str, Any]], dict[str, Any]]) -> None:
        self._model_chat_completion = model_chat_completion
        self._logger = logging.getLogger("agent.preferences.tool_planner")

    def plan_for_context(
        self,
        *,
        driver_id: str,
        status: dict[str, Any],
        preference_context: dict[str, Any],
    ) -> dict[str, Any]:
        brief = preference_context.get("preference_brief")
        if not isinstance(brief, list) or not brief:
            return preference_context

        raw = self._request_plan(driver_id=driver_id, status=status, preference_context=preference_context)
        planned = deepcopy(preference_context)
        patches = self._extract_patches(raw)
        patch_by_pref_id = {
            str(item.get("pref_id")): item for item in patches if isinstance(item, dict) and item.get("pref_id") is not None
        }

        for item in planned.get("preference_brief", []):
            if not isinstance(item, dict):
                continue
            pref_id = str(item.get("pref_id") or "")
            patch = patch_by_pref_id.get(pref_id, {})
            tools = patch.get("tools") if isinstance(patch, dict) else {}
            item["tools"] = normalize_tools(tools)

        self._logger.info(
            "preference_tool_plan ok driver_id=%s enabled_tools=%s",
            driver_id,
            sum(len(item.get("tools", {})) for item in planned.get("preference_brief", []) if isinstance(item, dict)),
        )
        return planned

    def _request_plan(
        self,
        *,
        driver_id: str,
        status: dict[str, Any],
        preference_context: dict[str, Any],
    ) -> dict[str, Any]:
        payload = {
            "driver_id": driver_id,
            "simulation_progress_minutes": status.get("simulation_progress_minutes"),
            "simulation_wall_time": status.get("simulation_wall_time"),
            "preference_brief": preference_context.get("preference_brief", []),
        }
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                response = self._model_chat_completion(
                    {
                        "messages": [
                            {"role": "system", "content": self._system_prompt()},
                            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                        ],
                        "response_format": {"type": "json_object"},
                    }
                )
                return extract_model_json_object(response)
            except Exception as exc:  # noqa: BLE001 - retry once for transient model issues.
                last_error = exc
                self._logger.warning(
                    "preference_tool_plan failed driver_id=%s attempt=%s error=%s",
                    driver_id,
                    attempt + 1,
                    exc,
                )
        raise RuntimeError(f"preference tool planning failed after retry: {last_error}") from last_error

    @staticmethod
    def _system_prompt() -> str:
        return (
            "你是货运司机偏好工具需求判断器。只允许输出 JSON 对象，禁止 markdown、解释和额外文本。"
            "你会收到 preference_brief。不要重新提炼偏好，不要修改 core_requirement、penalty_amount、needs_history。"
            "你的任务只是判断每条偏好是否需要 deterministic tools 辅助。没有工具需求时 tools 必须为 {}。"
            "只允许这些工具：geo_checks、candidate_geo_contribution、history_geo_summary、"
            "time_window_check、deadline_location_check、wait_generation。"
            "geo_checks 用于地区限制，例如禁入圆形区域、必须保持在经纬度范围内。"
            "candidate_geo_contribution 与 history_geo_summary 必须成对用于累计到访偏好，例如自然月内至少 N 个不同自然日到达某点半径内。"
            "time_window_check 用于固定时间窗口内不接单、不空车赶路、必须停车休息等偏好，"
            "只提取窗口 start、end、cross_day。"
            "deadline_location_check 用于必须在某个时间前到达指定坐标半径内的偏好，"
            "只提取 center、radius_km、deadline_time。"
            "wait_generation 用于生成 wait 候选，支持连续休息时长、固定休息窗口、月内完全休息天数三类。"
            "本版只支持明确坐标/半径或明确经纬度范围；只有地名且没有边界或坐标时，不要启用工具。"
            "工具配置必须只写启用工具，不要写 false。"
            "geo_checks 配置格式："
            '{"relation":"forbidden_inside|must_inside","center":[lat,lng]或null,"radius_km":数字或null,'
            '"lat_range":[min,max]或null,"lng_range":[min,max]或null,"reason":"事实说明"}。'
            "geo_checks 中 center+radius_km 与 lat_range+lng_range 只能二选一。"
            "candidate_geo_contribution 配置格式同 geo_checks，但 relation 必须是 must_visit。"
            "history_geo_summary 配置格式："
            '{"reason":"period=current_month; required_visit_count=N; count_unit=distinct_day",'
            '"required_visit_count":N,"period":"current_month","count_unit":"distinct_day"}。'
            "如果启用 history_geo_summary，必须同时启用 candidate_geo_contribution，并且两者描述同一个目标地点或区域。"
            "time_window_check 配置格式："
            '{"start":"HH:MM","end":"HH:MM","cross_day":true或false,"reason":"固定窗口说明"}。'
            "deadline_location_check 配置格式："
            '{"center":[lat,lng],"radius_km":数字,"deadline_time":"HH:MM","reason":"期限地点说明"}。'
            "wait_generation 配置格式："
            '{"continuous_rest":{"hours":数字,"weekdays_only":true或false}或null,'
            '"fixed_rest_window":{"start":"HH:MM","end":"HH:MM","cross_day":true或false}或null,'
            '"monthly_rest_days":{"days":数字}或null,"reason":"生成wait候选的原因"}。'
            "continuous_rest 中 weekdays_only 只在原文明确写平日、工作日、非周末时为 true；"
            "若原文是每天、每日或未限定工作日，则为 false。"
            "输出格式必须为："
            '{"preference_brief":[{"pref_id":"pref_0","tools":{}},'
            '{"pref_id":"pref_1","tools":{"candidate_geo_contribution":{...},"history_geo_summary":{...}}}]}。'
        )

    @staticmethod
    def _extract_patches(raw: Any) -> list[dict[str, Any]]:
        if not isinstance(raw, dict):
            return []
        for key in ("preference_brief", "tool_updates", "preferences"):
            value = raw.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        if raw.get("pref_id") is not None:
            return [raw]
        return []


def normalize_tools(value: Any) -> dict[str, Any]:
    raw_tools = value if isinstance(value, dict) else {}
    normalized: dict[str, Any] = {}
    for name in TOOL_NAMES:
        raw_config = raw_tools.get(name)
        if not raw_config or not isinstance(raw_config, dict):
            continue
        if raw_config.get("enabled") is False:
            continue
        config = _normalize_tool_config(name, raw_config)
        if config:
            normalized[name] = config
    return normalized


def _normalize_tool_config(name: str, raw: dict[str, Any]) -> dict[str, Any]:
    if name == "history_geo_summary":
        config = {
            "reason": str(raw.get("reason") or "").strip(),
            "required_visit_count": _positive_int(raw.get("required_visit_count")),
            "period": str(raw.get("period") or "current_month").strip() or "current_month",
            "count_unit": str(raw.get("count_unit") or "distinct_day").strip() or "distinct_day",
        }
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
        radius = _positive_float(raw.get("radius_km"))
        deadline_time = _time_hhmm(raw.get("deadline_time") or raw.get("deadline") or raw.get("time"))
        if center is None or radius is None or deadline_time is None:
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

    center = _lat_lng_pair(raw.get("center"))
    radius = _positive_float(raw.get("radius_km"))
    lat_range = _range_pair(raw.get("lat_range"))
    lng_range = _range_pair(raw.get("lng_range"))
    has_circle = center is not None and radius is not None
    has_range = lat_range is not None and lng_range is not None
    if has_circle == has_range:
        return {}

    return {
        "relation": relation,
        "center": center,
        "radius_km": radius,
        "lat_range": lat_range,
        "lng_range": lng_range,
        "reason": str(raw.get("reason") or "").strip(),
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
    return {
        "hours": hours,
        "weekdays_only": _bool_or_default(value.get("weekdays_only"), False),
    }


def _normalize_fixed_window(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    start = _time_hhmm(value.get("start") or value.get("start_time"))
    end = _time_hhmm(value.get("end") or value.get("end_time"))
    if start is None or end is None:
        return None
    return {
        "start": start,
        "end": end,
        "cross_day": _bool_or_default(value.get("cross_day"), _infer_cross_day(start, end)),
    }


def _normalize_monthly_rest_days(value: Any) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    days = _positive_int(value.get("days") or value.get("day_count"))
    if days is None:
        return None
    return {"days": days}
