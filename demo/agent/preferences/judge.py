"""LLM-backed preference judge for simulated take-order events."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from agent.history.json_utils import extract_model_json_object, json_dumps_compact
from agent.history.time_utils import coerce_float


DEFAULT_BATCH_SIZE = 5


class LLMPreferenceJudge:
    """Judge whether simulated order-taking events violate preference briefs."""

    def __init__(
        self,
        model_chat_completion: Callable[[dict[str, Any]], dict[str, Any]],
        *,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> None:
        self._model_chat_completion = model_chat_completion
        self._batch_size = max(1, int(batch_size))
        self._logger = logging.getLogger("agent.preferences.judge")

    def judge_candidates(
        self,
        *,
        driver_id: str,
        status: dict[str, Any],
        preference_context: dict[str, Any],
        history_summary: dict[str, Any],
        history_slice: dict[str, Any] | None,
        candidates: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        judged: list[dict[str, Any]] = []
        for batch_index, batch in enumerate(self._batches(candidates), start=1):
            batch_evaluations = self._request_batch_judgement(
                driver_id=driver_id,
                status=status,
                preference_brief=self._preference_brief(preference_context),
                history_summary=history_summary,
                history_slice=history_slice,
                batch=batch,
            )
            by_id = {str(item.get("cargo_id")): item for item in batch_evaluations}
            for candidate in batch:
                item = dict(candidate)
                item["preference_evaluation"] = self._normalize_evaluation(
                    by_id.get(str(candidate.get("cargo_id"))),
                    candidate,
                )
                judged.append(item)
            self._logger.info(
                "preference_judge batch_ok driver_id=%s batch=%s size=%s result=%s",
                driver_id,
                batch_index,
                len(batch),
                json_dumps_compact([item.get("preference_evaluation") for item in judged[-len(batch) :]]),
            )
        return judged

    def _request_batch_judgement(
        self,
        *,
        driver_id: str,
        status: dict[str, Any],
        preference_brief: list[dict[str, Any]],
        history_summary: dict[str, Any],
        history_slice: dict[str, Any] | None,
        batch: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        payload = {
            "driver_id": driver_id,
            "simulation_progress_minutes": status.get("simulation_progress_minutes"),
            "simulation_wall_time": status.get("simulation_wall_time"),
            "preference_brief": preference_brief,
            "history_summary": history_summary,
            "history_slice": history_slice,
            "simulated_events": [
                {
                    "cargo_id": candidate.get("cargo_id"),
                    "cargo_name": candidate.get("cargo_name"),
                    "simulated_event": self._simulated_event_for_payload(candidate.get("simulated_event")),
                    "tool_evidence": candidate.get("tool_evidence", {}),
                }
                for candidate in batch
            ],
            "judge_instruction": (
                "请逐个判断 simulated_events 中的 take_order 是否违反 preference_brief。"
                "优先使用 history_summary 判断常见历史指标；summary 不足时再查看 history_slice.events。"
                "若候选包含 tool_evidence，请把其中的地理、累计、固定时间窗口和期限地点事实作为辅助证据。"
                "只判断偏好违反，不选择动作，不排序，不计算收益。"
                "penalty_amount 和 penalty_cap 必须来自 preference_brief，不得编造。"
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
                raw = extract_model_json_object(response)
                evaluations = raw.get("evaluations")
                if not isinstance(evaluations, list):
                    raise ValueError("模型偏好评估结果缺少 evaluations 数组")
                return [item for item in evaluations if isinstance(item, dict)]
            except Exception as exc:  # noqa: BLE001 - retry once for transient model issues.
                last_error = exc
                self._logger.warning("preference_judge request failed attempt=%s error=%s", attempt + 1, exc)
        raise RuntimeError(f"preference judgement failed after retry: {last_error}") from last_error

    @staticmethod
    def _system_prompt() -> str:
        return (
            "你是货运司机偏好合规判断器。只允许输出一个 JSON 对象，禁止 markdown、解释和额外文本。"
            "你会收到 preference_brief、history_summary、可能为空的 history_slice，"
            "以及一批 simulated_events。"
            "不要把偏好重新分类；只根据 preference_brief 中的 source_text、core_requirement、penalty 信息，"
            "以及 simulated_events / tool_evidence 判断每个模拟接单是否违反偏好。"

            "history_summary 字段说明："
            "today.longest_wait_minutes 表示今日最长连续 wait 时长；"
            "today.current_wait_streak_minutes 表示当前仍在持续 wait 的连续分钟数；"
            "today.active_minutes 表示今日 take_order/reposition 等非 wait 活动时长；"
            "month.longest_wait_hours 表示本月内最长连续 wait 小时数；"
            "month.fully_idle_days 表示本月完全未接单且未 reposition 的完整休息天数；"
            "month.no_order_days 表示本月未成功接单的天数。"
            "history_slice 字段说明："
            "history_slice 是可选的详细历史片段，仅在 history_summary 不足以理解当前偏好状态时参考；"
            "如果 history_slice 为 null，则忽略该字段。"

            "simulated_event.metrics 字段说明："
            "pickup_deadhead_km 表示当前位置到装货点的空驶距离；"
            "pickup_minutes 表示预计到装货点耗时；"
            "waiting_minutes 表示到达装货点后等待装货窗口开始的时间；"
            "haul_distance_km 表示装货点到卸货点距离；"
            "estimated_total_minutes 表示从当前时刻接该单到完成订单的总预计耗时。"

            "能用 history_summary 判断的常见历史指标优先使用 summary；summary 不足以判断时，再查看 history_slice.events。"
            "若 simulated_events 中包含 tool_evidence，里面是程序计算好的地理、累计、固定时间窗口或期限地点事实，"
            "应优先信任这些工具事实，不要重新估算距离、时间窗或到访次数。"
            "输出格式必须为："
            '{"evaluations":[{"cargo_id":"...","violates_preferences":false,"violation_count":0,'
            '"violated_preferences":[{"pref_id":"pref_0","source_text":"...","reason":"...",'
            '"penalty_amount":0,"penalty_cap":null}],"preference_penalty":0,"reason":"..."}]}。'
            "preference_penalty 必须非负，且只能依据 preference_brief 中的 penalty_amount/penalty_cap 给出。"
        )

    def _batches(self, candidates: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
        return [candidates[i : i + self._batch_size] for i in range(0, len(candidates), self._batch_size)]

    @staticmethod
    def _preference_brief(preference_context: dict[str, Any]) -> list[dict[str, Any]]:
        brief = preference_context.get("preference_brief")
        return brief if isinstance(brief, list) else []

    @staticmethod
    def _simulated_event_for_payload(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        event = dict(value)
        event.pop("failure_flags", None)
        return event

    @staticmethod
    def _normalize_evaluation(raw: dict[str, Any] | None, candidate: dict[str, Any]) -> dict[str, Any]:
        if raw is None:
            raise ValueError(f"模型未返回候选 {candidate.get('cargo_id')} 的偏好评估结果")
        violated_preferences = raw.get("violated_preferences")
        if not isinstance(violated_preferences, list):
            violated_preferences = []
        violation_count = raw.get("violation_count")
        try:
            normalized_count = int(violation_count)
        except (TypeError, ValueError):
            normalized_count = len(violated_preferences)
        penalty = max(0.0, coerce_float(raw.get("preference_penalty"), 0.0))
        violates = raw.get("violates_preferences")
        if not isinstance(violates, bool):
            violates = normalized_count > 0 or penalty > 0
        return {
            "cargo_id": str(raw.get("cargo_id") or candidate.get("cargo_id") or ""),
            "violates_preferences": violates,
            "violation_count": max(0, normalized_count),
            "violated_preferences": [item for item in violated_preferences if isinstance(item, dict)],
            "preference_penalty": round(penalty, 2),
            "reason": str(raw.get("reason") or "").strip(),
        }
