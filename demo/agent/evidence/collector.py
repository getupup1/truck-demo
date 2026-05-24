"""Collect candidate-level tool evidence."""

from __future__ import annotations

from importlib import import_module
from typing import Any

from agent.preferences.tool_schema import module_path, result_key, stage_tools


def collect_candidate_evidence(
    candidates: list[dict[str, Any]],
    *,
    preference_context: dict[str, Any],
    tool_plan: dict[str, dict[str, Any]],
    speed_km_per_hour: float,
) -> list[dict[str, Any]]:
    if not candidates or not tool_plan:
        return candidates

    brief_by_id = _brief_by_pref_id(preference_context)
    enhanced: list[dict[str, Any]] = []
    for candidate in candidates:
        item = dict(candidate)
        evidence: dict[str, Any] = dict(item.get("tool_evidence") or {})
        for pref_id, tools in tool_plan.items():
            brief = brief_by_id.get(pref_id, {"pref_id": pref_id})
            for tool_name in stage_tools("candidate"):
                config = tools.get(tool_name)
                if not isinstance(config, dict):
                    continue
                result = _evaluate_candidate_tool(
                    tool_name,
                    brief=brief,
                    config=config,
                    candidate=item,
                    speed_km_per_hour=speed_km_per_hour,
                )
                _append_result(evidence, tool_name, result)
        if evidence:
            item["tool_evidence"] = evidence
        enhanced.append(item)
    return enhanced


def _evaluate_candidate_tool(
    tool_name: str,
    *,
    brief: dict[str, Any],
    config: dict[str, Any],
    candidate: dict[str, Any],
    speed_km_per_hour: float,
) -> dict[str, Any] | None:
    path = module_path(tool_name)
    if not path:
        return None
    module = import_module(path)
    evaluator = getattr(module, "evaluate_candidate", None)
    if evaluator is None:
        return None
    try:
        return evaluator(brief, config, candidate, speed_km_per_hour=speed_km_per_hour)
    except TypeError:
        return evaluator(brief, config, candidate)


def _append_result(evidence: dict[str, Any], tool_name: str, result: dict[str, Any] | None) -> None:
    if result is None:
        return
    key = result_key(tool_name)
    values = evidence.setdefault(key, [])
    if isinstance(values, list):
        values.append(result)


def _brief_by_pref_id(preference_context: dict[str, Any]) -> dict[str, dict[str, Any]]:
    brief = preference_context.get("preference_brief")
    if not isinstance(brief, list):
        return {}
    return {str(item.get("pref_id") or ""): item for item in brief if isinstance(item, dict)}
