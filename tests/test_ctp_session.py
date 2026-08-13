import sys
from pathlib import Path
from types import SimpleNamespace

from loguru import logger


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
        def ReqUserLogin(self, _request, _request_id):
            return -2

    publisher = StatusPublisher()
    publisher.settings = SimpleNamespace(
        broker_id="shared-broker",
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
        broker_id="shared-broker",
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
        "BrokerID": "shared-broker",
        "UserID": "td-user",
        "AppID": "shared-app",
        "AuthCode": "shared-auth",
    }
    assert vars(publisher.td_api.login_request) == {
        "BrokerID": "shared-broker",
        "UserID": "td-user",
        "Password": "td-secret",
    }
