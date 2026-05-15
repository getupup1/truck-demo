"""Feature helpers for model-based freight decisions."""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timedelta
from typing import Any


SIMULATION_EPOCH = datetime(2026, 3, 1, 0, 0, 0)
WALL_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
DEFAULT_DISTANCE_COST_PER_KM = 1.5
DEFAULT_TOP_K_CANDIDATES = 10
DEFAULT_MIN_SCORE_PER_HOUR = 20.0
PREFERENCE_CLASSIFICATION_KEYS = (
    "forbidden_cargo_names",
    "distance_limits",
    "fixed_rest_windows",
    "daily_rest_duration_constraints",
    "rest_windows",
    "region_limits",
    "deadline_location_constraints",
    "history_constraints",
    "urgent_tasks",
    "unknown_preferences",
    "unclassified_preferences",
)


def preference_signature(preferences: list[Any]) -> str:
    """Stable signature for currently visible preferences."""
    try:
        payload = json.dumps(preferences, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except TypeError:
        payload = repr(preferences)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def extract_preference_texts(preferences: list[Any]) -> list[str]:
    texts: list[str] = []
    for item in preferences:
        if isinstance(item, str):
            text = item.strip()
        elif isinstance(item, dict):
            text = str(item.get("content") or item.get("text") or "").strip()
        else:
            text = str(item).strip()
        if text:
            texts.append(text)
    return texts


def empty_preference_classification(preferences: list[Any]) -> dict[str, Any]:
    return {
        "forbidden_cargo_names": [],
        "distance_limits": [],
        "fixed_rest_windows": [],
        "daily_rest_duration_constraints": [],
        "rest_windows": [],
        "region_limits": [],
        "deadline_location_constraints": [],
        "history_constraints": [],
        "urgent_tasks": [],
        "unknown_preferences": [],
        "unclassified_preferences": list(preferences),
    }


def normalize_preference_classification(raw: Any, preferences: list[Any]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return empty_preference_classification(preferences)
    normalized: dict[str, Any] = {}
    for key in PREFERENCE_CLASSIFICATION_KEYS:
        value = raw.get(key)
        normalized[key] = value if isinstance(value, list) else []
    for key, value in raw.items():
        if key not in normalized:
            normalized[key] = value
    return normalized


def has_urgent_tasks(classification: dict[str, Any]) -> bool:
    tasks = classification.get("urgent_tasks")
    return isinstance(tasks, list) and len(tasks) > 0


def parse_wall_time_minutes(value: Any) -> int | None:
    if value is None:
        return None
    try:
        text = str(value).strip()
        if not text:
            return None
        dt = datetime.strptime(text, WALL_TIME_FORMAT)
    except (TypeError, ValueError):
        return None
    return int((dt - SIMULATION_EPOCH).total_seconds() // 60)


def format_simulation_time(minutes: int | None) -> str | None:
    if minutes is None:
        return None
    return (SIMULATION_EPOCH + timedelta(minutes=int(minutes))).strftime(WALL_TIME_FORMAT)


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    radius_km = 6371.0
    p1 = math.radians(lat1)
    l1 = math.radians(lng1)
    p2 = math.radians(lat2)
    l2 = math.radians(lng2)
    dp = p2 - p1
    dl = l2 - l1
    h = math.sin(dp * 0.5) ** 2 + math.cos(p1) * math.cos(p2) * (math.sin(dl * 0.5) ** 2)
    h = min(1.0, max(0.0, h))
    return 2.0 * radius_km * math.asin(math.sqrt(h))


def distance_to_minutes(distance_km: float, speed_km_per_hour: float) -> int:
    if distance_km <= 0:
        return 1
    return max(1, math.ceil((distance_km / speed_km_per_hour) * 60.0))


def _pickup_minutes(distance_km: float, speed_km_per_hour: float) -> int:
    if distance_km <= 1e-6:
        return 0
    return distance_to_minutes(distance_km, speed_km_per_hour)


def _coerce_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _coerce_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _load_window_minutes(cargo: dict[str, Any]) -> tuple[int, int] | None:
    raw = cargo.get("load_time")
    if not isinstance(raw, list) or len(raw) != 2:
        return None
    start = parse_wall_time_minutes(raw[0])
    end = parse_wall_time_minutes(raw[1])
    if start is None or end is None or end < start:
        return None
    return start, end


def enhance_cargo_candidates(
    items: list[dict[str, Any]],
    status: dict[str, Any],
    *,
    speed_km_per_hour: float = 60.0,
    simulation_horizon_minutes: int | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Add deterministic timing and distance features to API cargo candidates."""
    current_lat = _coerce_float(status.get("current_lat"))
    current_lng = _coerce_float(status.get("current_lng"))
    t0_minutes = _coerce_int(status.get("simulation_progress_minutes"))
    out: list[dict[str, Any]] = []

    for item in items[:limit]:
        cargo = item.get("cargo")
        if not isinstance(cargo, dict):
            continue
        start = cargo.get("start") if isinstance(cargo.get("start"), dict) else {}
        end = cargo.get("end") if isinstance(cargo.get("end"), dict) else {}
        start_lat = _coerce_float(start.get("lat"), current_lat)
        start_lng = _coerce_float(start.get("lng"), current_lng)
        end_lat = _coerce_float(end.get("lat"), start_lat)
        end_lng = _coerce_float(end.get("lng"), start_lng)

        pickup_deadhead_km = _coerce_float(item.get("distance_km"), -1.0)
        if pickup_deadhead_km < 0:
            pickup_deadhead_km = haversine_km(current_lat, current_lng, start_lat, start_lng)
        pickup_minutes = _pickup_minutes(pickup_deadhead_km, speed_km_per_hour)
        arrival_minutes = t0_minutes + pickup_minutes

        load_window = _load_window_minutes(cargo)
        load_time_missed = False
        waiting_minutes = 0
        ready_minutes = arrival_minutes
        if load_window is not None:
            load_start, load_end = load_window
            load_time_missed = arrival_minutes > load_end
            if not load_time_missed:
                waiting_minutes = max(0, load_start - arrival_minutes)
                ready_minutes = arrival_minutes + waiting_minutes

        cost_time_minutes = _coerce_int(cargo.get("cost_time_minutes"))
        finish_minutes: int | None
        estimated_total_minutes: int | None
        if load_time_missed:
            finish_minutes = None
            estimated_total_minutes = None
        else:
            finish_minutes = ready_minutes + cost_time_minutes
            estimated_total_minutes = pickup_minutes + waiting_minutes + cost_time_minutes

        remove_minutes = parse_wall_time_minutes(cargo.get("remove_time"))
        expired_after_scan = remove_minutes is not None and remove_minutes <= t0_minutes
        income_eligible = (
            None
            if simulation_horizon_minutes is None or finish_minutes is None
            else finish_minutes <= simulation_horizon_minutes
        )

        out.append(
            {
                "cargo_id": cargo.get("cargo_id"),
                "cargo_name": cargo.get("cargo_name"),
                "price": cargo.get("price"),
                "cost_time_minutes": cost_time_minutes,
                "remove_time": cargo.get("remove_time"),
                "remove_minutes": remove_minutes,
                "load_time": cargo.get("load_time"),
                "start": cargo.get("start"),
                "end": cargo.get("end"),
                "pickup_deadhead_km": round(float(pickup_deadhead_km), 2),
                "pickup_minutes": pickup_minutes,
                "haul_distance_km": round(haversine_km(start_lat, start_lng, end_lat, end_lng), 2),
                "arrival_minutes": arrival_minutes,
                "arrival_time": format_simulation_time(arrival_minutes),
                "waiting_minutes": waiting_minutes,
                "finish_minutes": finish_minutes,
                "finish_time": format_simulation_time(finish_minutes),
                "estimated_total_minutes": estimated_total_minutes,
                "load_time_missed": load_time_missed,
                "expired_after_scan": expired_after_scan,
                "income_eligible": income_eligible,
            }
        )
    return out


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _text_blob(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return " ".join(_text_blob(v) for v in value.values())
    if isinstance(value, list):
        return " ".join(_text_blob(v) for v in value)
    return str(value)


def _iter_preference_items(parsed_preferences: dict[str, Any] | None, *keys: str) -> list[Any]:
    if not isinstance(parsed_preferences, dict):
        return []
    out: list[Any] = []
    for key in keys:
        out.extend(_as_list(parsed_preferences.get(key)))
    return out


def _extract_penalty_amount(rule: Any, default: float = 0.0) -> float:
    if isinstance(rule, dict):
        for key in ("penalty_amount", "penalty", "estimated_penalty"):
            value = rule.get(key)
            if isinstance(value, (int, float)):
                return float(value)
            if isinstance(value, str):
                match = re.search(r"-?\d+(?:\.\d+)?", value)
                if match:
                    return float(match.group(0))
    text = _text_blob(rule)
    for pattern in (
        r"罚(?:分|款|金)?\s*(\d+(?:\.\d+)?)",
        r"扣(?:分)?\s*(\d+(?:\.\d+)?)",
        r"(\d+(?:\.\d+)?)\s*(?:分|元)",
    ):
        match = re.search(pattern, text)
        if match:
            return float(match.group(1))
    return float(default)


def _extract_limit_km(rule: Any) -> float | None:
    text = _text_blob(rule)
    matches = re.findall(r"(\d+(?:\.\d+)?)\s*(?:公里|千米|km|KM)", text)
    if not matches:
        return None
    return float(matches[0])


def daily_rest_requirement_minutes(parsed_preferences: dict[str, Any] | None) -> int | None:
    """Extract the daily continuous rest requirement from classified preferences."""
    requirements: list[int] = []
    for rule in _iter_preference_items(parsed_preferences, "daily_rest_duration_constraints"):
        text = _text_blob(rule)
        for value in re.findall(r"(\d+(?:\.\d+)?)\s*(?:小时|小時|hour|hours)", text, flags=re.IGNORECASE):
            requirements.append(int(math.ceil(float(value) * 60.0)))
        for value in re.findall(r"(\d+(?:\.\d+)?)\s*(?:分钟|分鐘|minute|minutes|min)", text, flags=re.IGNORECASE):
            requirements.append(int(math.ceil(float(value))))
    return max(requirements) if requirements else None


def _rule_names(rule: Any) -> list[str]:
    if not isinstance(rule, dict):
        return []
    names: list[str] = []
    for key in ("names", "cargo_names", "forbidden_cargo_names", "cargo_name"):
        value = rule.get(key)
        if isinstance(value, str):
            names.append(value)
        elif isinstance(value, list):
            names.extend(str(item) for item in value if str(item).strip())
    return [name.strip() for name in names if name.strip()]


def _candidate_action_interval(candidate: dict[str, Any]) -> tuple[int, int] | None:
    finish = candidate.get("finish_minutes")
    total = candidate.get("estimated_total_minutes")
    try:
        finish_minutes = int(finish)
        total_minutes = int(total)
    except (TypeError, ValueError):
        return None
    if total_minutes <= 0:
        return None
    return finish_minutes - total_minutes, finish_minutes


def _overlaps_daily_window(start_minutes: int, end_minutes: int, window_start: int, window_end: int) -> bool:
    if end_minutes <= start_minutes:
        return False
    day_start = start_minutes // 1440
    day_end = end_minutes // 1440
    for day in range(day_start, day_end + 1):
        base = day * 1440
        if window_end <= window_start:
            intervals = [(base + window_start, base + 1440), (base + 1440, base + 1440 + window_end)]
        else:
            intervals = [(base + window_start, base + window_end)]
        for win_start, win_end in intervals:
            if max(start_minutes, win_start) < min(end_minutes, win_end):
                return True
    return False


def _parse_daily_window_minutes(rule: Any) -> tuple[int, int] | None:
    if isinstance(rule, dict):
        start = rule.get("window_start_minutes")
        end = rule.get("window_end_minutes")
        if isinstance(start, (int, float)) and isinstance(end, (int, float)):
            return int(start), int(end)
    text = _text_blob(rule)
    if not text:
        return None

    if "12" in text and ("13" in text or "下午1" in text or "午后1" in text):
        return 12 * 60, 13 * 60
    if "23" in text and "8" in text:
        return 23 * 60, 8 * 60
    if "23" in text and "6" in text:
        return 23 * 60, 6 * 60
    if "23" in text and "4" in text:
        return 23 * 60, 4 * 60
    if ("凌晨" in text or "夜间" in text) and "2" in text and "5" in text:
        return 2 * 60, 5 * 60

    hour_matches = re.findall(r"(\d{1,2})(?:[:：]\d{1,2})?\s*(?:点|时|:|：)", text)
    if len(hour_matches) >= 2:
        return (int(hour_matches[0]) % 24) * 60, (int(hour_matches[1]) % 24) * 60
    return None


def filter_deterministic_failures(candidates: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Remove candidates that are already impossible before asking the model."""
    summary = {
        "input_count": len(candidates),
        "expired_after_scan": 0,
        "load_time_missed": 0,
        "income_ineligible": 0,
        "invalid_estimated_total_minutes": 0,
        "kept_after_deterministic_filter": 0,
    }
    kept: list[dict[str, Any]] = []
    for candidate in candidates:
        if candidate.get("expired_after_scan"):
            summary["expired_after_scan"] += 1
            continue
        if candidate.get("load_time_missed"):
            summary["load_time_missed"] += 1
            continue
        if candidate.get("income_eligible") is False:
            summary["income_ineligible"] += 1
            continue
        total = candidate.get("estimated_total_minutes")
        if not isinstance(total, int) or total <= 0:
            summary["invalid_estimated_total_minutes"] += 1
            continue
        kept.append(candidate)
    summary["kept_after_deterministic_filter"] = len(kept)
    return kept, summary


def estimate_candidate_preference_penalty(
    candidate: dict[str, Any],
    parsed_preferences: dict[str, Any] | None,
    history_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Estimate preference penalty for one candidate using LLM-classified preferences."""
    penalty = 0.0
    reasons: list[str] = []
    cargo_name = str(candidate.get("cargo_name") or "").strip()

    for rule in _iter_preference_items(parsed_preferences, "forbidden_cargo_names"):
        names = _rule_names(rule)
        text = _text_blob(rule)
        if cargo_name and (cargo_name in names or cargo_name in text):
            amount = _extract_penalty_amount(rule, 350.0)
            penalty += amount
            reasons.append(f"命中禁接品类：{cargo_name}，预计罚{amount:g}")

    for rule in _iter_preference_items(parsed_preferences, "distance_limits"):
        text = _text_blob(rule)
        limit_km = _extract_limit_km(rule)
        if limit_km is None:
            continue
        amount = _extract_penalty_amount(rule, 100.0)
        if any(word in text for word in ("空驶", "赴装货点", "到装货点", "pickup")):
            actual = _coerce_float(candidate.get("pickup_deadhead_km"))
            if actual > limit_km:
                penalty += amount
                reasons.append(f"赴装货点空驶{actual:g}km超过{limit_km:g}km，预计罚{amount:g}")
        elif any(word in text for word in ("装卸", "运输", "干线", "卸货点", "haul")):
            actual = _coerce_float(candidate.get("haul_distance_km"))
            if actual > limit_km:
                penalty += amount
                reasons.append(f"装卸距离{actual:g}km超过{limit_km:g}km，预计罚{amount:g}")

    interval = _candidate_action_interval(candidate)
    if interval is not None:
        start_minutes, end_minutes = interval
        for rule in _iter_preference_items(parsed_preferences, "fixed_rest_windows", "rest_windows"):
            window = _parse_daily_window_minutes(rule)
            if window is None:
                continue
            if _overlaps_daily_window(start_minutes, end_minutes, window[0], window[1]):
                amount = _extract_penalty_amount(rule, 100.0)
                penalty += amount
                reasons.append(f"预计与休息窗口重叠，预计罚{amount:g}")

    month_summary = history_summary.get("month", {}) if isinstance(history_summary, dict) else {}
    month_deadhead = _coerce_float(month_summary.get("deadhead_km")) if isinstance(month_summary, dict) else 0.0
    for rule in _iter_preference_items(parsed_preferences, "history_constraints", "distance_limits"):
        text = _text_blob(rule)
        if "月" not in text or "空驶" not in text:
            continue
        limit_km = _extract_limit_km(rule)
        if limit_km is None:
            continue
        projected = month_deadhead + _coerce_float(candidate.get("pickup_deadhead_km"))
        if projected > limit_km:
            amount = _extract_penalty_amount(rule, 100.0)
            penalty += amount
            reasons.append(f"月度空驶预计{projected:g}km超过{limit_km:g}km，预计罚{amount:g}")

    out = dict(candidate)
    out["estimated_preference_penalty"] = round(penalty, 2)
    out["penalty_summary"] = "；".join(reasons[:3]) if reasons else "无明显偏好罚分"
    return out


def candidate_has_fixed_rest_window_conflict(
    candidate: dict[str, Any],
    parsed_preferences: dict[str, Any] | None,
) -> bool:
    """Return true when a candidate would run through a fixed no-work window."""
    interval = _candidate_action_interval(candidate)
    if interval is None:
        return False
    start_minutes, end_minutes = interval
    for rule in _iter_preference_items(parsed_preferences, "fixed_rest_windows", "rest_windows"):
        window = _parse_daily_window_minutes(rule)
        if window is None:
            continue
        if _overlaps_daily_window(start_minutes, end_minutes, window[0], window[1]):
            return True
    return False


def score_and_rank_candidates(
    candidates: list[dict[str, Any]],
    parsed_preferences: dict[str, Any] | None,
    history_summary: dict[str, Any] | None,
    *,
    top_k: int = DEFAULT_TOP_K_CANDIDATES,
    distance_cost_per_km: float = DEFAULT_DISTANCE_COST_PER_KM,
    min_score_per_hour: float = DEFAULT_MIN_SCORE_PER_HOUR,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Apply deterministic filters, estimate penalties, and keep the best candidates."""
    filtered, summary = filter_deterministic_failures(candidates)
    scored: list[dict[str, Any]] = []
    fixed_rest_window_conflict = 0
    negative_net_income = 0
    low_score_per_hour = 0
    for candidate in filtered:
        if candidate_has_fixed_rest_window_conflict(candidate, parsed_preferences):
            fixed_rest_window_conflict += 1
            continue
        item = estimate_candidate_preference_penalty(candidate, parsed_preferences, history_summary)
        distance_cost_proxy = (
            _coerce_float(item.get("pickup_deadhead_km")) + _coerce_float(item.get("haul_distance_km"))
        ) * float(distance_cost_per_km)
        price = _coerce_float(item.get("price"))
        penalty = _coerce_float(item.get("estimated_preference_penalty"))
        estimated_net_income = price - distance_cost_proxy - penalty
        total_minutes = _coerce_int(item.get("estimated_total_minutes"))
        if estimated_net_income < 0:
            negative_net_income += 1
            continue
        score_per_hour = (estimated_net_income / total_minutes) * 60.0
        if score_per_hour < float(min_score_per_hour):
            low_score_per_hour += 1
            continue
        item["distance_cost_proxy"] = round(distance_cost_proxy, 2)
        item["estimated_net_income"] = round(estimated_net_income, 2)
        item["score_per_hour"] = round(score_per_hour, 4)
        scored.append(item)

    scored.sort(
        key=lambda item: (_coerce_float(item.get("score_per_hour")), _coerce_float(item.get("estimated_net_income"))),
        reverse=True,
    )
    kept = scored[: max(0, int(top_k))]
    summary["fixed_rest_window_conflict"] = fixed_rest_window_conflict
    summary["negative_estimated_net_income"] = negative_net_income
    summary["low_score_per_hour"] = low_score_per_hour
    summary["kept_after_scoring"] = len(kept)
    return kept, summary


def _iter_day_segments(start_min: int, end_min: int) -> list[tuple[int, int]]:
    if end_min <= start_min:
        return []
    out: list[tuple[int, int]] = []
    cur = start_min
    while cur < end_min:
        day_idx = cur // 1440
        day_end = (day_idx + 1) * 1440
        seg_end = min(day_end, end_min)
        out.append((day_idx, seg_end - cur))
        cur = seg_end
    return out


def _merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if not intervals:
        return []
    sorted_intervals = sorted(intervals)
    merged = [sorted_intervals[0]]
    for start, end in sorted_intervals[1:]:
        last_start, last_end = merged[-1]
        if start > last_end:
            merged.append((start, end))
        else:
            merged[-1] = (last_start, max(last_end, end))
    return merged


def _longest_merged_span(intervals: list[tuple[int, int]]) -> int:
    return max((end - start for start, end in _merge_intervals(intervals)), default=0)


def _current_wait_streak_minutes(intervals: list[tuple[int, int]], current_minutes: int) -> int:
    for start, end in reversed(_merge_intervals(intervals)):
        if end == current_minutes:
            return end - start
        if end < current_minutes:
            break
    return 0


def _build_step_contexts(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ctxs: list[dict[str, Any]] = []
    prev_end_minutes = 0
    for record in records:
        if not isinstance(record, dict):
            continue
        result = record.get("result")
        action_obj = record.get("action")
        if not isinstance(result, dict) or not isinstance(action_obj, dict):
            continue
        step_elapsed = _coerce_int(record.get("step_elapsed_minutes"), -1)
        query_scan_cost = _coerce_int(record.get("query_scan_cost_minutes"), -1)
        action_exec_cost = _coerce_int(record.get("action_exec_cost_minutes"), -1)
        step_end = _coerce_int(result.get("simulation_progress_minutes"), -1)
        if min(step_elapsed, query_scan_cost, action_exec_cost, step_end) < 0:
            continue
        step_start = prev_end_minutes
        action_start = step_start + query_scan_cost
        action_end = action_start + action_exec_cost
        pos_before = record.get("position_before") if isinstance(record.get("position_before"), dict) else {}
        pos_after = record.get("position_after") if isinstance(record.get("position_after"), dict) else {}
        params = action_obj.get("params") if isinstance(action_obj.get("params"), dict) else {}
        ctxs.append(
            {
                "step": record.get("step"),
                "action_name": str(action_obj.get("action", "")).strip().lower(),
                "params": params,
                "result": result,
                "step_start": step_start,
                "action_start": action_start,
                "action_end": action_end,
                "step_end": step_end,
                "action_exec_cost": action_exec_cost,
                "before_lat": _coerce_float(pos_before.get("lat")),
                "before_lng": _coerce_float(pos_before.get("lng")),
                "after_lat": _coerce_float(pos_after.get("lat")),
                "after_lng": _coerce_float(pos_after.get("lng")),
            }
        )
        prev_end_minutes = step_end
    return ctxs


def _wait_intervals(ctxs: list[dict[str, Any]], *, day: int | None = None) -> list[tuple[int, int]]:
    intervals: list[tuple[int, int]] = []
    day_start = day * 1440 if day is not None else None
    day_end = day_start + 1440 if day_start is not None else None
    for ctx in ctxs:
        if ctx["action_name"] != "wait" or ctx["action_exec_cost"] <= 0:
            continue
        start = int(ctx["action_start"])
        end = int(ctx["action_end"])
        if day_start is not None and day_end is not None:
            start = max(start, day_start)
            end = min(end, day_end)
        if end > start:
            intervals.append((start, end))
    return intervals


def _active_minutes_by_day(ctxs: list[dict[str, Any]], days: list[int]) -> dict[int, int]:
    active = {day: 0 for day in days}
    for ctx in ctxs:
        if ctx["action_name"] not in {"take_order", "reposition"}:
            continue
        for day, minutes in _iter_day_segments(int(ctx["action_start"]), int(ctx["action_end"])):
            if day in active:
                active[day] += minutes
    return active


def _deadhead_km(ctxs: list[dict[str, Any]], *, day: int | None = None, include_failed_take_order: bool = False) -> float:
    total = 0.0
    for ctx in ctxs:
        if day is not None and int(ctx["action_start"]) // 1440 != day:
            continue
        result = ctx["result"]
        if ctx["action_name"] == "reposition":
            total += _coerce_float(result.get("distance_km"))
        elif ctx["action_name"] == "take_order":
            accepted = bool(result.get("accepted", False))
            if accepted or include_failed_take_order:
                total += _coerce_float(result.get("pickup_deadhead_km"))
    return round(total, 2)


def summarize_decision_history(history_resp: dict[str, Any], current_minutes: int) -> dict[str, Any]:
    records = history_resp.get("records") if isinstance(history_resp, dict) else []
    if not isinstance(records, list):
        records = []
    ctxs = _build_step_contexts(records)
    current_day = max(0, int(current_minutes) // 1440)
    completed_days = list(range(max(0, int(current_minutes) // 1440)))

    today_take_orders = [c for c in ctxs if c["action_name"] == "take_order" and int(c["action_start"]) // 1440 == current_day]
    today_success = [c for c in today_take_orders if bool(c["result"].get("accepted", False))]
    today_failed = [c for c in today_take_orders if not bool(c["result"].get("accepted", False))]
    today_active = _active_minutes_by_day(ctxs, [current_day]).get(current_day, 0)
    first_today_order = min((int(c["action_start"]) for c in today_success), default=None)
    today_wait_intervals = _wait_intervals(ctxs, day=current_day)

    success_orders = [c for c in ctxs if c["action_name"] == "take_order" and bool(c["result"].get("accepted", False))]
    failed_orders = [c for c in ctxs if c["action_name"] == "take_order" and not bool(c["result"].get("accepted", False))]
    failure_reasons: dict[str, int] = {}
    for ctx in failed_orders:
        detail = str(ctx["result"].get("detail") or "unknown")
        failure_reasons[detail] = failure_reasons.get(detail, 0) + 1

    accepted_days = {int(c["action_start"]) // 1440 for c in success_orders}
    active_by_completed_day = _active_minutes_by_day(ctxs, completed_days)
    no_order_days = sum(1 for day in completed_days if day not in accepted_days)
    fully_idle_days = sum(1 for day in completed_days if active_by_completed_day.get(day, 0) == 0)
    month_wait_intervals = _wait_intervals(ctxs)
    longest_wait = _longest_merged_span(month_wait_intervals)

    recent: list[dict[str, Any]] = []
    for ctx in ctxs[-5:]:
        result = ctx["result"]
        recent.append(
            {
                "step": ctx.get("step"),
                "action": ctx["action_name"],
                "action_start_minutes": ctx["action_start"],
                "action_start_time": format_simulation_time(int(ctx["action_start"])),
                "action_end_minutes": ctx["action_end"],
                "action_end_time": format_simulation_time(int(ctx["action_end"])),
                "accepted": result.get("accepted"),
                "detail": result.get("detail"),
                "cargo_id": result.get("cargo_id") or ctx["params"].get("cargo_id"),
                "pickup_deadhead_km": result.get("pickup_deadhead_km"),
                "haul_distance_km": result.get("haul_distance_km"),
                "distance_km": result.get("distance_km"),
            }
        )

    return {
        "total_steps": history_resp.get("total_steps", len(records)) if isinstance(history_resp, dict) else len(records),
        "returned_count": len(records),
        "today": {
            "day_index": current_day,
            "successful_take_order_count": len(today_success),
            "failed_take_order_count": len(today_failed),
            "first_successful_order_start_minutes": first_today_order,
            "first_successful_order_start_time": format_simulation_time(first_today_order),
            "longest_wait_minutes": _longest_merged_span(today_wait_intervals),
            "current_wait_streak_minutes": _current_wait_streak_minutes(today_wait_intervals, int(current_minutes)),
            "active_minutes": today_active,
            "deadhead_km": _deadhead_km(ctxs, day=current_day),
            "failed_take_order_deadhead_km": _deadhead_km(ctxs, day=current_day, include_failed_take_order=True)
            - _deadhead_km(ctxs, day=current_day),
        },
        "month": {
            "successful_take_order_count": len(success_orders),
            "failed_take_order_count": len(failed_orders),
            "failure_reasons": failure_reasons,
            "completed_day_count": len(completed_days),
            "no_order_days": no_order_days,
            "fully_idle_days": fully_idle_days,
            "longest_wait_minutes": longest_wait,
            "longest_wait_hours": round(longest_wait / 60.0, 2),
            "deadhead_km": _deadhead_km(ctxs),
            "failed_take_order_deadhead_km": round(_deadhead_km(ctxs, include_failed_take_order=True) - _deadhead_km(ctxs), 2),
        },
        "recent": recent,
    }
