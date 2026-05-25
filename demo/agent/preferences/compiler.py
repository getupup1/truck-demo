"""Two-stage LLM compiler for ordinary preference DSL."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from agent.preferences.prompts import second_stage_system_prompt, stage1_system_prompt
from agent.preferences.schema import (
    DSL_TYPES,
    ENFORCEMENT_FIELDS,
    IR_TYPES,
    PREFERENCE_IR_FIELDS,
    STAGE1_FIELDS,
    PreferenceIR,
    Stage2PreferenceItem,
)


_SIMULATION_EPOCH = datetime(2026, 3, 1, 0, 0, 0)
_WALL_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


class PreferenceCompiler:
    """Compile visible preferences with stage-1 classification and stage-2 type prompts."""

    def __init__(self, model_chat_completion: Any) -> None:
        if model_chat_completion is None:
            raise ValueError("PreferenceCompiler requires model_chat_completion")
        self._model_chat_completion = model_chat_completion

    def signature(self, preferences: list[Any]) -> str:
        try:
            payload = json.dumps(preferences, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        except TypeError:
            payload = repr(preferences)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def compile(
        self,
        *,
        driver_id: str,
        preferences: list[Any],
        status: dict[str, Any],
    ) -> list[PreferenceIR]:
        if not preferences:
            return []
        stage1_items = self._compile_stage1(driver_id=driver_id, preferences=preferences, status=status)
        normalized_items = [_normalize_stage1_item(item) for item in stage1_items]
        item_order = {str(item["item_id"]): index for index, item in enumerate(normalized_items)}
        out: list[PreferenceIR] = []
        for dsl_type in DSL_TYPES:
            group = [item for item in normalized_items if item["dsl_type"] == dsl_type]
            if not group:
                continue
            out.extend(self._compile_stage2(dsl_type=dsl_type, items=group))
        verified = [_assert_preference_ir_shape(item) for item in out]
        verified.sort(key=lambda item: item_order.get(str(item["pref_id"]), len(item_order)))
        return verified

    def _compile_stage1(
        self,
        *,
        driver_id: str,
        preferences: list[Any],
        status: dict[str, Any],
    ) -> list[dict[str, Any]]:
        payload = {
            "driver_id": driver_id,
            "simulation_progress_minutes": status.get("simulation_progress_minutes"),
            "simulation_wall_time": status.get("simulation_wall_time"),
            "preferences": preferences,
        }
        data = self._chat_json(stage1_system_prompt(), payload)
        items = data.get("items")
        if not isinstance(items, list):
            raise ValueError("stage1 compiler output must contain an items list")
        return [_assert_stage1_item_shape(item) for item in items]

    def _compile_stage2(
        self,
        *,
        dsl_type: str,
        items: list[Stage2PreferenceItem],
    ) -> list[PreferenceIR]:
        payload = {"dsl_type": dsl_type, "items": items}
        data = self._chat_json(second_stage_system_prompt(dsl_type), payload)
        preferences = data.get("preferences")
        if not isinstance(preferences, list):
            raise ValueError(f"stage2 compiler output for {dsl_type} must contain a preferences list")
        return [_assert_preference_ir_shape(item) for item in preferences]

    def _chat_json(self, system_prompt: str, payload: dict[str, Any]) -> dict[str, Any]:
        model_resp = self._model_chat_completion(
            {
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
                "response_format": {"type": "json_object"},
            }
        )
        choices = model_resp.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ValueError("model response missing choices")
        message = choices[0].get("message", {})
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("model response content is empty")
        data = json.loads(content)
        if not isinstance(data, dict):
            raise ValueError("model response content must be a JSON object")
        return data


def _assert_stage1_item_shape(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise ValueError("stage1 item must be an object")
    missing = [field for field in STAGE1_FIELDS if field not in item]
    extra = [field for field in item if field not in STAGE1_FIELDS]
    if missing or extra:
        raise ValueError(f"stage1 item fields mismatch missing={missing} extra={extra}")
    if item["ir_type"] not in IR_TYPES:
        raise ValueError(f"unsupported stage1 ir_type: {item['ir_type']}")
    if item["dsl_type"] not in DSL_TYPES:
        raise ValueError(f"unsupported stage1 dsl_type: {item['dsl_type']}")
    return dict(item)


def _normalize_stage1_item(item: dict[str, Any]) -> Stage2PreferenceItem:
    normalized = dict(item)
    normalized["start_minutes"] = _parse_wall_time_minutes(item.get("start_time")) or 0
    end_time = item.get("end_time")
    normalized["end_minutes"] = None if end_time is None else _parse_wall_time_minutes(end_time)
    return normalized  # type: ignore[return-value]


def _assert_preference_ir_shape(item: Any) -> PreferenceIR:
    if not isinstance(item, dict):
        raise ValueError("PreferenceIR must be an object")
    item = _normalize_preference_ir(item)
    expected = set(PREFERENCE_IR_FIELDS)
    actual = set(item)
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing or extra:
        raise ValueError(f"PreferenceIR fields mismatch missing={missing} extra={extra}")
    if item["dsl_type"] not in DSL_TYPES:
        raise ValueError(f"unsupported PreferenceIR dsl_type: {item['dsl_type']}")
    enforcement = item.get("enforcement")
    if not isinstance(enforcement, dict):
        raise ValueError("PreferenceIR.enforcement must be an object")
    missing_enforcement = sorted(set(ENFORCEMENT_FIELDS) - set(enforcement))
    extra_enforcement = sorted(set(enforcement) - set(ENFORCEMENT_FIELDS))
    if missing_enforcement or extra_enforcement:
        raise ValueError(
            f"PreferenceIR.enforcement fields mismatch missing={missing_enforcement} extra={extra_enforcement}"
        )
    if "version" in item:
        raise ValueError("PreferenceIR must not contain version")
    return item  # type: ignore[return-value]


def _normalize_preference_ir(item: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(item)
    enforcement = normalized.get("enforcement")
    if isinstance(enforcement, dict) and "mode" in enforcement:
        enforcement = dict(enforcement)
        enforcement.pop("mode", None)
        normalized["enforcement"] = enforcement
    return normalized


def _parse_wall_time_minutes(value: Any) -> int | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() == "null":
        return None
    try:
        dt = datetime.strptime(text, _WALL_TIME_FORMAT)
    except ValueError:
        dt = datetime.fromisoformat(text.replace(" ", "T"))
    return int((dt - _SIMULATION_EPOCH).total_seconds() // 60)
