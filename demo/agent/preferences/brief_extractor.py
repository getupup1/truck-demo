"""LLM-backed light extraction of currently visible driver preferences."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from typing import Any

from agent.history.json_utils import extract_model_json_object
from agent.preferences.tool_prompts import brief_tool_intro
from agent.preferences.tool_schema import TOOL_NAMES, normalize_tool_flags


def preference_signature(preferences: list[Any]) -> str:
    try:
        payload = json.dumps(preferences, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except TypeError:
        payload = repr(preferences)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def extract_preference_texts(preferences: list[Any]) -> list[str]:
    texts: list[str] = []
    for item in preferences:
        if isinstance(item, str):
            text = item.strip()
        elif isinstance(item, dict):
            text = str(item.get("content") or item.get("text") or "").strip()
        else:
            text = str(item).strip()
        if text:
            texts.append(text)
    return texts


class PreferenceBriefExtractor:
    """Extract concise preference briefs without classifying preferences into fixed categories."""

    def __init__(self, model_chat_completion: Callable[[dict[str, Any]], dict[str, Any]]) -> None:
        self._model_chat_completion = model_chat_completion
        self._cache: dict[tuple[str, str], dict[str, Any]] = {}
        self._logger = logging.getLogger("agent.preferences.brief_extractor")

    def extract_for_status(self, driver_id: str, status: dict[str, Any]) -> dict[str, Any]:
        preferences = status.get("preferences") or []
        if not isinstance(preferences, list):
            preferences = []
        signature = preference_signature(preferences)
        cache_key = (driver_id, signature)
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        if not preferences:
            extracted = self._empty(driver_id, status, preferences, signature)
            self._cache[cache_key] = extracted
            return extracted

        raw = self._request_extract(driver_id, status, preferences)
        extracted = self._normalize(driver_id, status, preferences, signature, raw)
        self._cache[cache_key] = extracted
        self._logger.info(
            "preference_brief_extract ok driver_id=%s signature=%s briefs=%s",
            driver_id,
            signature[:12],
            len(extracted.get("preference_brief", [])),
        )
        return extracted

    def _request_extract(
        self,
        driver_id: str,
        status: dict[str, Any],
        preferences: list[Any],
    ) -> dict[str, Any]:
        payload = {
            "driver_id": driver_id,
            "simulation_progress_minutes": status.get("simulation_progress_minutes"),
            "simulation_wall_time": status.get("simulation_wall_time"),
            "preferences": preferences,
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
            except Exception as exc:  # noqa: BLE001 - retry once for transient model issues.
                last_error = exc
                self._logger.warning(
                    "preference_brief_extract failed driver_id=%s attempt=%s error=%s",
                    driver_id,
                    attempt + 1,
                    exc,
                )
        raise RuntimeError(f"preference brief extraction failed after retry: {last_error}") from last_error

    @staticmethod
    def _system_prompt() -> str:
        return (
            "你是货运司机偏好轻量提炼器。只允许输出一个 JSON 对象，禁止 markdown、解释和额外文本。"
            "只处理输入中当前可见的 preferences，不要推断未来才会出现的偏好。"
            "不要把偏好分类到固定类别，不要输出 categories、constraint_type、rules 或 rule_id。"
            "你的任务是保留原始偏好文本，并提炼可供后续 LLM 判断动作是否违反偏好的核心要求。"
            "输出格式必须是："
            '{"driver_id":"...","preference_brief":[{"pref_id":"pref_0","source_text":"原始偏好文本",'
            '"core_requirement":{"time":null,"action":"围绕 take_order / reposition / wait / 候选订单属性描述",'
            '"location":null,"value":null,"requirement":"一句话说明核心约束"},'
            '"penalty_amount":0,"penalty_cap":null,"needs_history":false,'
            '"tools":["geo_checks","wait_generation"]}]}。'
            "core_requirement.time 只填写原文明确出现的时间、日期、时段、周期或持续时长约束；没有则为 null。"
            "core_requirement.action 只填写原文明确约束的动作或候选属性，例如 take_order、wait、reposition、货类、订单号、收入、距离等；没有则为 null。"
            "core_requirement.location 只填写原文明确出现的位置约束；若是城市/区域，保留名称；若是圆形范围，写明圆心经纬度或地名及半径；"
            "若是经纬度范围或多边形范围，写明边界、范围或关键坐标；没有位置约束则为 null。"
            "core_requirement.value 用来保留原文明确出现的数值阈值、上限、下限、次数、金额、公里数、订单尾号等；没有则为 null。"
            "core_requirement.requirement 生成一句简洁要求，主要综合原文中的时间、动作、位置、数值、历史等约束条件；"
            "只描述实际存在的约束维度，没有的维度不要硬写。"
            "history_summary 固定包含 today 和 month 的通用聚合信息：当日/月内成功接单数、失败接单数、失败原因、"
            "月内已完成天数、不接单天数、完全闲置天数、最长连续 wait、当前 wait streak、活跃时长、"
            "pickup 空驶、reposition、送货、失败接单空驶和总行驶里程。"
            "needs_history 只能是 false 或字符串。若偏好可由当前候选动作和 history_summary 判断，则必须为 false。"
            "只有当偏好需要 history_summary 没有的逐事件信息时才填写字符串，例如今天第一笔订单开始时间、某天事件顺序、"
            "最近若干次具体动作、跨事件位置变化等。"
            "needs_history 字符串必须固定使用以下格式，方便程序解析："
            "start_simulation_wall_time=YYYY-MM-DD HH:MM:SS; "
            "end_simulation_wall_time=YYYY-MM-DD HH:MM:SS; "
            "reason=为什么 history_summary 不足。"
            "起止时间必须使用 simulation_wall_time 表示，不要写“今天”“本月”“最近”等相对时间；"
            "若需要今天历史，start_simulation_wall_time 写当天 00:00:00，end_simulation_wall_time 写当前 simulation_wall_time；"
            "若需要本月历史，start_simulation_wall_time 写本月首日 00:00:00，end_simulation_wall_time 写当前 simulation_wall_time。"
            "penalty_amount 和 penalty_cap 必须从输入 preference 原文对象复制；如果输入没有，则使用 0 和 null。"
            + brief_tool_intro(TOOL_NAMES)
            + "tools 只填写字符串数组，不要填写工具配置；没有需要工具时填写 []。"
            "只有当该偏好明显需要对应工具提供事实证据或动作候选时，才把对应工具名放入 tools。"
            "candidate_geo_contribution 和 history_geo_summary 应成对用于累计到访偏好。"
            "可以拆分复合偏好，但不要编造输入中没有的坐标、时间、货类、罚分。"
        )

    def _normalize(
        self,
        driver_id: str,
        status: dict[str, Any],
        preferences: list[Any],
        signature: str,
        raw: Any,
    ) -> dict[str, Any]:
        if not isinstance(raw, dict):
            return self._empty(driver_id, status, preferences, signature)

        raw_briefs = raw.get("preference_brief")
        if not isinstance(raw_briefs, list):
            raw_briefs = []
        metadata = self._preference_metadata(preferences)
        preference_brief: list[dict[str, Any]] = []
        for idx, raw_item in enumerate(raw_briefs):
            if not isinstance(raw_item, dict):
                continue
            source_text = str(raw_item.get("source_text") or raw_item.get("content") or "").strip()
            meta = self._match_metadata(source_text, metadata)
            if meta is None and idx < len(metadata):
                meta = metadata[idx]
            if meta is not None:
                source_text = str(meta.get("source_text") or "")
            if not source_text:
                continue
            pref_id = str(raw_item.get("pref_id") or f"pref_{idx}").strip() or f"pref_{idx}"
            preference_brief.append(
                {
                    "pref_id": pref_id,
                    "source_text": source_text,
                    "core_requirement": self._normalize_core_requirement(raw_item.get("core_requirement")),
                    "penalty_amount": self._number_or_default(
                        raw_item.get("penalty_amount")
                        if raw_item.get("penalty_amount") is not None
                        else self._meta_value(meta, "penalty_amount"),
                        0.0,
                    ),
                    "penalty_cap": self._number_or_none(
                        raw_item.get("penalty_cap")
                        if raw_item.get("penalty_cap") is not None
                        else self._meta_value(meta, "penalty_cap")
                    ),
                    "needs_history": self._normalize_needs_history(raw_item.get("needs_history")),
                    "tools": normalize_tool_flags(raw_item.get("tools")),
                }
            )

        return {
            "driver_id": driver_id,
            "preference_signature": signature,
            "extracted_at_progress_minutes": status.get("simulation_progress_minutes"),
            "extracted_at_wall_time": status.get("simulation_wall_time"),
            "preference_texts": extract_preference_texts(preferences),
            "preference_brief": preference_brief,
        }

    @staticmethod
    def _normalize_core_requirement(value: Any) -> dict[str, Any]:
        raw = value if isinstance(value, dict) else {}
        return {
            "time": raw.get("time"),
            "action": raw.get("action"),
            "location": raw.get("location"),
            "value": raw.get("value"),
            "requirement": raw.get("requirement"),
        }

    @staticmethod
    def _normalize_needs_history(value: Any) -> bool | str:
        if value is False or value is None:
            return False
        if isinstance(value, str):
            text = value.strip()
            return text if text else False
        if value is True:
            return "需要完整 event_log，模型未说明更具体的历史范围"
        return False

    @staticmethod
    def _preference_metadata(preferences: list[Any]) -> list[dict[str, Any]]:
        metadata: list[dict[str, Any]] = []
        for item in preferences:
            if isinstance(item, str):
                metadata.append({"source_text": item.strip(), "penalty_amount": 0, "penalty_cap": None})
            elif isinstance(item, dict):
                metadata.append(
                    {
                        "source_text": str(item.get("content") or item.get("text") or "").strip(),
                        "penalty_amount": item.get("penalty_amount"),
                        "penalty_cap": item.get("penalty_cap"),
                    }
                )
        return metadata

    @staticmethod
    def _match_metadata(source_text: str, metadata: list[dict[str, Any]]) -> dict[str, Any] | None:
        if not metadata:
            return None
        if not source_text:
            return metadata[0]
        canonical_source = PreferenceBriefExtractor._canonical_text(source_text)
        for item in metadata:
            text = str(item.get("source_text") or "")
            canonical_text = PreferenceBriefExtractor._canonical_text(text)
            if (
                source_text == text
                or source_text in text
                or text in source_text
                or canonical_source == canonical_text
                or canonical_source in canonical_text
                or canonical_text in canonical_source
            ):
                return item
        return None

    @staticmethod
    def _canonical_text(value: str) -> str:
        return "".join(str(value).split())

    @staticmethod
    def _meta_value(meta: dict[str, Any] | None, key: str) -> Any:
        if meta is None:
            return None
        return meta.get(key)

    @staticmethod
    def _number_or_default(value: Any, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _number_or_none(value: Any) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _empty(
        self,
        driver_id: str,
        status: dict[str, Any],
        preferences: list[Any],
        signature: str,
    ) -> dict[str, Any]:
        return {
            "driver_id": driver_id,
            "preference_signature": signature,
            "extracted_at_progress_minutes": status.get("simulation_progress_minutes"),
            "extracted_at_wall_time": status.get("simulation_wall_time"),
            "preference_texts": extract_preference_texts(preferences),
            "preference_brief": [],
        }
