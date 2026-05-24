from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from typing import Any


DEMO_ROOT = Path(__file__).resolve().parents[2]
if str(DEMO_ROOT) not in sys.path:
    sys.path.insert(0, str(DEMO_ROOT))

from agent.model_decision_service import ModelDecisionService
from agent.preference_router import PreferenceRouter
from agent.preferences.brief_extractor import PreferenceBriefExtractor
from agent.urgent.candidate_guard import apply_urgent_candidate_guard
from agent.urgent.schema import (
    build_urgent_relevant_events,
    normalize_step_decision,
    normalize_urgent_task_plan,
    select_priority_urgent_task,
)


class QueueModel:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = list(responses)
        self.payloads: list[dict[str, Any]] = []

    def __call__(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.payloads.append(payload)
        if not self.responses:
            raise AssertionError("model called more times than expected")
        content = json.dumps(self.responses.pop(0), ensure_ascii=False)
        return {"choices": [{"message": {"content": content}}]}


class FakeApi:
    def __init__(
        self,
        *,
        model: QueueModel,
        status: dict[str, Any],
        cargo_items: list[dict[str, Any]] | None = None,
    ) -> None:
        self.model_chat_completion = model
        self.status = dict(status)
        self.cargo_items = cargo_items or []
        self.query_cargo_count = 0
        self.history_count = 0

    def get_driver_status(self, driver_id: str) -> dict[str, Any]:
        return dict(self.status)

    def query_cargo(self, *, driver_id: str, latitude: float, longitude: float) -> dict[str, Any]:
        self.query_cargo_count += 1
        return {"items": self.cargo_items}

    def query_decision_history(self, driver_id: str, limit: int) -> dict[str, Any]:
        self.history_count += 1
        return {"records": []}


def _status(preferences: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "current_lat": 22.54,
        "current_lng": 114.06,
        "simulation_progress_minutes": 60,
        "simulation_wall_time": "2026-03-01 01:00:00",
        "preferences": preferences,
    }


def _urgent_task(mode: str = "force_action") -> dict[str, Any]:
    return {
        "driver_id": "D001",
        "urgent_tasks": [
            {
                "task_id": "urgent_0",
                "source_text": "urgent task",
                "visible_start_time": "2026-03-01 00:00:00",
                "active_start_time": "2026-03-01 00:00:00",
                "urgent_mode": mode,
                "penalty_amount": 10000,
                "penalty_cap": 10000,
                "candidate_guards": []
                if mode == "force_action"
                else [
                    {
                        "tool": "deadline_location_check",
                        "config": {
                            "center": [22.54, 114.06],
                            "radius_km": 0,
                            "deadline_wall_time": "2026-03-01 02:00:00",
                        },
                        "reason": "must still reach target",
                    }
                ],
                "stages": [
                    {
                        "stage_order": 1,
                        "action": "wait",
                        "params": {"duration_minutes": 10},
                        "reason": "urgent fallback wait",
                    }
                ],
            }
        ],
    }


class UrgentPipelineTests(unittest.TestCase):
    def test_router_splits_preferences_and_preserves_fields(self) -> None:
        preferences = [
            {"content": "ordinary rest", "penalty_amount": 1, "penalty_cap": 2},
            {"content": "urgent specified cargo", "penalty_amount": 10000, "penalty_cap": 10000},
        ]
        model = QueueModel(
            [
                {
                    "driver_id": "D001",
                    "ordinary_preferences": [{"pref_index": 0, "route_reason": "stable"}],
                    "urgent_preferences": [{"pref_index": 1, "route_reason": "deadline"}],
                }
            ]
        )

        routed = PreferenceRouter(model).route_for_status("D001", _status(preferences))

        self.assertEqual([item["content"] for item in routed["ordinary_preferences"]], ["ordinary rest"])
        self.assertEqual([item["content"] for item in routed["urgent_preferences"]], ["urgent specified cargo"])
        self.assertEqual(routed["urgent_preferences"][0]["penalty_amount"], 10000)

    def test_preference_extractor_override_sends_only_ordinary_preferences(self) -> None:
        ordinary = [{"content": "ordinary only", "penalty_amount": 1, "penalty_cap": 2}]
        model = QueueModel(
            [
                {
                    "driver_id": "D001",
                    "preference_brief": [
                        {
                            "pref_id": "pref_0",
                            "source_text": "ordinary only",
                            "core_requirement": {"requirement": "ordinary"},
                            "penalty_amount": 1,
                            "penalty_cap": 2,
                            "needs_history": False,
                            "tools": [],
                        }
                    ],
                }
            ]
        )

        PreferenceBriefExtractor(model).extract_for_status(
            "D001",
            _status([{"content": "urgent should not be sent"}]),
            preferences_override=ordinary,
        )

        sent = json.loads(model.payloads[0]["messages"][1]["content"])
        self.assertEqual([item["content"] for item in sent["preferences"]], ["ordinary only"])

    def test_urgent_schema_selects_earliest_deadline(self) -> None:
        plan = normalize_urgent_task_plan(
            {
                "driver_id": "D001",
                "urgent_tasks": [
                    {
                        **_urgent_task("planning_guard")["urgent_tasks"][0],
                        "task_id": "late",
                        "candidate_guards": [
                            {
                                "tool": "deadline_location_check",
                                "config": {"center": [1, 1], "deadline_wall_time": "2026-03-02 00:00:00"},
                            }
                        ],
                    },
                    {
                        **_urgent_task("planning_guard")["urgent_tasks"][0],
                        "task_id": "early",
                        "candidate_guards": [
                            {
                                "tool": "deadline_location_check",
                                "config": {"center": [1, 1], "deadline_wall_time": "2026-03-01 03:00:00"},
                            }
                        ],
                    },
                ],
            },
            driver_id="D001",
        )

        self.assertEqual(select_priority_urgent_task(plan)["task_id"], "early")
        decision = normalize_step_decision(
            {"urgent_mode": "force_action", "current_stage_order": 1, "action": "wait", "params": {"duration_minutes": 10}},
            _urgent_task()["urgent_tasks"][0],
        )
        self.assertEqual(decision["params"]["duration_minutes"], 10)

    def test_deadline_candidate_guard_removes_unsafe_orders(self) -> None:
        urgent_task = _urgent_task("planning_guard")["urgent_tasks"][0]
        candidates = [
            {
                "cargo_id": "unsafe",
                "simulated_event": {
                    "action_end_minutes": 10 * 60,
                    "position_after": {"lat": 30.0, "lng": 120.0},
                },
            }
        ]

        kept, summary = apply_urgent_candidate_guard(candidates, urgent_task, speed_km_per_hour=60.0)

        self.assertEqual(kept, [])
        self.assertEqual(summary["removed_count"], 1)

    def test_urgent_relevant_events_only_keeps_previous_event(self) -> None:
        urgent_task = {
            **_urgent_task("force_action")["urgent_tasks"][0],
            "visible_start_time": "2026-03-01 00:00:00",
        }
        events = [
            {"step": 1, "action": "wait", "action_end_minutes": 10},
            {"step": 2, "action": "reposition", "action_end_minutes": 20},
            {"step": 3, "action": "wait", "action_end_minutes": 30},
        ]

        relevant = build_urgent_relevant_events(events, urgent_task)

        self.assertEqual(len(relevant), 1)
        self.assertEqual(relevant[0]["step"], 3)

    def test_force_wait_returns_before_query_cargo(self) -> None:
        preferences = [{"content": "urgent family task", "penalty_amount": 10000, "penalty_cap": 10000}]
        model = QueueModel(
            [
                {"driver_id": "D001", "ordinary_preferences": [], "urgent_preferences": [{"pref_index": 0}]},
                _urgent_task("force_action"),
                {"urgent_mode": "force_action", "current_stage_order": 1, "action": "wait", "params": {"duration_minutes": 10}},
            ]
        )
        api = FakeApi(model=model, status=_status(preferences))

        action = ModelDecisionService(api).decide("D001")

        self.assertEqual(action, {"action": "wait", "params": {"duration_minutes": 10}})
        self.assertEqual(api.query_cargo_count, 0)

    def test_planning_guard_uses_fallback_when_no_safe_order(self) -> None:
        preferences = [{"content": "urgent deadline cargo", "penalty_amount": 10000, "penalty_cap": 10000}]
        cargo_items = [
            {
                "distance_km": 0,
                "cargo": {
                    "cargo_id": "C001",
                    "cargo_name": "normal",
                    "price": 100,
                    "remove_time": "2026-03-01 23:00:00",
                    "load_time": ["2026-03-01 01:00:00", "2026-03-01 02:00:00"],
                    "cost_time_minutes": 600,
                    "start": {"lat": 22.54, "lng": 114.06},
                    "end": {"lat": 30.0, "lng": 120.0},
                },
            }
        ]
        model = QueueModel(
            [
                {"driver_id": "D001", "ordinary_preferences": [], "urgent_preferences": [{"pref_index": 0}]},
                _urgent_task("planning_guard"),
                {"urgent_mode": "planning_guard", "current_stage_order": 1, "action": "wait", "params": {"duration_minutes": 10}},
            ]
        )
        api = FakeApi(model=model, status=_status(preferences), cargo_items=cargo_items)

        action = ModelDecisionService(api).decide("D001")

        self.assertEqual(action, {"action": "wait", "params": {"duration_minutes": 10}})
        self.assertEqual(api.query_cargo_count, 1)


if __name__ == "__main__":
    unittest.main()
