from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from typing import Any


DEMO_ROOT = Path(__file__).resolve().parents[2]
if str(DEMO_ROOT) not in sys.path:
    sys.path.insert(0, str(DEMO_ROOT))

from agent.preferences import DSL_TYPES, PREFERENCE_IR_FIELDS, PreferenceCompiler
from agent.preferences.prompts import second_stage_system_prompt, stage1_system_prompt, supported_second_stage_dsl_types


class FakeModel:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def __call__(self, payload: dict[str, Any]) -> dict[str, Any]:
        prompt = payload["messages"][0]["content"]
        self.prompts.append(prompt)
        if "第一阶段编译器" in prompt:
            content = {
                "items": [
                    {
                        "item_id": "pref_0_0",
                        "source_text": "每天23点前车辆须在自家位置（23.12，113.28）一公里内",
                        "parent_source_text": "每天23点前车辆须在自家位置（23.12，113.28）一公里内；当天23点至次日8点不接单、不空跑。",
                        "ir_type": "constraint",
                        "dsl_type": "deadline_location",
                        "start_time": "2026-03-01 00:00:00",
                        "end_time": "2026-03-31 23:59:59",
                        "penalty_amount": 300,
                        "penalty_cap": 3000,
                        "reason": "该子偏好要求每天在固定时间前到达固定地点",
                        "confidence": 0.95,
                    },
                    {
                        "item_id": "pref_0_1",
                        "source_text": "当天23点至次日8点不接单、不空跑",
                        "parent_source_text": "每天23点前车辆须在自家位置（23.12，113.28）一公里内；当天23点至次日8点不接单、不空跑。",
                        "ir_type": "constraint",
                        "dsl_type": "time_window_action_limit",
                        "start_time": "2026-03-01 00:00:00",
                        "end_time": "2026-03-31 23:59:59",
                        "penalty_amount": 300,
                        "penalty_cap": 3000,
                        "reason": "该子偏好禁止 take_order 和 reposition 在固定日内时间窗口发生",
                        "confidence": 0.95,
                    },
                ]
            }
        elif "dsl_type=deadline_location" in prompt:
            content = {"preferences": [_preference_ir("pref_0_0", "deadline_location")]}
        elif "dsl_type=time_window_action_limit" in prompt:
            content = {"preferences": [_preference_ir("pref_0_1", "time_window_action_limit")]}
        else:
            content = {"preferences": []}
        return {"choices": [{"message": {"content": json.dumps(content, ensure_ascii=False)}}]}


def _preference_ir(pref_id: str, dsl_type: str) -> dict[str, Any]:
    return {
        "pref_id": pref_id,
        "source_text": "x",
        "ir_type": "constraint",
        "dsl_type": dsl_type,
        "active_window": {"start_minutes": 0, "end_minutes": 44639},
        "event_filter": {"action_types": ["take_order"], "status": ["accepted", "simulated_success"]},
        "enforcement": {
            "penalty_unit": "per_event",
            "penalty_amount": 300,
            "penalty_cap": 3000,
            "boost_amount": 0,
        },
        "condition": {},
        "target": {},
        "scope": {},
        "confidence": 0.95,
        "meta": {"parent_source_text": "p", "reason": "r", "warnings": []},
    }


class PreferenceDslTests(unittest.TestCase):
    def test_two_stage_compiler_groups_by_dsl_type(self) -> None:
        fake = FakeModel()
        out = PreferenceCompiler(fake).compile(
            driver_id="D009",
            preferences=[
                {
                    "content": "每天23点前车辆须在自家位置（23.12，113.28）一公里内；当天23点至次日8点不接单、不空跑。",
                    "start_time": "2026-03-01 00:00:00",
                    "end_time": "2026-03-31 23:59:59",
                    "penalty_amount": 300,
                    "penalty_cap": 3000,
                }
            ],
            status={"simulation_progress_minutes": 0},
        )
        self.assertEqual(["deadline_location", "time_window_action_limit"], [item["dsl_type"] for item in out])
        self.assertTrue(any("第一阶段编译器" in prompt for prompt in fake.prompts))
        self.assertTrue(any("dsl_type=deadline_location" in prompt for prompt in fake.prompts))
        self.assertTrue(any("dsl_type=time_window_action_limit" in prompt for prompt in fake.prompts))

    def test_preference_ir_has_exact_top_level_fields(self) -> None:
        item = _preference_ir("pref_0_0", "attribute_match")
        self.assertEqual(set(PREFERENCE_IR_FIELDS), set(item))
        self.assertNotIn("version", item)
        self.assertNotIn("mode", item["enforcement"])

    def test_prompts_cover_supported_dsl_types_only(self) -> None:
        self.assertEqual(tuple(DSL_TYPES), supported_second_stage_dsl_types())
        for dsl_type in DSL_TYPES:
            prompt = second_stage_system_prompt(dsl_type)
            self.assertIn(f"dsl_type={dsl_type}", prompt)
            self.assertNotIn("specific_task", prompt)
            self.assertNotIn("sequence_workflow", prompt)
        self.assertNotIn("specific_task", stage1_system_prompt())
        self.assertNotIn("sequence_workflow", stage1_system_prompt())


if __name__ == "__main__":
    unittest.main()
