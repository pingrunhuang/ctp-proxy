from __future__ import annotations

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
                "CREATE INDEX IF NOT EXISTS idx_ctp_orders_sys_id ON ctp_orders(exchange_id, order_sys_id, updated_at DESC)"
            )

    def create(self, *, client_id: str, strategy_id: str, client_order_id: str, symbol: str, order_ref: str, payload: str) -> bool:
        with self._pool.connection() as connection:
            row = connection.execute(
                """
                INSERT INTO ctp_orders
                (client_id, strategy_id, client_order_id, order_ref, symbol, status, payload)
                VALUES (%s, %s, %s, %s, %s, 'ACCEPTED', %s::jsonb)
                ON CONFLICT (client_id, strategy_id, client_order_id) DO NOTHING
                RETURNING id
                """,
                (client_id, strategy_id, client_order_id, order_ref, symbol, payload),
            ).fetchone()
            return row is not None

    def get(self, client_id: str, strategy_id: str, client_order_id: str) -> dict[str, Any] | None:
        with self._pool.connection() as connection:
            row = connection.execute(
                "SELECT * FROM ctp_orders WHERE client_id=%s AND strategy_id=%s AND client_order_id=%s",
                (client_id, strategy_id, client_order_id),
            ).fetchone()
            return row

    def find_by_ctp(self, order_ref: str = "", exchange_id: str = "", order_sys_id: str = "") -> dict[str, Any] | None:
        with self._pool.connection() as connection:
            if order_ref:
                row = connection.execute(
                    "SELECT * FROM ctp_orders WHERE order_ref=%s ORDER BY updated_at DESC LIMIT 1",
                    (order_ref,),
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
            return None

    def update_ctp(self, *, order_ref: str, front_id: int, session_id: int, exchange_id: str, order_sys_id: str, status: str) -> None:
        with self._pool.connection() as connection:
            connection.execute(
                """
                UPDATE ctp_orders
                SET front_id=%s, session_id=%s, exchange_id=%s, order_sys_id=%s,
                    status=%s, updated_at=NOW()
                WHERE id = (
                    SELECT id FROM ctp_orders WHERE order_ref=%s ORDER BY updated_at DESC LIMIT 1
                )
                """,
                (front_id, session_id, exchange_id, order_sys_id, status, order_ref),
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

    def is_healthy(self) -> bool:
        try:
            with self._pool.connection() as connection:
                return connection.execute("SELECT 1 AS ok").fetchone()["ok"] == 1
        except Exception:
            return False

    def close(self) -> None:
        self._pool.close()
