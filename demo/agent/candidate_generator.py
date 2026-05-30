"""MVP 候选动作生成。"""

from __future__ import annotations

from typing import Any

from agent.cargo_simulator import CargoSimulator
from agent.settings import AgentSettings


class CandidateGenerator:
    """生成安全接单候选和无好单时的短等待候选。"""

    def __init__(self, settings: AgentSettings, cargo_simulator: CargoSimulator) -> None:
        self._settings = settings
        self._cargo_simulator = cargo_simulator

    def generate(
        self,
        status_after_scan: dict[str, Any],
        cargo_items: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        for item in cargo_items:
            if not isinstance(item, dict):
                continue
            cargo = item.get("cargo")
            if not isinstance(cargo, dict):
                continue
            cargo_id = str(cargo.get("cargo_id", "")).strip()
            if not cargo_id:
                continue
            simulation = self._cargo_simulator.simulate(status_after_scan, cargo)
            if simulation.get("rejected"):
                continue
            metrics = simulation["metrics"]
            if metrics["net_income"] <= self._settings.minimum_order_net_income:
                continue
            if metrics["net_income_per_hour"] <= self._settings.minimum_order_net_income_per_hour:
                continue
            candidates.append(
                {
                    "candidate_id": f"order:{cargo_id}",
                    "action": "take_order",
                    "params": {"cargo_id": cargo_id},
                    "final_score": metrics["net_income_per_hour"],
                    "reason": "positive_estimated_order_income",
                    "simulation": simulation,
                }
            )

        candidates.append(self.build_wait_candidate(int(status_after_scan["simulation_progress_minutes"])))
        return candidates

    def build_wait_candidate(self, now_minutes: int) -> dict[str, Any]:
        remaining_minutes = self._settings.simulation_horizon_minutes - now_minutes
        duration_minutes = max(1, min(self._settings.default_wait_minutes, remaining_minutes))
        return {
            "candidate_id": f"wait:{duration_minutes}",
            "action": "wait",
            "params": {"duration_minutes": duration_minutes},
            "final_score": 0.0,
            "reason": "no_profitable_safe_order",
        }
