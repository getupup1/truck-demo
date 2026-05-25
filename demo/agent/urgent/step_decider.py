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
                "urgent_relevant_events 最多只包含该紧急任务可见之后最近发生的上一条事件。"
                "请比较这条上一事件和 urgent_task.stages。"
                "如果上一事件的 action 和 params 匹配某个 stage，则输出该 stage 的下一个 stage 动作。"
                "如果没有上一事件，或上一事件不匹配任何 stage，则输出第一个 stage 动作。"
                "如果输出的 stage 中包含 until_wall_time 或 not_before_wall_time，请原样复制到 params，"
                "不要自行换算 duration_minutes；程序会根据当前时间统一换算。"
                "planning_guard 表示当前仍可尝试接普通订单；但如果 candidate_guard 过滤后没有安全订单，就必须执行一个紧急兜底动作。请输出这个兜底动作。兜底动作必须来自 urgent_task.stages，通常是前往目标地点、等待到指定时间、或接指定货源。"
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
            "你是货运司机紧急任务阶段决策器。只允许输出一个 JSON 对象，禁止 markdown、解释和额外文本。"
            "你会收到 urgent_task.stages 和 urgent_relevant_events。"
            "urgent_relevant_events 最多只包含一条事件：该紧急任务可见之后最近发生的上一条事件。"
            "不要编造 stages 之外的动作。"
            "请用上一事件匹配 stage：take_order 要匹配相同 cargo_id；wait 要匹配相同 duration_minutes；"
            "reposition 要匹配相同 latitude/longitude 目标点。"
            "如果上一事件匹配第 N 个 stage，则输出第 N+1 个 stage。"
            "如果上一事件为空，或无法匹配任何 stage，则输出第 1 个 stage。"
            "如果输出 wait stage 且 stage.params 中包含 until_wall_time，请原样输出 until_wall_time，"
            "不要自己计算 duration_minutes。"
            "如果输出 take_order 或 reposition stage 且 stage.params 中包含 not_before_wall_time，"
            "请原样输出 not_before_wall_time；程序会在当前时间早于该时间时自动改成 wait。"
            "如果上一事件已经匹配最后一个 stage，只有当任务要求继续执行该最后动作时才继续输出最后动作；"
            "否则输出最安全的最后 wait/reposition 阶段。"
            "如果 urgent_mode 是 planning_guard，请输出 candidate_guard 删除全部货源后应使用的 fallback stage 动作。"
            "输出格式必须严格为："
            '{"urgent_mode":"force_action","current_stage_order":1,"action":"wait",'
            '"params":{"duration_minutes":10},"reason":"..."}'
        )
