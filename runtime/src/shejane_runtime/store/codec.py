"""Stable JSON encoding helpers shared by SQLite store modules."""

from __future__ import annotations

import json
from typing import Any


def encode_payload(payload: dict[str, Any]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def json_payload(raw: Any) -> dict[str, Any]:
    try:
        value = json.loads(str(raw or "{}"))
    except (json.JSONDecodeError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}
