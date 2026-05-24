"""Compatibility wrappers for geographic evidence tools."""

from __future__ import annotations

from typing import Any

from agent.evidence.collector import collect_candidate_evidence
from agent.preferences.tool_schema import configured_context_to_tool_plan
from agent.tools.history_geo_summary import (
    build_geo_reposition_options as _build_geo_reposition_options,
    build_history_geo_summary as _build_history_geo_summary,
)


def attach_candidate_geo_evidence(
    candidates: list[dict[str, Any]],
    preference_context: dict[str, Any],
) -> list[dict[str, Any]]:
    """Attach geo tool evidence to each filtered candidate."""
    return collect_candidate_evidence(
        candidates,
        preference_context=preference_context,
        tool_plan=_geo_tool_plan(preference_context),
        speed_km_per_hour=60.0,
    )


def build_history_geo_summary(
    *,
    preference_context: dict[str, Any],
    event_log: list[dict[str, Any]],
    current_minutes: int,
) -> list[dict[str, Any]]:
    return _build_history_geo_summary(
        preference_context=preference_context,
        tool_plan=_geo_tool_plan(preference_context),
        event_log=event_log,
        current_minutes=current_minutes,
    )


def build_geo_reposition_options(
    *,
    history_geo_summary: list[dict[str, Any]],
    scored_candidates: list[dict[str, Any]],
    status: dict[str, Any],
    speed_km_per_hour: float,
    preference_context: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    return _build_geo_reposition_options(
        history_geo_summary=history_geo_summary,
        scored_candidates=scored_candidates,
        status=status,
        speed_km_per_hour=speed_km_per_hour,
        preference_context=preference_context,
        tool_plan=_geo_tool_plan(preference_context or {}),
    )


def _geo_tool_plan(preference_context: dict[str, Any]) -> dict[str, dict[str, Any]]:
    plan = configured_context_to_tool_plan(preference_context)
    allowed = {"geo_checks", "candidate_geo_contribution", "history_geo_summary"}
    return {
        pref_id: {name: config for name, config in tools.items() if name in allowed}
        for pref_id, tools in plan.items()
        if any(name in allowed for name in tools)
    }
