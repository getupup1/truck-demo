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
            "You parse urgent truck-driver preferences into executable staged tasks. Output only JSON. "
            "urgent_mode must be force_action when the driver should immediately perform staged actions "
            "instead of considering normal cargo. urgent_mode must be planning_guard when ordinary cargo may "
            "still be accepted only if it does not prevent the urgent obligation. "
            "candidate_guards is [] for force_action. For planning_guard, use deadline_location_check when "
            "candidate orders must still leave enough time to reach a target point before an absolute deadline. "
            "Stages are ordered fallback or required actions, each action being take_order, wait, or reposition. "
            "For deadline_location_check config, output center=[lat,lng], radius_km, deadline_wall_time as "
            "YYYY-MM-DD HH:MM:SS. Output shape: "
            '{"driver_id":"...","urgent_tasks":[{"task_id":"urgent_0","source_text":"...",'
            '"visible_start_time":"YYYY-MM-DD HH:MM:SS","active_start_time":"YYYY-MM-DD HH:MM:SS",'
            '"urgent_mode":"planning_guard","penalty_amount":0,"penalty_cap":null,'
            '"candidate_guards":[{"tool":"deadline_location_check","config":{"center":[0,0],'
            '"radius_km":0,"deadline_wall_time":"YYYY-MM-DD HH:MM:SS"},"reason":"..."}],'
            '"stages":[{"stage_order":1,"action":"reposition","params":{"latitude":0,"longitude":0},'
            '"reason":"..."}]}]}'
        )
