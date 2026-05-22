from __future__ import annotations

import sys
import unittest
from pathlib import Path


DEMO_ROOT = Path(__file__).resolve().parents[2]
if str(DEMO_ROOT) not in sys.path:
    sys.path.insert(0, str(DEMO_ROOT))

from agent.actions.options import ActionOptionBuilder
from agent.actions.validator import ActionValidator
from agent.tools.geo_evidence import (
    attach_candidate_geo_evidence,
    build_geo_reposition_options,
    build_history_geo_summary,
)


def _candidate(cargo_id: str = "C001", end_lat: float = 23.13, end_lng: float = 113.26) -> dict:
    return {
        "cargo_id": cargo_id,
        "cargo_name": "测试货物",
        "start": {"lat": 23.10, "lng": 113.20},
        "end": {"lat": end_lat, "lng": end_lng},
        "simulated_event": {
            "position_before": {"lat": 22.54, "lng": 114.06},
            "position_after": {"lat": end_lat, "lng": end_lng},
        },
    }


def _preference_context() -> dict:
    return {
        "preference_brief": [
            {
                "pref_id": "pref_geo",
                "source_text": "车辆不得进入以（23.30，113.52）为圆心、半径20公里的区域。",
                "tools": {
                    "geo_checks": {
                        "relation": "forbidden_inside",
                        "center": [23.30, 113.52],
                        "radius_km": 20,
                        "lat_range": None,
                        "lng_range": None,
                        "reason": "禁入圆形区域",
                    }
                },
            },
            {
                "pref_id": "pref_visit",
                "source_text": "自然月内至少5个不同的自然日到过（23.13，113.26）一公里内；同日多次只算一天。",
                "tools": {
                    "candidate_geo_contribution": {
                        "relation": "must_visit",
                        "center": [23.13, 113.26],
                        "radius_km": 1,
                        "lat_range": None,
                        "lng_range": None,
                        "reason": "到访目标点",
                    },
                    "history_geo_summary": {
                        "reason": "period=current_month; required_visit_count=5; count_unit=distinct_day",
                        "required_visit_count": 5,
                        "period": "current_month",
                        "count_unit": "distinct_day",
                    },
                },
            },
        ]
    }


class GeoToolTests(unittest.TestCase):
    def test_attach_candidate_geo_evidence_for_circle_and_contribution(self) -> None:
        candidates = [_candidate(end_lat=23.30, end_lng=113.52)]

        enhanced = attach_candidate_geo_evidence(candidates, _preference_context())

        evidence = enhanced[0]["tool_evidence"]
        geo_result = evidence["geo_checks_result"][0]
        contribution_result = evidence["candidate_geo_contribution_result"][0]
        self.assertTrue(geo_result["violates"])
        self.assertFalse(contribution_result["can_contribute"])

    def test_attach_candidate_geo_evidence_for_range_must_inside(self) -> None:
        context = {
            "preference_brief": [
                {
                    "pref_id": "pref_range",
                    "tools": {
                        "geo_checks": {
                            "relation": "must_inside",
                            "center": None,
                            "radius_km": None,
                            "lat_range": [22.42, 22.89],
                            "lng_range": [113.74, 114.66],
                            "reason": "必须在深圳经纬度范围内",
                        }
                    },
                }
            ]
        }

        enhanced = attach_candidate_geo_evidence([_candidate(end_lat=23.10, end_lng=113.80)], context)

        self.assertTrue(enhanced[0]["tool_evidence"]["geo_checks_result"][0]["violates"])

    def test_history_geo_summary_counts_distinct_days_for_take_order_reposition_and_wait(self) -> None:
        event_log = [
            {
                "step": 1,
                "action": "take_order",
                "action_end_minutes": 60,
                "position_after": {"lat": 23.13, "lng": 113.26},
            },
            {
                "step": 2,
                "action": "reposition",
                "action_end_minutes": 120,
                "position_after": {"lat": 23.1301, "lng": 113.2601},
            },
            {
                "step": 3,
                "action": "reposition",
                "action_end_minutes": 1500,
                "position_after": {"lat": 23.13, "lng": 113.26},
            },
            {
                "step": 4,
                "action": "wait",
                "action_end_minutes": 2940,
                "position_after": {"lat": 23.13, "lng": 113.26},
            },
        ]

        summaries = build_history_geo_summary(
            preference_context=_preference_context(),
            event_log=event_log,
            current_minutes=2940,
        )

        summary = summaries[0]
        self.assertTrue(summary["visited_today"])
        self.assertEqual(summary["current_visit_count"], 3)
        self.assertEqual(summary["remaining_visit_count"], 2)
        self.assertEqual(summary["matched_history_events"], [1, 2, 3, 4])

    def test_reposition_option_only_when_needed_and_no_candidate_contributes(self) -> None:
        history_summary = [
            {
                "pref_id": "pref_visit",
                "visited_today": False,
                "remaining_visit_count": 1,
                "target": {"center": [23.13, 113.26], "radius_km": 1, "lat_range": None, "lng_range": None},
                "summary": "今天尚未到访。",
            }
        ]
        status = {"current_lat": 22.54, "current_lng": 114.06}

        options = build_geo_reposition_options(
            history_geo_summary=history_summary,
            scored_candidates=[],
            status=status,
            speed_km_per_hour=60.0,
        )

        self.assertEqual(len(options), 1)
        self.assertEqual(options[0]["action"], "reposition")
        action_options = ActionOptionBuilder(top_k=0).build([], reposition_options=options)
        self.assertEqual(
            ActionValidator().validate({"action": "reposition", "params": options[0]["params"]}, action_options),
            {"action": "reposition", "params": options[0]["params"]},
        )

        contributing_candidate = {
            "tool_evidence": {
                "candidate_geo_contribution_result": [{"pref_id": "pref_visit", "can_contribute": True}]
            }
        }
        no_options = build_geo_reposition_options(
            history_geo_summary=history_summary,
            scored_candidates=[contributing_candidate],
            status=status,
            speed_km_per_hour=60.0,
        )
        self.assertEqual(no_options, [])


if __name__ == "__main__":
    unittest.main()
