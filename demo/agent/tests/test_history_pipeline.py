from __future__ import annotations

import sys
import unittest
from pathlib import Path


DEMO_ROOT = Path(__file__).resolve().parents[2]
if str(DEMO_ROOT) not in sys.path:
    sys.path.insert(0, str(DEMO_ROOT))

from agent.decision_features import summarize_decision_history
from agent.history.history import HistorySliceBuilder, build_event_log, build_history_summary


def _history_records() -> list[dict]:
    return [
        {
            "step": 1,
            "step_elapsed_minutes": 70,
            "query_scan_cost_minutes": 10,
            "action_exec_cost_minutes": 60,
            "position_before": {"lat": 22.54, "lng": 114.06},
            "position_after": {"lat": 22.60, "lng": 114.10},
            "action": {"action": "take_order", "params": {"cargo_id": "C001"}},
            "result": {
                "accepted": True,
                "cargo_id": "C001",
                "simulation_progress_minutes": 70,
                "simulation_wall_time": "2026-03-01 01:10:00",
                "pickup_deadhead_km": 3.0,
                "haul_distance_km": 8.0,
            },
        },
        {
            "step": 2,
            "step_elapsed_minutes": 40,
            "query_scan_cost_minutes": 10,
            "action_exec_cost_minutes": 30,
            "position_before": {"lat": 22.60, "lng": 114.10},
            "position_after": {"lat": 22.60, "lng": 114.10},
            "action": {"action": "wait", "params": {"duration_minutes": 30}},
            "result": {
                "simulation_progress_minutes": 110,
                "simulation_wall_time": "2026-03-01 01:50:00",
            },
        },
        {
            "step": 3,
            "step_elapsed_minutes": 1400,
            "query_scan_cost_minutes": 10,
            "action_exec_cost_minutes": 1390,
            "position_before": {"lat": 22.60, "lng": 114.10},
            "position_after": {"lat": 22.70, "lng": 114.20},
            "action": {"action": "take_order", "params": {"cargo_id": "C002"}},
            "result": {
                "accepted": True,
                "cargo_id": "C002",
                "simulation_progress_minutes": 1510,
                "simulation_wall_time": "2026-03-02 01:10:00",
                "pickup_deadhead_km": 4.0,
                "haul_distance_km": 10.0,
            },
        },
    ]


class HistoryPipelineTests(unittest.TestCase):
    def test_history_summary_matches_existing_decision_features_shape_and_values(self) -> None:
        history_resp = {"records": _history_records(), "total_steps": 3}
        event_log = build_event_log(history_resp)

        new_summary = build_history_summary(event_log, current_minutes=1510)
        old_summary = summarize_decision_history(history_resp, current_minutes=1510)

        self.assertEqual(new_summary["today"]["successful_take_order_count"], old_summary["today"]["successful_take_order_count"])
        self.assertEqual(new_summary["today"]["deadhead_km"], old_summary["today"]["deadhead_km"])
        self.assertEqual(new_summary["month"]["successful_take_order_count"], old_summary["month"]["successful_take_order_count"])
        self.assertEqual(new_summary["month"]["deadhead_km"], old_summary["month"]["deadhead_km"])
        self.assertNotIn("recent", new_summary)
        self.assertNotIn("first_successful_order_start_minutes", new_summary["today"])
        self.assertNotIn("first_successful_order_start_time", new_summary["today"])

    def test_history_summary_distance_metrics_are_split_for_today_and_month(self) -> None:
        event_log = [
            {
                "step": 1,
                "action": "take_order",
                "params": {"cargo_id": "C001"},
                "action_start_minutes": 60,
                "action_start_time": "2026-03-01 01:00:00",
                "action_end_minutes": 120,
                "action_end_time": "2026-03-01 02:00:00",
                "result": {
                    "accepted": True,
                    "cargo_id": "C001",
                    "pickup_deadhead_km": 3.0,
                    "haul_distance_km": 8.0,
                },
            },
            {
                "step": 2,
                "action": "take_order",
                "params": {"cargo_id": "C002"},
                "action_start_minutes": 1450,
                "action_start_time": "2026-03-02 00:10:00",
                "action_end_minutes": 1510,
                "action_end_time": "2026-03-02 01:10:00",
                "result": {
                    "accepted": True,
                    "cargo_id": "C002",
                    "pickup_deadhead_km": 4.0,
                    "haul_distance_km": 10.0,
                },
            },
            {
                "step": 3,
                "action": "take_order",
                "params": {"cargo_id": "C003"},
                "action_start_minutes": 1520,
                "action_start_time": "2026-03-02 01:20:00",
                "action_end_minutes": 1530,
                "action_end_time": "2026-03-02 01:30:00",
                "result": {
                    "accepted": False,
                    "detail": "load_time_missed",
                    "cargo_id": "C003",
                    "pickup_deadhead_km": 2.0,
                    "haul_distance_km": 99.0,
                },
            },
            {
                "step": 4,
                "action": "reposition",
                "params": {},
                "action_start_minutes": 1540,
                "action_start_time": "2026-03-02 01:40:00",
                "action_end_minutes": 1570,
                "action_end_time": "2026-03-02 02:10:00",
                "result": {"distance_km": 5.0},
            },
            {
                "step": 5,
                "action": "wait",
                "params": {"duration_minutes": 30},
                "action_start_minutes": 1570,
                "action_start_time": "2026-03-02 02:10:00",
                "action_end_minutes": 1600,
                "action_end_time": "2026-03-02 02:40:00",
                "result": {},
            },
        ]

        summary = build_history_summary(event_log, current_minutes=1600)

        self.assertEqual(summary["today"]["pickup_deadhead_km"], 4.0)
        self.assertEqual(summary["today"]["reposition_km"], 5.0)
        self.assertEqual(summary["today"]["haul_distance_km"], 10.0)
        self.assertEqual(summary["today"]["failed_take_order_deadhead_km"], 2.0)
        self.assertEqual(summary["today"]["deadhead_km"], 9.0)
        self.assertEqual(summary["today"]["total_driving_km"], 21.0)
        self.assertEqual(summary["month"]["pickup_deadhead_km"], 7.0)
        self.assertEqual(summary["month"]["reposition_km"], 5.0)
        self.assertEqual(summary["month"]["haul_distance_km"], 18.0)
        self.assertEqual(summary["month"]["failed_take_order_deadhead_km"], 2.0)
        self.assertEqual(summary["month"]["deadhead_km"], 12.0)
        self.assertEqual(summary["month"]["total_driving_km"], 32.0)

    def test_history_slice_builder_current_day_and_full_fallback(self) -> None:
        event_log = build_event_log({"records": _history_records()})
        builder = HistorySliceBuilder()

        today_context = builder.build(
            event_log=event_log,
            current_minutes=1510,
            preference_context={
                "preference_brief": [
                    {
                        "pref_id": "pref_0",
                        "needs_history": "需要从 event_log 截取当前自然日 00:00:00 至当前 simulation_wall_time 的历史",
                    }
                ]
            },
        )
        today_slice = today_context["history_slice"]
        self.assertIsNotNone(today_slice)
        assert today_slice is not None
        self.assertEqual(today_slice["period"]["start_time"], "2026-03-02 00:00:00")
        self.assertEqual([event["step"] for event in today_slice["events"]], [3])

        fallback_context = builder.build(
            event_log=event_log,
            current_minutes=1510,
            preference_context={"preference_brief": [{"pref_id": "pref_1", "needs_history": "需要判断一个很奇怪的历史条件"}]},
        )
        fallback_slice = fallback_context["history_slice"]
        self.assertIsNotNone(fallback_slice)
        assert fallback_slice is not None
        self.assertEqual([event["step"] for event in fallback_slice["events"]], [1, 2, 3])

    def test_history_slice_builder_prefers_explicit_wall_time_period(self) -> None:
        event_log = build_event_log({"records": _history_records()})

        context = HistorySliceBuilder().build(
            event_log=event_log,
            current_minutes=1510,
            preference_context={
                "preference_brief": [
                    {
                        "pref_id": "pref_0",
                        "needs_history": (
                            "start_simulation_wall_time=2026-03-02 00:00:00; "
                            "end_simulation_wall_time=2026-03-02 01:10:00; "
                            "required_events=take_order; "
                            "required_fields=action_start_time,cargo_id; "
                            "reason=需要查询当天第一笔订单开始时间"
                        ),
                    }
                ]
            },
        )

        history_slice = context["history_slice"]
        self.assertIsNotNone(history_slice)
        assert history_slice is not None
        self.assertEqual(history_slice["period"]["start_time"], "2026-03-02 00:00:00")
        self.assertEqual(history_slice["period"]["end_time"], "2026-03-02 01:10:00")
        self.assertEqual([event["step"] for event in history_slice["events"]], [3])

    def test_history_slice_builder_returns_none_without_history_need(self) -> None:
        context = HistorySliceBuilder().build(
            event_log=build_event_log({"records": _history_records()}),
            current_minutes=1510,
            preference_context={"preference_brief": [{"pref_id": "pref_0", "needs_history": False}]},
        )

        self.assertIsNone(context["history_slice"])
        self.assertIn("history_summary", context)


if __name__ == "__main__":
    unittest.main()
