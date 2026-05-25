"""Model decision service with two-stage Preference DSL compilation."""

from __future__ import annotations

import json
import logging
import math
from typing import Any

from agent.preferences import PreferenceCompiler, PreferenceIR
from simkit.ports import SimulationApiPort


FALLBACK_WAIT_MINUTES = 10
MAX_PROMPT_CARGO_CANDIDATES = 20


class ModelDecisionService:
    """Single-step freight decision service.

    This stage only compiles preferences into DSL and passes them into the
    decision prompt. Event evaluation, scoring, and final selection will be
    rebuilt in a later phase.
    """

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
        self._compiler = PreferenceCompiler(self._api.model_chat_completion)
        self._preference_cache: dict[tuple[str, str], list[PreferenceIR]] = {}

    def decide(self, driver_id: str) -> dict[str, Any]:
        status = self._api.get_driver_status(driver_id)
        active_preferences = self._active_preferences(status)
        preference_irs = self._compile_preferences(driver_id, active_preferences, status)

        lat = float(status["current_lat"])
        lng = float(status["current_lng"])
        cargo_resp = self._api.query_cargo(driver_id=driver_id, latitude=lat, longitude=lng)
        cargo_items = cargo_resp.get("items", [])
        if not isinstance(cargo_items, list):
            cargo_items = []

        status_after_scan = self._api.get_driver_status(driver_id)
        active_preferences = self._active_preferences(status_after_scan)
        preference_irs = self._compile_preferences(driver_id, active_preferences, status_after_scan)
        cargo_candidates = self._compact_cargo_candidates(cargo_items)

        self._logger.info(
            "decision input driver_id=%s time_min=%s cargo_items=%s preference_ir=%s",
            driver_id,
            status_after_scan.get("simulation_progress_minutes"),
            len(cargo_items),
            len(preference_irs),
        )

        if not cargo_candidates:
            return {"action": "wait", "params": {"duration_minutes": FALLBACK_WAIT_MINUTES}}

        prompt = self._build_prompt(
            driver_id=driver_id,
            status=status_after_scan,
            active_preferences=active_preferences,
            preference_irs=preference_irs,
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
        except Exception as exc:  # noqa: BLE001 - keep the simulation moving on action selection issues.
            self._logger.warning("decision model failed driver_id=%s error=%s; using fallback", driver_id, exc)
            action = self._fallback_action(cargo_candidates)

        self._logger.info(
            "decision output driver_id=%s action=%s params=%s",
            driver_id,
            action.get("action"),
            action.get("params"),
        )
        return action

    def _compile_preferences(
        self,
        driver_id: str,
        preferences: list[Any],
        status: dict[str, Any],
    ) -> list[PreferenceIR]:
        signature = self._compiler.signature(preferences)
        cache_key = (driver_id, signature)
        cached = self._preference_cache.get(cache_key)
        if cached is not None:
            return cached
        compiled = self._compiler.compile(driver_id=driver_id, preferences=preferences, status=status)
        self._preference_cache[cache_key] = compiled
        return compiled

    @staticmethod
    def _active_preferences(status: dict[str, Any]) -> list[Any]:
        active_preferences = status.get("preferences") or []
        return active_preferences if isinstance(active_preferences, list) else []

    def _decision_system_prompt(self) -> str:
        return (
            "You are a freight dispatch selector. Output exactly one JSON object and no markdown. "
            'The schema is {"action":"take_order|reposition|wait","params":{...}}. '
            "For take_order, params.cargo_id must be one of the provided cargo_candidates. "
            "For reposition, params.latitude and params.longitude must be numeric. "
            "For wait, params.duration_minutes must be a positive integer. "
            "PreferenceIR items are compiled DSL context only; scoring and filtering are not rebuilt in this phase. "
            "Use active_preferences and preference_ir to avoid obvious preference violations."
        )

    def _build_prompt(
        self,
        *,
        driver_id: str,
        status: dict[str, Any],
        active_preferences: list[Any],
        preference_irs: list[PreferenceIR],
        cargo_candidates: list[dict[str, Any]],
    ) -> str:
        context = {
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
            "preference_ir": preference_irs,
            "cargo_candidates": cargo_candidates,
        }
        return json.dumps(context, ensure_ascii=False)

    @staticmethod
    def _compact_cargo_candidates(cargo_items: list[Any]) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        for item in cargo_items[:MAX_PROMPT_CARGO_CANDIDATES]:
            if not isinstance(item, dict):
                continue
            cargo = item.get("cargo")
            if not isinstance(cargo, dict):
                continue
            cargo_id = str(cargo.get("cargo_id") or "").strip()
            if not cargo_id:
                continue
            candidates.append(
                {
                    "cargo_id": cargo_id,
                    "cargo_name": cargo.get("cargo_name"),
                    "price": cargo.get("price"),
                    "distance_km": item.get("distance_km"),
                    "start": cargo.get("start"),
                    "end": cargo.get("end"),
                    "load_time": cargo.get("load_time"),
                    "remove_time": cargo.get("remove_time"),
                    "cost_time_minutes": cargo.get("cost_time_minutes"),
                }
            )
        return candidates

    def _validate_action_against_candidates(
        self,
        action: dict[str, Any],
        cargo_candidates: list[dict[str, Any]],
    ) -> None:
        action_name = action.get("action")
        if action_name == "take_order":
            cargo_id = str(action.get("params", {}).get("cargo_id", "")).strip()
            candidate_ids = {str(item.get("cargo_id")) for item in cargo_candidates}
            if cargo_id not in candidate_ids:
                raise ValueError(f"take_order cargo_id not in cargo_candidates: {cargo_id}")
        elif action_name == "wait":
            duration = int(action.get("params", {}).get("duration_minutes", 0))
            if duration <= 0:
                raise ValueError("wait.duration_minutes must be positive")
        elif action_name == "reposition":
            latitude = float(action.get("params", {}).get("latitude"))
            longitude = float(action.get("params", {}).get("longitude"))
            if not (math.isfinite(latitude) and math.isfinite(longitude)):
                raise ValueError("reposition coordinates must be finite")
        else:
            raise ValueError(f"unsupported action: {action_name}")

    @staticmethod
    def _fallback_action(cargo_candidates: list[dict[str, Any]]) -> dict[str, Any]:
        if cargo_candidates:
            return {"action": "take_order", "params": {"cargo_id": str(cargo_candidates[0].get("cargo_id"))}}
        return {"action": "wait", "params": {"duration_minutes": FALLBACK_WAIT_MINUTES}}

    def _parse_action(self, model_resp: dict[str, Any]) -> dict[str, Any]:
        choices = model_resp.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ValueError("model response missing choices")
        message = choices[0].get("message", {})
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("model response content is empty")
        action = json.loads(content)
        if not isinstance(action, dict):
            raise ValueError("model action is not a JSON object")
        action_name = str(action.get("action", "")).strip().lower()
        params = action.get("params")
        if action_name not in {"take_order", "reposition", "wait"}:
            raise ValueError(f"unknown action: {action_name}")
        if not isinstance(params, dict):
            raise ValueError("action.params must be an object")
        if action_name == "take_order":
            cargo_id = str(params.get("cargo_id", "")).strip()
            if not cargo_id:
                raise ValueError("take_order missing cargo_id")
            return {"action": "take_order", "params": {"cargo_id": cargo_id}}
        if action_name == "reposition":
            return {
                "action": "reposition",
                "params": {"latitude": float(params["latitude"]), "longitude": float(params["longitude"])},
            }
        duration_minutes = int(params["duration_minutes"])
        if duration_minutes <= 0:
            raise ValueError("wait.duration_minutes must be positive")
        return {"action": "wait", "params": {"duration_minutes": duration_minutes}}
