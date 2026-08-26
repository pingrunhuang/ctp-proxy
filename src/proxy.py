from __future__ import annotations

import json
import queue
import threading
from typing import Any

import zmq
from loguru import logger

from config import Settings
from ctp_session import CtpSession
from protocol import (
    Direction,
    Offset,
    event_payload,
    normalize_symbols,
    request_identity,
    require_fields,
    response_error,
    response_ok,
)
from registry import OrderRegistry, SubscriptionRegistry


class CtpProxy:
    def __init__(
        self,
        settings: Settings,
        session: Any | None = None,
        order_registry: Any | None = None,
    ) -> None:
        self.settings = settings
        self.subscriptions = SubscriptionRegistry()
        self.order_registry = order_registry or OrderRegistry(
            settings.database_url,
            min_size=settings.database_pool_min_size,
            max_size=settings.database_pool_max_size,
            connect_timeout_seconds=settings.database_connect_timeout_seconds,
        )
        self._publish_queue: queue.Queue[tuple[str, str, dict[str, Any]]] = queue.Queue(maxsize=100_000)
        self.session = session or CtpSession(settings, self.enqueue_publish, self.order_registry)
        self.session.set_active_symbols_provider(self.subscriptions.active_symbols)
        if settings.initial_symbols:
            self.subscriptions.subscribe("proxy", "startup", settings.initial_symbols)

        self.context: zmq.Context | None = None
        self.pub_socket: zmq.Socket | None = None
        self.rep_socket: zmq.Socket | None = None
        self.active = threading.Event()
        self._publisher_thread: threading.Thread | None = None
        self._command_thread: threading.Thread | None = None

    def enqueue_publish(self, topic: str, event: str, data: dict[str, Any]) -> None:
        try:
            self._publish_queue.put_nowait((topic, event, data))
        except queue.Full:
            logger.error("Publish queue is full; dropping topic={}", topic)

    def connect(self, timeout: float = 20.0) -> bool:
        return bool(self.session.connect(timeout))

    def start(self) -> None:
        if self.active.is_set():
            return
        self.context = zmq.Context()
        self.pub_socket = self.context.socket(zmq.PUB)
        self.pub_socket.setsockopt(zmq.LINGER, 0)
        self.pub_socket.bind(f"tcp://{self.settings.zmq_bind_host}:{self.settings.zmq_pub_port}")
        self.rep_socket = self.context.socket(zmq.REP)
        self.rep_socket.setsockopt(zmq.LINGER, 0)
        self.rep_socket.bind(f"tcp://{self.settings.zmq_bind_host}:{self.settings.zmq_rep_port}")
        self.active.set()
        self._publisher_thread = threading.Thread(target=self._publisher_loop, name="ctp-publisher", daemon=True)
        self._command_thread = threading.Thread(target=self._command_loop, name="ctp-commands", daemon=True)
        self._publisher_thread.start()
        self._command_thread.start()
        logger.info(
            "CTP proxy listening: PUB={}:{} REP={}:{}",
            self.settings.zmq_bind_host,
            self.settings.zmq_pub_port,
            self.settings.zmq_bind_host,
            self.settings.zmq_rep_port,
        )

    def _publisher_loop(self) -> None:
        assert self.pub_socket is not None
        while self.active.is_set() or not self._publish_queue.empty():
            try:
                topic, event, data = self._publish_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self.pub_socket.send_multipart(
                    [topic.encode("utf-8"), json.dumps(event_payload(event, data), ensure_ascii=False, allow_nan=False).encode("utf-8")]
                )
            except Exception:
                logger.exception("Failed to publish topic={}", topic)

    def _command_loop(self) -> None:
        assert self.rep_socket is not None
        poller = zmq.Poller()
        poller.register(self.rep_socket, zmq.POLLIN)
        while self.active.is_set():
            try:
                events = dict(poller.poll(250))
                if self.rep_socket not in events:
                    continue
                request = self.rep_socket.recv_json()
                response = self.handle_command(request)
            except Exception as exc:
                logger.exception("Command loop error")
                response = response_error(str(exc))
            try:
                self.rep_socket.send_json(response)
            except zmq.ZMQError:
                if self.active.is_set():
                    logger.exception("Failed to send command response")

    def _query_max_age(self, request: dict[str, Any]) -> float | None:
        if request.get("force_refresh"):
            return None
        max_age_ms = request.get("max_age_ms")
        if max_age_ms is None:
            return self.settings.snapshot_ttl_seconds
        return max(float(max_age_ms) / 1000.0, 0.0)

    def handle_command(self, request: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(request, dict):
            return response_error("request must be a JSON object")
        request_id = str(request.get("request_id", "")) or None
        try:
            action = str(request.get("action", "")).strip().lower()
            if not action:
                raise ValueError("Missing required field: action")
            client_id, strategy_id, client_order_id = request_identity(request)

            if action == "ping":
                return response_ok(
                    {
                        "service": "ctp-proxy",
                        "ready": self.session.is_ready(),
                        "database_ready": self.order_registry.is_healthy(),
                        "active_symbols": self.subscriptions.active_symbols(),
                        "md_enabled": self.settings.enable_md,
                        "td_enabled": self.settings.enable_td,
                    },
                    request_id,
                )
            if action == "subscribe_market_data":
                if not self.settings.enable_md:
                    raise RuntimeError("CTP market data is disabled by CTP_ENABLE_MD=false")
                symbols = normalize_symbols(request.get("symbols"))
                newly_active = self.subscriptions.subscribe(client_id, strategy_id, symbols)
                self.session.subscribe_market_data(newly_active)
                return response_ok(
                    {"symbols": symbols, "newly_active": newly_active, "active_symbols": self.subscriptions.active_symbols()},
                    request_id,
                )
            if action == "unsubscribe_market_data":
                if not self.settings.enable_md:
                    raise RuntimeError("CTP market data is disabled by CTP_ENABLE_MD=false")
                symbols = normalize_symbols(request.get("symbols"))
                newly_inactive = self.subscriptions.unsubscribe(client_id, strategy_id, symbols)
                self.session.unsubscribe_market_data(newly_inactive)
                return response_ok(
                    {"symbols": symbols, "newly_inactive": newly_inactive, "active_symbols": self.subscriptions.active_symbols()},
                    request_id,
                )
            if action == "get_account":
                if not self.settings.enable_td:
                    raise RuntimeError("CTP trading is disabled by CTP_ENABLE_TD=false")
                return response_ok(self.session.query_account(self._query_max_age(request)), request_id)
            if action == "get_positions":
                if not self.settings.enable_td:
                    raise RuntimeError("CTP trading is disabled by CTP_ENABLE_TD=false")
                return response_ok(self.session.query_positions(self._query_max_age(request)), request_id)
            if action == "get_orders":
                if request.get("local_only"):
                    data = self.order_registry.list(strategy_id if request.get("strategy_only") else None)
                    if client_id:
                        data = [row for row in data if str(row.get("client_id") or "") == client_id]
                else:
                    if not self.settings.enable_td:
                        raise RuntimeError("CTP trading is disabled by CTP_ENABLE_TD=false")
                    data = self.session.query_orders(self._query_max_age(request))
                return response_ok(data, request_id)
            if action == "get_trades":
                require_fields(request, "client_id", "strategy_id")
                return response_ok(
                    self.order_registry.list_trades(
                        client_id,
                        strategy_id,
                        after_id=int(request.get("after_id") or 0),
                        limit=int(request.get("limit") or 500),
                    ),
                    request_id,
                )
            if action == "get_trade_cursor":
                require_fields(request, "client_id", "strategy_id")
                return response_ok(
                    {
                        "cursor": self.order_registry.latest_trade_cursor(
                            client_id,
                            strategy_id,
                        )
                    },
                    request_id,
                )
            if action == "place_order":
                if not self.settings.enable_td:
                    raise RuntimeError("CTP trading is disabled by CTP_ENABLE_TD=false")
                require_fields(
                    request,
                    "client_id",
                    "strategy_id",
                    "client_order_id",
                    "symbol",
                    "direction",
                    "offset",
                    "price",
                    "volume",
                )
                Direction(str(request["direction"]).upper())
                Offset(str(request["offset"]).upper())
                if int(request["volume"]) <= 0:
                    raise ValueError("volume must be positive")
                if float(request["price"]) <= 0:
                    raise ValueError("price must be positive")
                data = self.session.place_order(request, client_id, strategy_id, client_order_id)
                return response_ok(data, request_id)
            if action == "cancel_order":
                if not self.settings.enable_td:
                    raise RuntimeError("CTP trading is disabled by CTP_ENABLE_TD=false")
                require_fields(request, "client_id", "strategy_id")
                if not client_order_id and not request.get("order_ref") and not request.get("order_sys_id"):
                    raise ValueError("cancel_order requires client_order_id or CTP order identifiers")
                return response_ok(self.session.cancel_order(request, client_id, strategy_id), request_id)
            if action == "status":
                return response_ok(
                    {
                        "ready": self.session.is_ready(),
                        "database_ready": self.order_registry.is_healthy(),
                        "active_symbols": self.subscriptions.active_symbols(),
                        "md_enabled": self.settings.enable_md,
                        "td_enabled": self.settings.enable_td,
                        "published_queue_size": self._publish_queue.qsize(),
                    },
                    request_id,
                )
            raise ValueError(f"Unsupported action: {action}")
        except (ValueError, RuntimeError, TimeoutError) as exc:
            logger.warning("Command rejected: action={} error={}", request.get("action"), exc)
            return response_error(str(exc), request_id)
        except Exception as exc:
            logger.exception("Unexpected command failure: action={}", request.get("action"))
            return response_error(str(exc), request_id)

    def stop(self) -> None:
        if not self.active.is_set() and self.context is None:
            self.session.close()
            self.order_registry.close()
            return
        self.active.clear()
        self.session.close()
        for thread in (self._command_thread, self._publisher_thread):
            if thread and thread.is_alive():
                thread.join(timeout=2)
        for socket in (self.rep_socket, self.pub_socket):
            if socket is not None:
                socket.close(0)
        if self.context is not None:
            self.context.term()
        self.context = None
        self.order_registry.close()
