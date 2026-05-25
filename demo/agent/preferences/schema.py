"""Schema definitions for the two-stage Preference DSL compiler."""

from __future__ import annotations

from typing import Any, Literal, TypedDict


ActionType = Literal["take_order", "reposition", "wait"]
EventStatus = Literal["accepted", "simulated_success"]
IRType = Literal["constraint", "policy", "unresolved"]
DSLType = Literal[
    "attribute_match",
    "metric_threshold",
    "time_window_action_limit",
    "continuous_rest_requirement",
    "aggregate_quota",
    "geo_region_limit",
    "deadline_location",
    "unresolved",
]
PenaltyUnit = Literal["per_event", "per_day", "per_km_over", "per_minute", "once"]


ACTION_TYPES = ("take_order", "reposition", "wait")
EVENT_STATUSES = ("accepted", "simulated_success")
IR_TYPES = ("constraint", "policy", "unresolved")
DSL_TYPES = (
    "attribute_match",
    "metric_threshold",
    "time_window_action_limit",
    "continuous_rest_requirement",
    "aggregate_quota",
    "geo_region_limit",
    "deadline_location",
    "unresolved",
)
PENALTY_UNITS = ("per_event", "per_day", "per_km_over", "per_minute", "once")
ENFORCEMENT_FIELDS = ("penalty_unit", "penalty_amount", "penalty_cap", "boost_amount")


STAGE1_FIELDS = (
    "item_id",
    "source_text",
    "parent_source_text",
    "ir_type",
    "dsl_type",
    "start_time",
    "end_time",
    "penalty_amount",
    "penalty_cap",
    "reason",
    "confidence",
)

PREFERENCE_IR_FIELDS = (
    "pref_id",
    "source_text",
    "ir_type",
    "dsl_type",
    "active_window",
    "event_filter",
    "enforcement",
    "condition",
    "target",
    "scope",
    "confidence",
    "meta",
)


class Stage1PreferenceItem(TypedDict):
    item_id: str
    source_text: str
    parent_source_text: str
    ir_type: str
    dsl_type: str
    start_time: str | None
    end_time: str | None
    penalty_amount: float | None
    penalty_cap: float | None
    reason: str
    confidence: float


class Stage2PreferenceItem(Stage1PreferenceItem):
    start_minutes: int
    end_minutes: int | None


class ActiveWindow(TypedDict):
    start_minutes: int
    end_minutes: int | None


class EventFilter(TypedDict):
    action_types: list[str]
    status: list[str]


class Enforcement(TypedDict):
    penalty_unit: str
    penalty_amount: float
    penalty_cap: float | None
    boost_amount: float


class PreferenceIR(TypedDict):
    pref_id: str
    source_text: str
    ir_type: str
    dsl_type: str
    active_window: ActiveWindow
    event_filter: EventFilter
    enforcement: Enforcement
    condition: dict[str, Any]
    target: dict[str, Any]
    scope: dict[str, Any]
    confidence: float
    meta: dict[str, Any]
