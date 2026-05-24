"""LLM decider for the current stage of an urgent task."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from agent.history.json_utils import extract_model_json_object
from agent.urgent.schema import build_urgent_relevant_events, normalize_step_decision


class UrgentStepDecider:
    def __init__(self, model_chat_completion: Callable[[dict[str, Any]], dict[str, Any]]) -> None:
        self._model_chat_completion = model_chat_completion
        self._logger = logging.getLogger("agent.urgent.step_decider")

    def decide(
        self,
        *,
        driver_id: str,
        status: dict[str, Any],
        urgent_task: dict[str, Any],
        event_log: list[dict[str, Any]],
    ) -> dict[str, Any]:
        relevant_events = build_urgent_relevant_events(event_log, urgent_task)
        payload = {
            "driver_id": driver_id,
            "simulation_progress_minutes": status.get("simulation_progress_minutes"),
            "simulation_wall_time": status.get("simulation_wall_time"),
            "driver_status": {
                "current_lat": status.get("current_lat"),
                "current_lng": status.get("current_lng"),
            },
            "urgent_task": urgent_task,
            "urgent_relevant_events": relevant_events,
            "decision_instruction": (
                "urgent_relevant_events contains at most the last event after this urgent task became visible. "
                "Compare that last event with urgent_task.stages. If it matches one stage action and params, "
                "output the next stage action. If there is no event, or the last event does not match any stage, "
                "output the first stage action. "
                "For planning_guard, output the fallback action to use when no safe cargo remains."
            ),
        }
        raw = self._request_decide(payload)
        decision = normalize_step_decision(raw, urgent_task)
        self._logger.info(
            "urgent_step_decide ok driver_id=%s task_id=%s mode=%s action=%s",
            driver_id,
            urgent_task.get("task_id"),
            decision.get("urgent_mode"),
            decision.get("action"),
        )
        return decision

    def _request_decide(self, payload: dict[str, Any]) -> dict[str, Any]:
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
                self._logger.warning("urgent_step_decide failed attempt=%s error=%s", attempt + 1, exc)
        raise RuntimeError(f"urgent step decision failed after retry: {last_error}") from last_error

    @staticmethod
    def _system_prompt() -> str:
        return (
            "You decide the current action for one urgent truck-driver task. Output only JSON. "
            "Use urgent_task.stages and urgent_relevant_events. urgent_relevant_events contains at most one item: "
            "the previous event after the urgent task became visible. Do not invent actions outside the stages. "
            "Match the previous event to a stage by action and params: same cargo_id for take_order, same duration "
            "for wait, and same latitude/longitude target for reposition. If the previous event matches stage N, "
            "output stage N+1. If previous event is empty or does not match any stage, output stage 1. "
            "If the previous event matches the final stage, keep outputting the final stage only when the task "
            "requires continuing that final action; otherwise output the safest final wait/reposition stage. "
            "If the task urgent_mode is planning_guard, output the fallback stage action that should be used "
            "if candidate_guard removes every cargo. Output shape: "
            '{"urgent_mode":"force_action","current_stage_order":1,"action":"wait",'
            '"params":{"duration_minutes":10},"reason":"..."}'
        )
