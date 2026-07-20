from __future__ import annotations

import math
import time
import uuid
from dataclasses import asdict, dataclass, is_dataclass
from datetime import date, datetime
from enum import Enum
from typing import Any


SCHEMA_VERSION = 1


class Direction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class Offset(str, Enum):
    OPEN = "OPEN"
    CLOSE = "CLOSE"
    CLOSETODAY = "CLOSETODAY"
    CLOSEYESTERDAY = "CLOSEYESTERDAY"


def request_identity(request: dict[str, Any]) -> tuple[str, str, str]:
    client_id = str(request.get("client_id", "default")).strip() or "default"
    strategy_id = str(request.get("strategy_id", "default")).strip() or "default"
    client_order_id = str(request.get("client_order_id", "")).strip()
    return client_id, strategy_id, client_order_id


def require_fields(request: dict[str, Any], *fields: str) -> None:
    missing = [name for name in fields if request.get(name) in (None, "")]
    if missing:
        raise ValueError(f"Missing required fields: {', '.join(missing)}")


def new_client_order_id(strategy_id: str) -> str:
    return f"{strategy_id}-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"


def normalize_symbols(value: Any) -> list[str]:
    if isinstance(value, str):
        values = value.split(",")
    elif isinstance(value, (list, tuple, set)):
        values = value
    else:
        raise ValueError("symbols must be a string or an array")
    return list(dict.fromkeys(str(item).strip() for item in values if str(item).strip()))


def json_value(value: Any) -> Any:
    if is_dataclass(value):
        return json_value(asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def response_ok(data: Any = None, request_id: str | None = None) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "ok",
        "request_id": request_id,
        "data": json_value(data),
        "error": None,
    }


def response_error(message: str, request_id: str | None = None) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "error",
        "request_id": request_id,
        "data": None,
        "error": {"message": message},
    }


def event_payload(event: str, data: Any) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "event": event,
        "published_at": int(time.time() * 1000),
        "data": json_value(data),
    }
