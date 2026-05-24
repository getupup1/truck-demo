"""Build action-level context and preference-driven action options."""

from __future__ import annotations

import inspect
from importlib import import_module
from typing import Any

from agent.preferences.tool_schema import module_path, stage_tools


class ActionToolBuilder:
    def __init__(self, *, speed_km_per_hour: float) -> None:
        self._speed_km_per_hour = float(speed_km_per_hour)

    def build(
        self,
        *,
        preference_context: dict[str, Any],
        tool_plan: dict[str, dict[str, Any]],
        event_log: list[dict[str, Any]],
        history_summary: dict[str, Any],
        status: dict[str, Any],
        scored_candidates: list[dict[str, Any]],
    ) -> dict[str, Any]:
        context: dict[str, Any] = {}
        for tool_name in stage_tools("context"):
            if not _has_tool_config(tool_plan, tool_name):
                continue
            module = _tool_module(tool_name)
            builder = getattr(module, "build_context", None)
            if builder is None:
                continue
            context[tool_name] = builder(
                preference_context=preference_context,
                tool_plan=tool_plan,
                event_log=event_log,
                current_minutes=int(status.get("simulation_progress_minutes", 0) or 0),
            )

        wait_options: list[dict[str, Any]] = []
        reposition_options: list[dict[str, Any]] = []
        for tool_name in stage_tools("action"):
            if not _has_tool_config(tool_plan, tool_name):
                continue
            module = _tool_module(tool_name)
            action_builder = getattr(module, "build_action_options", None)
            if action_builder is None:
                continue
            options = _call_with_supported_kwargs(
                action_builder,
                preference_context=preference_context,
                tool_plan=tool_plan,
                context=context,
                history_geo_summary=context.get("history_geo_summary", []),
                history_summary=history_summary,
                scored_candidates=scored_candidates,
                status=status,
                event_log=event_log,
                speed_km_per_hour=self._speed_km_per_hour,
            )
            if not isinstance(options, list):
                continue
            for option in options:
                if not isinstance(option, dict):
                    continue
                if option.get("action") == "wait":
                    wait_options.append(option)
                elif option.get("action") == "reposition":
                    reposition_options.append(option)

        for tool_name in stage_tools("action"):
            if not _has_tool_config(tool_plan, tool_name):
                continue
            module = _tool_module(tool_name)
            annotator = getattr(module, "annotate_action_options", None)
            if annotator is None:
                continue
            reposition_options = annotator(
                reposition_options,
                preference_context=preference_context,
                tool_plan=tool_plan,
                context=context,
            )

        return {
            "extra_wait_options": wait_options,
            "reposition_options": reposition_options,
            "tool_context": context,
        }


def _has_tool_config(tool_plan: dict[str, dict[str, Any]], tool_name: str) -> bool:
    return any(isinstance(tools, dict) and isinstance(tools.get(tool_name), dict) for tools in tool_plan.values())


def _tool_module(tool_name: str) -> Any:
    return import_module(module_path(tool_name))


def _call_with_supported_kwargs(func: Any, **kwargs: Any) -> Any:
    signature = inspect.signature(func)
    if any(param.kind == inspect.Parameter.VAR_KEYWORD for param in signature.parameters.values()):
        return func(**kwargs)
    filtered = {key: value for key, value in kwargs.items() if key in signature.parameters}
    return func(**filtered)
