import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from config import Settings


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
        "CTP_BROKER_ID": "shared-broker",
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

    assert settings.broker_id == "shared-broker"
    assert settings.md_user_id == "md-user"
    assert settings.md_password == "md-secret"
    assert settings.td_user_id == "td-user"
    assert settings.td_password == "td-secret"
    assert settings.app_id == "shared-app"
    assert settings.auth_code == "shared-auth"


def test_legacy_credentials_remain_supported(monkeypatch):
    canonical_names = (
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

    assert settings.broker_id == "legacy-broker"
    assert settings.md_user_id == "legacy-user"
    assert settings.md_password == "legacy-secret"
    assert settings.td_user_id == "legacy-user"
    assert settings.td_password == "legacy-secret"
    assert settings.app_id == "legacy-app"
    assert settings.auth_code == "legacy-auth"
