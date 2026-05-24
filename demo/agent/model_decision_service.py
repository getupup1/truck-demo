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
from agent.preferences.brief_extractor import PreferenceBriefExtractor
from agent.preferences.judge import LLMPreferenceJudge
from agent.preferences.tool_planner import PreferenceToolPlanner
from agent.history.history import HistorySliceBuilder, build_event_log
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
        self._preference_extractor = PreferenceBriefExtractor(api.model_chat_completion)
        self._tool_planner = PreferenceToolPlanner(api.model_chat_completion)
        self._preference_judge = LLMPreferenceJudge(api.model_chat_completion, batch_size=5)
        self._history_slice_builder = HistorySliceBuilder()
        self._action_option_builder = ActionOptionBuilder(top_k=10)
        self._action_tool_builder = ActionToolBuilder(speed_km_per_hour=self._reposition_speed_km_per_hour)
        self._action_decider = LLMActionDecider(api.model_chat_completion)
        self._action_validator = ActionValidator()

    def decide(self, driver_id: str) -> dict[str, Any]:
        status = self._api.get_driver_status(driver_id)
        lat = float(status["current_lat"])
        lng = float(status["current_lng"])

        cargo_resp = self._api.query_cargo(driver_id=driver_id, latitude=lat, longitude=lng)
        items = cargo_resp.get("items", [])
        if not isinstance(items, list):
            items = []

        status_after_scan = self._api.get_driver_status(driver_id)
        preference_context = self._preference_extractor.extract_for_status(driver_id, status_after_scan)
        tool_plan = self._tool_planner.plan_for_context(
            driver_id=driver_id,
            status=status_after_scan,
            preference_context=preference_context,
        )

        history_resp = self._api.query_decision_history(driver_id, -1)
        event_log = build_event_log(history_resp)
        history_context = self._history_slice_builder.build(
            event_log=event_log,
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
            event_log=event_log,
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
            filter_summary,
            len(judged_candidates),
            len(action_options.get("orders", [])),
            action,
        )
        return action
