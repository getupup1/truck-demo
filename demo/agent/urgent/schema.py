"""Schema normalization helpers for urgent preference flow."""

from __future__ import annotations

from typing import Any

from agent.history.time_utils import coerce_float, coerce_int, parse_wall_time_minutes


VALID_URGENT_MODES = {"force_action", "planning_guard"}
VALID_ACTIONS = {"take_order", "wait", "reposition"}
DEFAULT_STEP_DECIDER_NEAR_START_MINUTES = 60


def normalize_urgent_task_plan(raw: Any, *, driver_id: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("urgent task plan must be a JSON object")
    tasks_raw = raw.get("urgent_tasks")
    if not isinstance(tasks_raw, list):
        raise ValueError("urgent task plan missing urgent_tasks")
    tasks: list[dict[str, Any]] = []
    for idx, item in enumerate(tasks_raw):
        if not isinstance(item, dict):
            continue
        task = _normalize_task(item, idx)
        if task is not None:
            tasks.append(task)
    if not tasks:
        raise ValueError("urgent task plan contains no valid urgent_tasks")
    return {"driver_id": str(raw.get("driver_id") or driver_id), "urgent_tasks": tasks}


def select_priority_urgent_task(plan: dict[str, Any]) -> dict[str, Any] | None:
    tasks = plan.get("urgent_tasks")
    if not isinstance(tasks, list) or not tasks:
        return None
    return sorted(
        [task for task in tasks if isinstance(task, dict)],
        key=lambda task: (
            _task_deadline_minutes(task),
            -coerce_float(task.get("penalty_amount"), 0.0),
            str(task.get("task_id") or ""),
        ),
    )[0]


def build_urgent_relevant_events(
    event_log: list[dict[str, Any]],
    urgent_task: dict[str, Any],
    *,
    max_events: int = 1,
) -> list[dict[str, Any]]:
    visible_start_minutes = parse_wall_time_minutes(urgent_task.get("visible_start_time"))
    active_start_minutes = parse_wall_time_minutes(urgent_task.get("active_start_time"))
    start_minutes = visible_start_minutes if visible_start_minutes is not None else active_start_minutes or 0
    relevant = [
        event
        for event in event_log
        if isinstance(event, dict) and coerce_int(event.get("action_end_minutes"), 0) >= start_minutes
    ]
    return relevant[-max(1, int(max_events)) :]


def planning_guard_needs_step_decider(
    urgent_task: dict[str, Any],
    status: dict[str, Any],
    event_log: list[dict[str, Any]],
    *,
    near_start_minutes: int = DEFAULT_STEP_DECIDER_NEAR_START_MINUTES,
) -> bool:
    """Return whether a planning_guard task needs an LLM stage decision now."""
    if urgent_task.get("urgent_mode") != "planning_guard":
        return True
    active_start_minutes = parse_wall_time_minutes(urgent_task.get("active_start_time"))
    if active_start_minutes is None:
        return True
    current_minutes = _current_status_minutes(status)
    if current_minutes >= active_start_minutes - max(0, int(near_start_minutes)):
        return True
    first_stage = first_stage_decision(urgent_task)
    recent_events = build_urgent_relevant_events(event_log, urgent_task)
    return bool(recent_events and _stage_matches_event(first_stage, recent_events[-1]))


def first_stage_decision(urgent_task: dict[str, Any]) -> dict[str, Any]:
    """Build an executable-looking step decision from the first urgent stage."""
    stages = urgent_task.get("stages")
    if not isinstance(stages, list) or not stages:
        raise ValueError("urgent task has no stages")
    stage = sorted(
        [item for item in stages if isinstance(item, dict)],
        key=lambda item: coerce_int(item.get("stage_order"), 0),
    )[0]
    action = str(stage.get("action") or "").strip()
    if action not in VALID_ACTIONS:
        raise ValueError(f"unsupported urgent stage action: {action}")
    return {
        "urgent_mode": str(urgent_task.get("urgent_mode") or "planning_guard"),
        "current_stage_order": coerce_int(stage.get("stage_order"), 1),
        "action": action,
        "params": dict(stage.get("params") if isinstance(stage.get("params"), dict) else {}),
        "reason": str(stage.get("reason") or "").strip(),
    }


def normalize_step_decision(raw: Any, urgent_task: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("urgent step decision must be a JSON object")
    mode = str(raw.get("urgent_mode") or urgent_task.get("urgent_mode") or "").strip()
    if mode not in VALID_URGENT_MODES:
        raise ValueError(f"unsupported urgent_mode: {mode}")
    action = str(raw.get("action") or "").strip()
    if action not in VALID_ACTIONS:
        raise ValueError(f"unsupported urgent action: {action}")
    params = _normalize_action_params(action, raw.get("params"))
    return {
        "urgent_mode": mode,
        "current_stage_order": coerce_int(raw.get("current_stage_order"), 0),
        "action": action,
        "params": params,
        "reason": str(raw.get("reason") or "").strip(),
    }


def resolve_urgent_wait_action(
    step_decision: dict[str, Any],
    urgent_task: dict[str, Any],
    status: dict[str, Any],
) -> dict[str, Any]:
    """Resolve absolute urgent wait gates into executable duration_minutes."""
    decision = {
        "urgent_mode": step_decision.get("urgent_mode"),
        "current_stage_order": step_decision.get("current_stage_order"),
        "action": step_decision.get("action"),
        "params": dict(step_decision.get("params") if isinstance(step_decision.get("params"), dict) else {}),
        "reason": str(step_decision.get("reason") or "").strip(),
    }
    current_minutes = _current_status_minutes(status)
    stage = _matching_stage(decision, urgent_task)
    stage_params = stage.get("params") if isinstance(stage, dict) and isinstance(stage.get("params"), dict) else {}
    params = decision["params"]

    if decision.get("action") == "wait":
        until_wall_time = params.get("until_wall_time") or stage_params.get("until_wall_time")
        if until_wall_time:
            duration = _duration_until(until_wall_time, current_minutes)
            decision["params"] = {"duration_minutes": duration}
            decision["reason"] = _append_reason(
                decision["reason"],
                f"按紧急偏好的固定等待截止时间 {until_wall_time} 计算等待 {duration} 分钟。",
            )
        return decision

    not_before_wall_time = params.get("not_before_wall_time") or stage_params.get("not_before_wall_time")
    if not_before_wall_time:
        target_minutes = parse_wall_time_minutes(not_before_wall_time)
        if target_minutes is not None and target_minutes > current_minutes:
            duration = target_minutes - current_minutes
            return {
                "urgent_mode": decision.get("urgent_mode"),
                "current_stage_order": decision.get("current_stage_order"),
                "action": "wait",
                "params": {"duration_minutes": duration},
                "reason": _append_reason(
                    decision["reason"],
                    f"当前早于阶段允许执行时间 {not_before_wall_time}，先等待 {duration} 分钟。",
                ),
            }
    return decision


def _normalize_task(item: dict[str, Any], idx: int) -> dict[str, Any] | None:
    mode = str(item.get("urgent_mode") or "").strip()
    if mode not in VALID_URGENT_MODES:
        return None
    stages = [_normalize_stage(stage) for stage in item.get("stages", []) if isinstance(stage, dict)]
    stages = [stage for stage in stages if stage is not None]
    if not stages:
        return None
    return {
        "task_id": str(item.get("task_id") or f"urgent_{idx}").strip() or f"urgent_{idx}",
        "source_text": str(item.get("source_text") or "").strip(),
        "visible_start_time": item.get("visible_start_time"),
        "active_start_time": item.get("active_start_time"),
        "urgent_mode": mode,
        "penalty_amount": item.get("penalty_amount"),
        "penalty_cap": item.get("penalty_cap"),
        "candidate_guards": _normalize_candidate_guards(item.get("candidate_guards")),
        "stages": stages,
    }


def _normalize_stage(item: dict[str, Any]) -> dict[str, Any] | None:
    action = str(item.get("action") or "").strip()
    if action not in VALID_ACTIONS:
        return None
    return {
        "stage_order": coerce_int(item.get("stage_order"), 0),
        "action": action,
        "params": _normalize_action_params(action, item.get("params")),
        "reason": str(item.get("reason") or "").strip(),
    }


def _normalize_action_params(action: str, value: Any) -> dict[str, Any]:
    params = value if isinstance(value, dict) else {}
    if action == "take_order":
        cargo_id = str(params.get("cargo_id") or "").strip()
        if not cargo_id:
            raise ValueError("urgent take_order missing cargo_id")
        out = {"cargo_id": cargo_id}
        not_before = _valid_wall_time(params.get("not_before_wall_time"))
        if not_before:
            out["not_before_wall_time"] = not_before
        return out
    if action == "wait":
        duration = coerce_int(params.get("duration_minutes"), 0)
        until = _valid_wall_time(params.get("until_wall_time"))
        if duration <= 0 and not until:
            raise ValueError("urgent wait missing positive duration_minutes or until_wall_time")
        out: dict[str, Any] = {}
        if duration > 0:
            out["duration_minutes"] = duration
        if until:
            out["until_wall_time"] = until
        return out
    latitude = coerce_float(params.get("latitude"), float("nan"))
    longitude = coerce_float(params.get("longitude"), float("nan"))
    if latitude != latitude or longitude != longitude:
        raise ValueError("urgent reposition missing latitude/longitude")
    out = {"latitude": round(latitude, 6), "longitude": round(longitude, 6)}
    not_before = _valid_wall_time(params.get("not_before_wall_time"))
    if not_before:
        out["not_before_wall_time"] = not_before
    return out


def _normalize_candidate_guards(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    guards: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        tool = str(item.get("tool") or "").strip()
        if tool != "deadline_location_check":
            continue
        config = item.get("config") if isinstance(item.get("config"), dict) else {}
        center = _lat_lng_pair(config.get("center"))
        deadline_wall_time = str(config.get("deadline_wall_time") or "").strip()
        if center is None or not deadline_wall_time:
            continue
        guards.append(
            {
                "tool": tool,
                "config": {
                    "center": center,
                    "radius_km": max(0.0, coerce_float(config.get("radius_km"), 0.0)),
                    "deadline_wall_time": deadline_wall_time,
                },
                "reason": str(item.get("reason") or "").strip(),
            }
        )
    return guards


def _lat_lng_pair(value: Any) -> list[float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    lat = coerce_float(value[0], float("nan"))
    lng = coerce_float(value[1], float("nan"))
    if lat != lat or lng != lng:
        return None
    return [round(lat, 6), round(lng, 6)]


def _task_deadline_minutes(task: dict[str, Any]) -> int:
    candidates: list[int] = []
    for guard in task.get("candidate_guards", []):
        if not isinstance(guard, dict):
            continue
        config = guard.get("config") if isinstance(guard.get("config"), dict) else {}
        minutes = parse_wall_time_minutes(config.get("deadline_wall_time"))
        if minutes is not None:
            candidates.append(minutes)
    for key in ("active_start_time", "visible_start_time"):
        minutes = parse_wall_time_minutes(task.get(key))
        if minutes is not None:
            candidates.append(minutes)
    return min(candidates) if candidates else 10**12


def _valid_wall_time(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if parse_wall_time_minutes(text) is None:
        return None
    return text


def _current_status_minutes(status: dict[str, Any]) -> int:
    raw_minutes = status.get("simulation_progress_minutes")
    try:
        return int(raw_minutes)
    except (TypeError, ValueError):
        parsed = parse_wall_time_minutes(status.get("simulation_wall_time"))
        return parsed if parsed is not None else 0


def _duration_until(until_wall_time: Any, current_minutes: int) -> int:
    target_minutes = parse_wall_time_minutes(until_wall_time)
    if target_minutes is None:
        raise ValueError(f"invalid urgent until_wall_time: {until_wall_time}")
    return max(1, target_minutes - current_minutes)


def _matching_stage(decision: dict[str, Any], urgent_task: dict[str, Any]) -> dict[str, Any] | None:
    stages = urgent_task.get("stages")
    if not isinstance(stages, list):
        return None
    current_stage_order = coerce_int(decision.get("current_stage_order"), 0)
    if current_stage_order > 0:
        for stage in stages:
            if isinstance(stage, dict) and coerce_int(stage.get("stage_order"), 0) == current_stage_order:
                return stage
    for stage in stages:
        if isinstance(stage, dict) and _stage_matches_decision(stage, decision):
            return stage
    return None


def _stage_matches_decision(stage: dict[str, Any], decision: dict[str, Any]) -> bool:
    if stage.get("action") != decision.get("action"):
        return False
    stage_params = stage.get("params") if isinstance(stage.get("params"), dict) else {}
    decision_params = decision.get("params") if isinstance(decision.get("params"), dict) else {}
    action = decision.get("action")
    if action == "take_order":
        return str(stage_params.get("cargo_id") or "") == str(decision_params.get("cargo_id") or "")
    if action == "wait":
        if stage_params.get("until_wall_time") and decision_params.get("until_wall_time"):
            return stage_params.get("until_wall_time") == decision_params.get("until_wall_time")
        return coerce_int(stage_params.get("duration_minutes"), -1) == coerce_int(
            decision_params.get("duration_minutes"), -2
        )
    if action == "reposition":
        return (
            abs(coerce_float(stage_params.get("latitude"), float("nan")) - coerce_float(decision_params.get("latitude"), float("nan")))
            <= 1e-6
            and abs(
                coerce_float(stage_params.get("longitude"), float("nan"))
                - coerce_float(decision_params.get("longitude"), float("nan"))
            )
            <= 1e-6
        )
    return False


def _stage_matches_event(stage_decision: dict[str, Any], event: dict[str, Any]) -> bool:
    action = str(stage_decision.get("action") or "").strip()
    if action != str(event.get("action") or "").strip():
        return False
    event_params = dict(event.get("params") if isinstance(event.get("params"), dict) else {})
    result = event.get("result") if isinstance(event.get("result"), dict) else {}
    if action == "take_order":
        expected = str(stage_decision.get("params", {}).get("cargo_id") or "")
        actual = str(event_params.get("cargo_id") or result.get("cargo_id") or "")
        return bool(expected and actual and expected == actual)
    if action == "wait":
        expected_params = stage_decision.get("params") if isinstance(stage_decision.get("params"), dict) else {}
        expected_duration = coerce_int(expected_params.get("duration_minutes"), -1)
        if expected_duration <= 0:
            return True
        actual_duration = coerce_int(
            event_params.get("duration_minutes"),
            coerce_int(event.get("action_end_minutes"), 0) - coerce_int(event.get("action_start_minutes"), 0),
        )
        return actual_duration == expected_duration
    if action == "reposition":
        expected_params = stage_decision.get("params") if isinstance(stage_decision.get("params"), dict) else {}
        actual_lat = coerce_float(event_params.get("latitude"), float("nan"))
        actual_lng = coerce_float(event_params.get("longitude"), float("nan"))
        if actual_lat != actual_lat or actual_lng != actual_lng:
            position_after = event.get("position_after") if isinstance(event.get("position_after"), dict) else {}
            actual_lat = coerce_float(position_after.get("lat"), float("nan"))
            actual_lng = coerce_float(position_after.get("lng"), float("nan"))
        return (
            abs(coerce_float(expected_params.get("latitude"), float("nan")) - actual_lat) <= 1e-6
            and abs(coerce_float(expected_params.get("longitude"), float("nan")) - actual_lng) <= 1e-6
        )
    return False


def _append_reason(current: str, addition: str) -> str:
    if not current:
        return addition
    return f"{current} {addition}"
