import os
import sys
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from registry import OrderRegistry


DATABASE_URL = os.getenv("TEST_DATABASE_URL")


@pytest.mark.integration
@pytest.mark.skipif(not DATABASE_URL, reason="TEST_DATABASE_URL is not configured")
def test_postgres_order_registry_round_trip():
    registry = OrderRegistry(DATABASE_URL)
    client_order_id = "pytest-postgres-registry"
    try:
        with registry._pool.connection() as connection:
            connection.execute(
                "DELETE FROM ctp_orders WHERE client_id=%s AND strategy_id=%s AND client_order_id=%s",
                ("pytest", "registry", client_order_id),
            )
        assert registry.create(
            client_id="pytest",
            strategy_id="registry",
            client_order_id=client_order_id,
            symbol="au2608",
            order_ref="9001",
            payload='{"source":"pytest"}',
        )
        assert not registry.create(
            client_id="pytest",
            strategy_id="registry",
            client_order_id=client_order_id,
            symbol="au2608",
            order_ref="9002",
            payload="{}",
        )
        registry.update_ctp(
            order_ref="9001",
            front_id=1,
            session_id=2,
            exchange_id="SHFE",
            order_sys_id="SYS-9001",
            status="SUBMITTED",
        )
        order = registry.get("pytest", "registry", client_order_id)
        assert order["order_sys_id"] == "SYS-9001"
        assert registry.find_by_ctp(exchange_id="SHFE", order_sys_id="SYS-9001")["strategy_id"] == "registry"
        assert registry.is_healthy()
    finally:
        with registry._pool.connection() as connection:
            connection.execute(
                "DELETE FROM ctp_orders WHERE client_id=%s AND strategy_id=%s AND client_order_id=%s",
                ("pytest", "registry", client_order_id),
            )
        registry.close()
