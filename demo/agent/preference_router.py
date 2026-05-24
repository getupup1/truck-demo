"""Route currently visible preferences into ordinary and urgent groups."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from agent.history.json_utils import extract_model_json_object
from agent.preferences.brief_extractor import extract_preference_texts, preference_signature


class PreferenceRouter:
    """LLM router that keeps urgent preferences out of the ordinary pipeline."""

    def __init__(self, model_chat_completion: Callable[[dict[str, Any]], dict[str, Any]]) -> None:
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

        raw = self._request_route(driver_id, status, preferences)
        routed = self._normalize(driver_id, status, preferences, signature, raw)
        self._cache[cache_key] = routed
        self._logger.info(
            "preference_route ok driver_id=%s ordinary=%s urgent=%s signature=%s",
            driver_id,
            len(routed.get("ordinary_preferences", [])),
            len(routed.get("urgent_preferences", [])),
            signature[:12],
        )
        return routed

    def _request_route(self, driver_id: str, status: dict[str, Any], preferences: list[Any]) -> dict[str, Any]:
        payload = {
            "driver_id": driver_id,
            "simulation_progress_minutes": status.get("simulation_progress_minutes"),
            "simulation_wall_time": status.get("simulation_wall_time"),
            "preferences": [
                {
                    "pref_index": idx,
                    **(_preference_object(item)),
                }
                for idx, item in enumerate(preferences)
            ],
            "preference_texts": extract_preference_texts(preferences),
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
            except Exception as exc:  # noqa: BLE001 - retry once for transient model failures.
                last_error = exc
                self._logger.warning(
                    "preference_route failed driver_id=%s attempt=%s error=%s",
                    driver_id,
                    attempt + 1,
                    exc,
                )
        raise RuntimeError(f"preference routing failed after retry: {last_error}") from last_error

    @staticmethod
    def _system_prompt() -> str:
        return (
            "You are a routing layer for truck-driver preferences. Output only one JSON object. "
            "Split each currently visible preference into ordinary_preferences or urgent_preferences. "
            "Ordinary preferences are stable preferences about orders, rest, geography, income, mileage, "
            "or recurring monthly/daily constraints. Urgent preferences are temporary, high-priority tasks "
            "that describe a specific future obligation, specified cargo, family emergency, hard staged action, "
            "or a short-lived deadline that may require force_action or candidate guarding. "
            "Do not rewrite preference contents. Preserve pref_index and original fields. "
            "Output exactly: "
            '{"driver_id":"...","ordinary_preferences":[{"pref_index":0,"content":"...",'
            '"start_time":null,"end_time":null,"penalty_amount":0,"penalty_cap":null,'
            '"route_reason":"..."}],"urgent_preferences":[...]}'
        )

    def _normalize(
        self,
        driver_id: str,
        status: dict[str, Any],
        preferences: list[Any],
        signature: str,
        raw: Any,
    ) -> dict[str, Any]:
        base = [_routed_preference(item, idx) for idx, item in enumerate(preferences)]
        urgent_indexes = _indexes_from_items(raw.get("urgent_preferences") if isinstance(raw, dict) else None, base)
        ordinary_indexes = _indexes_from_items(raw.get("ordinary_preferences") if isinstance(raw, dict) else None, base)
        urgent_indexes = [idx for idx in urgent_indexes if 0 <= idx < len(base)]
        ordinary_indexes = [idx for idx in ordinary_indexes if 0 <= idx < len(base) and idx not in urgent_indexes]
        for idx in range(len(base)):
            if idx not in urgent_indexes and idx not in ordinary_indexes:
                ordinary_indexes.append(idx)

        return {
            "driver_id": driver_id,
            "preference_signature": signature,
            "routed_at_progress_minutes": status.get("simulation_progress_minutes"),
            "routed_at_wall_time": status.get("simulation_wall_time"),
            "ordinary_preferences": [base[idx] for idx in ordinary_indexes],
            "urgent_preferences": [base[idx] for idx in urgent_indexes],
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


def _indexes_from_items(value: Any, base: list[dict[str, Any]]) -> list[int]:
    if not isinstance(value, list):
        return []
    indexes: list[int] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        idx = _coerce_index(item.get("pref_index"))
        if idx is None:
            idx = _match_index_by_content(str(item.get("content") or ""), base)
        if idx is not None and idx not in indexes:
            if 0 <= idx < len(base) and item.get("route_reason"):
                base[idx]["route_reason"] = str(item.get("route_reason") or "")
            indexes.append(idx)
    return indexes


def _coerce_index(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _match_index_by_content(content: str, base: list[dict[str, Any]]) -> int | None:
    text = "".join(content.split())
    if not text:
        return None
    for item in base:
        candidate = "".join(str(item.get("content") or "").split())
        if text == candidate or text in candidate or candidate in text:
            return int(item.get("pref_index", -1))
    return None
