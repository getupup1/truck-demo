"""Agent-side preference-driven decision service."""

from __future__ import annotations

import logging
from typing import Any

from agent.actions.decider import LLMActionDecider
from agent.actions.options import ActionOptionBuilder
from agent.actions.validator import ActionValidator
from agent.candidates.enhancer import enhance_cargo_candidates
from agent.candidates.filtering import filter_basic_candidates
from agent.candidates.scorer import score_candidates
from agent.evidence.action_tool_builder import ActionToolBuilder
from agent.evidence.collector import collect_candidate_evidence
from agent.preference_router import PreferenceRouter
from agent.preferences.brief_extractor import PreferenceBriefExtractor
from agent.preferences.judge import LLMPreferenceJudge
from agent.preferences.tool_planner import PreferenceToolPlanner
from agent.history.history import HistorySliceBuilder, build_event_log
from agent.urgent.candidate_guard import apply_urgent_candidate_guard
from agent.urgent.options import build_urgent_action_options, cargo_visible
from agent.urgent.parser import UrgentTaskParser
from agent.urgent.schema import (
    first_stage_decision,
    planning_guard_needs_step_decider,
    resolve_urgent_wait_action,
    select_priority_urgent_task,
)
from agent.urgent.step_decider import UrgentStepDecider
from simkit.ports import SimulationApiPort


class ModelDecisionService:
    """Preference brief + LLM-judged candidate pipeline."""

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
        self._preference_router = PreferenceRouter(api.model_chat_completion)
        self._preference_extractor = PreferenceBriefExtractor(api.model_chat_completion)
        self._tool_planner = PreferenceToolPlanner(api.model_chat_completion)
        self._preference_judge = LLMPreferenceJudge(api.model_chat_completion, batch_size=5)
        self._urgent_parser = UrgentTaskParser(api.model_chat_completion)
        self._urgent_step_decider = UrgentStepDecider(api.model_chat_completion)
        self._history_slice_builder = HistorySliceBuilder()
        self._action_option_builder = ActionOptionBuilder(top_k=10)
        self._action_tool_builder = ActionToolBuilder(speed_km_per_hour=self._reposition_speed_km_per_hour)
        self._action_decider = LLMActionDecider(api.model_chat_completion)
        self._action_validator = ActionValidator()

    def decide(self, driver_id: str) -> dict[str, Any]:
        status = self._api.get_driver_status(driver_id)
        initial_route = self._preference_router.route_for_status(driver_id, status)
        event_log_cache: list[dict[str, Any]] | None = None

        def event_log() -> list[dict[str, Any]]:
            nonlocal event_log_cache
            if event_log_cache is None:
                event_log_cache = build_event_log(self._api.query_decision_history(driver_id, -1))
            return event_log_cache

        initial_urgent_task = self._active_urgent_task(driver_id, status, initial_route)
        if initial_urgent_task is not None and initial_urgent_task.get("urgent_mode") == "force_action":
            step_decision = self._urgent_step_decider.decide(
                driver_id=driver_id,
                status=status,
                urgent_task=initial_urgent_task,
                event_log=event_log(),
            )
            step_decision = resolve_urgent_wait_action(step_decision, initial_urgent_task, status)
            cargo_items: list[dict[str, Any]] = []
            validation_status = status
            if step_decision.get("action") == "take_order":
                cargo_items = self._query_cargo_items(driver_id, status)
                validation_status = self._api.get_driver_status(driver_id)
                self._ensure_urgent_take_order_visible(step_decision, cargo_items)
            return self._validate_urgent_action(step_decision, validation_status, cargo_items)

        cargo_resp = self._query_cargo_response(driver_id, status)
        items = cargo_resp.get("items", [])
        if not isinstance(items, list):
            items = []

        status_after_scan = self._api.get_driver_status(driver_id)
        route_after_scan = self._preference_router.route_for_status(driver_id, status_after_scan)
        urgent_task = self._active_urgent_task(driver_id, status_after_scan, route_after_scan) or initial_urgent_task
        urgent_fallback_action: dict[str, Any] | None = None
        if urgent_task is not None:
            if (
                urgent_task.get("urgent_mode") == "planning_guard"
                and not planning_guard_needs_step_decider(urgent_task, status_after_scan, event_log())
            ):
                urgent_fallback_action = first_stage_decision(urgent_task)
            else:
                urgent_fallback_action = self._urgent_step_decider.decide(
                    driver_id=driver_id,
                    status=status_after_scan,
                    urgent_task=urgent_task,
                    event_log=event_log(),
                )
            urgent_fallback_action = resolve_urgent_wait_action(urgent_fallback_action, urgent_task, status_after_scan)
            if urgent_task.get("urgent_mode") == "force_action":
                self._ensure_urgent_take_order_visible(urgent_fallback_action, items)
                return self._validate_urgent_action(urgent_fallback_action, status_after_scan, items)

        preference_context = self._preference_extractor.extract_for_status(
            driver_id,
            status_after_scan,
            preferences_override=route_after_scan.get("ordinary_preferences", []),
        )
        tool_plan = self._tool_planner.plan_for_context(
            driver_id=driver_id,
            status=status_after_scan,
            preference_context=preference_context,
        )

        history_context = self._history_slice_builder.build(
            event_log=event_log(),
            current_minutes=int(status_after_scan.get("simulation_progress_minutes", 0) or 0),
            preference_context=preference_context,
        )
        enhanced_candidates = enhance_cargo_candidates(
            items,
            status_after_scan,
            speed_km_per_hour=self._reposition_speed_km_per_hour,
            simulation_horizon_minutes=self._simulation_horizon_minutes,
            limit=30,
        )
        filtered_candidates, filter_summary = filter_basic_candidates(enhanced_candidates)
        urgent_guard_summary: dict[str, Any] | None = None
        if urgent_task is not None and urgent_task.get("urgent_mode") == "planning_guard":
            filtered_candidates, urgent_guard_summary = apply_urgent_candidate_guard(
                filtered_candidates,
                urgent_task,
                speed_km_per_hour=self._reposition_speed_km_per_hour,
            )
            if not filtered_candidates:
                if urgent_fallback_action is None:
                    urgent_fallback_action = self._urgent_step_decider.decide(
                        driver_id=driver_id,
                        status=status_after_scan,
                        urgent_task=urgent_task,
                        event_log=event_log(),
                    )
                    urgent_fallback_action = resolve_urgent_wait_action(
                        urgent_fallback_action,
                        urgent_task,
                        status_after_scan,
                    )
                self._ensure_urgent_take_order_visible(urgent_fallback_action, items)
                return self._validate_urgent_action(urgent_fallback_action, status_after_scan, items)

        evidence_candidates = collect_candidate_evidence(
            filtered_candidates,
            preference_context=preference_context,
            tool_plan=tool_plan,
            speed_km_per_hour=self._reposition_speed_km_per_hour,
        )
        judged_candidates = self._preference_judge.judge_candidates(
            driver_id=driver_id,
            status=status_after_scan,
            preference_context=preference_context,
            history_summary=history_context["history_summary"],
            history_slice=history_context["history_slice"],
            candidates=evidence_candidates,
        )
        scored_candidates = score_candidates(judged_candidates)
        action_tool_result = self._action_tool_builder.build(
            preference_context=preference_context,
            tool_plan=tool_plan,
            event_log=event_log(),
            history_summary=history_context["history_summary"],
            status=status_after_scan,
            scored_candidates=scored_candidates,
        )
        action_options = self._action_option_builder.build(
            scored_candidates,
            status=status_after_scan,
            tool_context=action_tool_result["tool_context"],
            extra_wait_options=action_tool_result["extra_wait_options"],
            reposition_options=action_tool_result["reposition_options"],
        )
        selected_action = self._action_decider.decide(
            driver_id=driver_id,
            status=status_after_scan,
            preference_context=preference_context,
            history_summary=history_context["history_summary"],
            history_slice=history_context["history_slice"],
            action_options=action_options,
        )
        action = self._action_validator.validate(selected_action, action_options)

        self._logger.info(
            "decision driver_id=%s time_min=%s cargo_items=%s enhanced=%s filter_summary=%s judged=%s top_orders=%s action=%s",
            driver_id,
            status_after_scan.get("simulation_progress_minutes"),
            len(items),
            len(enhanced_candidates),
            {"basic": filter_summary, "urgent_guard": urgent_guard_summary},
            len(judged_candidates),
            len(action_options.get("orders", [])),
            action,
        )
        return action

    def _query_cargo_response(self, driver_id: str, status: dict[str, Any]) -> dict[str, Any]:
        lat = float(status["current_lat"])
        lng = float(status["current_lng"])
        return self._api.query_cargo(driver_id=driver_id, latitude=lat, longitude=lng)

    def _query_cargo_items(self, driver_id: str, status: dict[str, Any]) -> list[dict[str, Any]]:
        cargo_resp = self._query_cargo_response(driver_id, status)
        items = cargo_resp.get("items", [])
        return items if isinstance(items, list) else []

    def _active_urgent_task(
        self,
        driver_id: str,
        status: dict[str, Any],
        route: dict[str, Any],
    ) -> dict[str, Any] | None:
        urgent_preferences = route.get("urgent_preferences")
        if not isinstance(urgent_preferences, list) or not urgent_preferences:
            return None
        plan = self._urgent_parser.parse_for_preferences(
            driver_id=driver_id,
            status=status,
            urgent_preferences=urgent_preferences,
        )
        return select_priority_urgent_task(plan)

    def _validate_urgent_action(
        self,
        step_decision: dict[str, Any],
        status: dict[str, Any],
        cargo_items: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        action_options = build_urgent_action_options(
            step_decision,
            status=status,
            cargo_items=cargo_items or [],
            speed_km_per_hour=self._reposition_speed_km_per_hour,
        )
        return self._action_validator.validate(step_decision, action_options)

    @staticmethod
    def _ensure_urgent_take_order_visible(step_decision: dict[str, Any], cargo_items: list[dict[str, Any]]) -> None:
        if step_decision.get("action") != "take_order":
            return
        cargo_id = str(step_decision.get("params", {}).get("cargo_id") or "")
        if not cargo_visible(cargo_items, cargo_id):
            raise ValueError(f"urgent take_order cargo_id is not visible after scan: {cargo_id}")
