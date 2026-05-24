"""Compatibility wrappers for temporal evidence and action option helpers."""

from __future__ import annotations

from typing import Any

from agent.evidence.collector import collect_candidate_evidence
from agent.preferences.tool_schema import configured_context_to_tool_plan
from agent.tools.deadline_location_check import build_deadline_reposition_options as _build_deadline_reposition_options
from agent.tools.reposition_factory import make_reposition_option
from agent.tools.time_window_checks import (
    DAY_MINUTES,
    attach_reposition_time_window_evidence as _attach_reposition_time_window_evidence,
    current_or_next_window,
    next_time_of_day_minutes,
    overlap_fixed_window,
    time_to_minutes,
)
from agent.tools.wait_factory import make_wait_option
from agent.tools.wait_generation import (
    DEFAULT_WINDOW_LEAD_MINUTES,
    build_preference_wait_options as _build_preference_wait_options,
)


def attach_candidate_temporal_evidence(
    candidates: list[dict[str, Any]],
    preference_context: dict[str, Any],
    *,
    speed_km_per_hour: float,
) -> list[dict[str, Any]]:
    """Attach time-window and deadline evidence to filtered cargo candidates."""
    return collect_candidate_evidence(
        candidates,
        preference_context=preference_context,
        tool_plan=_temporal_tool_plan(preference_context),
        speed_km_per_hour=speed_km_per_hour,
    )


def attach_reposition_time_window_evidence(
    reposition_options: list[dict[str, Any]],
    preference_context: dict[str, Any],
) -> list[dict[str, Any]]:
    return _attach_reposition_time_window_evidence(
        reposition_options,
        preference_context=preference_context,
        tool_plan=_temporal_tool_plan(preference_context),
    )


def build_deadline_reposition_options(
    *,
    preference_context: dict[str, Any],
    scored_candidates: list[dict[str, Any]],
    status: dict[str, Any],
    speed_km_per_hour: float,
) -> list[dict[str, Any]]:
    return _build_deadline_reposition_options(
        preference_context=preference_context,
        tool_plan=_temporal_tool_plan(preference_context),
        scored_candidates=scored_candidates,
        status=status,
        speed_km_per_hour=speed_km_per_hour,
    )


def build_preference_wait_options(
    *,
    preference_context: dict[str, Any],
    history_summary: dict[str, Any],
    status: dict[str, Any],
    event_log: list[dict[str, Any]] | None = None,
    lead_minutes: int = DEFAULT_WINDOW_LEAD_MINUTES,
) -> list[dict[str, Any]]:
    return _build_preference_wait_options(
        preference_context=preference_context,
        tool_plan=_temporal_tool_plan(preference_context),
        history_summary=history_summary,
        status=status,
        event_log=event_log,
        lead_minutes=lead_minutes,
    )


def _temporal_tool_plan(preference_context: dict[str, Any]) -> dict[str, dict[str, Any]]:
    plan = configured_context_to_tool_plan(preference_context)
    allowed = {"time_window_check", "deadline_location_check", "wait_generation"}
    return {
        pref_id: {name: config for name, config in tools.items() if name in allowed}
        for pref_id, tools in plan.items()
        if any(name in allowed for name in tools)
    }


# Backward-compatible aliases for older tests/imports.
_overlap_fixed_window = overlap_fixed_window
_current_or_next_window = current_or_next_window
_next_time_of_day_minutes = next_time_of_day_minutes
_time_to_minutes = time_to_minutes
