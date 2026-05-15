"""模型决策服务：依赖 `simkit.ports.SimulationApiPort`，由评测进程注入具体环境。"""

from __future__ import annotations

import json
import logging
from typing import Any

from agent.decision_features import (
    daily_rest_requirement_minutes,
    empty_preference_classification,
    enhance_cargo_candidates,
    extract_preference_texts,
    normalize_preference_classification,
    preference_signature,
    score_and_rank_candidates,
    summarize_decision_history,
)
from simkit.ports import SimulationApiPort


FALLBACK_WAIT_MINUTES = 10


class ModelDecisionService:
    """基于大模型的单步决策：拉取状态与候选货源，请求补全并解析为结构化动作。"""

    def __init__(
        self,
        api: SimulationApiPort,
        *,
        reposition_speed_km_per_hour: float = 60.0,
        simulation_horizon_minutes: int | None = None,
    ) -> None:
        self._api = api
        self._logger = logging.getLogger("agent.decision_service")
        self._reposition_speed_km_per_hour = float(reposition_speed_km_per_hour)
        self._simulation_horizon_minutes = simulation_horizon_minutes
        self._preference_cache: dict[tuple[str, str], dict[str, Any]] = {}

    def decide(self, driver_id: str) -> dict[str, Any]:
        # # 评测内需可查「本会话已落盘前的动作流水」
        # hist_preview = self._api.query_decision_history(driver_id, 1)
        # last_step = None
        # recs = hist_preview.get("records") or []
        # if recs:
        #     last_step = recs[-1].get("step")
        # self._logger.info(
        #     "[demo] query_decision_history(step=1 上一执行)  total_steps=%s returned=%s last_record_step=%s",
        #     hist_preview.get("total_steps"),
        #     hist_preview.get("returned_count"),
        #     last_step,
        # )

        status = self._api.get_driver_status(driver_id)
        lat = float(status["current_lat"])
        lng = float(status["current_lng"])
        active_preferences = status.get("preferences") or []
        if not isinstance(active_preferences, list):
            active_preferences = []
        parsed_preferences = self._classify_preferences(
            driver_id=driver_id,
            preferences=active_preferences,
            status=status,
        )
        history_resp = self._api.query_decision_history(driver_id, -1)
        current_minutes = int(status.get("simulation_progress_minutes", 0) or 0)
        history_summary = summarize_decision_history(history_resp, current_minutes)
        rest_action = self._daily_rest_action(parsed_preferences, history_summary)
        if rest_action is not None:
            self._logger.info(
                "decision auto_rest driver_id=%s time_min=%s action=%s params=%s",
                driver_id,
                current_minutes,
                rest_action.get("action"),
                rest_action.get("params"),
            )
            return rest_action

        cargo_resp = self._api.query_cargo(driver_id=driver_id, latitude=lat, longitude=lng)
        items = cargo_resp.get("items", [])
        #新增
        status_after_scan = self._api.get_driver_status(driver_id)
        active_preferences = status_after_scan.get("preferences") or []
        if not isinstance(active_preferences, list):
            active_preferences = []
        parsed_preferences = self._classify_preferences(
            driver_id=driver_id,
            preferences=active_preferences,
            status=status_after_scan,
        )
        current_minutes = int(status_after_scan.get("simulation_progress_minutes", 0) or 0)
        history_summary = summarize_decision_history(history_resp, current_minutes)
        rest_action = self._daily_rest_action(parsed_preferences, history_summary)
        if rest_action is not None:
            self._logger.info(
                "decision auto_rest_after_scan driver_id=%s time_min=%s action=%s params=%s",
                driver_id,
                current_minutes,
                rest_action.get("action"),
                rest_action.get("params"),
            )
            return rest_action

        enhanced_candidates = enhance_cargo_candidates(
            items if isinstance(items, list) else [],
            status_after_scan,
            speed_km_per_hour=self._reposition_speed_km_per_hour,
            simulation_horizon_minutes=self._simulation_horizon_minutes,
            limit=30,
        )
        cargo_candidates, filtered_summary = score_and_rank_candidates(
            enhanced_candidates,
            parsed_preferences,
            history_summary,
            top_k=10,
        )
        self._logger.info(
            "decision input driver_id=%s time_min=%s loc=(%.5f,%.5f) cargo_items=%s",
            driver_id,
            status.get("simulation_progress_minutes"),
            lat,
            lng,
            len(items) if isinstance(items, list) else 0,
        )
        if not cargo_candidates:
            action = self._fallback_action(cargo_candidates)
            self._logger.info(
                "decision fallback driver_id=%s reason=no_candidates action=%s params=%s",
                driver_id,
                action.get("action"),
                action.get("params"),
            )
            return action

        prompt = self._build_prompt(
            driver_id=driver_id,
            status=status_after_scan,
            active_preferences=active_preferences,
            parsed_preferences=parsed_preferences,
            history_summary=history_summary,
            filtered_summary=filtered_summary,
            cargo_candidates=cargo_candidates,
        )
        try:
            model_resp = self._api.model_chat_completion(
                {
                    "messages": [
                        {"role": "system", "content": self._decision_system_prompt()},
                        {"role": "user", "content": prompt},
                    ],
                    "response_format": {"type": "json_object"},
                }
            )
            action = self._parse_action(model_resp)
            self._validate_action_against_candidates(action, cargo_candidates)
        except Exception as exc:  # noqa: BLE001 - keep the simulation moving on model issues.
            self._logger.warning("decision model failed driver_id=%s error=%s; using fallback", driver_id, exc)
            action = self._fallback_action(cargo_candidates)
        self._logger.info(
            "decision output driver_id=%s action=%s params=%s",
            driver_id,
            action.get("action"),
            action.get("params"),
        )
        return action

    def _daily_rest_action(
        self,
        parsed_preferences: dict[str, Any],
        history_summary: dict[str, Any],
    ) -> dict[str, Any] | None:
        if parsed_preferences.get("urgent_tasks"):
            return None
        requirement = daily_rest_requirement_minutes(parsed_preferences)
        if requirement is None or requirement <= 0:
            return None
        today = history_summary.get("today", {}) if isinstance(history_summary, dict) else {}
        if not isinstance(today, dict):
            return None
        longest_wait = self._as_int(today.get("longest_wait_minutes"))
        if longest_wait >= requirement:
            return None
        current_streak = self._as_int(today.get("current_wait_streak_minutes"))
        duration = requirement - current_streak if current_streak > 0 else requirement
        if duration <= 0:
            return None
        return {"action": "wait", "params": {"duration_minutes": int(duration)}}

    def _decision_system_prompt(self) -> str:
        return (
            "你是货运调度决策器。只允许输出一个JSON对象，格式必须是"
            '{"action":"take_order|reposition|wait","params":{...}}。'
            "禁止输出markdown、解释或额外文本。"
            "当action是take_order时，params必须包含cargo_id字符串，且cargo_id必须来自user JSON里的cargo_candidates；"
            "当action是reposition时，params必须包含latitude和longitude数值；"
            "当action是wait时，params必须包含duration_minutes正整数。"
            "simulation_progress_minutes为自2026-03-01 00:00:00起的仿真经过分钟数。"
            "user JSON包含active_preferences原始偏好、parsed_preferences结构化偏好、history_summary历史摘要、"
            "filtered_summary过滤统计、cargo_candidates候选货源。"
            "cargo_candidates已经过remove_time、load_time、基础收益和初步偏好罚分筛选排序；候选罚分只是初步估算。"
            "但estimated_preference_penalty、penalty_summary、estimated_net_income、score_per_hour都只是辅助估算，"
            "可能漏掉部分偏好风险，也可能没有覆盖紧急偏好冲突。"
            "你必须结合active_preferences、parsed_preferences、history_summary和urgent_tasks再次判断，不能只依赖分数。"
            "如果parsed_preferences.urgent_tasks非空，应优先执行紧急偏好对应动作；"
            "若接某个订单会导致错过紧急任务，应选择wait或reposition。"
            "如果没有合适候选、候选收益低、偏好风险高、或当前更适合等待，可以输出wait。"
            "候选货源含load_time为装货时间窗[开始,结束]；若无法赶上装货窗口，take_order会失败。"
            "若接单后无法在仿真总时长内完成装货与干线，take_order会失败，且不推进时间与位置。"
        )

    def _validate_action_against_candidates(
        self,
        action: dict[str, Any],
        cargo_candidates: list[dict[str, Any]],
    ) -> None:
        if action.get("action") != "take_order":
            return
        cargo_id = str(action.get("params", {}).get("cargo_id", "")).strip()
        candidate_ids = {str(candidate.get("cargo_id")) for candidate in cargo_candidates}
        if cargo_id not in candidate_ids:
            raise ValueError(f"take_order cargo_id not in cargo_candidates: {cargo_id}")

    def _fallback_action(self, cargo_candidates: list[dict[str, Any]]) -> dict[str, Any]:
        positive_candidates = [
            candidate
            for candidate in cargo_candidates
            if self._as_float(candidate.get("estimated_net_income")) > 0
        ]
        if positive_candidates:
            best = max(
                positive_candidates,
                key=lambda candidate: (
                    self._as_float(candidate.get("score_per_hour")),
                    self._as_float(candidate.get("estimated_net_income")),
                ),
            )
            return {"action": "take_order", "params": {"cargo_id": str(best.get("cargo_id"))}}
        return {"action": "wait", "params": {"duration_minutes": FALLBACK_WAIT_MINUTES}}

    @staticmethod
    def _as_float(value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _as_int(value: Any) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    def _build_prompt(
        self,
        *,
        driver_id: str,
        status: dict[str, Any],
        active_preferences: list[Any],
        parsed_preferences: dict[str, Any],
        history_summary: dict[str, Any],
        filtered_summary: dict[str, Any],
        cargo_candidates: list[dict[str, Any]],
    ) -> str:
        decision_context = {
            "driver_id": driver_id,
            "simulation_progress_minutes": status.get("simulation_progress_minutes"),
            "simulation_wall_time": status.get("simulation_wall_time"),
            "driver_status": {
                "current_lat": status.get("current_lat"),
                "current_lng": status.get("current_lng"),
                "truck_length": status.get("truck_length"),
                "completed_order_count": status.get("completed_order_count"),
            },
            "active_preferences": active_preferences,
            "parsed_preferences": parsed_preferences,
            "history_summary": history_summary,
            "filtered_summary": filtered_summary,
            "cargo_candidates": cargo_candidates,
        }
        return json.dumps(decision_context, ensure_ascii=False)

    def _classify_preferences(
        self,
        *,
        driver_id: str,
        preferences: list[Any],
        status: dict[str, Any],
    ) -> dict[str, Any]:
        signature = preference_signature(preferences)
        cache_key = (driver_id, signature)
        cached = self._preference_cache.get(cache_key)
        if cached is not None:
            return cached

        parsed = self._request_preference_classification(driver_id, preferences, status)
        self._preference_cache[cache_key] = parsed
        return parsed

    def _request_preference_classification(
        self,
        driver_id: str,
        preferences: list[Any],
        status: dict[str, Any],
    ) -> dict[str, Any]:
        payload = {
            "driver_id": driver_id,
            "simulation_progress_minutes": status.get("simulation_progress_minutes"),
            "simulation_wall_time": status.get("simulation_wall_time"),
            "preferences": preferences,
            "preference_texts": extract_preference_texts(preferences),
        }
        try:
            model_resp = self._api.model_chat_completion(
                {
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                            "你是司机偏好解析器。只允许输出一个JSON对象，禁止markdown和解释。"
                            "你的任务是把司机偏好拆解成便于调度决策使用的结构化偏好。"
                            "如果一条偏好包含多个要求，必须拆成多个子偏好；每个子偏好都保留相同的source_text。"

                            "输出字段必须固定包含："
                            "forbidden_cargo_names, distance_limits, fixed_rest_windows, "
                            "daily_rest_duration_constraints, region_limits, "
                            "deadline_location_constraints, history_constraints, urgent_tasks, "
                            "unknown_preferences。"

                            "每个数组中的偏好对象只能包含以下字段："
                            "source_text, constraint_value, start_time, end_time, "
                            "penalty_amount, penalty_cap, preference_action。"
                            "不要新增其他字段。缺失值用null。"

                            "字段含义："
                            "source_text：原始偏好文本，不能改写或丢失。"
                            "constraint_value：用简短中文总结该子偏好的核心约束。"
                            "start_time/end_time：该偏好的生效日期范围，必须从输入复制。"
                            "penalty_amount/penalty_cap：罚分金额和封顶，必须从输入复制。"
                            "preference_action：说明为了满足该偏好，调度动作应该怎么做；"
                            "必须使用动作名take_order、reposition、wait进行描述，但不要只写动作名，"
                            "要写成可执行建议，例如："
                            "take_order时不要选择cargo_name为「蔬菜」的货源；"
                            "每天需要连续wait达到480分钟；"
                            "23:00至次日08:00应wait，不要take_order或reposition；"
                            "23点前需要reposition到指定坐标附近。"

                            "分类规则："
                            "1. forbidden_cargo_names：不接/尽量不拉某货源品类，例如蔬菜、机械设备。"
                            "preference_action应写明take_order时不要选择哪些cargo_name。"

                            "2. distance_limits：单笔装货点至卸货点距离、赴装货点空驶距离、月度空驶总里程等距离上限。"
                            "preference_action应写明take_order或reposition时要控制哪个距离指标。"

                            "3. fixed_rest_windows：有明确每天几点到几点的休息/禁接单/禁空跑窗口，"
                            "例如每天12:00-13:00、23:00-次日06:00。"
                            "preference_action应写明该时间段内应wait，不要take_order或reposition。"

                            "4. daily_rest_duration_constraints：每天必须连续停车休息N小时，但没有固定几点休息，"
                            "例如每天至少连续停车熄火休息满8小时。不要把这类放入fixed_rest_windows。"
                            "preference_action应写明每天需要连续wait达到多少分钟，take_order和reposition会打断连续休息。"

                            "5. region_limits：持续性区域限制，例如始终在深圳市范围内、不得进入某圆形区域。"
                            "preference_action应写明take_order或reposition后车辆位置不得进入/离开哪些区域。"

                            "6. deadline_location_constraints：截止某时间前必须在某坐标/区域内，"
                            "例如每天23点前车辆须在自家位置1公里内。"
                            "preference_action应写明截止时间前需要reposition或通过接单路径到达指定坐标附近。"

                            "7. history_constraints：必须依赖历史动作统计才能判断的月度/每日约束，"
                            "例如自然月内至少4天不接单、自然月内至少2天完全歇着、同日接单不得超过3单、"
                            "月度空驶总里程不得超过某值。"
                            "注意：每天连续休息N小时优先放入daily_rest_duration_constraints，不要重复放入history_constraints。"

                            "8. urgent_tasks：临时约定、指定货源、家事、强deadline、高罚分任务。"
                            "preference_action应写明应优先take_order指定cargo_id，或reposition到指定地点，或wait到指定时间。"
                            "constraint_value应写明地点、时间以及订单（如果有的话），避免注意时间冲突，不要因为上一步的动作导致无法完成urgent_tasks"

                            "9. unknown_preferences：无法归类或无法确定执行动作的偏好。"

                            "复合偏好拆分规则："
                            "如果偏好同时包含位置deadline和夜间不接单，例如："
                            "每天23点前到家；23点至次日8点不接单不空跑，"
                            "必须拆成两个子偏好："
                            "一个放入deadline_location_constraints，"
                            "一个放入fixed_rest_windows。"

                            "不要编造输入中不存在的坐标、时间、罚分或品类。"
                        )
                        },
                        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                    ],
                    "response_format": {"type": "json_object"},
                }
            )
            raw = self._extract_model_json_object(model_resp)
        except Exception as exc:  # noqa: BLE001 - keep the simulation moving on model issues.
            self._logger.warning("preference classification failed driver_id=%s error=%s", driver_id, exc)
            return empty_preference_classification(preferences)
        return normalize_preference_classification(raw, preferences)

    def _extract_model_json_object(self, model_resp: dict[str, Any]) -> dict[str, Any]:
        choices = model_resp.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ValueError("模型返回缺少 choices")
        message = choices[0].get("message", {})
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("模型返回 content 为空")
        data = json.loads(content)
        if not isinstance(data, dict):
            raise ValueError("模型返回不是JSON对象")
        return data

    def _parse_action(self, model_resp: dict[str, Any]) -> dict[str, Any]:
        choices = model_resp.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ValueError("模型返回缺少 choices")
        message = choices[0].get("message", {})
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("模型返回 content 为空")
        action = json.loads(content)
        if not isinstance(action, dict):
            raise ValueError("模型返回动作不是JSON对象")
        action_name = str(action.get("action", "")).strip().lower()
        params = action.get("params")
        if action_name not in {"take_order", "reposition", "wait"}:
            raise ValueError(f"模型返回未知action: {action_name}")
        if not isinstance(params, dict):
            raise ValueError("模型返回 params 必须是对象")
        if action_name == "take_order":
            cargo_id = str(params.get("cargo_id", "")).strip()
            if not cargo_id:
                raise ValueError("take_order 缺少有效 cargo_id")
            return {"action": "take_order", "params": {"cargo_id": cargo_id}}
        if action_name == "reposition":
            latitude = float(params["latitude"])
            longitude = float(params["longitude"])
            return {"action": "reposition", "params": {"latitude": latitude, "longitude": longitude}}
        duration_minutes = int(params["duration_minutes"])
        if duration_minutes <= 0:
            raise ValueError("wait.duration_minutes 必须为正整数")
        return {"action": "wait", "params": {"duration_minutes": duration_minutes}}
