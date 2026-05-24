from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from typing import Any

import requests


DEMO_ROOT = Path(__file__).resolve().parents[2]
SERVER_ROOT = DEMO_ROOT / "server"
if str(DEMO_ROOT) not in sys.path:
    sys.path.insert(0, str(DEMO_ROOT))

from agent.candidates.enhancer import enhance_cargo_candidates
from agent.candidates.filtering import filter_basic_candidates
from agent.model_decision_service import ModelDecisionService
from agent.preferences.brief_extractor import PreferenceBriefExtractor
from agent.preferences.judge import LLMPreferenceJudge
from agent.preferences.tool_planner import PreferenceToolPlanner


class RealModelApi:
    def __init__(self) -> None:
        config_path = SERVER_ROOT / "config" / "config.json"
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        self._api_url = str(raw["model_api_url"])
        self._api_key = os.environ.get("DASHSCOPE_API_KEY") or os.environ.get("TIANCHI_MODEL_API_KEY") or str(
            raw["model_api_key"]
        )
        self._model_name = str(raw["model_name"])
        self._timeout = float(raw.get("model_timeout_seconds", 180))
        self._session = requests.Session()
        self._session.trust_env = False
        self.calls = 0
        self.last_payload: dict[str, Any] | None = None

    def model_chat_completion(self, payload: dict[str, Any]) -> dict[str, Any]:
        body = dict(payload)
        body.setdefault("model", self._model_name)
        self.calls += 1
        self.last_payload = payload
        response = self._session.post(
            self._api_url,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
            },
            json=body,
            timeout=self._timeout,
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("model response is not a JSON object")
        return data

    def close(self) -> None:
        self._session.close()


def _status(progress: int = 0) -> dict[str, Any]:
    return {
        "driver_id": "T001",
        "current_lat": 22.54,
        "current_lng": 114.06,
        "truck_length": "4.2米",
        "completed_order_count": 0,
        "simulation_progress_minutes": progress,
        "simulation_wall_time": "2026-03-01 00:00:00" if progress == 0 else "2026-03-01 00:10:00",
        "preferences": [
            {
                "content": "不接货源品类为「蔬菜」的订单。",
                "start_time": "2026-03-01 00:00:00",
                "end_time": "2026-03-31 23:59:59",
                "penalty_amount": 350,
                "penalty_cap": 3500,
            },
            {
                "content": "每天至少连续停车休息满3小时。",
                "start_time": "2026-03-01 00:00:00",
                "end_time": "2026-03-31 23:59:59",
                "penalty_amount": 300,
                "penalty_cap": 6000,
            },
        ],
    }


def _items(count: int = 6) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    names = ["蔬菜", "数码家电", "水果", "机械设备", "空包装", "食品饮料"]
    for idx in range(count):
        out.append(
            {
                "distance_km": float(idx + 1),
                "cargo": {
                    "cargo_id": f"C{idx + 1:03d}",
                    "cargo_name": names[idx % len(names)],
                    "price": 1000 + idx,
                    "remove_time": "2026-03-01 03:00:00",
                    "load_time": ["2026-03-01 01:00:00", "2026-03-01 04:00:00"],
                    "cost_time_minutes": 30 + idx,
                    "start": {"lat": 22.54 + idx * 0.01, "lng": 114.06, "city": "深圳市"},
                    "end": {"lat": 22.60 + idx * 0.01, "lng": 114.10, "city": "深圳市"},
                },
            }
        )
    return out


class RealLlmPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.api = RealModelApi()

    def tearDown(self) -> None:
        self.api.close()

    def test_preference_brief_extractor_uses_real_llm_and_cache(self) -> None:
        extractor = PreferenceBriefExtractor(self.api.model_chat_completion)

        extracted = extractor.extract_for_status("T001", _status())
        extracted_again = extractor.extract_for_status("T001", _status())

        self.assertGreaterEqual(len(extracted["preference_brief"]), 1)
        self.assertEqual(extracted["preference_signature"], extracted_again["preference_signature"])
        self.assertNotIn("rules", extracted)
        self.assertNotIn("categories", extracted)
        self.assertNotIn("constraint_type", json.dumps(extracted, ensure_ascii=False))
        self.assertTrue(all(isinstance(item.get("tools"), list) for item in extracted["preference_brief"]))
        self.assertTrue(all(isinstance(value, str) for item in extracted["preference_brief"] for value in item["tools"]))
        self.assertEqual(self.api.calls, 1)

    def test_preference_tool_planner_uses_real_llm_for_geo_tools(self) -> None:
        planner = PreferenceToolPlanner(self.api.model_chat_completion)
        context = {
            "driver_id": "T001",
            "preference_brief": [
                {
                    "pref_id": "pref_0",
                    "source_text": "车辆不得进入以（23.30，113.52）为圆心、半径20公里的区域。",
                    "core_requirement": {
                        "time": None,
                        "action": "车辆位置",
                        "location": "以（23.30，113.52）为圆心、半径20公里的区域",
                        "value": "20公里",
                        "requirement": "车辆不得进入指定圆形区域。",
                    },
                    "penalty_amount": 1000,
                    "penalty_cap": 1000,
                    "needs_history": False,
                    "tools": ["geo_checks"],
                }
            ],
        }

        tool_plan = planner.plan_for_context(driver_id="T001", status=_status(progress=10), preference_context=context)

        tools = tool_plan["pref_0"]
        self.assertIn("geo_checks", tools)
        self.assertEqual(tools["geo_checks"]["relation"], "forbidden_inside")
        self.assertEqual(tools["geo_checks"]["center"], [23.3, 113.52])
        self.assertEqual(tools["geo_checks"]["radius_km"], 20.0)

    def test_preference_judge_uses_real_llm_in_batches_of_five(self) -> None:
        extractor = PreferenceBriefExtractor(self.api.model_chat_completion)
        preference_context = extractor.extract_for_status("T001", _status(progress=10))
        enhanced = enhance_cargo_candidates(_items(6), _status(progress=10), speed_km_per_hour=60.0)
        candidates, _summary = filter_basic_candidates(enhanced)
        judge = LLMPreferenceJudge(self.api.model_chat_completion, batch_size=5)

        judged = judge.judge_candidates(
            driver_id="T001",
            status=_status(progress=10),
            preference_context=preference_context,
            history_summary={"today": {}, "month": {}},
            history_slice=None,
            candidates=candidates,
        )

        self.assertEqual(len(judged), 6)
        self.assertEqual(self.api.calls, 3)
        self.assertTrue(all("preference_evaluation" in item for item in judged))
        self.assertTrue(all("violated_preferences" in item["preference_evaluation"] for item in judged))
        self.assertTrue(all("preference_penalty" in item["preference_evaluation"] for item in judged))
        self.assertIsNotNone(self.api.last_payload)
        assert self.api.last_payload is not None
        user_payload = self.api.last_payload["messages"][1]["content"]
        self.assertIn("preference_brief", user_payload)
        self.assertIn("history_summary", user_payload)
        self.assertIn("history_slice", user_payload)
        self.assertIn("simulated_events", user_payload)
        self.assertIn("tool_evidence", user_payload)
        self.assertNotIn("compiled_preferences", user_payload)

    def test_decide_smoke_returns_valid_action_with_real_llm(self) -> None:
        port = RealLlmDecisionPort(self.api)
        service = ModelDecisionService(port)

        action = service.decide("T001")

        self.assertIn(action["action"], {"take_order", "wait", "reposition"})
        if action["action"] == "take_order":
            self.assertIn(action["params"]["cargo_id"], {"C001", "C002", "C003"})
        elif action["action"] == "wait":
            self.assertIn(action["params"]["duration_minutes"], {10, 30, 60, 180, 480})
        else:
            self.assertIn("latitude", action["params"])
            self.assertIn("longitude", action["params"])
        self.assertGreaterEqual(self.api.calls, 3)


class RealLlmDecisionPort:
    def __init__(self, model_api: RealModelApi) -> None:
        self._model_api = model_api
        self._progress = 0

    def get_driver_status(self, driver_id: str) -> dict[str, Any]:
        status = _status(progress=self._progress)
        status["driver_id"] = driver_id
        return status

    def query_cargo(self, driver_id: str, latitude: float, longitude: float) -> dict[str, Any]:
        self._progress = 10
        return {"driver_id": driver_id, "items": _items(3)}

    def query_decision_history(self, driver_id: str, step: int) -> dict[str, Any]:
        return {
            "driver_id": driver_id,
            "total_steps": 0,
            "step_param": step,
            "returned_count": 0,
            "records": [],
        }

    def model_chat_completion(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._model_api.model_chat_completion(payload)


if __name__ == "__main__":
    unittest.main()
