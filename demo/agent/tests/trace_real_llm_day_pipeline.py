from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import requests


DEMO_ROOT = Path(__file__).resolve().parents[2]
if str(DEMO_ROOT) not in sys.path:
    sys.path.insert(0, str(DEMO_ROOT))

from agent.actions.decider import LLMActionDecider
from agent.actions.options import ActionOptionBuilder
from agent.actions.validator import ActionValidator
from agent.candidates.enhancer import enhance_cargo_candidates
from agent.candidates.filtering import filter_basic_candidates
from agent.candidates.scorer import score_candidates
from agent.evidence.action_tool_builder import ActionToolBuilder
from agent.evidence.collector import collect_candidate_evidence
from agent.history.history import HistorySliceBuilder, build_event_log
from agent.history.json_utils import extract_model_json_object
from agent.preferences.brief_extractor import PreferenceBriefExtractor
from agent.preferences.judge import LLMPreferenceJudge
from agent.preferences.tool_planner import PreferenceToolPlanner
from server.bench.settings import load_settings
from simkit import simulation_actions
from simkit.cargo_repository import CargoRepository
from simkit.driver_state_manager import DriverStateManager


DEFAULT_DRIVER_IDS = ("D001", "D003")
DEFAULT_HORIZON_MINUTES = 1440


class TraceModelClient:
    def __init__(self, *, api_url: str, api_key: str, model_name: str, timeout_seconds: float) -> None:
        self._api_url = api_url
        self._api_key = api_key
        self._model_name = model_name
        self._timeout = timeout_seconds
        self._session = requests.Session()
        self._session.trust_env = False
        self.calls: list[dict[str, Any]] = []

    def close(self) -> None:
        self._session.close()

    def model_chat_completion(self, payload: dict[str, Any]) -> dict[str, Any]:
        body = dict(payload)
        body.setdefault("model", self._model_name)
        started = time.perf_counter()
        response = self._session.post(
            self._api_url,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self._api_key}"},
            json=body,
            timeout=self._timeout,
        )
        elapsed = round(time.perf_counter() - started, 3)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("model response is not a JSON object")
        self.calls.append(
            {
                "call_index": len(self.calls),
                "phase": _classify_phase(payload),
                "elapsed_seconds": elapsed,
                "request": payload,
                "response": data,
                "parsed_response_json": _try_extract_model_json(data),
                "usage": data.get("usage", {}),
            }
        )
        return data

    def usage_since(self, start_index: int) -> dict[str, int]:
        usage = {"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0, "total_tokens": 0}
        for call in self.calls[start_index:]:
            item = call.get("usage") if isinstance(call.get("usage"), dict) else {}
            usage["prompt_tokens"] += int(item.get("prompt_tokens", 0) or 0)
            usage["completion_tokens"] += int(item.get("completion_tokens", 0) or 0)
            usage["total_tokens"] += int(item.get("total_tokens", 0) or 0)
            details = item.get("completion_tokens_details") if isinstance(item.get("completion_tokens_details"), dict) else {}
            usage["reasoning_tokens"] += int(details.get("reasoning_tokens", 0) or 0)
        return usage


class TraceSimulationPort:
    def __init__(
        self,
        *,
        repo: CargoRepository,
        manager: DriverStateManager,
        model_client: TraceModelClient,
        session_actions_by_driver: dict[str, list[dict[str, Any]]],
        nearest_cargo_limit: int = 100,
        cargo_view_batch_size: int = 10,
    ) -> None:
        self._repo = repo
        self._manager = manager
        self._model_client = model_client
        self._session_actions_by_driver = session_actions_by_driver
        self._nearest_cargo_limit = nearest_cargo_limit
        self._cargo_view_batch_size = cargo_view_batch_size

    def get_driver_status(self, driver_id: str) -> dict[str, Any]:
        return self._manager.get_driver_status(driver_id)

    def query_cargo(self, driver_id: str, latitude: float, longitude: float) -> dict[str, Any]:
        raw = simulation_actions.query_cargo(
            self._repo,
            self._manager,
            driver_id,
            latitude,
            longitude,
            k=self._nearest_cargo_limit,
        )
        items = raw.get("items", [])
        count = len(items) if isinstance(items, list) else 0
        simulation_actions.apply_cargo_query_scan_cost(
            self._repo,
            self._manager,
            driver_id,
            count,
            cargo_view_batch_size=self._cargo_view_batch_size,
        )
        return raw

    def query_decision_history(self, driver_id: str, step: int) -> dict[str, Any]:
        records = list(self._session_actions_by_driver.get(driver_id, []))
        if step == -1:
            out = records
        elif step <= 0:
            out = []
        else:
            out = records[-step:]
        return {
            "driver_id": driver_id,
            "total_steps": len(records),
            "step_param": step,
            "returned_count": len(out),
            "records": out,
        }

    def model_chat_completion(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._model_client.model_chat_completion(payload)


class TraceDecisionPipeline:
    def __init__(
        self,
        port: TraceSimulationPort,
        model_client: TraceModelClient,
        *,
        reposition_speed_km_per_hour: float,
        simulation_horizon_minutes: int,
    ) -> None:
        self._port = port
        self._model_client = model_client
        self._speed = reposition_speed_km_per_hour
        self._horizon = simulation_horizon_minutes
        self._preference_extractor = PreferenceBriefExtractor(port.model_chat_completion)
        self._tool_planner = PreferenceToolPlanner(port.model_chat_completion)
        self._history_slice_builder = HistorySliceBuilder()
        self._preference_judge = LLMPreferenceJudge(port.model_chat_completion, batch_size=5)
        self._action_tool_builder = ActionToolBuilder(speed_km_per_hour=self._speed)
        self._action_option_builder = ActionOptionBuilder(top_k=10)
        self._action_decider = LLMActionDecider(port.model_chat_completion)
        self._action_validator = ActionValidator()

    def decide_with_trace(self, driver_id: str, step: int) -> tuple[dict[str, Any], dict[str, Any]]:
        call_start = len(self._model_client.calls)
        status = self._port.get_driver_status(driver_id)
        lat = float(status["current_lat"])
        lng = float(status["current_lng"])

        cargo_resp = self._port.query_cargo(driver_id=driver_id, latitude=lat, longitude=lng)
        items = cargo_resp.get("items", [])
        if not isinstance(items, list):
            items = []

        status_after_scan = self._port.get_driver_status(driver_id)

        preference_context = self._preference_extractor.extract_for_status(driver_id, status_after_scan)
        tool_plan = self._tool_planner.plan_for_context(
            driver_id=driver_id,
            status=status_after_scan,
            preference_context=preference_context,
        )
        history_resp = self._port.query_decision_history(driver_id, -1)
        event_log = build_event_log(history_resp)
        history_context = self._history_slice_builder.build(
            event_log=event_log,
            current_minutes=int(status_after_scan.get("simulation_progress_minutes", 0) or 0),
            preference_context=preference_context,
        )
        enhanced_candidates = enhance_cargo_candidates(
            items,
            status_after_scan,
            speed_km_per_hour=self._speed,
            simulation_horizon_minutes=self._horizon,
            limit=30,
        )
        filtered_candidates, filter_summary = filter_basic_candidates(enhanced_candidates)
        evidence_candidates = collect_candidate_evidence(
            filtered_candidates,
            preference_context=preference_context,
            tool_plan=tool_plan,
            speed_km_per_hour=self._speed,
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
        action["model_usage"] = self._model_client.usage_since(call_start)

        trace = {
            "step": step,
            "status_before_scan": status,
            "cargo_response": {"driver_id": cargo_resp.get("driver_id"), "items_count": len(items), "items": items},
            "status_after_scan": status_after_scan,
            "preference_context": preference_context,
            "tool_plan": tool_plan,
            "history_response": history_resp,
            "event_log": event_log,
            "history_context": history_context,
            "enhanced_candidates": enhanced_candidates,
            "filtered_candidates": filtered_candidates,
            "filter_summary": filter_summary,
            "evidence_candidates": evidence_candidates,
            "judged_candidates": judged_candidates,
            "scored_candidates": scored_candidates,
            "action_tool_result": action_tool_result,
            "action_options": action_options,
            "selected_action_raw": selected_action,
            "validated_action": action,
            "llm_call_indices": list(range(call_start, len(self._model_client.calls))),
        }
        return action, trace


def main() -> None:
    settings = load_settings()
    driver_ids = tuple(
        item.strip()
        for item in os.environ.get("TRACE_DRIVER_IDS", ",".join(DEFAULT_DRIVER_IDS)).split(",")
        if item.strip()
    )
    horizon_minutes = int(os.environ.get("TRACE_HORIZON_MINUTES", str(DEFAULT_HORIZON_MINUTES)))
    max_steps_per_driver = int(os.environ.get("TRACE_MAX_STEPS_PER_DRIVER", "80"))
    output_dir = DEMO_ROOT / "agent" / "llm_trace_outputs"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"real_llm_day_trace_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"

    model_client = TraceModelClient(
        api_url=settings.model_api_url,
        api_key=settings.model_api_key,
        model_name=settings.model_name,
        timeout_seconds=settings.model_timeout_seconds,
    )
    session_actions_by_driver: dict[str, list[dict[str, Any]]] = {}
    results: dict[str, Any] = {
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "driver_ids": driver_ids,
        "horizon_minutes": horizon_minutes,
        "max_steps_per_driver": max_steps_per_driver,
        "drivers": {},
        "llm_calls": model_client.calls,
    }

    try:
        for driver_id in driver_ids:
            repo = CargoRepository(settings.cargo_dataset_path)
            manager = DriverStateManager(settings.drivers_path)
            repo.load()
            manager.load()
            manager.start_simulation(driver_id=driver_id, progress_minutes=0)
            repo.sync_time_minutes(0)
            status0 = manager.get_driver_status(driver_id)
            if _has_temporary_preference(status0):
                raise ValueError(f"{driver_id} 含临时约定偏好，请换一个司机")

            session_actions_by_driver[driver_id] = []
            port = TraceSimulationPort(
                repo=repo,
                manager=manager,
                model_client=model_client,
                session_actions_by_driver=session_actions_by_driver,
            )
            pipeline = TraceDecisionPipeline(
                port,
                model_client,
                reposition_speed_km_per_hour=settings.reposition_speed_km_per_hour,
                simulation_horizon_minutes=horizon_minutes,
            )

            driver_trace = {
                "driver_id": driver_id,
                "initial_status": status0,
                "steps": [],
                "actions": session_actions_by_driver[driver_id],
            }
            results["drivers"][driver_id] = driver_trace
            step = 0
            try:
                while manager.get_simulation_progress_minutes() < horizon_minutes and repo.size > 0 and step < max_steps_per_driver:
                    step += 1
                    before_status = manager.get_driver_status(driver_id)
                    step_start_minutes = manager.get_simulation_progress_minutes()
                    action, trace = pipeline.decide_with_trace(driver_id, step)
                    progress_after_decision = manager.get_simulation_progress_minutes()
                    result = _apply_action(
                        repo=repo,
                        manager=manager,
                        driver_id=driver_id,
                        action=action,
                        reposition_speed_km_per_hour=settings.reposition_speed_km_per_hour,
                        simulation_horizon_minutes=horizon_minutes,
                    )
                    after_status = manager.get_driver_status(driver_id)
                    action_record = _normalize_for_output(
                        {
                            "step": step,
                            "driver_id": driver_id,
                            "step_elapsed_minutes": manager.get_simulation_progress_minutes() - step_start_minutes,
                            "query_scan_cost_minutes": progress_after_decision - step_start_minutes,
                            "action_exec_cost_minutes": manager.get_simulation_progress_minutes() - progress_after_decision,
                            "position_before": {"lat": before_status["current_lat"], "lng": before_status["current_lng"]},
                            "position_after": {"lat": after_status["current_lat"], "lng": after_status["current_lng"]},
                            "simulation_end_time": manager.get_simulation_wall_time(),
                            "action": action,
                            "token_usage": action.get("model_usage", {}),
                            "result": result,
                        }
                    )
                    session_actions_by_driver[driver_id].append(action_record)
                    trace["action_result"] = result
                    trace["status_after_action"] = after_status
                    trace["action_record"] = action_record
                    driver_trace["steps"].append(_normalize_for_output(trace))
                    _write_json(output_path, results)
            except Exception as exc:  # noqa: BLE001 - diagnostics should preserve failure context.
                driver_trace["error"] = {
                    "type": type(exc).__name__,
                    "message": str(exc),
                    "step": step,
                    "simulation_progress_minutes": manager.get_simulation_progress_minutes(),
                    "simulation_wall_time": manager.get_simulation_wall_time(),
                }
                _write_json(output_path, results)
                raise
            finally:
                driver_trace["final_status"] = manager.get_driver_status(driver_id)
                driver_trace["completed_steps"] = len(driver_trace["steps"])
                driver_trace["ended_because"] = _end_reason(manager, repo, step, horizon_minutes, max_steps_per_driver)
                _write_json(output_path, results)
    finally:
        model_client.close()

    _write_json(output_path, results)
    print(str(output_path))


def _apply_action(
    *,
    repo: CargoRepository,
    manager: DriverStateManager,
    driver_id: str,
    action: dict[str, Any],
    reposition_speed_km_per_hour: float,
    simulation_horizon_minutes: int,
) -> dict[str, Any]:
    action_name = str(action.get("action", "")).strip().lower()
    params = action.get("params", {})
    if not isinstance(params, dict):
        raise ValueError("action.params must be a dict")
    if action_name == "wait":
        return simulation_actions.wait(repo, manager, driver_id, int(params.get("duration_minutes", 1)))
    if action_name == "reposition":
        return simulation_actions.reposition(
            repo,
            manager,
            driver_id,
            float(params["latitude"]),
            float(params["longitude"]),
            speed_km_per_hour=reposition_speed_km_per_hour,
        )
    if action_name == "take_order":
        cargo_id = str(params["cargo_id"])
        cargo = repo.get_by_id(cargo_id)
        if cargo is None:
            progress = manager.advance_progress(driver_id, 1)
            repo.sync_time_minutes(progress)
            return {
                "action": "take_order",
                "accepted": False,
                "detail": f"cargo_id 已失效: {cargo_id}",
                "simulation_progress_minutes": progress,
                "simulation_wall_time": manager.get_simulation_wall_time(),
            }
        try:
            return simulation_actions.take_order(
                repo,
                manager,
                driver_id,
                cargo_id,
                reposition_speed_km_per_hour=reposition_speed_km_per_hour,
                simulation_horizon_minutes=simulation_horizon_minutes,
            )
        except ValueError:
            progress = manager.advance_progress(driver_id, 1)
            repo.sync_time_minutes(progress)
            return {
                "action": "take_order",
                "accepted": False,
                "detail": f"cargo_id 已失效: {cargo_id}",
                "simulation_progress_minutes": progress,
                "simulation_wall_time": manager.get_simulation_wall_time(),
            }
    raise ValueError(f"unsupported action: {action_name}")


def _classify_phase(payload: dict[str, Any]) -> str:
    messages = payload.get("messages") if isinstance(payload.get("messages"), list) else []
    system = str(messages[0].get("content") if messages and isinstance(messages[0], dict) else "")
    if "偏好轻量提炼器" in system:
        return "preference_brief_extraction"
    if "工具配置提取器" in system:
        return "tool_plan_extraction"
    if "偏好合规判断器" in system:
        return "preference_judge"
    if "最终动作选择器" in system:
        return "action_decider"
    return "unknown"


def _try_extract_model_json(response: dict[str, Any]) -> Any:
    try:
        return extract_model_json_object(response)
    except Exception as exc:  # noqa: BLE001 - diagnostic output should include parse failures.
        return {"error": str(exc)}


def _has_temporary_preference(status: dict[str, Any]) -> bool:
    preferences = status.get("preferences") if isinstance(status.get("preferences"), list) else []
    return any("临时约定" in str(item.get("content") if isinstance(item, dict) else item) for item in preferences)


def _normalize_for_output(value: Any) -> Any:
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, list):
        return [_normalize_for_output(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _normalize_for_output(item) for key, item in value.items()}
    return value


def _end_reason(manager: DriverStateManager, repo: CargoRepository, step: int, horizon: int, max_steps: int) -> str:
    if manager.get_simulation_progress_minutes() >= horizon:
        return "horizon_reached"
    if repo.size <= 0:
        return "cargo_empty"
    if step >= max_steps:
        return "max_steps_reached"
    return "unknown"


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
