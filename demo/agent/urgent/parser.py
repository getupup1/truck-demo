"""LLM parser for urgent preferences."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from agent.history.json_utils import extract_model_json_object
from agent.preferences.brief_extractor import preference_signature
from agent.urgent.schema import normalize_urgent_task_plan


class UrgentTaskParser:
    def __init__(self, model_chat_completion: Callable[[dict[str, Any]], dict[str, Any]]) -> None:
        self._model_chat_completion = model_chat_completion
        self._cache: dict[tuple[str, str], dict[str, Any]] = {}
        self._logger = logging.getLogger("agent.urgent.parser")

    def parse_for_preferences(
        self,
        *,
        driver_id: str,
        status: dict[str, Any],
        urgent_preferences: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if not urgent_preferences:
            return {"driver_id": driver_id, "urgent_tasks": []}
        signature = preference_signature(urgent_preferences)
        cache_key = (driver_id, signature)
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        raw = self._request_parse(driver_id=driver_id, status=status, urgent_preferences=urgent_preferences)
        parsed = normalize_urgent_task_plan(raw, driver_id=driver_id)
        self._cache[cache_key] = parsed
        self._logger.info("urgent_parse ok driver_id=%s tasks=%s", driver_id, len(parsed.get("urgent_tasks", [])))
        return parsed

    def _request_parse(
        self,
        *,
        driver_id: str,
        status: dict[str, Any],
        urgent_preferences: list[dict[str, Any]],
    ) -> dict[str, Any]:
        payload = {
            "driver_id": driver_id,
            "simulation_progress_minutes": status.get("simulation_progress_minutes"),
            "simulation_wall_time": status.get("simulation_wall_time"),
            "urgent_preferences": urgent_preferences,
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
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                self._logger.warning("urgent_parse failed driver_id=%s attempt=%s error=%s", driver_id, attempt + 1, exc)
        raise RuntimeError(f"urgent task parsing failed after retry: {last_error}") from last_error

    @staticmethod
    def _system_prompt() -> str:
        return (
            "你是货运司机紧急偏好解析器。只允许输出一个 JSON 对象，禁止 markdown、解释和额外文本。"
            "你的任务是把紧急偏好解析为可执行的阶段任务 urgent_tasks。"
            "当司机应该立即执行阶段动作、暂时不考虑普通货源时，urgent_mode 必须为 force_action。"
            "当司机仍可接普通订单，但订单不能影响紧急任务完成时，urgent_mode 必须为 planning_guard。"
            "force_action 的 candidate_guards 必须为 []。"
            "planning_guard 中，如果候选订单完成后仍必须在绝对截止时间前赶到某个目标点，"
            "则使用 candidate_guards 中的 deadline_location_check。"
            "stages 表示按顺序执行的兜底动作或必需动作，每个 action 只能是 take_order、wait 或 reposition。"
            "如果某个 wait 阶段要求等待到固定时间点，不要计算 duration_minutes，"
            "请在 params 中输出 until_wall_time，格式为 YYYY-MM-DD HH:MM:SS。"
            "如果某个 take_order 或 reposition 阶段必须等到某个时间点之后才能执行，"
            "请在 params 中输出 not_before_wall_time，格式为 YYYY-MM-DD HH:MM:SS。"
            "duration_minutes 只用于原文明确写了等待若干分钟的相对时长，例如等待10分钟。"
            "例如熟货源有上架时间时，take_order 阶段应在 params 中保留 not_before_wall_time 为该上架时间；"
            "如果司机提前到达熟货地点，后续程序会自动生成 wait 到上架时间。"
            "deadline_location_check.config 必须输出 center=[lat,lng]、radius_km、"
            "deadline_wall_time，且 deadline_wall_time 格式为 YYYY-MM-DD HH:MM:SS。"
            "输出格式必须严格为："
            '{"driver_id":"...","urgent_tasks":[{"task_id":"urgent_0","source_text":"...",'
            '"visible_start_time":"YYYY-MM-DD HH:MM:SS","active_start_time":"YYYY-MM-DD HH:MM:SS",'
            '"urgent_mode":"planning_guard","penalty_amount":0,"penalty_cap":null,'
            '"candidate_guards":[{"tool":"deadline_location_check","config":{"center":[0,0],'
            '"radius_km":0,"deadline_wall_time":"YYYY-MM-DD HH:MM:SS"},"reason":"..."}],'
            '"stages":[{"stage_order":1,"action":"reposition","params":{"latitude":0,"longitude":0},'
            '"reason":"..."}]}]}'
        )
