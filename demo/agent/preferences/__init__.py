"""司机偏好 DSL 与运行时 progress。"""

from agent.preferences.cargo_rules import (
    build_cargo_category_policy_rule,
)
from agent.preferences.distance_rules import build_distance_limit_rule
from agent.preferences.registry import calculate_rule_progress, supported_rule_types
from agent.preferences.off_day_rules import build_monthly_off_day_quota_rule
from agent.preferences.region_rules import build_region_policy_rule
from agent.preferences.region_visit_rules import build_region_visit_quota_rule
from agent.preferences.rest_rules import (
    build_daily_continuous_rest_rule,
    build_daily_rest_window_rule,
)

__all__ = [
    "build_cargo_category_policy_rule",
    "build_daily_continuous_rest_rule",
    "build_daily_rest_window_rule",
    "build_distance_limit_rule",
    "build_monthly_off_day_quota_rule",
    "build_region_policy_rule",
    "build_region_visit_quota_rule",
    "calculate_rule_progress",
    "supported_rule_types",
]
