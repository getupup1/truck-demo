"""MVP 候选排序。"""

from __future__ import annotations

from typing import Any


class CandidateScorer:
    """优先选择单位占用时间净收益更高的候选。"""

    def rank(self, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(
            candidates,
            key=lambda candidate: (
                float(candidate["final_score"]),
                float(candidate.get("simulation", {}).get("metrics", {}).get("net_income", 0.0)),
            ),
            reverse=True,
        )
