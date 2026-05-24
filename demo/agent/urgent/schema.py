"""Schema normalization helpers for urgent preference flow."""

from __future__ import annotations

from typing import Any

from agent.history.time_utils import coerce_float, coerce_int, parse_wall_time_minutes


VALID_URGENT_MODES = {"force_action", "planning_guard"}
VALID_ACTIONS = {"take_order", "wait", "reposition"}


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
    start_minutes = (
        parse_wall_time_minutes(urgent_task.get("visible_start_time"))
        or parse_wall_time_minutes(urgent_task.get("active_start_time"))
        or 0
    )
    relevant = [
        event
        for event in event_log
        if isinstance(event, dict) and coerce_int(event.get("action_end_minutes"), 0) >= start_minutes
    ]
    return relevant[-max(1, int(max_events)) :]


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
        return {"cargo_id": cargo_id}
    if action == "wait":
        duration = coerce_int(params.get("duration_minutes"), 0)
        if duration <= 0:
            raise ValueError("urgent wait missing positive duration_minutes")
        return {"duration_minutes": duration}
    latitude = coerce_float(params.get("latitude"), float("nan"))
    longitude = coerce_float(params.get("longitude"), float("nan"))
    if latitude != latitude or longitude != longitude:
        raise ValueError("urgent reposition missing latitude/longitude")
    return {"latitude": round(latitude, 6), "longitude": round(longitude, 6)}


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
