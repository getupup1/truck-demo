"""增量轨迹记忆：将动作日志整理为偏好 progress 可直接消费的轻量快照。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from simkit.ports import SimulationApiPort


class TrajectoryMemory:
    """按司机缓存历史动作；常规拉取最后一步，发现缺口或重置时自动全量重建。"""

    def __init__(self, api: SimulationApiPort) -> None:
        self._api = api
        self._events_by_driver: dict[str, list[dict[str, Any]]] = {}
        self._total_steps_by_driver: dict[str, int] = {}

    def refresh(self, driver_id: str) -> dict[str, Any]:
        """同步司机最新历史并返回快照。"""
        driver_id = driver_id.strip()
        latest = self._query(driver_id, 1)
        total_steps = self._read_total_steps(latest)
        cached_steps = self._total_steps_by_driver.get(driver_id, 0)
        events = self._events_by_driver.setdefault(driver_id, [])

        if total_steps == cached_steps:
            return self.snapshot(driver_id)

        records = self._read_records(latest)
        if total_steps == cached_steps + 1 and len(records) == 1:
            events.append(self._normalize_record(records[0], self._last_end_minutes(events)))
            self._total_steps_by_driver[driver_id] = total_steps
            return self.snapshot(driver_id)

        full = self._query(driver_id, -1)
        full_records = self._read_records(full)
        full_total_steps = self._read_total_steps(full)
        if len(full_records) != full_total_steps:
            raise ValueError("query_decision_history(step=-1) 返回记录数与 total_steps 不一致")

        rebuilt: list[dict[str, Any]] = []
        for record in full_records:
            rebuilt.append(self._normalize_record(record, self._last_end_minutes(rebuilt)))
        self._events_by_driver[driver_id] = rebuilt
        self._total_steps_by_driver[driver_id] = full_total_steps
        return self.snapshot(driver_id)

    def snapshot(self, driver_id: str) -> dict[str, Any]:
        """返回独立副本，避免规则层误改内部缓存。"""
        driver_id = driver_id.strip()
        events = self._events_by_driver.get(driver_id, [])
        return {
            "driver_id": driver_id,
            "total_steps": self._total_steps_by_driver.get(driver_id, 0),
            "last_step_end_minutes": self._last_end_minutes(events),
            "action_counts": self._count_actions(events),
            "active_minutes_by_day": self._active_minutes_by_day(events),
            "wait_intervals_by_day": self._wait_intervals_by_day(events),
            "events": deepcopy(events),
        }

    def _query(self, driver_id: str, step: int) -> dict[str, Any]:
        response = self._api.query_decision_history(driver_id, step)
        if not isinstance(response, dict):
            raise TypeError("query_decision_history 返回值必须为对象")
        return response

    @staticmethod
    def _read_total_steps(response: dict[str, Any]) -> int:
        total_steps = int(response.get("total_steps", 0))
        if total_steps < 0:
            raise ValueError("query_decision_history.total_steps 不能为负数")
        return total_steps

    @staticmethod
    def _read_records(response: dict[str, Any]) -> list[dict[str, Any]]:
        records = response.get("records", [])
        if not isinstance(records, list) or any(not isinstance(item, dict) for item in records):
            raise TypeError("query_decision_history.records 必须为对象数组")
        return records

    @staticmethod
    def _last_end_minutes(events: list[dict[str, Any]]) -> int:
        return int(events[-1]["step_end_minutes"]) if events else 0

    @staticmethod
    def _normalize_record(record: dict[str, Any], previous_end_minutes: int) -> dict[str, Any]:
        action_obj = record.get("action")
        result = record.get("result")
        if not isinstance(action_obj, dict) or not isinstance(result, dict):
            raise TypeError("历史记录缺少有效 action 或 result")

        query_scan_minutes = int(record.get("query_scan_cost_minutes", 0))
        action_exec_minutes = int(record.get("action_exec_cost_minutes", 0))
        step_elapsed_minutes = int(record.get("step_elapsed_minutes", 0))
        if min(query_scan_minutes, action_exec_minutes, step_elapsed_minutes) < 0:
            raise ValueError("历史记录耗时不能为负数")
        if step_elapsed_minutes != query_scan_minutes + action_exec_minutes:
            raise ValueError("历史记录 step_elapsed_minutes 与查询、动作耗时不一致")

        action_start_minutes = previous_end_minutes + query_scan_minutes
        action_end_minutes = action_start_minutes + action_exec_minutes
        step_end_minutes = int(result["simulation_progress_minutes"])
        if step_end_minutes != action_end_minutes:
            raise ValueError("历史记录结束时间与耗时不一致")

        return {
            "step": int(record["step"]),
            "action": str(action_obj.get("action", "")).strip().lower(),
            "params": deepcopy(action_obj.get("params") or {}),
            "step_start_minutes": previous_end_minutes,
            "action_start_minutes": action_start_minutes,
            "action_end_minutes": action_end_minutes,
            "step_end_minutes": step_end_minutes,
            "query_scan_cost_minutes": query_scan_minutes,
            "action_exec_cost_minutes": action_exec_minutes,
            "position_before": TrajectoryMemory._position(record.get("position_before")),
            "position_after": TrajectoryMemory._position(record.get("position_after")),
            "accepted": result.get("accepted"),
            "metrics": TrajectoryMemory._distance_metrics(result),
            "cargo_touch_points": TrajectoryMemory._cargo_touch_points(result),
        }

    @staticmethod
    def _position(raw: Any) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise TypeError("历史记录位置必须为对象")
        position: dict[str, Any] = {"lat": float(raw["lat"]), "lng": float(raw["lng"])}
        if "region_names" in raw:
            names = raw["region_names"]
            if not isinstance(names, list) or any(not isinstance(name, str) for name in names):
                raise TypeError("历史记录位置 region_names 必须为字符串数组")
            position["region_names"] = list(names)
        return position

    @staticmethod
    def _distance_metrics(result: dict[str, Any]) -> dict[str, float]:
        metrics: dict[str, float] = {}
        for field in ("pickup_deadhead_km", "haul_distance_km", "distance_km"):
            if field in result:
                metrics[field] = float(result[field])
        return metrics

    @staticmethod
    def _cargo_touch_points(result: dict[str, Any]) -> list[dict[str, Any]]:
        raw_points = result.get("cargo_touch_points", [])
        if not isinstance(raw_points, list):
            raise TypeError("历史记录 cargo_touch_points 必须为数组")
        points: list[dict[str, Any]] = []
        for raw in raw_points:
            if not isinstance(raw, dict):
                raise TypeError("历史记录 cargo_touch_points 元素必须为对象")
            point: dict[str, Any] = {"lat": float(raw["lat"]), "lng": float(raw["lng"])}
            if "at_minutes" in raw:
                point["at_minutes"] = int(raw["at_minutes"])
            if "region_names" in raw:
                names = raw["region_names"]
                if not isinstance(names, list) or any(not isinstance(name, str) for name in names):
                    raise TypeError("历史记录 cargo_touch_points.region_names 必须为字符串数组")
                point["region_names"] = list(names)
            points.append(point)
        return points

    @staticmethod
    def _count_actions(events: list[dict[str, Any]]) -> dict[str, int]:
        counts = {"take_order": 0, "reposition": 0, "wait": 0}
        for event in events:
            action = event["action"]
            counts[action] = counts.get(action, 0) + 1
        return counts

    @staticmethod
    def _active_minutes_by_day(events: list[dict[str, Any]]) -> dict[str, int]:
        active: dict[str, int] = {}
        for event in events:
            if event["action"] not in {"take_order", "reposition"}:
                continue
            for day, start, end in TrajectoryMemory._split_by_day(
                event["action_start_minutes"],
                event["action_end_minutes"],
            ):
                active[day] = active.get(day, 0) + end - start
        return active

    @staticmethod
    def _wait_intervals_by_day(events: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        intervals_by_day: dict[str, list[dict[str, Any]]] = {}
        for event in events:
            if event["action"] != "wait":
                continue
            for day, start, end in TrajectoryMemory._split_by_day(
                event["action_start_minutes"],
                event["action_end_minutes"],
            ):
                interval = {
                    "start_minutes": start,
                    "end_minutes": end,
                    "duration_minutes": end - start,
                    "position": deepcopy(event["position_after"]),
                }
                intervals_by_day.setdefault(day, []).append(interval)
        return intervals_by_day

    @staticmethod
    def _split_by_day(start_minutes: int, end_minutes: int) -> list[tuple[str, int, int]]:
        spans: list[tuple[str, int, int]] = []
        cursor = int(start_minutes)
        end_minutes = int(end_minutes)
        while cursor < end_minutes:
            day_index = cursor // 1440
            day_end = (day_index + 1) * 1440
            span_end = min(day_end, end_minutes)
            spans.append((str(day_index), cursor, span_end))
            cursor = span_end
        return spans
