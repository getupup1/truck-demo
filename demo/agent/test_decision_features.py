from __future__ import annotations

import json
import math
import sys
import time
import unittest
from pathlib import Path
from typing import Any

import requests

AGENT_DIR = Path(__file__).resolve().parent
DEMO_ROOT = AGENT_DIR.parent
if str(DEMO_ROOT) not in sys.path:
    sys.path.insert(0, str(DEMO_ROOT))

from agent.decision_features import format_simulation_time, haversine_km, parse_wall_time_minutes
from agent.model_decision_service import ModelDecisionService


DATA_DIR = DEMO_ROOT / "server" / "data"
CONFIG_PATH = DEMO_ROOT / "server" / "config" / "config.json"
DRIVERS_PATH = DATA_DIR / "drivers.json"
CARGO_DATASET_PATH = DATA_DIR / "cargo_dataset.jsonl"

RESULT_DIR = AGENT_DIR / "result"
PREFERENCE_OUTPUT_PATH = RESULT_DIR / "preference_classification.json"
PROMPT_OUTPUT_PATH = RESULT_DIR / "model_prompts.json"
OLD_TEST_LOG_PATH = AGENT_DIR / "test_log.json"

ENABLE_REAL_LLM_TRACE = True
REAL_LLM_MAX_STEPS_PER_DRIVER = 40
REAL_LLM_PROGRESS_PRINT = True

ONE_DAY_END_MINUTES = 24 * 60
CARGO_VIEW_BATCH_SIZE = 10
NEAREST_CARGO_LIMIT = 100
REPOSITION_SPEED_KM_PER_HOUR = 60.0


def _progress(message: str) -> None:
    if REAL_LLM_PROGRESS_PRINT:
        print(message, flush=True)


def _load_model_config() -> dict[str, Any]:
    raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return {
        "model_api_url": raw["model_api_url"],
        "model_api_key": str(raw.get("model_api_key", "")).strip(),
        "model_name": raw["model_name"],
        "model_timeout_seconds": float(raw.get("model_timeout_seconds", 120)),
    }


def _load_driver_profile(driver_id: str) -> dict[str, Any]:
    drivers = json.loads(DRIVERS_PATH.read_text(encoding="utf-8"))
    for driver in drivers:
        if driver.get("driver_id") == driver_id:
            return driver
    raise AssertionError(f"driver_id not found: {driver_id}")


def _load_cargo_until(end_minutes: int) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with CARGO_DATASET_PATH.open(encoding="utf-8") as f:
        for line in f:
            row = line.strip()
            if not row:
                continue
            cargo = json.loads(row)
            create_minutes = parse_wall_time_minutes(cargo.get("create_time"))
            if create_minutes is None:
                continue
            if create_minutes >= end_minutes:
                break
            records.append(cargo)
    return records


def _normalize_cargo_for_query(cargo: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(cargo)
    if "price" in normalized:
        normalized["price"] = round(float(normalized["price"]) / 100.0, 2)
    return normalized


def _distance_to_minutes(distance_km: float) -> int:
    if distance_km <= 0:
        return 1
    return max(1, math.ceil(distance_km / REPOSITION_SPEED_KM_PER_HOUR * 60.0))


def _pickup_minutes(distance_km: float) -> int:
    if distance_km <= 1e-6:
        return 0
    return _distance_to_minutes(distance_km)


def _load_window_minutes(cargo: dict[str, Any]) -> tuple[int, int] | None:
    raw = cargo.get("load_time")
    if not isinstance(raw, list) or len(raw) != 2:
        return None
    start = parse_wall_time_minutes(raw[0])
    end = parse_wall_time_minutes(raw[1])
    if start is None or end is None or end < start:
        return None
    return start, end


def _visible_preferences(driver: dict[str, Any], current_minutes: int) -> list[Any]:
    out: list[Any] = []
    for pref in driver.get("preferences", []):
        if not isinstance(pref, dict):
            out.append(pref)
            continue
        start = parse_wall_time_minutes(pref.get("start_time"))
        end = parse_wall_time_minutes(pref.get("end_time"))
        if start is None or end is None or start <= current_minutes <= end:
            out.append(pref)
    return out


def _compact_usage(data: dict[str, Any]) -> dict[str, int]:
    usage = data.get("usage", {}) if isinstance(data.get("usage"), dict) else {}
    return {
        "prompt_tokens": int(usage.get("prompt_tokens", 0) or 0),
        "completion_tokens": int(usage.get("completion_tokens", 0) or 0),
        "total_tokens": int(usage.get("total_tokens", 0) or 0),
    }


def _model_content(data: dict[str, Any]) -> str:
    choices = data.get("choices", [])
    if not isinstance(choices, list) or not choices:
        return ""
    message = choices[0].get("message", {})
    content = message.get("content")
    return content if isinstance(content, str) else ""


def _parse_content_object(data: dict[str, Any]) -> dict[str, Any]:
    content = _model_content(data)
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return {"raw_content": content}
    return parsed if isinstance(parsed, dict) else {"raw_content": content}


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


class RealTraceEnvironment:
    def __init__(self, *, driver: dict[str, Any], cargos: list[dict[str, Any]], model_config: dict[str, Any]) -> None:
        self.driver = driver
        self.driver_id = str(driver["driver_id"])
        self.driver_name = str(driver.get("name", ""))
        self.current_lat = float(driver["current_lat"])
        self.current_lng = float(driver["current_lng"])
        self.progress_minutes = 0
        self.completed_order_count = 0
        self.history: list[dict[str, Any]] = []
        self.taken_cargo_ids: set[str] = set()
        self.cargo_by_id = {str(cargo["cargo_id"]): cargo for cargo in cargos}
        self.model_config = model_config
        self.session = requests.Session()
        self.session.trust_env = False
        self.preference_records: list[dict[str, Any]] = []
        self.prompt_records: list[dict[str, Any]] = []

    def get_driver_status(self, driver_id: str) -> dict[str, Any]:
        return {
            "driver_id": driver_id,
            "name": self.driver_name,
            "vehicle_no": self.driver.get("vehicle_no", ""),
            "truck_length": self.driver.get("truck_length", ""),
            "preferences": _visible_preferences(self.driver, self.progress_minutes),
            "current_lat": self.current_lat,
            "current_lng": self.current_lng,
            "simulation_started": True,
            "simulation_progress_minutes": self.progress_minutes,
            "simulation_wall_time": format_simulation_time(self.progress_minutes),
            "current_order_cargo_id": None,
            "completed_order_count": self.completed_order_count,
        }

    def query_cargo(self, driver_id: str, latitude: float, longitude: float) -> dict[str, Any]:
        online: list[tuple[float, dict[str, Any]]] = []
        for cargo_id, cargo in self.cargo_by_id.items():
            if cargo_id in self.taken_cargo_ids:
                continue
            create_minutes = parse_wall_time_minutes(cargo.get("create_time"))
            remove_minutes = parse_wall_time_minutes(cargo.get("remove_time"))
            if create_minutes is None or remove_minutes is None:
                continue
            if not (create_minutes <= self.progress_minutes <= remove_minutes):
                continue
            start = cargo.get("start") or {}
            distance_km = haversine_km(float(latitude), float(longitude), float(start["lat"]), float(start["lng"]))
            online.append((distance_km, cargo))
        online.sort(key=lambda item: item[0])
        selected = online[:NEAREST_CARGO_LIMIT]
        scan_minutes = math.ceil(len(selected) / CARGO_VIEW_BATCH_SIZE) if selected else 0
        self.progress_minutes += scan_minutes
        return {
            "driver_id": driver_id,
            "items": [
                {"distance_km": distance_km, "cargo": _normalize_cargo_for_query(cargo)}
                for distance_km, cargo in selected
            ],
        }

    def query_decision_history(self, driver_id: str, step: int) -> dict[str, Any]:
        if step == -1:
            records = list(self.history)
        elif step <= 0:
            records = []
        else:
            records = self.history[-step:]
        return {
            "driver_id": driver_id,
            "total_steps": len(self.history),
            "step_param": step,
            "returned_count": len(records),
            "records": records,
        }

    def model_chat_completion(self, payload: dict[str, Any]) -> dict[str, Any]:
        system_content = payload["messages"][0]["content"]
        user_content = payload["messages"][1]["content"]
        prompt = json.loads(user_content)
        if "司机偏好解析器" in system_content:
            kind = "preference_classification"
        else:
            kind = "decision"

        call_step = len(self.history) + 1
        _progress(
            "[llm:start] "
            f"driver={self.driver_id} step={call_step} kind={kind} "
            f"prompt_chars={len(user_content)}"
        )
        body = dict(payload)
        body.setdefault("model", self.model_config["model_name"])
        started = time.perf_counter()
        try:
            response = self.session.post(
                str(self.model_config["model_api_url"]),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.model_config['model_api_key']}",
                },
                json=body,
                timeout=float(self.model_config["model_timeout_seconds"]),
            )
            elapsed = round(time.perf_counter() - started, 3)
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            elapsed = round(time.perf_counter() - started, 3)
            _progress(
                "[llm:error] "
                f"driver={self.driver_id} step={call_step} kind={kind} "
                f"elapsed={elapsed}s error={type(exc).__name__}: {exc}"
            )
            raise

        output = _parse_content_object(data)
        if kind == "preference_classification":
            self.preference_records.append(
                {
                    "driver_id": self.driver_id,
                    "driver_name": self.driver_name,
                    "step": call_step,
                    "simulation_progress_minutes": self.progress_minutes,
                    "simulation_wall_time": format_simulation_time(self.progress_minutes),
                    "active_preferences": prompt.get("preferences", []),
                    "classification_output": output,
                }
            )
        else:
            self.prompt_records.append(
                {
                    "driver_id": self.driver_id,
                    "driver_name": self.driver_name,
                    "step": call_step,
                    "simulation_progress_minutes": self.progress_minutes,
                    "simulation_wall_time": format_simulation_time(self.progress_minutes),
                    "prompt": prompt,
                    "action": output,
                    "usage": _compact_usage(data),
                }
            )

        _progress(
            "[llm:done] "
            f"driver={self.driver_id} step={call_step} kind={kind} "
            f"elapsed={elapsed}s"
        )
        return data

    def set_last_decision_action(self, action: dict[str, Any]) -> None:
        if self.prompt_records:
            self.prompt_records[-1]["action"] = action

    def apply_action(self, action: dict[str, Any]) -> dict[str, Any]:
        name = str(action.get("action", "")).strip().lower()
        params = action.get("params") if isinstance(action.get("params"), dict) else {}
        if name == "take_order":
            return self._apply_take_order(str(params.get("cargo_id", "")).strip())
        if name == "reposition":
            latitude = float(params["latitude"])
            longitude = float(params["longitude"])
            distance_km = haversine_km(self.current_lat, self.current_lng, latitude, longitude)
            duration = _distance_to_minutes(distance_km)
            self.current_lat = latitude
            self.current_lng = longitude
            self.progress_minutes += duration
            return {
                "current_lat": latitude,
                "current_lng": longitude,
                "simulation_progress_minutes": self.progress_minutes,
                "simulation_wall_time": format_simulation_time(self.progress_minutes),
                "distance_km": round(distance_km, 2),
            }
        duration = max(1, int(params.get("duration_minutes", 60)))
        remaining = max(0, ONE_DAY_END_MINUTES - self.progress_minutes)
        self.progress_minutes += min(duration, remaining)
        return {
            "simulation_progress_minutes": self.progress_minutes,
            "simulation_wall_time": format_simulation_time(self.progress_minutes),
        }

    def append_history(
        self,
        *,
        step_start_minutes: int,
        before_status: dict[str, Any],
        action: dict[str, Any],
        progress_after_decision: int,
        result: dict[str, Any],
        after_status: dict[str, Any],
    ) -> None:
        step_end = int(after_status["simulation_progress_minutes"])
        self.history.append(
            {
                "step": len(self.history) + 1,
                "driver_id": self.driver_id,
                "step_elapsed_minutes": step_end - step_start_minutes,
                "query_scan_cost_minutes": progress_after_decision - step_start_minutes,
                "action_exec_cost_minutes": step_end - progress_after_decision,
                "position_before": {
                    "lat": float(before_status["current_lat"]),
                    "lng": float(before_status["current_lng"]),
                },
                "position_after": {
                    "lat": float(after_status["current_lat"]),
                    "lng": float(after_status["current_lng"]),
                },
                "simulation_end_time": after_status["simulation_wall_time"],
                "action": action,
                "token_usage": {},
                "result": result,
            }
        )

    def _apply_take_order(self, cargo_id: str) -> dict[str, Any]:
        cargo = self.cargo_by_id.get(cargo_id)
        if cargo is None or cargo_id in self.taken_cargo_ids:
            return {
                "accepted": False,
                "detail": "cargo_id_not_available",
                "driver_id": self.driver_id,
                "cargo_id": cargo_id,
                "simulation_progress_minutes": self.progress_minutes,
                "simulation_wall_time": format_simulation_time(self.progress_minutes),
                "pickup_deadhead_km": 0,
                "haul_distance_km": 0,
            }

        self.taken_cargo_ids.add(cargo_id)
        start = cargo.get("start") or {}
        end = cargo.get("end") or {}
        start_lat = float(start["lat"])
        start_lng = float(start["lng"])
        end_lat = float(end["lat"])
        end_lng = float(end["lng"])
        pickup_deadhead_km = haversine_km(self.current_lat, self.current_lng, start_lat, start_lng)
        self.current_lat = start_lat
        self.current_lng = start_lng
        self.progress_minutes += _pickup_minutes(pickup_deadhead_km)

        load_window = _load_window_minutes(cargo)
        if load_window is not None:
            load_start, load_end = load_window
            if self.progress_minutes > load_end:
                return {
                    "accepted": False,
                    "detail": "load_time_window_expired",
                    "driver_id": self.driver_id,
                    "cargo_id": cargo_id,
                    "simulation_progress_minutes": self.progress_minutes,
                    "simulation_wall_time": format_simulation_time(self.progress_minutes),
                    "pickup_deadhead_km": round(pickup_deadhead_km, 2),
                    "haul_distance_km": 0,
                }
            if self.progress_minutes < load_start:
                self.progress_minutes = load_start

        self.progress_minutes += int(cargo.get("cost_time_minutes", 0) or 0)
        self.current_lat = end_lat
        self.current_lng = end_lng
        self.completed_order_count += 1
        return {
            "accepted": True,
            "detail": "trace_env_take_order_completed",
            "driver_id": self.driver_id,
            "cargo_id": cargo_id,
            "simulation_progress_minutes": self.progress_minutes,
            "simulation_wall_time": format_simulation_time(self.progress_minutes),
            "pickup_deadhead_km": round(pickup_deadhead_km, 2),
            "haul_distance_km": round(haversine_km(start_lat, start_lng, end_lat, end_lng), 2),
            "income_eligible": self.progress_minutes <= ONE_DAY_END_MINUTES,
        }


class RealLlmTraceTest(unittest.TestCase):
    @unittest.skipUnless(ENABLE_REAL_LLM_TRACE, "set ENABLE_REAL_LLM_TRACE=True in this file to call the real LLM")
    def test_real_llm_one_day_trace_for_d001_and_d009(self) -> None:
        model_config = _load_model_config()
        if not model_config["model_api_key"]:
            self.skipTest("model api key is missing")
        RESULT_DIR.mkdir(parents=True, exist_ok=True)
        if OLD_TEST_LOG_PATH.exists():
            OLD_TEST_LOG_PATH.unlink()

        all_preference_records: list[dict[str, Any]] = []
        all_prompt_records: list[dict[str, Any]] = []
        cargos = _load_cargo_until(ONE_DAY_END_MINUTES)
        _progress(
            "[trace:start] "
            f"drivers=D001,D009 max_steps_per_driver={REAL_LLM_MAX_STEPS_PER_DRIVER}"
        )
        try:
            for driver_id in ("D001", "D009"):
                driver = _load_driver_profile(driver_id)
                env = RealTraceEnvironment(driver=driver, cargos=cargos, model_config=model_config)
                service = ModelDecisionService(env)
                _progress(f"[driver:start] driver={driver_id} name={driver.get('name')}")

                while env.progress_minutes < ONE_DAY_END_MINUTES and len(env.history) < REAL_LLM_MAX_STEPS_PER_DRIVER:
                    step_start_minutes = env.progress_minutes
                    before_status = env.get_driver_status(driver_id)
                    _progress(
                        "[step:start] "
                        f"driver={driver_id} step={len(env.history) + 1} "
                        f"sim_min={step_start_minutes} wall={before_status['simulation_wall_time']}"
                    )
                    try:
                        action = service.decide(driver_id)
                    except Exception as exc:  # noqa: BLE001
                        _progress(
                            "[step:error] "
                            f"driver={driver_id} step={len(env.history) + 1} "
                            f"error={type(exc).__name__}: {exc}"
                        )
                        break
                    env.set_last_decision_action(action)
                    _progress(
                        "[action] "
                        f"driver={driver_id} step={len(env.history) + 1} "
                        f"action={action.get('action')} params={action.get('params')}"
                    )
                    progress_after_decision = env.progress_minutes
                    result = env.apply_action(action)
                    after_status = env.get_driver_status(driver_id)
                    env.append_history(
                        step_start_minutes=step_start_minutes,
                        before_status=before_status,
                        action=action,
                        progress_after_decision=progress_after_decision,
                        result=result,
                        after_status=after_status,
                    )
                    _progress(
                        "[step:done] "
                        f"driver={driver_id} step={len(env.history)} "
                        f"sim_min={step_start_minutes}->{env.progress_minutes} "
                        f"result={result.get('detail') or result.get('simulation_wall_time')}"
                    )

                all_preference_records.extend(env.preference_records)
                all_prompt_records.extend(env.prompt_records)
                _write_json(PREFERENCE_OUTPUT_PATH, all_preference_records)
                _write_json(PROMPT_OUTPUT_PATH, all_prompt_records)
                _progress(
                    "[driver:done] "
                    f"driver={driver_id} steps={len(env.history)} "
                    f"final_min={env.progress_minutes}"
                )
        finally:
            _write_json(PREFERENCE_OUTPUT_PATH, all_preference_records)
            _write_json(PROMPT_OUTPUT_PATH, all_prompt_records)
            _progress(f"[trace:done] wrote {PREFERENCE_OUTPUT_PATH} and {PROMPT_OUTPUT_PATH}")

        self.assertGreater(len(all_preference_records), 0)
        self.assertGreater(len(all_prompt_records), 0)


if __name__ == "__main__":
    unittest.main()
