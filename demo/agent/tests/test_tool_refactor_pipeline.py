from __future__ import annotations

import sys
import unittest
from pathlib import Path


DEMO_ROOT = Path(__file__).resolve().parents[2]
if str(DEMO_ROOT) not in sys.path:
    sys.path.insert(0, str(DEMO_ROOT))

from agent.evidence.action_tool_builder import ActionToolBuilder
from agent.evidence.collector import collect_candidate_evidence
from agent.preferences.tool_prompts import TOOL_BRIEFS, TOOL_DETAILS
from agent.preferences.tool_schema import TOOL_NAMES, TOOL_REGISTRY, default_tool_flags, normalize_tool_flags, normalize_tool_plan, stage_tools


def _status(progress: int = 22 * 60 + 50) -> dict:
    return {
        "current_lat": 22.54,
        "current_lng": 114.06,
        "simulation_progress_minutes": progress,
        "simulation_wall_time": "2026-03-01 22:50:00",
    }


def _preference_context() -> dict:
    flags = list(TOOL_NAMES)
    return {
        "preference_brief": [
            {
                "pref_id": "pref_geo",
                "source_text": "车辆不得进入以（23.30，113.52）为圆心、半径20公里的区域。",
                "tools": flags,
            }
        ]
    }


def _tool_plan() -> dict:
    return {
        "pref_geo": {
            "geo_checks": {
                "relation": "forbidden_inside",
                "center": [23.30, 113.52],
                "radius_km": 20,
                "lat_range": None,
                "lng_range": None,
                "reason": "禁入圆形区域",
            },
            "candidate_geo_contribution": {
                "relation": "must_visit",
                "center": [23.13, 113.26],
                "radius_km": 1,
                "lat_range": None,
                "lng_range": None,
                "reason": "到访目标点",
            },
            "history_geo_summary": {
                "reason": "period=current_month; required_visit_count=2; count_unit=distinct_day",
                "required_visit_count": 2,
                "period": "current_month",
                "count_unit": "distinct_day",
            },
            "time_window_check": {"start": "23:00", "end": "04:00", "cross_day": True},
            "deadline_location_check": {
                "center": [22.55, 114.07],
                "radius_km": 1,
                "deadline_time": "23:00",
                "reason": "23点前到家",
            },
            "wait_generation": {"continuous_rest": {"hours": 3, "weekdays_only": False}},
        }
    }


class ToolRefactorPipelineTests(unittest.TestCase):
    def test_tool_schema_normalizes_boolean_flags_and_independent_plan(self) -> None:
        flags = normalize_tool_flags({"geo_checks": True, "wait_generation": {"enabled": True}})
        self.assertIn("geo_checks", flags)
        self.assertIn("wait_generation", flags)
        self.assertNotIn("deadline_location_check", flags)
        self.assertEqual(normalize_tool_flags(["geo_checks", "not_a_tool", "geo_checks"]), ["geo_checks"])
        self.assertEqual(default_tool_flags(), [])
        self.assertIn("geo_checks", stage_tools("candidate"))
        self.assertIn("wait_generation", stage_tools("action"))
        self.assertIn("geo_checks", TOOL_BRIEFS)
        self.assertIn("geo_checks", TOOL_DETAILS)
        self.assertTrue(all("brief" not in schema and "detail" not in schema for schema in TOOL_REGISTRY.values()))
        self.assertTrue(all("module" in schema for schema in TOOL_REGISTRY.values()))

        raw = {
            "tool_plan": {
                "pref_geo": {
                    "geo_checks": {
                        "relation": "forbidden_inside",
                        "center": [23.30, 113.52],
                        "radius_km": "20公里",
                    },
                    "deadline_location_check": {
                        "center": [22.55, 114.07],
                        "radius_km": 1,
                        "deadline_time": "23:00",
                    },
                }
            }
        }
        context = {"preference_brief": [{"pref_id": "pref_geo", "tools": flags}]}

        plan = normalize_tool_plan(raw, context)

        self.assertEqual(plan["pref_geo"]["geo_checks"]["radius_km"], 20.0)
        self.assertNotIn("deadline_location_check", plan["pref_geo"])

    def test_deadline_location_check_defaults_radius_when_missing(self) -> None:
        flags = ["deadline_location_check"]
        raw = {
            "tool_plan": {
                "pref_deadline": {
                    "deadline_location_check": {
                        "center": [22.55, 114.07],
                        "deadline_time": "23:00",
                    }
                }
            }
        }
        context = {"preference_brief": [{"pref_id": "pref_deadline", "tools": flags}]}

        plan = normalize_tool_plan(raw, context)

        self.assertEqual(plan["pref_deadline"]["deadline_location_check"]["radius_km"], 1.0)

    def test_candidate_evidence_collector_uses_tool_plan_not_brief_config(self) -> None:
        candidate = {
            "cargo_id": "C001",
            "start": {"lat": 23.30, "lng": 113.52},
            "end": {"lat": 23.13, "lng": 113.26},
            "simulated_event": {
                "action_start_minutes": 22 * 60 + 30,
                "action_end_minutes": 24 * 60 + 30,
                "position_before": {"lat": 22.54, "lng": 114.06},
                "position_after": {"lat": 23.13, "lng": 113.26},
            },
        }

        enhanced = collect_candidate_evidence(
            [candidate],
            preference_context=_preference_context(),
            tool_plan=_tool_plan(),
            speed_km_per_hour=60.0,
        )

        evidence = enhanced[0]["tool_evidence"]
        self.assertTrue(evidence["geo_checks_result"][0]["violates"])
        self.assertTrue(evidence["candidate_geo_contribution_result"][0]["can_contribute"])
        self.assertTrue(evidence["time_window_check_result"][0]["overlaps_forbidden_window"])
        self.assertIn("deadline_location_check_result", evidence)

    def test_action_tool_builder_outputs_context_waits_and_repositions(self) -> None:
        result = ActionToolBuilder(speed_km_per_hour=60.0).build(
            preference_context=_preference_context(),
            tool_plan=_tool_plan(),
            event_log=[],
            history_summary={
                "today": {"longest_wait_minutes": 0, "current_wait_streak_minutes": 0},
                "month": {},
            },
            status=_status(),
            scored_candidates=[],
        )

        self.assertIn("history_geo_summary", result["tool_context"])
        self.assertNotIn("target", result["tool_context"]["history_geo_summary"][0])
        self.assertNotIn("matched_history_events", result["tool_context"]["history_geo_summary"][0])
        self.assertTrue(result["extra_wait_options"])
        self.assertTrue(result["reposition_options"])
        self.assertTrue(all("simulated_event" not in option for option in result["extra_wait_options"]))
        self.assertTrue(all("simulated_event" not in option for option in result["reposition_options"]))


if __name__ == "__main__":
    unittest.main()
