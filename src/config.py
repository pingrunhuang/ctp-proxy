from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


TRUE_VALUES = frozenset({"1", "true", "yes", "on", "y"})


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    return default if value is None else value.strip().lower() in TRUE_VALUES


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    return default if value is None else int(value)


def _float_env(name: str, default: float) -> float:
    value = os.getenv(name)
    return default if value is None else float(value)


def _symbols_env(name: str) -> list[str]:
    return [item.strip() for item in os.getenv(name, "").split(",") if item.strip()]


def _env_with_legacy_fallback(name: str, legacy_name: str, default: str = "") -> str:
    value = os.getenv(name)
    return os.getenv(legacy_name, default) if value is None else value


def normalize_front(value: str) -> str:
    value = value.strip()
    if value and "://" not in value:
        return f"tcp://{value}"
    return value


@dataclass(slots=True)
class Settings:
    broker_id: str
    md_user_id: str
    md_password: str
    td_user_id: str
    td_password: str
    app_id: str
    auth_code: str
    front_md: str
    front_td: str
    production_mode: bool = True
    initial_symbols: list[str] = field(default_factory=list)
    zmq_bind_host: str = "0.0.0.0"
    zmq_pub_port: int = 5565
    zmq_rep_port: int = 5566
    query_min_interval_seconds: float = 1.0
    query_timeout_seconds: float = 10.0
    snapshot_ttl_seconds: float = 5.0
    flow_path: Path = Path("flow")
    database_url: str = "postgresql://ctp_proxy:ctp_proxy@127.0.0.1:5432/ctp_proxy"
    database_pool_min_size: int = 1
    database_pool_max_size: int = 5
    database_connect_timeout_seconds: float = 10.0

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            broker_id=os.getenv("CTP_BROKER_ID", ""),
            md_user_id=_env_with_legacy_fallback("CTP_MD_USER_ID", "CTP_USER_ID"),
            md_password=_env_with_legacy_fallback("CTP_MD_PASSWORD", "CTP_PASSWORD"),
            td_user_id=_env_with_legacy_fallback("CTP_TD_USER_ID", "CTP_USER_ID"),
            td_password=_env_with_legacy_fallback("CTP_TD_PASSWORD", "CTP_PASSWORD"),
            app_id=os.getenv("CTP_APP_ID", "simnow_client_test"),
            auth_code=os.getenv("CTP_AUTH_CODE", "0000000000000000"),
            front_md=normalize_front(os.getenv("CTP_FRONT_MD", "")),
            front_td=normalize_front(os.getenv("CTP_FRONT_TD", "")),
            production_mode=_bool_env(
                "CTP_B_IS_PRODUCTION_MODE",
                _bool_env("CTP_PRODUCTION_MODE", True),
            ),
            initial_symbols=_symbols_env("CTP_SYMBOLS"),
            zmq_bind_host=os.getenv("ZMQ_BIND_HOST", "0.0.0.0"),
            zmq_pub_port=_int_env("ZMQ_PUB_PORT", 5565),
            zmq_rep_port=_int_env("ZMQ_REP_PORT", 5566),
            query_min_interval_seconds=_float_env("CTP_QUERY_MIN_INTERVAL_SECONDS", 1.0),
            query_timeout_seconds=_float_env("CTP_QUERY_TIMEOUT_SECONDS", 10.0),
            snapshot_ttl_seconds=_float_env("CTP_SNAPSHOT_TTL_SECONDS", 5.0),
            flow_path=Path(os.getenv("CTP_FLOW_PATH", "flow")),
            database_url=os.getenv(
                "DATABASE_URL",
                "postgresql://ctp_proxy:ctp_proxy@127.0.0.1:5432/ctp_proxy",
            ),
            database_pool_min_size=_int_env("DATABASE_POOL_MIN_SIZE", 1),
            database_pool_max_size=_int_env("DATABASE_POOL_MAX_SIZE", 5),
            database_connect_timeout_seconds=_float_env(
                "DATABASE_CONNECT_TIMEOUT_SECONDS", 10.0
            ),
        )

    def validate(self) -> None:
        missing = [
            name
            for name, value in {
                "CTP_BROKER_ID": self.broker_id,
                "CTP_MD_USER_ID (or CTP_USER_ID)": self.md_user_id,
                "CTP_MD_PASSWORD (or CTP_PASSWORD)": self.md_password,
                "CTP_TD_USER_ID (or CTP_USER_ID)": self.td_user_id,
                "CTP_TD_PASSWORD (or CTP_PASSWORD)": self.td_password,
                "CTP_APP_ID": self.app_id,
                "CTP_AUTH_CODE": self.auth_code,
                "CTP_FRONT_MD": self.front_md,
                "CTP_FRONT_TD": self.front_td,
                "DATABASE_URL": self.database_url,
            }.items()
            if not value
        ]
        if missing:
            raise ValueError(f"Missing required configuration: {', '.join(missing)}")
        if not self.database_url.startswith(("postgresql://", "postgres://")):
            raise ValueError("DATABASE_URL must be a PostgreSQL connection URL")
        if self.database_pool_min_size < 1:
            raise ValueError("DATABASE_POOL_MIN_SIZE must be at least 1")
        if self.database_pool_max_size < self.database_pool_min_size:
            raise ValueError("DATABASE_POOL_MAX_SIZE must be >= DATABASE_POOL_MIN_SIZE")
