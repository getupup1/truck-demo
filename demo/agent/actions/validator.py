"""Validate final model-selected actions against offered options."""

from __future__ import annotations

from typing import Any


class ActionValidator:
    def validate(self, action: dict[str, Any], action_options: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(action, dict):
            raise ValueError("最终动作必须是 JSON 对象")
        action_name = str(action.get("action", "")).strip().lower()
        params = action.get("params")
        if not isinstance(params, dict):
            raise ValueError("最终动作 params 必须是对象")
        if action_name == "take_order":
            cargo_id = str(params.get("cargo_id", "")).strip()
            allowed = {
                str(option.get("params", {}).get("cargo_id"))
                for option in action_options.get("orders", [])
                if isinstance(option, dict) and isinstance(option.get("params"), dict)
            }
            if cargo_id not in allowed:
                raise ValueError(f"take_order cargo_id 不在最终候选中: {cargo_id}")
            return {"action": "take_order", "params": {"cargo_id": cargo_id}}
        if action_name == "wait":
            try:
                duration = int(params.get("duration_minutes"))
            except (TypeError, ValueError) as exc:
                raise ValueError("wait.duration_minutes 必须是正整数") from exc
            allowed_wait = {
                int(option.get("params", {}).get("duration_minutes"))
                for option in action_options.get("wait_options", [])
                if isinstance(option, dict) and isinstance(option.get("params"), dict)
            }
            if duration not in allowed_wait:
                raise ValueError(f"wait duration 不在最终候选中: {duration}")
            return {"action": "wait", "params": {"duration_minutes": duration}}
        if action_name == "reposition":
            try:
                latitude = float(params.get("latitude"))
                longitude = float(params.get("longitude"))
            except (TypeError, ValueError) as exc:
                raise ValueError("reposition.latitude/longitude 必须是数字") from exc
            for option in action_options.get("reposition_options", []):
                if not isinstance(option, dict) or not isinstance(option.get("params"), dict):
                    continue
                try:
                    opt_lat = float(option["params"].get("latitude"))
                    opt_lng = float(option["params"].get("longitude"))
                except (TypeError, ValueError):
                    continue
                if abs(latitude - opt_lat) <= 1e-6 and abs(longitude - opt_lng) <= 1e-6:
                    return {"action": "reposition", "params": {"latitude": opt_lat, "longitude": opt_lng}}
            raise ValueError(f"reposition 目标不在最终候选中: {latitude},{longitude}")
        raise ValueError(f"不支持的最终动作: {action_name}")
