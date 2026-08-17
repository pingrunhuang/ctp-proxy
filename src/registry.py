from __future__ import annotations

import json
import threading
import time
from collections import defaultdict
from typing import Any

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


class SubscriptionRegistry:
    def __init__(self) -> None:
        self._subscriptions: dict[str, set[tuple[str, str]]] = defaultdict(set)
        self._lock = threading.RLock()

    def subscribe(self, client_id: str, strategy_id: str, symbols: list[str]) -> list[str]:
        newly_active: list[str] = []
        with self._lock:
            owner = (client_id, strategy_id)
            for symbol in symbols:
                if not self._subscriptions[symbol]:
                    newly_active.append(symbol)
                self._subscriptions[symbol].add(owner)
        return newly_active

    def unsubscribe(self, client_id: str, strategy_id: str, symbols: list[str]) -> list[str]:
        newly_inactive: list[str] = []
        with self._lock:
            owner = (client_id, strategy_id)
            for symbol in symbols:
                subscribers = self._subscriptions.get(symbol)
                if not subscribers:
                    continue
                subscribers.discard(owner)
                if not subscribers:
                    self._subscriptions.pop(symbol, None)
                    newly_inactive.append(symbol)
        return newly_inactive

    def active_symbols(self) -> list[str]:
        with self._lock:
            return sorted(self._subscriptions)


class SnapshotCache:
    def __init__(self) -> None:
        self._items: dict[str, tuple[float, Any]] = {}
        self._lock = threading.RLock()

    def put(self, key: str, value: Any) -> None:
        with self._lock:
            self._items[key] = (time.monotonic(), value)

    def get(self, key: str, max_age_seconds: float) -> Any | None:
        with self._lock:
            item = self._items.get(key)
            if item is None or time.monotonic() - item[0] > max_age_seconds:
                return None
            return item[1]

    def peek(self, key: str) -> Any | None:
        with self._lock:
            item = self._items.get(key)
            return None if item is None else item[1]


class OrderRegistry:
    """PostgreSQL-backed ownership and CTP identifier mapping."""

    def __init__(
        self,
        database_url: str,
        min_size: int = 1,
        max_size: int = 5,
        connect_timeout_seconds: float = 10.0,
    ) -> None:
        self._pool = ConnectionPool(
            conninfo=database_url,
            min_size=min_size,
            max_size=max_size,
            timeout=connect_timeout_seconds,
            kwargs={"autocommit": True, "row_factory": dict_row},
            open=False,
        )
        self._pool.open(wait=True, timeout=connect_timeout_seconds)
        self._initialize_schema()

    def _initialize_schema(self) -> None:
        with self._pool.connection() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS ctp_orders (
                    id BIGSERIAL PRIMARY KEY,
                    client_id TEXT NOT NULL,
                    strategy_id TEXT NOT NULL,
                    client_order_id TEXT NOT NULL,
                    order_ref TEXT,
                    front_id INTEGER,
                    session_id INTEGER,
                    exchange_id TEXT,
                    order_sys_id TEXT,
                    symbol TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE (client_id, strategy_id, client_order_id)
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_ctp_orders_order_ref ON ctp_orders(order_ref, updated_at DESC)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_ctp_orders_local_id ON ctp_orders(front_id, session_id, order_ref, updated_at DESC)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_ctp_orders_sys_id ON ctp_orders(exchange_id, order_sys_id, updated_at DESC)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS ctp_trades (
                    id BIGSERIAL PRIMARY KEY,
                    event_id TEXT NOT NULL UNIQUE,
                    client_id TEXT NOT NULL DEFAULT '',
                    strategy_id TEXT NOT NULL DEFAULT '',
                    account_id TEXT NOT NULL DEFAULT '',
                    trading_day TEXT NOT NULL DEFAULT '',
                    trade_id TEXT NOT NULL DEFAULT '',
                    payload JSONB NOT NULL,
                    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_ctp_trades_owner_cursor "
                "ON ctp_trades(client_id, strategy_id, id)"
            )

    def create(
        self,
        *,
        client_id: str,
        strategy_id: str,
        client_order_id: str,
        symbol: str,
        order_ref: str,
        front_id: int,
        session_id: int,
        exchange_id: str,
        payload: str,
    ) -> bool:
        with self._pool.connection() as connection:
            row = connection.execute(
                """
                INSERT INTO ctp_orders
                (client_id, strategy_id, client_order_id, order_ref, front_id,
                 session_id, exchange_id, symbol, status, payload)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'PENDING_SUBMIT', %s::jsonb)
                ON CONFLICT (client_id, strategy_id, client_order_id) DO NOTHING
                RETURNING id
                """,
                (
                    client_id,
                    strategy_id,
                    client_order_id,
                    order_ref,
                    front_id,
                    session_id,
                    exchange_id,
                    symbol,
                    payload,
                ),
            ).fetchone()
            return row is not None

    def get(self, client_id: str, strategy_id: str, client_order_id: str) -> dict[str, Any] | None:
        with self._pool.connection() as connection:
            row = connection.execute(
                "SELECT * FROM ctp_orders WHERE client_id=%s AND strategy_id=%s AND client_order_id=%s",
                (client_id, strategy_id, client_order_id),
            ).fetchone()
            return row

    def find_by_ctp(
        self,
        order_ref: str = "",
        exchange_id: str = "",
        order_sys_id: str = "",
        front_id: int = 0,
        session_id: int = 0,
    ) -> dict[str, Any] | None:
        with self._pool.connection() as connection:
            if front_id and session_id and order_ref:
                row = connection.execute(
                    """
                    SELECT * FROM ctp_orders
                    WHERE front_id=%s AND session_id=%s AND order_ref=%s
                    ORDER BY updated_at DESC LIMIT 1
                    """,
                    (front_id, session_id, order_ref),
                ).fetchone()
                if row is not None:
                    return row
            if exchange_id and order_sys_id:
                row = connection.execute(
                    "SELECT * FROM ctp_orders WHERE exchange_id=%s AND order_sys_id=%s ORDER BY updated_at DESC LIMIT 1",
                    (exchange_id, order_sys_id),
                ).fetchone()
                if row is not None:
                    return row
            # Compatibility for records created before session identifiers were
            # persisted. Never fall back to a different, populated CTP session.
            if order_ref:
                row = connection.execute(
                    """
                    SELECT * FROM ctp_orders
                    WHERE order_ref=%s AND front_id IS NULL AND session_id IS NULL
                    ORDER BY updated_at DESC LIMIT 1
                    """,
                    (order_ref,),
                ).fetchone()
                if row is not None:
                    return row
            return None

    def update_ctp(self, *, order_ref: str, front_id: int, session_id: int, exchange_id: str, order_sys_id: str, status: str) -> None:
        with self._pool.connection() as connection:
            connection.execute(
                """
                UPDATE ctp_orders
                SET front_id=%s, session_id=%s, exchange_id=%s, order_sys_id=%s,
                    status=%s, updated_at=NOW()
                WHERE id = (
                    SELECT id FROM ctp_orders
                    WHERE (
                        front_id=%s AND session_id=%s AND order_ref=%s
                    ) OR (
                        order_ref=%s AND front_id IS NULL AND session_id IS NULL
                    )
                    ORDER BY
                        CASE WHEN front_id=%s AND session_id=%s THEN 0 ELSE 1 END,
                        updated_at DESC
                    LIMIT 1
                )
                """,
                (
                    front_id,
                    session_id,
                    exchange_id,
                    order_sys_id,
                    status,
                    front_id,
                    session_id,
                    order_ref,
                    order_ref,
                    front_id,
                    session_id,
                ),
            )

    def list(self, strategy_id: str | None = None) -> list[dict[str, Any]]:
        with self._pool.connection() as connection:
            if strategy_id:
                rows = connection.execute(
                    "SELECT * FROM ctp_orders WHERE strategy_id=%s ORDER BY updated_at DESC",
                    (strategy_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM ctp_orders ORDER BY updated_at DESC"
                ).fetchall()
            return rows

    def record_trade(self, payload: dict[str, Any]) -> bool:
        """Persist one immutable trade before it is published."""
        event_id = str(payload.get("event_id") or "").strip()
        if not event_id:
            raise ValueError("trade payload requires event_id")
        with self._pool.connection() as connection:
            row = connection.execute(
                """
                INSERT INTO ctp_trades(
                    event_id, client_id, strategy_id, account_id,
                    trading_day, trade_id, payload
                ) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
                ON CONFLICT (event_id) DO NOTHING
                RETURNING id
                """,
                (
                    event_id,
                    str(payload.get("client_id") or ""),
                    str(payload.get("strategy_id") or ""),
                    str(payload.get("account_id") or ""),
                    str(payload.get("trading_day") or ""),
                    str(payload.get("trade_id") or ""),
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str),
                ),
            ).fetchone()
            return row is not None

    def list_trades(
        self,
        client_id: str,
        strategy_id: str,
        *,
        after_id: int = 0,
        limit: int = 500,
    ) -> dict[str, Any]:
        page_size = min(max(int(limit), 1), 1000)
        with self._pool.connection() as connection:
            rows = connection.execute(
                """
                SELECT id, payload
                FROM ctp_trades
                WHERE client_id=%s AND strategy_id=%s AND id>%s
                ORDER BY id ASC
                LIMIT %s
                """,
                (client_id, strategy_id, max(int(after_id), 0), page_size + 1),
            ).fetchall()
        has_more = len(rows) > page_size
        page = rows[:page_size]
        return {
            "trades": [row["payload"] for row in page],
            "next_after_id": int(page[-1]["id"]) if page else max(int(after_id), 0),
            "has_more": has_more,
        }

    def is_healthy(self) -> bool:
        try:
            with self._pool.connection() as connection:
                return connection.execute("SELECT 1 AS ok").fetchone()["ok"] == 1
        except Exception:
            return False

    def close(self) -> None:
        self._pool.close()
