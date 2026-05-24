"""LLM final action decider."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from agent.history.json_utils import extract_model_json_object


class LLMActionDecider:
    def __init__(self, model_chat_completion: Callable[[dict[str, Any]], dict[str, Any]]) -> None:
        self._model_chat_completion = model_chat_completion
        self._logger = logging.getLogger("agent.actions.decider")

    def decide(
        self,
        *,
        driver_id: str,
        status: dict[str, Any],
        preference_context: dict[str, Any],
        history_summary: dict[str, Any],
        history_slice: dict[str, Any] | None,
        action_options: dict[str, Any],
    ) -> dict[str, Any]:
        payload = {
            "driver_id": driver_id,
            "simulation_progress_minutes": status.get("simulation_progress_minutes"),
            "simulation_wall_time": status.get("simulation_wall_time"),
            "driver_status": {
                "current_lat": status.get("current_lat"),
                "current_lng": status.get("current_lng"),
                "truck_length": status.get("truck_length"),
                "completed_order_count": status.get("completed_order_count"),
            },
            "preference_brief": preference_context.get("preference_brief", []),
            "history_summary": history_summary,
            "history_slice": history_slice,
            "action_options": action_options,
            "decision_instruction": (
                "只能从 action_options.orders、action_options.wait_options 或 action_options.reposition_options 中选择一个动作。"
                "不要输出解释。若订单偏好风险高、收益低或当前更适合等待，选择 wait。"
                "wait 和 reposition 候选中的起止时间、位置、reason、tool_evidence 是程序生成的动作事实。"
                "只有当 reposition_options 中存在选项时，才允许选择 reposition。"
            ),
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
                self._logger.warning("action_decider failed driver_id=%s attempt=%s error=%s", driver_id, attempt + 1, exc)
        raise RuntimeError(f"action decision failed after retry: {last_error}") from last_error

    @staticmethod
    def _system_prompt() -> str:
        return (
            "你是货运调度最终动作选择器。只允许输出一个 JSON 对象，禁止 markdown、解释和额外文本。"
            "输出格式必须为 {\"action\":\"take_order|wait|reposition\",\"params\":{...}}。"
            "take_order 的 cargo_id 必须来自 action_options.orders；wait 的 duration_minutes 必须来自 action_options.wait_options。"
            "reposition 的 latitude/longitude 必须来自 action_options.reposition_options，不能自由生成坐标。"
            "wait/reposition 候选的 action_start_time、action_end_time、position_before、position_after、reason 或 tool_evidence，"
            "必须作为动作起止时间、位置和偏好事实参考。"
            "history_summary 字段说明："
            "today.longest_wait_minutes 表示今日最长连续 wait 时长；"
            "today.current_wait_streak_minutes 表示当前仍在持续 wait 的连续分钟数；"
            "today.active_minutes 表示今日 take_order/reposition 等非 wait 活动时长；"
            "month.longest_wait_hours 表示本月内最长连续 wait 小时数；"
            "month.fully_idle_days 表示本月完全未接单且未 reposition 的完整休息天数；"
            "month.no_order_days 表示本月未成功接单的天数。"

            "你需要结合 preference_brief、history_summary、history_slice、tool_context 和订单评分选择当前动作。"
            "action_options.orders 中的 score 和 net_income 已经综合了距离成本和偏好罚分，"
        )
