"""订单可执行性模拟：对齐赛方分钟、距离和装货窗口径。"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any

from agent.settings import AgentSettings

_SIMULATION_EPOCH = datetime(2026, 3, 1, 0, 0, 0)
_WALL_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    radius_km = 6371.0
    p1 = math.radians(lat1)
    l1 = math.radians(lng1)
    p2 = math.radians(lat2)
    l2 = math.radians(lng2)
    dp = p2 - p1
    dl = l2 - l1
    h = math.sin(dp * 0.5) ** 2 + math.cos(p1) * math.cos(p2) * (math.sin(dl * 0.5) ** 2)
    return 2.0 * radius_km * math.asin(math.sqrt(min(1.0, max(0.0, h))))


def distance_to_minutes(distance_km: float, speed_km_per_hour: float) -> int:
    if distance_km <= 0:
        return 1
    return max(1, math.ceil(distance_km / speed_km_per_hour * 60))


def wall_time_to_simulation_minutes(text: str) -> int:
    wall_time = datetime.strptime(text.strip(), _WALL_TIME_FORMAT)
    return int((wall_time - _SIMULATION_EPOCH).total_seconds() // 60)


def simulation_minutes_to_wall_time(minutes: int) -> str:
    return (_SIMULATION_EPOCH + timedelta(minutes=int(minutes))).strftime(_WALL_TIME_FORMAT)


class CargoSimulator:
    """模拟订单，不执行任何环境动作。"""

    def __init__(self, settings: AgentSettings) -> None:
        self._settings = settings

    def simulate(self, status: dict[str, Any], cargo: dict[str, Any]) -> dict[str, Any]:
        try:
            return self._simulate_valid_shape(status, cargo)
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            return self._blocked(f"invalid_cargo:{type(exc).__name__}")

    def _simulate_valid_shape(
        self,
        status: dict[str, Any],
        cargo: dict[str, Any],
    ) -> dict[str, Any]:
        now_minutes = int(status["simulation_progress_minutes"])
        create_minutes = wall_time_to_simulation_minutes(str(cargo["create_time"]))
        remove_minutes = wall_time_to_simulation_minutes(str(cargo["remove_time"]))
        if not create_minutes <= now_minutes <= remove_minutes:
            return self._blocked("cargo_not_online_after_scan")

        driver_truck_length = str(status.get("truck_length", "")).strip()
        cargo_truck_lengths = cargo.get("truck_length")
        if (
            driver_truck_length
            and isinstance(cargo_truck_lengths, list)
            and cargo_truck_lengths
            and driver_truck_length not in {str(item).strip() for item in cargo_truck_lengths}
        ):
            return self._blocked("truck_length_mismatch")

        start = cargo["start"]
        end = cargo["end"]
        start_lat, start_lng = float(start["lat"]), float(start["lng"])
        end_lat, end_lng = float(end["lat"]), float(end["lng"])
        pickup_km = haversine_km(
            float(status["current_lat"]),
            float(status["current_lng"]),
            start_lat,
            start_lng,
        )
        pickup_minutes = (
            distance_to_minutes(pickup_km, self._settings.reposition_speed_km_per_hour)
            if pickup_km > 1e-6
            else 0
        )
        arrival_minutes = now_minutes + pickup_minutes

        wait_for_load_minutes = 0
        load_time = cargo.get("load_time")
        if load_time is not None:
            if not isinstance(load_time, list) or len(load_time) != 2:
                return self._blocked("invalid_load_time")
            load_start = wall_time_to_simulation_minutes(str(load_time[0]))
            load_end = wall_time_to_simulation_minutes(str(load_time[1]))
            if load_end < load_start:
                return self._blocked("invalid_load_time")
            if arrival_minutes > load_end:
                return self._blocked("cannot_reach_load_window")
            wait_for_load_minutes = max(0, load_start - arrival_minutes)

        transport_minutes = int(cargo["cost_time_minutes"])
        if transport_minutes < 0:
            return self._blocked("invalid_transport_minutes")
        finish_minutes = arrival_minutes + wait_for_load_minutes + transport_minutes
        if finish_minutes > self._settings.simulation_horizon_minutes:
            return self._blocked("simulation_horizon_exceeded")

        haul_km = haversine_km(start_lat, start_lng, end_lat, end_lng)
        gross_income = float(cargo.get("price", 0.0))
        estimated_cost = (pickup_km + haul_km) * self._settings.estimated_cost_per_km
        net_income = gross_income - estimated_cost
        occupied_minutes = max(1, finish_minutes - now_minutes)
        net_income_per_hour = net_income / occupied_minutes * 60
        return {
            "action_end_minutes": finish_minutes,
            "action_end_time": simulation_minutes_to_wall_time(finish_minutes),
            "position_after": {"lat": end_lat, "lng": end_lng},
            "metrics": {
                "pickup_deadhead_km": pickup_km,
                "pickup_minutes": pickup_minutes,
                "waiting_minutes": wait_for_load_minutes,
                "haul_distance_km": haul_km,
                "estimated_total_minutes": occupied_minutes,
                "gross_income": gross_income,
                "estimated_cost": estimated_cost,
                "net_income": net_income,
                "net_income_per_hour": net_income_per_hour,
            },
        }

    @staticmethod
    def _blocked(reason: str) -> dict[str, Any]:
        return {"rejected": True, "reason": reason}
