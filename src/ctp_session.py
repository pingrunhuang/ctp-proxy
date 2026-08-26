from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from loguru import logger

from config import Settings
from protocol import Direction, Offset
from registry import OrderRegistry, SnapshotCache

try:
    import openctp_ctp.thostmduserapi as mdapi
    import openctp_ctp.thosttraderapi as tdapi

    CTP_AVAILABLE = True
except ImportError:
    CTP_AVAILABLE = False

    class _UnavailableModule:
        CThostFtdcMdSpi = object
        CThostFtdcTraderSpi = object

    mdapi = _UnavailableModule()
    tdapi = _UnavailableModule()


Publisher = Callable[[str, str, dict[str, Any]], None]
CANCELLABLE_ORDER_STATUSES = frozenset({"SUBMITTED", "PARTTRADED"})


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _error_details(info: Any) -> dict[str, Any] | None:
    if info is None:
        return None
    error_id = int(getattr(info, "ErrorID", 0) or 0)
    if not error_id:
        return None
    raw_message = getattr(info, "ErrorMsg", "")
    if isinstance(raw_message, bytes):
        for encoding in ("utf-8", "gb18030"):
            try:
                error_message = raw_message.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        else:
            error_message = raw_message.decode("gb18030", errors="replace")
    else:
        error_message = str(raw_message or "")
    error_message = error_message.strip()
    return {
        "error": f"{error_id}: {error_message}" if error_message else str(error_id),
        "error_id": error_id,
        "error_message": error_message,
    }


def _error(info: Any) -> str | None:
    details = _error_details(info)
    return None if details is None else str(details["error"])


def _finite(value: Any) -> float | None:
    try:
        value = float(value)
        return value if abs(value) < 1e100 else None
    except (TypeError, ValueError):
        return None


def _trade_event_id(
    *,
    gateway_name: str,
    account_id: str,
    trading_day: str,
    exchange: str,
    trade_id: str,
    fallback: dict[str, Any],
) -> str:
    """Return a stable identifier for one native fill."""
    identity: dict[str, Any] = {
        "gateway_name": gateway_name,
        "account_id": account_id,
        "trading_day": trading_day,
        "exchange": exchange,
        "trade_id": trade_id,
    }
    if not trade_id:
        identity["fallback"] = fallback
    encoded = json.dumps(
        identity,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"trade:{gateway_name.lower()}:{hashlib.sha256(encoded).hexdigest()}"


def _exchange_timestamp(data: Any) -> int:
    update_time = _text(getattr(data, "UpdateTime", ""))
    millis = int(getattr(data, "UpdateMillisec", 0) or 0)
    action_day = _text(getattr(data, "ActionDay", ""))
    now = datetime.now()
    try:
        if action_day and len(action_day) == 8:
            date_text = action_day
        else:
            date_text = now.strftime("%Y%m%d")
        value = datetime.strptime(
            f"{date_text} {update_time}.{millis:03d}", "%Y%m%d %H:%M:%S.%f"
        )
        if not action_day:
            delta = (now - value).total_seconds()
            if delta > 12 * 60 * 60:
                value += timedelta(days=1)
            elif delta < -12 * 60 * 60:
                value -= timedelta(days=1)
        return int(value.timestamp() * 1000)
    except (TypeError, ValueError):
        return 0


def _direction_from_ctp(value: Any) -> str:
    return "BUY" if value == getattr(tdapi, "THOST_FTDC_D_Buy", "0") else "SELL"


def _offset_from_ctp(value: Any) -> str:
    value = _text(value)[:1]
    mapping = {
        getattr(tdapi, "THOST_FTDC_OF_Open", "0"): "OPEN",
        getattr(tdapi, "THOST_FTDC_OF_Close", "1"): "CLOSE",
        getattr(tdapi, "THOST_FTDC_OF_CloseToday", "3"): "CLOSETODAY",
        getattr(tdapi, "THOST_FTDC_OF_CloseYesterday", "4"): "CLOSEYESTERDAY",
    }
    return mapping.get(value, "CLOSE")


def _status_from_ctp(value: Any) -> str:
    mapping = {
        getattr(tdapi, "THOST_FTDC_OST_AllTraded", "0"): "TRADED",
        getattr(tdapi, "THOST_FTDC_OST_PartTradedQueueing", "1"): "PARTTRADED",
        getattr(tdapi, "THOST_FTDC_OST_PartTradedNotQueueing", "2"): "CANCELLED",
        getattr(tdapi, "THOST_FTDC_OST_NoTradeQueueing", "3"): "SUBMITTED",
        getattr(tdapi, "THOST_FTDC_OST_NoTradeNotQueueing", "4"): "CANCELLED",
        getattr(tdapi, "THOST_FTDC_OST_Canceled", "5"): "CANCELLED",
    }
    return mapping.get(value, "UNKNOWN")


class CtpMdSpi(mdapi.CThostFtdcMdSpi):
    def __init__(self, session: "CtpSession") -> None:
        super().__init__()
        self.session = session

    def OnFrontConnected(self) -> None:
        logger.info("CTP MD connected; logging in")
        request = mdapi.CThostFtdcReqUserLoginField()
        request.BrokerID = self.session.settings.md_broker_id
        request.UserID = self.session.settings.md_user_id
        request.Password = self.session.settings.md_password
        result = self.session.md_api.ReqUserLogin(
            request,
            self.session.next_request_id(),
        )
        if result:
            self.session.publish_status(
                "MD_LOGIN_REQUEST_FAILED",
                error=f"return_code={result}",
            )

    def OnFrontDisconnected(self, reason: int) -> None:
        self.session.md_ready.clear()
        self.session.publish_status("MD_DISCONNECTED", reason=reason)

    def OnRspUserLogin(self, _login: Any, info: Any, _request_id: int, _last: bool) -> None:
        error = _error(info)
        if error:
            self.session.publish_status("MD_LOGIN_FAILED", error=error)
            return
        self.session.md_ready.set()
        self.session.publish_status("MD_READY")
        symbols = self.session.active_symbols()
        if symbols:
            self.session.subscribe_market_data(symbols)

    def OnRspSubMarketData(self, instrument: Any, info: Any, _request_id: int, _last: bool) -> None:
        error = _error(info)
        symbol = _text(getattr(instrument, "InstrumentID", ""))
        if error:
            logger.error("CTP market-data subscription failed for {}: {}", symbol, error)
        else:
            logger.info("CTP market-data subscribed: {}", symbol)

    def OnRtnDepthMarketData(self, data: Any) -> None:
        if data is None:
            return
        symbol = _text(getattr(data, "InstrumentID", ""))
        update_time = _text(getattr(data, "UpdateTime", ""))
        millis = int(getattr(data, "UpdateMillisec", 0) or 0)
        compact_time = update_time.replace(":", "")
        payload = {
            "gateway_name": "CTP",
            "symbol": symbol,
            "exchange": _text(getattr(data, "ExchangeID", "")) or "CTP",
            "trading_day": _text(getattr(data, "TradingDay", "")),
            "action_day": _text(getattr(data, "ActionDay", "")),
            "last_price": _finite(getattr(data, "LastPrice", None)),
            "volume": int(getattr(data, "Volume", 0) or 0),
            "open_interest": _finite(getattr(data, "OpenInterest", None)),
            "upper_limit": _finite(getattr(data, "UpperLimitPrice", None)),
            "lower_limit": _finite(getattr(data, "LowerLimitPrice", None)),
            "bid_price_1": _finite(getattr(data, "BidPrice1", None)),
            "bid_volume_1": int(getattr(data, "BidVolume1", 0) or 0),
            "ask_price_1": _finite(getattr(data, "AskPrice1", None)),
            "ask_volume_1": int(getattr(data, "AskVolume1", 0) or 0),
            "update_time": update_time,
            "update_millisec": _exchange_timestamp(data),
            "int_time": int(f"{compact_time}{millis:03d}") if compact_time.isdigit() else 0,
            "local_time": int(time.time() * 1000),
        }
        logger.debug(f"Depth MD: {payload}")
        self.session.publish(f"marketdata.CTP.{symbol}", "marketdata", payload)


class CtpTraderSpi(tdapi.CThostFtdcTraderSpi):
    def __init__(self, session: "CtpSession") -> None:
        super().__init__()
        self.session = session
        self.front_id = 0
        self.session_id = 0
        self.order_ref = 0
        self.position_buffer: dict[tuple[str, str], dict[str, Any]] = {}
        self.order_query_buffer: list[dict[str, Any]] = []

    def OnFrontConnected(self) -> None:
        self.session.publish_status("TD_CONNECTED")
        request = tdapi.CThostFtdcReqAuthenticateField()
        request.BrokerID = self.session.settings.td_broker_id
        request.UserID = self.session.settings.td_user_id
        request.AppID = self.session.settings.app_id
        request.AuthCode = self.session.settings.auth_code
        result = self.session.td_api.ReqAuthenticate(
            request,
            self.session.next_request_id(),
        )
        if result:
            self.session.publish_status(
                "TD_AUTH_REQUEST_FAILED",
                error=f"return_code={result}",
            )

    def OnFrontDisconnected(self, reason: int) -> None:
        self.session.td_ready.clear()
        self.session.publish_status("TD_DISCONNECTED", reason=reason)

    def OnRspAuthenticate(self, _response: Any, info: Any, request_id: int, last: bool) -> None:
        error_details = _error_details(info)
        if error_details:
            self.session.publish_status(
                "TD_AUTH_FAILED",
                **error_details,
                broker_id=self.session.settings.td_broker_id,
                user_id=self.session.settings.td_user_id,
                request_id=request_id,
                is_last=last,
            )
            return
        request = tdapi.CThostFtdcReqUserLoginField()
        request.BrokerID = self.session.settings.td_broker_id
        request.UserID = self.session.settings.td_user_id
        request.Password = self.session.settings.td_password
        result = self.session.td_api.ReqUserLogin(
            request,
            self.session.next_request_id(),
        )
        if result:
            self.session.publish_status(
                "TD_LOGIN_REQUEST_FAILED",
                error=f"return_code={result}",
            )

    def OnRspUserLogin(self, login: Any, info: Any, request_id: int, last: bool) -> None:
        error_details = _error_details(info)
        if error_details:
            self.session.publish_status(
                "TD_LOGIN_FAILED",
                **error_details,
                broker_id=self.session.settings.td_broker_id,
                user_id=self.session.settings.td_user_id,
                request_id=request_id,
                is_last=last,
            )
            return
        self.front_id = int(getattr(login, "FrontID", 0) or 0)
        self.session_id = int(getattr(login, "SessionID", 0) or 0)
        self.order_ref = int(_text(getattr(login, "MaxOrderRef", "0")) or "0")
        logger.info(f"TD login success: front_id={self.front_id}, session_id={self.session_id}, order_ref={self.order_ref}")
        request = tdapi.CThostFtdcSettlementInfoConfirmField()
        request.BrokerID = self.session.settings.td_broker_id
        request.InvestorID = self.session.settings.td_user_id
        result = self.session.td_api.ReqSettlementInfoConfirm(
            request,
            self.session.next_request_id(),
        )
        if result:
            self.session.publish_status(
                "SETTLEMENT_CONFIRM_REQUEST_FAILED",
                error=f"return_code={result}",
            )

    def OnRspSettlementInfoConfirm(self, _response: Any, info: Any, _request_id: int, _last: bool) -> None:
        error = _error(info)
        if error:
            self.session.publish_status("SETTLEMENT_CONFIRM_FAILED", error=error)
            return
        self.session.td_ready.set()
        self.session.publish_status("TD_READY")

    def OnRspQryTradingAccount(self, data: Any, info: Any, _request_id: int, last: bool) -> None:
        error = _error(info)
        if error:
            self.session.complete_query("account", error=error)
            return
        if data is not None:
            account = {
                "gateway_name": "CTP",
                "account_id": _text(getattr(data, "AccountID", "")) or self.session.settings.td_user_id,
                "currency": _text(getattr(data, "CurrencyID", "")) or "CNY",
                "balance": _finite(getattr(data, "Balance", 0)),
                "available": _finite(getattr(data, "Available", 0)),
                "frozen": sum(
                    float(getattr(data, field, 0) or 0)
                    for field in ("FrozenMargin", "FrozenCash", "FrozenCommission")
                ),
                "margin": _finite(getattr(data, "CurrMargin", 0)),
                "close_profit": _finite(getattr(data, "CloseProfit", 0)),
                "position_profit": _finite(getattr(data, "PositionProfit", 0)),
                "trading_day": _text(getattr(data, "TradingDay", "")),
            }
            self.session.cache.put("account", account)
            self.session.publish(f"account.{account['account_id']}", "account", account)
        if last:
            self.session.complete_query("account", self.session.cache.peek("account"))

    def OnRspQryInvestorPosition(self, data: Any, info: Any, _request_id: int, last: bool) -> None:
        error = _error(info)
        if error:
            self.position_buffer.clear()
            self.session.complete_query("positions", error=error)
            return
        if data is not None:
            raw_direction = getattr(data, "PosiDirection", "")
            direction = {
                getattr(tdapi, "THOST_FTDC_PD_Net", "1"): "NET",
                getattr(tdapi, "THOST_FTDC_PD_Long", "2"): "LONG",
                getattr(tdapi, "THOST_FTDC_PD_Short", "3"): "SHORT",
            }.get(raw_direction, "UNKNOWN")
            symbol = _text(getattr(data, "InstrumentID", ""))
            key = (symbol, direction)
            position = self.position_buffer.setdefault(
                key,
                {
                    "gateway_name": "CTP",
                    "account_id": self.session.settings.td_user_id,
                    "symbol": symbol,
                    "exchange": _text(getattr(data, "ExchangeID", "")),
                    "direction": direction,
                    "volume": 0,
                    "yesterday_volume": 0,
                    "frozen": 0,
                },
            )
            volume = int(getattr(data, "Position", 0) or 0)
            position["volume"] += volume
            if getattr(data, "PositionDate", "") == getattr(tdapi, "THOST_FTDC_PSD_History", "2"):
                position["yesterday_volume"] += volume
            if direction == "LONG":
                frozen = getattr(data, "ShortFrozen", 0)
            elif direction == "SHORT":
                frozen = getattr(data, "LongFrozen", 0)
            else:
                frozen = int(getattr(data, "LongFrozen", 0) or 0) + int(getattr(data, "ShortFrozen", 0) or 0)
            position["frozen"] += int(frozen or 0)
        if last:
            positions = list(self.position_buffer.values())
            self.position_buffer.clear()
            self.session.cache.put("positions", positions)
            self.session.publish(f"positions.{self.session.settings.td_user_id}", "positions", positions)
            self.session.complete_query("positions", positions)

    def OnRspQryOrder(self, data: Any, info: Any, _request_id: int, last: bool) -> None:
        error = _error(info)
        if error:
            self.order_query_buffer.clear()
            self.session.complete_query("orders", error=error)
            return
        if data is not None:
            self.order_query_buffer.append(self.session.order_payload(data))
        if last:
            orders = self.order_query_buffer[:]
            self.order_query_buffer.clear()
            self.session.cache.put("orders", orders)
            self.session.complete_query("orders", orders)

    def OnRspOrderInsert(self, order: Any, info: Any, _request_id: int, _last: bool) -> None:
        error = _error(info)
        if error and order is not None:
            payload = self.session.order_payload(order, status="REJECTED", status_message=error)
            self.session.publish_order(payload)

    def OnErrRtnOrderInsert(self, order: Any, info: Any) -> None:
        self.OnRspOrderInsert(order, info, 0, True)

    def OnRspOrderAction(self, action: Any, info: Any, _request_id: int, _last: bool) -> None:
        error = _error(info)
        if error:
            self.session.publish(
                "errors.CTP",
                "cancel_error",
                {
                    "message": error,
                    "order_ref": _text(getattr(action, "OrderRef", "")),
                    "front_id": int(getattr(action, "FrontID", 0) or 0),
                    "session_id": int(getattr(action, "SessionID", 0) or 0),
                    "exchange": _text(getattr(action, "ExchangeID", "")),
                    "symbol": _text(getattr(action, "InstrumentID", "")),
                },
            )

    def OnErrRtnOrderAction(self, action: Any, info: Any) -> None:
        self.OnRspOrderAction(action, info, 0, True)

    def OnRtnOrder(self, data: Any) -> None:
        if data is not None:
            self.session.publish_order(self.session.order_payload(data))

    def OnRtnTrade(self, data: Any) -> None:
        if data is not None:
            self.session.publish_trade(self.session.trade_payload(data))


class CtpSession:
    def __init__(self, settings: Settings, publish: Publisher, order_registry: OrderRegistry) -> None:
        self.settings = settings
        self.publish = publish
        self.order_registry = order_registry
        self.cache = SnapshotCache()
        self.md_ready = threading.Event()
        self.td_ready = threading.Event()
        self.md_api: Any = None
        self.td_api: Any = None
        self.md_spi: Any = None
        self.td_spi: Any = None
        self._request_id = 0
        self._request_lock = threading.Lock()
        self._query_lock = threading.Lock()
        self._last_query_at = 0.0
        self._pending_queries: dict[str, tuple[threading.Event, dict[str, Any]]] = {}
        self._active_symbols_provider: Callable[[], list[str]] = lambda: []

    def set_active_symbols_provider(self, provider: Callable[[], list[str]]) -> None:
        self._active_symbols_provider = provider

    def active_symbols(self) -> list[str]:
        return self._active_symbols_provider()

    @property
    def md_enabled(self) -> bool:
        return self.settings.enable_md

    def next_request_id(self) -> int:
        with self._request_lock:
            self._request_id += 1
            return self._request_id

    def connect(self, timeout: float = 20.0) -> bool:
        if not CTP_AVAILABLE:
            raise RuntimeError("openctp-ctp is not installed on this platform")
        td_flow = Path(self.settings.flow_path) / "td"
        td_flow.mkdir(parents=True, exist_ok=True)
        self.td_api = tdapi.CThostFtdcTraderApi.CreateFtdcTraderApi(str(td_flow.absolute()) + "/", self.settings.production_mode)
        self.td_spi = CtpTraderSpi(self)
        self.td_api.RegisterSpi(self.td_spi)
        self.td_api.RegisterFront(self.settings.front_td)
        self.td_api.SubscribePrivateTopic(tdapi.THOST_TERT_QUICK)
        self.td_api.SubscribePublicTopic(tdapi.THOST_TERT_QUICK)
        if self.md_enabled:
            md_flow = Path(self.settings.flow_path) / "md"
            md_flow.mkdir(parents=True, exist_ok=True)
            self.md_api = mdapi.CThostFtdcMdApi.CreateFtdcMdApi(str(md_flow.absolute()) + "/", self.settings.production_mode)
            self.md_spi = CtpMdSpi(self)
            self.md_api.RegisterSpi(self.md_spi)
            self.md_api.RegisterFront(self.settings.front_md)
            self.md_api.Init()
        self.td_api.Init()
        md_ok = not self.md_enabled or self.md_ready.wait(timeout)
        td_ok = self.td_ready.wait(timeout)
        if not (md_ok and td_ok):
            logger.error(
                "CTP session is not ready after login wait: "
                "md_enabled={} md_ready={} td_ready={} timeout_seconds={}",
                self.md_enabled,
                self.md_ready.is_set(),
                self.td_ready.is_set(),
                timeout,
            )
        return md_ok and td_ok

    def is_ready(self) -> bool:
        return (not self.md_enabled or self.md_ready.is_set()) and self.td_ready.is_set()

    def publish_status(self, status: str, **details: Any) -> None:
        if status.endswith("_FAILED"):
            logger.error("CTP session status={} details={}", status, details)
        elif status.endswith("_DISCONNECTED"):
            logger.warning("CTP session status={} details={}", status, details)
        else:
            logger.info("CTP session status={} details={}", status, details)
        self.publish("status.CTP", "status", {"gateway_name": "CTP", "status": status, **details})

    def subscribe_market_data(self, symbols: list[str]) -> None:
        if not self.md_enabled:
            raise RuntimeError("CTP market data is disabled by CTP_ENABLE_MD=false")
        if not symbols or not self.md_ready.is_set():
            return
        result = self.md_api.SubscribeMarketData([symbol.encode("utf-8") for symbol in symbols], len(symbols))
        if result:
            raise RuntimeError(f"CTP SubscribeMarketData returned {result}")

    def unsubscribe_market_data(self, symbols: list[str]) -> None:
        if not self.md_enabled:
            raise RuntimeError("CTP market data is disabled by CTP_ENABLE_MD=false")
        if not symbols or not self.md_ready.is_set():
            return
        result = self.md_api.UnSubscribeMarketData([symbol.encode("utf-8") for symbol in symbols], len(symbols))
        if result:
            raise RuntimeError(f"CTP UnSubscribeMarketData returned {result}")

    def _wait_for_query_slot(self) -> None:
        wait = self.settings.query_min_interval_seconds - (time.monotonic() - self._last_query_at)
        if wait > 0:
            time.sleep(wait)
        self._last_query_at = time.monotonic()

    def complete_query(self, kind: str, result: Any = None, error: str | None = None) -> None:
        pending = self._pending_queries.get(kind)
        if pending:
            event, holder = pending
            holder["result"] = result
            holder["error"] = error
            event.set()

    def _query(self, kind: str, send: Callable[[int], int], max_age_seconds: float | None) -> Any:
        if max_age_seconds is not None:
            cached = self.cache.get(kind, max_age_seconds)
            if cached is not None:
                return cached
        if not self.td_ready.is_set():
            raise RuntimeError("CTP trader session is not ready")
        with self._query_lock:
            if max_age_seconds is not None:
                cached = self.cache.get(kind, max_age_seconds)
                if cached is not None:
                    return cached
            self._wait_for_query_slot()
            event = threading.Event()
            holder: dict[str, Any] = {}
            self._pending_queries[kind] = (event, holder)
            result_code = send(self.next_request_id())
            if result_code:
                self._pending_queries.pop(kind, None)
                raise RuntimeError(f"CTP {kind} query returned {result_code}")
            if not event.wait(self.settings.query_timeout_seconds):
                self._pending_queries.pop(kind, None)
                raise TimeoutError(f"CTP {kind} query timed out")
            self._pending_queries.pop(kind, None)
            if holder.get("error"):
                raise RuntimeError(holder["error"])
            return holder.get("result")

    def query_account(self, max_age_seconds: float | None) -> Any:
        def send(request_id: int) -> int:
            request = tdapi.CThostFtdcQryTradingAccountField()
            request.BrokerID = self.settings.td_broker_id
            request.InvestorID = self.settings.td_user_id
            return self.td_api.ReqQryTradingAccount(request, request_id)

        return self._query("account", send, max_age_seconds)

    def query_positions(self, max_age_seconds: float | None) -> Any:
        def send(request_id: int) -> int:
            request = tdapi.CThostFtdcQryInvestorPositionField()
            request.BrokerID = self.settings.td_broker_id
            request.InvestorID = self.settings.td_user_id
            return self.td_api.ReqQryInvestorPosition(request, request_id)

        return self._query("positions", send, max_age_seconds)

    def query_orders(self, max_age_seconds: float | None) -> Any:
        def send(request_id: int) -> int:
            request = tdapi.CThostFtdcQryOrderField()
            request.BrokerID = self.settings.td_broker_id
            request.InvestorID = self.settings.td_user_id
            return self.td_api.ReqQryOrder(request, request_id)

        return self._query("orders", send, max_age_seconds)

    def place_order(self, request: dict[str, Any], client_id: str, strategy_id: str, client_order_id: str) -> dict[str, Any]:
        if not self.td_ready.is_set():
            raise RuntimeError("CTP trader session is not ready")
        assert self.td_spi is not None
        with self._request_lock:
            self.td_spi.order_ref += 1
            order_ref = str(self.td_spi.order_ref)
        inserted = self.order_registry.create(
            client_id=client_id,
            strategy_id=strategy_id,
            client_order_id=client_order_id,
            symbol=str(request["symbol"]),
            order_ref=order_ref,
            front_id=self.td_spi.front_id,
            session_id=self.td_spi.session_id,
            exchange_id=str(request.get("exchange", "")),
            payload=json.dumps(request, ensure_ascii=False, sort_keys=True),
        )
        if not inserted:
            return {"duplicate": True, "order": self.order_registry.get(client_id, strategy_id, client_order_id)}

        order = tdapi.CThostFtdcInputOrderField()
        order.BrokerID = self.settings.td_broker_id
        order.InvestorID = self.settings.td_user_id
        order.UserID = self.settings.td_user_id
        order.InstrumentID = str(request["symbol"])
        if request.get("exchange"):
            order.ExchangeID = str(request["exchange"])
        order.OrderRef = order_ref
        order.OrderPriceType = tdapi.THOST_FTDC_OPT_LimitPrice
        order.Direction = tdapi.THOST_FTDC_D_Buy if Direction(str(request["direction"]).upper()) is Direction.BUY else tdapi.THOST_FTDC_D_Sell
        offset_mapping = {
            Offset.OPEN: tdapi.THOST_FTDC_OF_Open,
            Offset.CLOSE: tdapi.THOST_FTDC_OF_Close,
            Offset.CLOSETODAY: tdapi.THOST_FTDC_OF_CloseToday,
            Offset.CLOSEYESTERDAY: tdapi.THOST_FTDC_OF_CloseYesterday,
        }
        order.CombOffsetFlag = offset_mapping[Offset(str(request["offset"]).upper())]
        order.CombHedgeFlag = tdapi.THOST_FTDC_HF_Speculation
        order.LimitPrice = float(request["price"])
        order.VolumeTotalOriginal = int(request["volume"])
        order.TimeCondition = tdapi.THOST_FTDC_TC_GFD
        order.VolumeCondition = tdapi.THOST_FTDC_VC_AV
        order.MinVolume = 1
        order.ContingentCondition = tdapi.THOST_FTDC_CC_Immediately
        order.ForceCloseReason = tdapi.THOST_FTDC_FCC_NotForceClose
        order.IsAutoSuspend = 0
        order.UserForceClose = 0
        result = self.td_api.ReqOrderInsert(order, self.next_request_id())
        if result:
            self.order_registry.update_ctp(
                order_ref=order_ref,
                front_id=self.td_spi.front_id,
                session_id=self.td_spi.session_id,
                exchange_id=str(request.get("exchange", "")),
                order_sys_id="",
                status="REJECTED",
            )
            raise RuntimeError(f"CTP ReqOrderInsert returned {result}")
        return {
            "accepted": True,
            "duplicate": False,
            "client_id": client_id,
            "strategy_id": strategy_id,
            "client_order_id": client_order_id,
            "order_ref": order_ref,
        }

    def cancel_order(self, request: dict[str, Any], client_id: str, strategy_id: str) -> dict[str, Any]:
        record = None
        if request.get("client_order_id"):
            record = self.order_registry.get(client_id, strategy_id, str(request["client_order_id"]))
            if record is None:
                raise ValueError("Unknown client_order_id")
        source = record or request
        if record is not None:
            status = str(record.get("status", "")).upper()
            if status not in CANCELLABLE_ORDER_STATUSES:
                raise RuntimeError(
                    "CTP order is not cancellable before an active OnRtnOrder: "
                    f"client_order_id={request['client_order_id']} status={status or 'UNKNOWN'}"
                )
        order_ref = str(source.get("order_ref", "") or "")
        front_id = int(source.get("front_id", 0) or 0)
        session_id = int(source.get("session_id", 0) or 0)
        if not order_ref or not front_id or not session_id:
            raise RuntimeError(
                "CTP cancel requires FrontID + SessionID + OrderRef from OnRtnOrder"
            )
        action = tdapi.CThostFtdcInputOrderActionField()
        action.BrokerID = self.settings.td_broker_id
        action.InvestorID = self.settings.td_user_id
        action.UserID = self.settings.td_user_id
        action.InstrumentID = str(source.get("symbol", ""))
        action.ExchangeID = str(source.get("exchange_id", source.get("exchange", "")) or "")
        action.OrderRef = order_ref
        action.FrontID = front_id
        action.SessionID = session_id
        action.ActionFlag = tdapi.THOST_FTDC_AF_Delete
        request_id = self.next_request_id()
        action.OrderActionRef = request_id
        result = self.td_api.ReqOrderAction(action, request_id)
        if result:
            raise RuntimeError(f"CTP ReqOrderAction returned {result}")
        return {
            "accepted": True,
            "client_order_id": request.get("client_order_id"),
            "order_ref": action.OrderRef,
            "front_id": action.FrontID,
            "session_id": action.SessionID,
        }

    def order_payload(self, data: Any, status: str | None = None, status_message: str | None = None) -> dict[str, Any]:
        order_ref = _text(getattr(data, "OrderRef", ""))
        exchange_id = _text(getattr(data, "ExchangeID", ""))
        order_sys_id = getattr(data, "OrderSysID", "")
        front_id = int(
            getattr(data, "FrontID", 0)
            or getattr(self.td_spi, "front_id", 0)
            or 0
        )
        session_id = int(
            getattr(data, "SessionID", 0)
            or getattr(self.td_spi, "session_id", 0)
            or 0
        )
        owner = self.order_registry.find_by_ctp(
            order_ref=order_ref,
            exchange_id=exchange_id,
            order_sys_id=order_sys_id,
            front_id=front_id,
            session_id=session_id,
        ) or {}
        actual_status = status or _status_from_ctp(getattr(data, "OrderStatus", ""))
        self.order_registry.update_ctp(
            order_ref=order_ref,
            front_id=front_id,
            session_id=session_id,
            exchange_id=exchange_id,
            order_sys_id=order_sys_id,
            status=actual_status,
        )
        return {
            "gateway_name": "CTP",
            "account_id": self.settings.td_user_id,
            "client_id": owner.get("client_id"),
            "strategy_id": owner.get("strategy_id"),
            "client_order_id": owner.get("client_order_id"),
            "symbol": _text(getattr(data, "InstrumentID", owner.get("symbol", ""))),
            "exchange": exchange_id,
            "order_id": order_sys_id or order_ref,
            "order_ref": order_ref,
            "front_id": front_id,
            "session_id": session_id,
            "direction": _direction_from_ctp(getattr(data, "Direction", "")),
            "offset": _offset_from_ctp(getattr(data, "CombOffsetFlag", "")),
            "price": _finite(getattr(data, "LimitPrice", 0)),
            "volume": int(getattr(data, "VolumeTotalOriginal", 0) or 0),
            "traded": int(getattr(data, "VolumeTraded", 0) or 0),
            "status": actual_status,
            "status_message": status_message if status_message is not None else _text(getattr(data, "StatusMsg", "")),
        }

    def trade_payload(self, data: Any) -> dict[str, Any]:
        order_sys_id = getattr(data, "OrderSysID", "")
        owner = self.order_registry.find_by_ctp(
            order_ref=_text(getattr(data, "OrderRef", "")),
            exchange_id=_text(getattr(data, "ExchangeID", "")),
            order_sys_id=order_sys_id,
        ) or {}
        gateway_name = "CTP"
        account_id = self.settings.td_user_id
        trading_day = _text(getattr(data, "TradingDay", ""))
        exchange = _text(getattr(data, "ExchangeID", ""))
        trade_id = _text(getattr(data, "TradeID", ""))
        normalized_order_id = _text(order_sys_id)
        order_ref = _text(getattr(data, "OrderRef", ""))
        symbol = _text(getattr(data, "InstrumentID", ""))
        direction = _direction_from_ctp(getattr(data, "Direction", ""))
        offset = _offset_from_ctp(getattr(data, "OffsetFlag", ""))
        price = _finite(getattr(data, "Price", 0))
        volume = int(getattr(data, "Volume", 0) or 0)
        trade_time = _text(getattr(data, "TradeTime", ""))
        return {
            "event_id": _trade_event_id(
                gateway_name=gateway_name,
                account_id=account_id,
                trading_day=trading_day,
                exchange=exchange,
                trade_id=trade_id,
                fallback={
                    "order_id": normalized_order_id,
                    "order_ref": order_ref,
                    "symbol": symbol,
                    "direction": direction,
                    "offset": offset,
                    "price": price,
                    "volume": volume,
                    "trade_time": trade_time,
                },
            ),
            "gateway_name": gateway_name,
            "account_id": account_id,
            "client_id": owner.get("client_id"),
            "strategy_id": owner.get("strategy_id"),
            "client_order_id": owner.get("client_order_id"),
            "symbol": symbol,
            "exchange": exchange,
            "trade_id": trade_id,
            "order_id": order_sys_id,
            "order_ref": order_ref,
            "direction": direction,
            "offset": offset,
            "price": price,
            "volume": volume,
            "trade_time": trade_time,
            "trading_day": trading_day,
        }

    def publish_order(self, payload: dict[str, Any]) -> None:
        self.publish(f"orders.{self.settings.td_user_id}", "order", payload)
        if payload.get("strategy_id"):
            self.publish(f"orders.{self.settings.td_user_id}.{payload['strategy_id']}", "order", payload)

    def publish_trade(self, payload: dict[str, Any]) -> None:
        try:
            trade_cursor = self.order_registry.record_trade(payload)
        except Exception:
            logger.exception(
                "Failed to persist CTP trade before publish event_id={}",
                payload.get("event_id"),
            )
            return
        logger.debug(
            "Persisted CTP trade event_id={} strategy_id={} trade_cursor={}",
            payload.get("event_id"),
            payload.get("strategy_id"),
            trade_cursor,
        )
        if not trade_cursor:
            logger.debug("Skip duplicate CTP trade event_id={}", payload.get("event_id"))
            return
        published_payload = {**payload, "trade_cursor": int(trade_cursor)}
        self.publish(f"trades.{self.settings.td_user_id}", "trade", published_payload)
        if published_payload.get("strategy_id"):
            self.publish(
                f"trades.{self.settings.td_user_id}.{published_payload['strategy_id']}",
                "trade",
                published_payload,
            )

    def close(self) -> None:
        self.md_ready.clear()
        self.td_ready.clear()
        for api in (self.md_api, self.td_api):
            if api is not None:
                try:
                    api.Release()
                except Exception:
                    logger.exception("Failed to release CTP API")
