"""Basic deterministic candidate filtering for stage-one decision flow."""

from __future__ import annotations

from typing import Any


def filter_basic_candidates(candidates: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    summary = {
        "input_count": len(candidates),
        "expired_after_scan": 0,
        "load_time_missed": 0,
        "invalid_estimated_total_minutes": 0,
        "kept_after_basic_filter": 0,
    }
    kept: list[dict[str, Any]] = []
    for candidate in candidates:
        if candidate.get("expired_after_scan"):
            summary["expired_after_scan"] += 1
            continue
        if candidate.get("load_time_missed"):
            summary["load_time_missed"] += 1
            continue
        total = candidate.get("estimated_total_minutes")
        if not isinstance(total, int) or total <= 0:
            summary["invalid_estimated_total_minutes"] += 1
            continue
        kept.append(candidate)
    summary["kept_after_basic_filter"] = len(kept)
    return kept, summary

