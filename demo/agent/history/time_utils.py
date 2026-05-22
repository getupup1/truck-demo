"""Time, distance, and coercion helpers for agent-side simulation estimates."""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any


SIMULATION_EPOCH = datetime(2026, 3, 1, 0, 0, 0)
WALL_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


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


def pickup_distance_to_minutes(distance_km: float, speed_km_per_hour: float) -> int:
    if distance_km <= 1e-6:
        return 0
    return distance_to_minutes(distance_km, speed_km_per_hour)


def coerce_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def coerce_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default

