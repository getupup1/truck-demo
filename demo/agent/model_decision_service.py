"""MVP 决策服务：确定性扫描、模拟、评分和输出保护。"""

from __future__ import annotations

import logging
from typing import Any

from simkit.ports import SimulationApiPort

from agent.candidate_generator import CandidateGenerator
from agent.cargo_simulator import CargoSimulator
from agent.safety_guard import SafetyGuard
from agent.scorer import CandidateScorer
from agent.settings import AgentSettings


class ModelDecisionService:
    """评测固定入口；MVP 暂不调用模型，后续在候选排序后接入 LLM 仲裁。"""

    def __init__(self, api: SimulationApiPort) -> None:
        self._api = api
        self._settings = AgentSettings()
        self._cargo_simulator = CargoSimulator(self._settings)
        self._candidate_generator = CandidateGenerator(self._settings, self._cargo_simulator)
        self._scorer = CandidateScorer()
        self._safety_guard = SafetyGuard(self._settings)
        self._logger = logging.getLogger("agent.decision_service")

    def decide(self, driver_id: str) -> dict[str, Any]:
        status = self._api.get_driver_status(driver_id)
        now_minutes = int(status["simulation_progress_minutes"])
        remaining_minutes = self._settings.simulation_horizon_minutes - now_minutes
        if remaining_minutes <= self._settings.end_of_month_wait_threshold_minutes:
            candidate = self._candidate_generator.build_wait_candidate(now_minutes)
            action = self._safety_guard.emit(candidate, [candidate])
            self._logger.info(
                "decision output driver_id=%s action=%s params=%s reason=end_of_month",
                driver_id,
                action["action"],
                action["params"],
            )
            return action

        lat = float(status["current_lat"])
        lng = float(status["current_lng"])
        cargo_response = self._api.query_cargo(
            driver_id=driver_id,
            latitude=lat,
            longitude=lng,
            k=self._settings.cargo_query_k,
        )
        cargo_items = cargo_response.get("items", [])
        if not isinstance(cargo_items, list):
            raise TypeError("query_cargo 返回的 items 必须为列表")
        status_after_scan = self._api.get_driver_status(driver_id)
        scan_cost_minutes = (
            int(status_after_scan["simulation_progress_minutes"])
            - int(status["simulation_progress_minutes"])
        )
        candidates = self._candidate_generator.generate(status_after_scan, cargo_items)
        ranked = self._scorer.rank(candidates)
        selected = ranked[0]
        action = self._safety_guard.emit(selected, candidates)
        self._logger.info(
            "decision output driver_id=%s time_min=%s scan_cost_min=%s cargo_items=%s "
            "safe_candidates=%s selected=%s action=%s params=%s",
            driver_id,
            status_after_scan["simulation_progress_minutes"],
            scan_cost_minutes,
            len(cargo_items),
            len(candidates),
            selected["candidate_id"],
            action["action"],
            action["params"],
        )
        return action
