import sys
from pathlib import Path
from types import SimpleNamespace

from loguru import logger
import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import ctp_session
from ctp_session import CtpMdSpi, CtpSession, CtpTraderSpi


class StatusPublisher:
    def __init__(self):
        self.messages = []

    def publish(self, topic, event, data):
        self.messages.append((topic, event, data))


def test_failed_session_status_is_logged_and_published():
    records = []
    sink_id = logger.add(lambda message: records.append(message.record))
    publisher = StatusPublisher()

    try:
        CtpSession.publish_status(
            publisher,
            "MD_LOGIN_FAILED",
            error="7: invalid credentials",
        )
    finally:
        logger.remove(sink_id)

    assert publisher.messages == [
        (
            "status.CTP",
            "status",
            {
                "gateway_name": "CTP",
                "status": "MD_LOGIN_FAILED",
                "error": "7: invalid credentials",
            },
        )
    ]
    assert any(
        record["level"].name == "ERROR"
        and "status=MD_LOGIN_FAILED" in record["message"]
        and "7: invalid credentials" in record["message"]
        for record in records
    )


def test_ready_session_status_is_logged_as_info():
    records = []
    sink_id = logger.add(lambda message: records.append(message.record))
    publisher = StatusPublisher()

    try:
        CtpSession.publish_status(publisher, "MD_READY")
    finally:
        logger.remove(sink_id)

    assert any(
        record["level"].name == "INFO"
        and "status=MD_READY" in record["message"]
        for record in records
    )


def test_immediate_md_login_request_failure_is_logged(monkeypatch):
    class LoginRequest:
        pass

    class MdApi:
        def __init__(self):
            self.login_request = None

        def ReqUserLogin(self, request, _request_id):
            self.login_request = request
            return -2

    publisher = StatusPublisher()
    publisher.settings = SimpleNamespace(
        md_broker_id="md-broker",
        md_user_id="md-user",
        md_password="md-secret",
    )
    publisher.md_api = MdApi()
    publisher.next_request_id = lambda: 1
    publisher.publish_status = lambda status, **details: CtpSession.publish_status(
        publisher,
        status,
        **details,
    )
    monkeypatch.setattr(
        ctp_session.mdapi,
        "CThostFtdcReqUserLoginField",
        LoginRequest,
        raising=False,
    )
    records = []
    sink_id = logger.add(lambda message: records.append(message.record))

    try:
        CtpMdSpi(publisher).OnFrontConnected()
    finally:
        logger.remove(sink_id)

    assert publisher.messages[-1][2]["status"] == "MD_LOGIN_REQUEST_FAILED"
    assert publisher.messages[-1][2]["error"] == "return_code=-2"
    assert vars(publisher.md_api.login_request) == {
        "BrokerID": "md-broker",
        "UserID": "md-user",
        "Password": "md-secret",
    }
    assert any(
        record["level"].name == "ERROR"
        and "MD_LOGIN_REQUEST_FAILED" in record["message"]
        and "return_code=-2" in record["message"]
        for record in records
    )


def test_td_authentication_and_login_use_td_credentials(monkeypatch):
    class AuthRequest:
        pass

    class LoginRequest:
        pass

    class TdApi:
        def __init__(self):
            self.auth_request = None
            self.login_request = None

        def ReqAuthenticate(self, request, _request_id):
            self.auth_request = request
            return 0

        def ReqUserLogin(self, request, _request_id):
            self.login_request = request
            return 0

    publisher = StatusPublisher()
    publisher.settings = SimpleNamespace(
        td_broker_id="td-broker",
        td_user_id="td-user",
        td_password="td-secret",
        app_id="shared-app",
        auth_code="shared-auth",
    )
    publisher.td_api = TdApi()
    publisher.next_request_id = lambda: 1
    publisher.publish_status = lambda status, **details: CtpSession.publish_status(
        publisher,
        status,
        **details,
    )
    monkeypatch.setattr(
        ctp_session.tdapi,
        "CThostFtdcReqAuthenticateField",
        AuthRequest,
        raising=False,
    )
    monkeypatch.setattr(
        ctp_session.tdapi,
        "CThostFtdcReqUserLoginField",
        LoginRequest,
        raising=False,
    )

    spi = CtpTraderSpi(publisher)
    spi.OnFrontConnected()
    spi.OnRspAuthenticate(None, SimpleNamespace(ErrorID=0), 1, True)

    assert vars(publisher.td_api.auth_request) == {
        "BrokerID": "td-broker",
        "UserID": "td-user",
        "AppID": "shared-app",
        "AuthCode": "shared-auth",
    }
    assert vars(publisher.td_api.login_request) == {
        "BrokerID": "td-broker",
        "UserID": "td-user",
        "Password": "td-secret",
    }


@pytest.mark.parametrize(
    ("callback_name", "expected_status"),
    (
        ("OnRspAuthenticate", "TD_AUTH_FAILED"),
        ("OnRspUserLogin", "TD_LOGIN_FAILED"),
    ),
)
def test_td_login_errors_log_complete_rsp_info(callback_name, expected_status):
    publisher = StatusPublisher()
    publisher.settings = SimpleNamespace(
        td_broker_id="td-broker",
        td_user_id="td-user",
    )
    publisher.publish_status = lambda status, **details: CtpSession.publish_status(
        publisher,
        status,
        **details,
    )
    records = []
    sink_id = logger.add(lambda message: records.append(message.record))

    try:
        callback = getattr(CtpTraderSpi(publisher), callback_name)
        callback(
            None,
            SimpleNamespace(ErrorID=7, ErrorMsg="登录信息错误".encode("gb18030")),
            42,
            True,
        )
    finally:
        logger.remove(sink_id)

    assert publisher.messages[-1] == (
        "status.CTP",
        "status",
        {
            "gateway_name": "CTP",
            "status": expected_status,
            "error": "7: 登录信息错误",
            "error_id": 7,
            "error_message": "登录信息错误",
            "broker_id": "td-broker",
            "user_id": "td-user",
            "request_id": 42,
            "is_last": True,
        },
    )
    assert any(
        record["level"].name == "ERROR"
        and f"status={expected_status}" in record["message"]
        and "登录信息错误" in record["message"]
        and "request_id" in record["message"]
        for record in records
    )


class CancelRegistry:
    def __init__(self, record):
        self.record = record

    def get(self, client_id, strategy_id, client_order_id):
        if (client_id, strategy_id, client_order_id) == ("engine", "strategy", "order-1"):
            return self.record
        return None


class CancelTdApi:
    def __init__(self):
        self.calls = []

    def ReqOrderAction(self, action, request_id):
        self.calls.append((action, request_id))
        return 0


def cancel_session(record):
    session = CtpSession(
        SimpleNamespace(
            td_broker_id="9999",
            td_user_id="td-user",
            query_min_interval_seconds=0,
        ),
        lambda *_args: None,
        CancelRegistry(record),
    )
    session.td_ready.set()
    session.td_spi = SimpleNamespace(front_id=7, session_id=11, order_ref=2)
    session.td_api = CancelTdApi()
    return session


def test_cancel_rejects_order_before_active_order_return():
    session = cancel_session(
        {
            "status": "PENDING_SUBMIT",
            "symbol": "ag2609",
            "exchange_id": "SHFE",
            "order_ref": "2",
            "front_id": 7,
            "session_id": 11,
        }
    )

    with pytest.raises(RuntimeError, match="before an active OnRtnOrder"):
        session.cancel_order(
            {"client_order_id": "order-1"},
            "engine",
            "strategy",
        )

    assert session.td_api.calls == []


def test_cancel_uses_only_front_session_and_order_ref(monkeypatch):
    class OrderAction:
        pass

    monkeypatch.setattr(
        ctp_session.tdapi,
        "CThostFtdcInputOrderActionField",
        OrderAction,
        raising=False,
    )
    monkeypatch.setattr(
        ctp_session.tdapi,
        "THOST_FTDC_AF_Delete",
        "0",
        raising=False,
    )
    session = cancel_session(
        {
            "status": "SUBMITTED",
            "symbol": "ag2609",
            "exchange_id": "SHFE",
            "order_ref": "2",
            "front_id": 7,
            "session_id": 11,
            "order_sys_id": "         131",
        }
    )

    result = session.cancel_order(
        {"client_order_id": "order-1"},
        "engine",
        "strategy",
    )

    action, request_id = session.td_api.calls[0]
    assert action.FrontID == 7
    assert action.SessionID == 11
    assert action.OrderRef == "2"
    assert not hasattr(action, "OrderSysID")
    assert action.OrderActionRef == request_id
    assert result == {
        "accepted": True,
        "client_order_id": "order-1",
        "order_ref": "2",
        "front_id": 7,
        "session_id": 11,
    }


def test_order_return_preserves_fixed_width_order_sys_id():
    class RecordingRegistry:
        def __init__(self):
            self.find_kwargs = None
            self.update_kwargs = None

        def find_by_ctp(self, **kwargs):
            self.find_kwargs = kwargs
            return {
                "client_id": "engine",
                "strategy_id": "strategy",
                "client_order_id": "order-1",
                "symbol": "ag2609",
            }

        def update_ctp(self, **kwargs):
            self.update_kwargs = kwargs

    raw_order_sys_id = "         131   "
    registry = RecordingRegistry()
    session = CtpSession(
        SimpleNamespace(
            td_user_id="td-user",
            query_min_interval_seconds=0,
        ),
        lambda *_args: None,
        registry,
    )
    session.td_spi = SimpleNamespace(front_id=7, session_id=11)

    payload = session.order_payload(
        SimpleNamespace(
            OrderRef="2",
            FrontID=7,
            SessionID=11,
            ExchangeID="SHFE",
            OrderSysID=raw_order_sys_id,
            OrderStatus="3",
            InstrumentID="ag2609",
        )
    )

    assert registry.find_kwargs["order_sys_id"] == raw_order_sys_id
    assert registry.update_kwargs["order_sys_id"] == raw_order_sys_id
    assert payload["order_id"] == raw_order_sys_id


def test_trade_payload_has_stable_native_fill_identity():
    class OwnedRegistry:
        def find_by_ctp(self, **_kwargs):
            return {
                "client_id": "engine-01",
                "strategy_id": "arb-ctp",
                "client_order_id": "arb-ctp-1",
            }

    session = CtpSession(
        SimpleNamespace(
            td_user_id="td-user",
            query_min_interval_seconds=0,
        ),
        lambda *_args: None,
        OwnedRegistry(),
    )
    fill = SimpleNamespace(
        OrderRef="7",
        ExchangeID="SHFE",
        OrderSysID="         131",
        InstrumentID="au2610",
        TradeID="TRADE-1",
        Direction="0",
        OffsetFlag="0",
        Price=812.5,
        Volume=2,
        TradeTime="10:31:05",
        TradingDay="20260814",
    )

    first = session.trade_payload(fill)
    repeated = session.trade_payload(fill)
    different = session.trade_payload(
        SimpleNamespace(**{**vars(fill), "TradeID": "TRADE-2"})
    )

    assert first["event_id"] == repeated["event_id"]
    assert first["event_id"].startswith("trade:ctp:")
    assert first["event_id"] != different["event_id"]
    assert first["gateway_name"] == "CTP"
    assert first["account_id"] == "td-user"
    assert first["trading_day"] == "20260814"
    assert first["exchange"] == "SHFE"
    assert first["trade_id"] == "TRADE-1"
    assert first["order_id"] == "         131"
    assert first["client_id"] == "engine-01"
    assert first["strategy_id"] == "arb-ctp"
    assert first["client_order_id"] == "arb-ctp-1"


def test_trade_is_persisted_before_publish():
    class TradeRegistry:
        def __init__(self):
            self.trades = []

        def record_trade(self, payload):
            self.trades.append(dict(payload))
            return True

    registry = TradeRegistry()
    published = []
    session = CtpSession(
        SimpleNamespace(td_user_id="td-user", query_min_interval_seconds=0),
        lambda topic, event, data: published.append((topic, event, data)),
        registry,
    )
    payload = {
        "event_id": "trade:ctp:persisted-1",
        "client_id": "engine",
        "strategy_id": "arb-ctp",
    }

    session.publish_trade(payload)

    assert registry.trades == [payload]
    assert published[0] == ("trades.td-user", "trade", payload)
