from __future__ import annotations

import sys
import unittest
from pathlib import Path


DEMO_ROOT = Path(__file__).resolve().parents[2]
if str(DEMO_ROOT) not in sys.path:
    sys.path.insert(0, str(DEMO_ROOT))

from agent.candidates.enhancer import enhance_cargo_candidates
from agent.candidates.filtering import filter_basic_candidates
from agent.candidates.scorer import score_candidates
from agent.actions.options import ActionOptionBuilder
from agent.actions.validator import ActionValidator


class CandidatePipelineTests(unittest.TestCase):
    def test_enhance_builds_simulated_event(self) -> None:
        status = {
            "current_lat": 22.54,
            "current_lng": 114.06,
            "simulation_progress_minutes": 60,
            "simulation_wall_time": "2026-03-01 01:00:00",
        }
        items = [
            {
                "distance_km": 0.0,
                "cargo": {
                    "cargo_id": "C001",
                    "cargo_name": "蔬菜",
                    "price": 1000,
                    "remove_time": "2026-03-01 03:00:00",
                    "load_time": ["2026-03-01 01:00:00", "2026-03-01 02:00:00"],
                    "cost_time_minutes": 30,
                    "start": {"lat": 22.54, "lng": 114.06, "city": "深圳市"},
                    "end": {"lat": 22.60, "lng": 114.10, "city": "深圳市"},
                },
            }
        ]

        candidates = enhance_cargo_candidates(items, status, speed_km_per_hour=60.0)

        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        self.assertEqual(candidate["cargo_id"], "C001")
        self.assertNotIn("price", candidate)
        self.assertNotIn("price", candidate["simulated_event"]["cargo"])
        self.assertEqual(candidate["gross_income"], 1000)
        self.assertEqual(candidate["simulated_event"]["cargo"]["gross_income"], 1000)
        self.assertEqual(candidate["pickup_minutes"], 0)
        self.assertEqual(candidate["estimated_total_minutes"], 30)
        self.assertEqual(candidate["simulated_event"]["action"], "take_order")
        self.assertEqual(candidate["simulated_event"]["params"]["cargo_id"], "C001")
        self.assertFalse(candidate["simulated_event"]["failure_flags"]["load_time_missed"])

    def test_basic_filter_only_removes_deterministic_failures(self) -> None:
        candidates = [
            {"cargo_id": "expired", "expired_after_scan": True, "load_time_missed": False, "estimated_total_minutes": 10},
            {"cargo_id": "missed", "expired_after_scan": False, "load_time_missed": True, "estimated_total_minutes": None},
            {"cargo_id": "invalid", "expired_after_scan": False, "load_time_missed": False, "estimated_total_minutes": 0},
            {
                "cargo_id": "kept_negative_income_later",
                "expired_after_scan": False,
                "load_time_missed": False,
                "estimated_total_minutes": 10,
                "price": -1,
            },
        ]

        kept, summary = filter_basic_candidates(candidates)

        self.assertEqual([item["cargo_id"] for item in kept], ["kept_negative_income_later"])
        self.assertEqual(summary["expired_after_scan"], 1)
        self.assertEqual(summary["load_time_missed"], 1)
        self.assertEqual(summary["invalid_estimated_total_minutes"], 1)
        self.assertEqual(summary["kept_after_basic_filter"], 1)

    def test_score_candidates_uses_income_distance_and_preference_penalty(self) -> None:
        scored = score_candidates(
            [
                {
                    "cargo_id": "low_penalty",
                    "gross_income": 1000,
                    "pickup_deadhead_km": 10,
                    "haul_distance_km": 20,
                    "estimated_total_minutes": 100,
                    "preference_evaluation": {"preference_penalty": 0},
                },
                {
                    "cargo_id": "high_penalty",
                    "gross_income": 1100,
                    "pickup_deadhead_km": 10,
                    "haul_distance_km": 20,
                    "estimated_total_minutes": 50,
                    "preference_evaluation": {"preference_penalty": 200},
                },
            ],
            distance_cost_per_km=1.5,
        )

        self.assertEqual([item["cargo_id"] for item in scored], ["high_penalty", "low_penalty"])
        self.assertEqual(scored[0]["distance_cost"], 45.0)
        self.assertEqual(scored[0]["net_income"], 855.0)
        self.assertEqual(scored[0]["score"], 17.1)
        self.assertEqual(scored[1]["net_income"], 955.0)
        self.assertEqual(scored[1]["score"], 9.55)

    def test_action_validator_accepts_only_offered_orders_and_waits(self) -> None:
        action_options = ActionOptionBuilder(top_k=1).build(
            [
                {
                    "cargo_id": "C001",
                    "score": 100,
                    "gross_income": 1000,
                    "distance_cost": 10,
                    "preference_penalty": 0,
                }
            ]
        )
        validator = ActionValidator()

        self.assertEqual(
            validator.validate({"action": "take_order", "params": {"cargo_id": "C001"}}, action_options),
            {"action": "take_order", "params": {"cargo_id": "C001"}},
        )
        self.assertEqual(
            validator.validate({"action": "wait", "params": {"duration_minutes": 30}}, action_options),
            {"action": "wait", "params": {"duration_minutes": 30}},
        )
        with self.assertRaises(ValueError):
            validator.validate({"action": "take_order", "params": {"cargo_id": "C999"}}, action_options)
        with self.assertRaises(ValueError):
            validator.validate({"action": "wait", "params": {"duration_minutes": 15}}, action_options)


if __name__ == "__main__":
    unittest.main()
