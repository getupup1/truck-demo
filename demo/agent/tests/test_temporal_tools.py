from __future__ import annotations

import sys
import unittest
from pathlib import Path


DEMO_ROOT = Path(__file__).resolve().parents[2]
if str(DEMO_ROOT) not in sys.path:
    sys.path.insert(0, str(DEMO_ROOT))

from agent.actions.options import ActionOptionBuilder
from agent.tools.temporal_evidence import (
    attach_candidate_temporal_evidence,
    attach_reposition_time_window_evidence,
    build_deadline_reposition_options,
    build_preference_wait_options,
    make_reposition_option,
)


def _status(progress: int = 0) -> dict:
    return {
        "current_lat": 22.54,
        "current_lng": 114.06,
        "simulation_progress_minutes": progress,
        "simulation_wall_time": "2026-03-01 00:00:00",
    }


def _time_window_context(start: str = "23:00", end: str = "04:00", cross_day: bool = True) -> dict:
    return {
        "preference_brief": [
            {
                "pref_id": "pref_window",
                "source_text": "每天23点至次日4点不接单、不空车赶路。",
                "tools": {
                    "time_window_check": {
                        "start": start,
                        "end": end,
                        "cross_day": cross_day,
                        "reason": "固定休息窗口",
                    }
                },
            }
        ]
    }


class TemporalToolTests(unittest.TestCase):
    def test_time_window_check_handles_cross_day_overlap(self) -> None:
        candidate = {
            "cargo_id": "C001",
            "simulated_event": {
                "action_start_minutes": 22 * 60 + 30,
                "action_end_minutes": 24 * 60 + 30,
            },
        }

        enhanced = attach_candidate_temporal_evidence(
            [candidate],
            _time_window_context(),
            speed_km_per_hour=60.0,
        )

        result = enhanced[0]["tool_evidence"]["time_window_check_result"][0]
        self.assertTrue(result["overlaps_forbidden_window"])
        self.assertEqual(result["overlap_minutes"], 90)

    def test_time_window_check_reports_no_overlap(self) -> None:
        candidate = {
            "cargo_id": "C001",
            "simulated_event": {
                "action_start_minutes": 12 * 60,
                "action_end_minutes": 13 * 60,
            },
        }

        enhanced = attach_candidate_temporal_evidence(
            [candidate],
            _time_window_context(),
            speed_km_per_hour=60.0,
        )

        result = enhanced[0]["tool_evidence"]["time_window_check_result"][0]
        self.assertFalse(result["overlaps_forbidden_window"])
        self.assertEqual(result["overlap_minutes"], 0)

    def test_deadline_location_check_for_candidate_reachability(self) -> None:
        context = {
            "preference_brief": [
                {
                    "pref_id": "pref_deadline",
                    "source_text": "每天23点前回到家附近。",
                    "tools": {
                        "deadline_location_check": {
                            "center": [22.54, 114.06],
                            "radius_km": 1,
                            "deadline_time": "23:00",
                            "reason": "23点前到家",
                        }
                    },
                }
            ]
        }
        candidate = {
            "cargo_id": "C001",
            "simulated_event": {
                "action_start_minutes": 22 * 60,
                "action_end_minutes": 22 * 60 + 20,
                "position_after": {"lat": 23.54, "lng": 114.06},
            },
        }

        enhanced = attach_candidate_temporal_evidence([candidate], context, speed_km_per_hour=60.0)

        result = enhanced[0]["tool_evidence"]["deadline_location_check_result"][0]
        self.assertFalse(result["can_reach_deadline"])
        self.assertEqual(result["deadline_time"], "2026-03-01 23:00:00")
        self.assertIn("不可达", result["summary"])

    def test_preference_wait_options_for_continuous_rest(self) -> None:
        context = {
            "preference_brief": [
                {
                    "pref_id": "pref_rest",
                    "tools": {"wait_generation": {"continuous_rest": {"hours": 4}}},
                }
            ]
        }
        history_summary = {
            "today": {"longest_wait_minutes": 60, "current_wait_streak_minutes": 60},
            "month": {},
        }

        options = build_preference_wait_options(
            preference_context=context,
            history_summary=history_summary,
            status=_status(progress=10 * 60),
        )

        self.assertEqual(options[0]["params"]["duration_minutes"], 180)
        self.assertNotIn("simulated_event", options[0])
        self.assertEqual(options[0]["action"], "wait")
        self.assertEqual(options[0]["action_end_minutes"], 13 * 60)

    def test_weekday_only_continuous_rest_wait_skips_weekend(self) -> None:
        context = {
            "preference_brief": [
                {
                    "pref_id": "pref_weekday_rest",
                    "tools": {"wait_generation": {"continuous_rest": {"hours": 4, "weekdays_only": True}}},
                }
            ]
        }
        history_summary = {
            "today": {"longest_wait_minutes": 0, "current_wait_streak_minutes": 0},
            "month": {},
        }

        sunday_options = build_preference_wait_options(
            preference_context=context,
            history_summary=history_summary,
            status=_status(progress=10 * 60),
        )
        monday_options = build_preference_wait_options(
            preference_context=context,
            history_summary=history_summary,
            status=_status(progress=1440 + 10 * 60),
        )

        self.assertEqual(sunday_options, [])
        self.assertEqual(monday_options[0]["params"]["duration_minutes"], 240)

    def test_fixed_window_wait_only_generated_near_or_inside_window(self) -> None:
        near_options = build_preference_wait_options(
            preference_context={"preference_brief": [{"pref_id": "pref_window", "tools": {"wait_generation": {"fixed_rest_window": {"start": "23:00", "end": "04:00", "cross_day": True}}}}]},
            history_summary={"today": {"current_wait_streak_minutes": 0}, "month": {}},
            status=_status(progress=22 * 60 + 30),
        )
        noon_options = build_preference_wait_options(
            preference_context={"preference_brief": [{"pref_id": "pref_window", "tools": {"wait_generation": {"fixed_rest_window": {"start": "23:00", "end": "04:00", "cross_day": True}}}}]},
            history_summary={"today": {"current_wait_streak_minutes": 0}, "month": {}},
            status=_status(progress=12 * 60),
        )

        self.assertEqual(near_options[0]["params"]["duration_minutes"], 330)
        self.assertEqual(noon_options, [])

    def test_fixed_window_wait_uses_event_log_for_cross_day_streak(self) -> None:
        context = {
            "preference_brief": [
                {
                    "pref_id": "pref_window",
                    "tools": {"wait_generation": {"fixed_rest_window": {"start": "23:00", "end": "04:00", "cross_day": True}}},
                }
            ]
        }

        options = build_preference_wait_options(
            preference_context=context,
            history_summary={"today": {"current_wait_streak_minutes": 60}, "month": {}},
            status=_status(progress=25 * 60),
            event_log=[
                {
                    "action": "wait",
                    "action_start_minutes": 23 * 60,
                    "action_end_minutes": 25 * 60,
                }
            ],
        )

        self.assertEqual(options[0]["params"]["duration_minutes"], 180)
        self.assertIn("继续满足", options[0]["reason"])

    def test_monthly_rest_wait_goes_to_day_end_only_when_today_idle(self) -> None:
        context = {
            "preference_brief": [
                {
                    "pref_id": "pref_month_idle",
                    "tools": {"wait_generation": {"monthly_rest_days": {"days": 2}}},
                }
            ]
        }

        options = build_preference_wait_options(
            preference_context=context,
            history_summary={
                "today": {"active_minutes": 0, "successful_take_order_count": 0, "failed_take_order_count": 0},
                "month": {"fully_idle_days": 1},
            },
            status=_status(progress=10 * 60),
        )
        active_options = build_preference_wait_options(
            preference_context=context,
            history_summary={
                "today": {"active_minutes": 30, "successful_take_order_count": 0, "failed_take_order_count": 0},
                "month": {"fully_idle_days": 1},
            },
            status=_status(progress=10 * 60),
        )

        self.assertEqual(options[0]["params"]["duration_minutes"], 14 * 60)
        self.assertEqual(active_options, [])

    def test_deadline_reposition_option_and_reposition_time_window_evidence(self) -> None:
        context = {
            "preference_brief": [
                {
                    "pref_id": "pref_deadline",
                    "tools": {
                        "deadline_location_check": {
                            "center": [22.55, 114.07],
                            "radius_km": 1,
                            "deadline_time": "23:00",
                            "reason": "23点前到家",
                        },
                        "time_window_check": {"start": "23:00", "end": "04:00", "cross_day": True},
                    },
                }
            ]
        }
        scored_candidates = [
            {
                "tool_evidence": {
                    "deadline_location_check_result": [
                        {"pref_id": "pref_deadline", "can_reach_deadline": False}
                    ]
                }
            }
        ]

        options = build_deadline_reposition_options(
            preference_context=context,
            scored_candidates=scored_candidates,
            status=_status(progress=22 * 60 + 50),
            speed_km_per_hour=60.0,
        )
        options = attach_reposition_time_window_evidence(options, context)

        self.assertEqual(len(options), 1)
        self.assertEqual(options[0]["tool_origin"], "deadline_location_check")
        self.assertNotIn("simulated_event", options[0])
        self.assertEqual(options[0]["action"], "reposition")
        self.assertIn("deadline_location_check", options[0])
        self.assertIn("time_window_check_result", options[0]["tool_evidence"])

        contributing = [
            {
                "tool_evidence": {
                    "deadline_location_check_result": [
                        {"pref_id": "pref_deadline", "can_reach_deadline": True}
                    ]
                }
            }
        ]
        self.assertEqual(
            build_deadline_reposition_options(
                preference_context=context,
                scored_candidates=contributing,
                status=_status(progress=22 * 60 + 50),
                speed_km_per_hour=60.0,
            ),
            [],
        )

    def test_action_option_builder_adds_simulated_wait_and_merges_preference_wait(self) -> None:
        extra_wait = build_preference_wait_options(
            preference_context={"preference_brief": [{"pref_id": "pref_rest", "tools": {"wait_generation": {"continuous_rest": {"hours": 3}}}}]},
            history_summary={"today": {"longest_wait_minutes": 0, "current_wait_streak_minutes": 0}, "month": {}},
            status=_status(progress=60),
        )

        action_options = ActionOptionBuilder(top_k=0).build(
            [],
            status=_status(progress=60),
            extra_wait_options=extra_wait,
        )

        wait_180 = next(option for option in action_options["wait_options"] if option["params"]["duration_minutes"] == 180)
        self.assertNotIn("simulated_event", wait_180)
        self.assertEqual(wait_180["action"], "wait")
        self.assertEqual(wait_180["source"], "continuous_rest_wait")


if __name__ == "__main__":
    unittest.main()
