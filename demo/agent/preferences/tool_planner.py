"""Second-pass LLM planner for enabled preference evidence tools."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from agent.history.json_utils import extract_model_json_object
from agent.preferences.tool_prompts import detailed_tool_prompt
from agent.preferences.tool_schema import enabled_tool_names, normalize_tool_plan


class PreferenceToolPlanner:
    """Extract structured configs only for tools enabled by PreferenceBriefExtractor."""

    def __init__(self, model_chat_completion: Callable[[dict[str, Any]], dict[str, Any]]) -> None:
        self._model_chat_completion = model_chat_completion
        self._cache: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
        self._logger = logging.getLogger("agent.preferences.tool_planner")

    def plan_for_context(
        self,
        *,
        driver_id: str,
        status: dict[str, Any],
        preference_context: dict[str, Any],
    ) -> dict[str, dict[str, Any]]:
        enabled = enabled_tool_names(preference_context)
        if not enabled:
            return {}
        signature = str(preference_context.get("preference_signature") or json.dumps(preference_context.get("preference_brief", []), ensure_ascii=False, sort_keys=True))
        cache_key = (driver_id, signature)
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        raw = self._request_plan(
            driver_id=driver_id,
            status=status,
            preference_context=preference_context,
            enabled_tools=enabled,
        )
        tool_plan = normalize_tool_plan(raw, preference_context)
        self._cache[cache_key] = tool_plan
        self._logger.info(
            "preference_tool_plan ok driver_id=%s prefs=%s tools=%s",
            driver_id,
            len(tool_plan),
            sum(len(tools) for tools in tool_plan.values()),
        )
        return tool_plan

    def _request_plan(
        self,
        *,
        driver_id: str,
        status: dict[str, Any],
        preference_context: dict[str, Any],
        enabled_tools: list[str],
    ) -> dict[str, Any]:
        payload = {
            "driver_id": driver_id,
            "simulation_progress_minutes": status.get("simulation_progress_minutes"),
            "simulation_wall_time": status.get("simulation_wall_time"),
            "enabled_tools": enabled_tools,
            "preference_brief": preference_context.get("preference_brief", []),
        }
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                response = self._model_chat_completion(
                    {
                        "messages": [
                            {"role": "system", "content": self._system_prompt(enabled_tools)},
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
    def _system_prompt(enabled_tools: list[str]) -> str:
        return (
            "你是货运司机偏好工具配置提取器。只允许输出 JSON 对象，禁止 markdown、解释和额外文本。"
            "你会收到 preference_brief，其中 tools 是第一阶段判断出的工具名数组。"
            "不要重新提炼偏好，不要修改 core_requirement、penalty_amount、needs_history。"
            "只为 tools 数组中出现的工具提取配置；未启用的工具不要输出。"
            "本版只支持明确坐标/半径或明确经纬度范围；只有地名且没有边界或坐标时，不要输出地理工具配置。"
            "重要区分：如果偏好是“某时间前到达/回到某地点”，例如每天23点前回家，必须使用 deadline_location_check，"
            "不要把它配置成 geo_checks.must_inside；geo_checks.must_inside 只用于原文明确要求车辆始终/全程/一直保持在某范围内。"
            + detailed_tool_prompt(enabled_tools)
            + "输出格式必须为："
            '{"tool_plan":{"pref_0":{"geo_checks":{...}},"pref_1":{"wait_generation":{...}}}}。'
            "tool_plan 的 key 必须来自输入 preference_brief[*].pref_id。"
        )
