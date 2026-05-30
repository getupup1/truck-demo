"""MVP 决策参数：后续调参集中放在这里。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AgentSettings:
    """Agent 可自行维护的参数，不依赖评测端内部配置。"""

    cargo_query_k: int = 50
    estimated_cost_per_km: float = 1.5
    reposition_speed_km_per_hour: float = 60.0
    simulation_horizon_minutes: int = 31 * 24 * 60
    default_wait_minutes: int = 30
    end_of_month_wait_threshold_minutes: int = 30
    minimum_order_net_income: float = 0.0
    minimum_order_net_income_per_hour: float = 0.0

