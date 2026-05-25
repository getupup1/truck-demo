"""Route currently visible preferences into ordinary and urgent groups."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from agent.preferences.brief_extractor import preference_signature


URGENT_MARKER = "临时约定"


class PreferenceRouter:
    """Rule-first router that keeps urgent preferences out of the ordinary pipeline."""

    def __init__(self, model_chat_completion: Callable[[dict[str, Any]], dict[str, Any]] | None = None) -> None:
        self._model_chat_completion = model_chat_completion
        self._cache: dict[tuple[str, str], dict[str, Any]] = {}
        self._logger = logging.getLogger("agent.preference_router")

    def route_for_status(self, driver_id: str, status: dict[str, Any]) -> dict[str, Any]:
        preferences = status.get("preferences") or []
        if not isinstance(preferences, list):
            preferences = []
        signature = preference_signature(preferences)
        cache_key = (driver_id, signature)
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        if not preferences:
            routed = self._empty(driver_id, status, signature)
            self._cache[cache_key] = routed
            return routed

        routed = self._rule_route(driver_id, status, preferences, signature)
        self._cache[cache_key] = routed
        self._logger.info(
            "preference_route ok driver_id=%s ordinary=%s urgent=%s signature=%s",
            driver_id,
            len(routed.get("ordinary_preferences", [])),
            len(routed.get("urgent_preferences", [])),
            signature[:12],
        )
        return routed

    def _rule_route(
        self,
        driver_id: str,
        status: dict[str, Any],
        preferences: list[Any],
        signature: str,
    ) -> dict[str, Any]:
        ordinary_preferences: list[dict[str, Any]] = []
        urgent_preferences: list[dict[str, Any]] = []
        for idx, item in enumerate(preferences):
            routed = _routed_preference(item, idx)
            if _is_urgent_preference(routed):
                routed["route_reason"] = f"contains marker: {URGENT_MARKER}"
                urgent_preferences.append(routed)
            else:
                routed["route_reason"] = "ordinary preference"
                ordinary_preferences.append(routed)
        return {
            "driver_id": driver_id,
            "preference_signature": signature,
            "routed_at_progress_minutes": status.get("simulation_progress_minutes"),
            "routed_at_wall_time": status.get("simulation_wall_time"),
            "ordinary_preferences": ordinary_preferences,
            "urgent_preferences": urgent_preferences,
        }

    @staticmethod
    def _empty(driver_id: str, status: dict[str, Any], signature: str) -> dict[str, Any]:
        return {
            "driver_id": driver_id,
            "preference_signature": signature,
            "routed_at_progress_minutes": status.get("simulation_progress_minutes"),
            "routed_at_wall_time": status.get("simulation_wall_time"),
            "ordinary_preferences": [],
            "urgent_preferences": [],
        }


def _preference_object(item: Any) -> dict[str, Any]:
    if isinstance(item, dict):
        return {
            "content": item.get("content") or item.get("text") or "",
            "start_time": item.get("start_time"),
            "end_time": item.get("end_time"),
            "penalty_amount": item.get("penalty_amount"),
            "penalty_cap": item.get("penalty_cap"),
        }
    return {
        "content": str(item),
        "start_time": None,
        "end_time": None,
        "penalty_amount": 0,
        "penalty_cap": None,
    }


def _routed_preference(item: Any, index: int) -> dict[str, Any]:
    obj = _preference_object(item)
    return {
        "pref_index": index,
        "content": str(obj.get("content") or "").strip(),
        "start_time": obj.get("start_time"),
        "end_time": obj.get("end_time"),
        "penalty_amount": obj.get("penalty_amount"),
        "penalty_cap": obj.get("penalty_cap"),
        "route_reason": "",
    }


def _is_urgent_preference(item: dict[str, Any]) -> bool:
    return URGENT_MARKER in str(item.get("content") or "")
