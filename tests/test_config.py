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
