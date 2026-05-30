"""货物属性类偏好 DSL。"""

from __future__ import annotations

from typing import Any

CARGO_CATEGORY_POLICY = "cargo_category_policy"


def build_cargo_category_policy_rule(
    rule_id: str,
    categories: list[str],
) -> dict[str, Any]:
    """构造“禁止某些货物品类”的原子 DSL。"""
    rule = {
        "rule_id": rule_id,
        "type": CARGO_CATEGORY_POLICY,
        "progress_kind": "none",
        "active_period": None,
        "params": {
            "categories": list(categories),
        },
    }
    validate_cargo_category_policy_rule(rule)
    return rule


def validate_cargo_category_policy_rule(rule: dict[str, Any]) -> None:
    """校验货物品类原子 DSL。"""
    if not isinstance(rule, dict):
        raise TypeError("rule 必须为对象")
    rule_id = rule.get("rule_id")
    if not isinstance(rule_id, str) or not rule_id.strip():
        raise ValueError("rule.rule_id 必须为非空字符串")
    if rule.get("type") != CARGO_CATEGORY_POLICY:
        raise ValueError(f"规则类型应为 {CARGO_CATEGORY_POLICY}")
    if rule.get("progress_kind") != "none":
        raise ValueError("cargo_category_policy progress_kind 必须为 none")
    if rule.get("active_period") is not None:
        raise ValueError("当前 cargo_category_policy 暂不支持 active_period")
    params = rule.get("params")
    if not isinstance(params, dict):
        raise TypeError("rule.params 必须为对象")
    categories = params.get("categories")
    if not isinstance(categories, list) or not categories:
        raise ValueError("cargo_category_policy.params.categories 必须为非空数组")
    if any(not isinstance(category, str) or not category.strip() for category in categories):
        raise ValueError("cargo_category_policy.params.categories 只能包含非空字符串")
    if len(set(categories)) != len(categories):
        raise ValueError("cargo_category_policy.params.categories 不能重复")
