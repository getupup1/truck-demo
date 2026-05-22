"""JSON response helpers for model calls."""

from __future__ import annotations

import json
from typing import Any


def extract_model_json_object(model_resp: dict[str, Any]) -> dict[str, Any]:
    choices = model_resp.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("模型返回缺少 choices")
    message = choices[0].get("message", {})
    if not isinstance(message, dict):
        raise ValueError("模型返回 message 格式无效")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("模型返回 content 为空")
    data = json.loads(content)
    if not isinstance(data, dict):
        raise ValueError("模型返回不是 JSON 对象")
    return data


def json_dumps_compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

