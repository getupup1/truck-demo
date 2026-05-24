"""Build validator-compatible action options for urgent actions."""

from __future__ import annotations

from typing import Any

from agent.history.time_utils import coerce_float, distance_to_minutes, haversine_km
from agent.tools.reposition_factory import make_reposition_option
from agent.tools.wait_factory import make_wait_option


def build_urgent_action_options(
    action_decision: dict[str, Any],
    *,
    status: dict[str, Any],
    cargo_items: list[dict[str, Any]] | None = None,
    speed_km_per_hour: float = 60.0,
) -> dict[str, Any]:
    action = str(action_decision.get("action") or "").strip()
    params = action_decision.get("params") if isinstance(action_decision.get("params"), dict) else {}
    reason = str(action_decision.get("reason") or "urgent action")
    options = {"orders": [], "wait_options": [], "reposition_options": [], "tool_context": {}}
    if action == "take_order":
        cargo_id = str(params.get("cargo_id") or "").strip()
        options["orders"] = [_order_option(cargo_id, cargo_items or [], reason)]
        return options
    if action == "wait":
        options["wait_options"] = [
            make_wait_option(
                duration_minutes=int(params.get("duration_minutes")),
                status=status,
                source="urgent_wait",
                reason=reason,
            )
        ]
        return options
    if action == "reposition":
        target_lat = coerce_float(params.get("latitude"), float("nan"))
        target_lng = coerce_float(params.get("longitude"), float("nan"))
        current_lat = coerce_float(status.get("current_lat"), float("nan"))
        current_lng = coerce_float(status.get("current_lng"), float("nan"))
        if target_lat != target_lat or target_lng != target_lng or current_lat != current_lat or current_lng != current_lng:
            raise ValueError("urgent reposition option has invalid coordinates")
        distance_km = haversine_km(current_lat, current_lng, target_lat, target_lng)
        estimated_minutes = distance_to_minutes(distance_km, speed_km_per_hour)
        options["reposition_options"] = [
            make_reposition_option(
                option_id="urgent_reposition",
                pref_id="urgent",
                target_lat=target_lat,
                target_lng=target_lng,
                status=status,
                distance_km=distance_km,
                estimated_minutes=estimated_minutes,
                reason=reason,
                tool_origin="urgent_step_decider",
            )
        ]
        return options
    raise ValueError(f"unsupported urgent action: {action}")


def cargo_visible(cargo_items: list[dict[str, Any]], cargo_id: str) -> bool:
    return _find_cargo_item(cargo_items, cargo_id) is not None


def _order_option(cargo_id: str, cargo_items: list[dict[str, Any]], reason: str) -> dict[str, Any]:
    if not cargo_id:
        raise ValueError("urgent take_order missing cargo_id")
    item = _find_cargo_item(cargo_items, cargo_id)
    cargo = item.get("cargo") if isinstance(item, dict) and isinstance(item.get("cargo"), dict) else {}
    return {
        "option_id": f"urgent_order_{cargo_id}",
        "action": "take_order",
        "params": {"cargo_id": cargo_id},
        "reason": reason,
        "candidate": {
            "cargo_id": cargo_id,
            "cargo_name": cargo.get("cargo_name"),
            "start": cargo.get("start"),
            "end": cargo.get("end"),
            "load_time": cargo.get("load_time"),
            "remove_time": cargo.get("remove_time"),
        },
    }


def _find_cargo_item(cargo_items: list[dict[str, Any]], cargo_id: str) -> dict[str, Any] | None:
    target = str(cargo_id)
    for item in cargo_items:
        cargo = item.get("cargo") if isinstance(item, dict) else None
        if isinstance(cargo, dict) and str(cargo.get("cargo_id") or "") == target:
            return item
    return None
