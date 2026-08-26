import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from config import Settings


def test_market_data_is_enabled_by_default(monkeypatch):
    monkeypatch.delenv("CTP_ENABLE_MD", raising=False)
    assert Settings.from_env().enable_md is True


def test_trading_is_enabled_by_default(monkeypatch):
    monkeypatch.delenv("CTP_ENABLE_TD", raising=False)
    assert Settings.from_env().enable_td is True


def test_td_only_mode_does_not_require_md_configuration():
    settings = Settings(
        md_broker_id="", td_broker_id="td", md_user_id="", md_password="",
        td_user_id="td-user", td_password="td-password", app_id="app",
        auth_code="auth", front_md="", front_td="tcp://td", enable_md=False,
    )
    settings.validate()


def test_td_only_mode_rejects_startup_symbols():
    settings = Settings(
        md_broker_id="", td_broker_id="td", md_user_id="", md_password="",
        td_user_id="td-user", td_password="td-password", app_id="app",
        auth_code="auth", front_md="", front_td="tcp://td", enable_md=False,
        initial_symbols=["ag2612"],
    )
    with pytest.raises(ValueError, match="CTP_SYMBOLS must be empty"):
        settings.validate()


def test_md_only_mode_does_not_require_td_configuration():
    settings = Settings(
        md_broker_id="md", td_broker_id="", md_user_id="md-user",
        md_password="md-password", td_user_id="", td_password="",
        app_id="", auth_code="", front_md="tcp://md", front_td="",
        enable_td=False,
    )
    settings.validate()


def test_both_sessions_cannot_be_disabled():
    settings = Settings(
        md_broker_id="", td_broker_id="", md_user_id="", md_password="",
        td_user_id="", td_password="", app_id="", auth_code="",
        front_md="", front_td="", enable_md=False, enable_td=False,
    )
    with pytest.raises(ValueError, match="At least one"):
        settings.validate()


def test_engine_compatible_production_mode_defaults_to_true(monkeypatch):
    monkeypatch.delenv("CTP_B_IS_PRODUCTION_MODE", raising=False)
    monkeypatch.delenv("CTP_PRODUCTION_MODE", raising=False)

    assert Settings.from_env().production_mode is True


def test_engine_compatible_production_mode_accepts_false(monkeypatch):
    monkeypatch.setenv("CTP_B_IS_PRODUCTION_MODE", "false")

    assert Settings.from_env().production_mode is False


def test_engine_compatible_name_takes_precedence_over_legacy_name(monkeypatch):
    monkeypatch.setenv("CTP_B_IS_PRODUCTION_MODE", "yes")
    monkeypatch.setenv("CTP_PRODUCTION_MODE", "false")

    assert Settings.from_env().production_mode is True


def test_legacy_production_mode_remains_supported(monkeypatch):
    monkeypatch.delenv("CTP_B_IS_PRODUCTION_MODE", raising=False)
    monkeypatch.setenv("CTP_PRODUCTION_MODE", "off")

    assert Settings.from_env().production_mode is False


def test_md_and_td_credentials_can_be_configured_separately(monkeypatch):
    values = {
        "CTP_MD_BROKER_ID": "md-broker",
        "CTP_TD_BROKER_ID": "td-broker",
        "CTP_MD_USER_ID": "md-user",
        "CTP_MD_PASSWORD": "md-secret",
        "CTP_TD_USER_ID": "td-user",
        "CTP_TD_PASSWORD": "td-secret",
        "CTP_APP_ID": "shared-app",
        "CTP_AUTH_CODE": "shared-auth",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)

    settings = Settings.from_env()

    assert settings.md_broker_id == "md-broker"
    assert settings.td_broker_id == "td-broker"
    assert settings.md_user_id == "md-user"
    assert settings.md_password == "md-secret"
    assert settings.td_user_id == "td-user"
    assert settings.td_password == "td-secret"
    assert settings.app_id == "shared-app"
    assert settings.auth_code == "shared-auth"


def test_legacy_credentials_remain_supported(monkeypatch):
    canonical_names = (
        "CTP_MD_BROKER_ID",
        "CTP_TD_BROKER_ID",
        "CTP_MD_USER_ID",
        "CTP_MD_PASSWORD",
        "CTP_TD_USER_ID",
        "CTP_TD_PASSWORD",
    )
    for name in canonical_names:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CTP_BROKER_ID", "legacy-broker")
    monkeypatch.setenv("CTP_USER_ID", "legacy-user")
    monkeypatch.setenv("CTP_PASSWORD", "legacy-secret")
    monkeypatch.setenv("CTP_APP_ID", "legacy-app")
    monkeypatch.setenv("CTP_AUTH_CODE", "legacy-auth")

    settings = Settings.from_env()

    assert settings.md_broker_id == "legacy-broker"
    assert settings.td_broker_id == "legacy-broker"
    assert settings.md_user_id == "legacy-user"
    assert settings.md_password == "legacy-secret"
    assert settings.td_user_id == "legacy-user"
    assert settings.td_password == "legacy-secret"
    assert settings.app_id == "legacy-app"
    assert settings.auth_code == "legacy-auth"
