"""输出动作前的最终保护。"""

from __future__ import annotations

import math
from typing import Any

from agent.settings import AgentSettings


class SafetyGuard:
    """只允许输出结构合法且来自当前安全候选集的动作。"""

    def __init__(self, settings: AgentSettings) -> None:
        self._settings = settings

    def emit(self, candidate: dict[str, Any], candidates: list[dict[str, Any]]) -> dict[str, Any]:
        if not self._is_safe(candidate, candidates):
            candidate = self._fallback_wait_candidate()
        return {"action": candidate["action"], "params": dict(candidate["params"])}

    def _is_safe(self, candidate: dict[str, Any], candidates: list[dict[str, Any]]) -> bool:
        action = candidate.get("action")
        params = candidate.get("params")
        if not isinstance(params, dict):
            return False
        if action == "take_order":
            cargo_id = str(params.get("cargo_id", "")).strip()
            return bool(cargo_id) and any(
                item.get("action") == "take_order"
                and not item.get("simulation", {}).get("rejected")
                and str(item.get("params", {}).get("cargo_id", "")).strip() == cargo_id
                for item in candidates
            )
        if action == "wait":
            try:
                return int(params["duration_minutes"]) > 0
            except (KeyError, TypeError, ValueError):
                return False
        if action == "reposition":
            try:
                lat = float(params["latitude"])
                lng = float(params["longitude"])
            except (KeyError, TypeError, ValueError):
                return False
            return math.isfinite(lat) and math.isfinite(lng) and -90 <= lat <= 90 and -180 <= lng <= 180
        return False

    def _fallback_wait_candidate(self) -> dict[str, Any]:
        return {
            "candidate_id": "wait:fallback",
            "action": "wait",
            "params": {"duration_minutes": self._settings.default_wait_minutes},
            "final_score": 0.0,
            "reason": "safety_guard_fallback",
        }
