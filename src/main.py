from __future__ import annotations

import argparse
import signal
import threading

from dotenv import load_dotenv
from loguru import logger

from config import Settings
from logger import configure_logger
from proxy import CtpProxy


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="CTP ZeroMQ proxy")
    parser.add_argument("--connect-timeout", type=float, default=20.0)
    return parser.parse_args()


def main() -> int:
    load_dotenv()
    configure_logger()
    args = parse_args()
    settings = Settings.from_env()
    settings.validate()
    proxy = CtpProxy(settings)
    shutdown = threading.Event()

    def request_shutdown(signum: int, _frame: object) -> None:
        logger.info("Received signal {}; shutting down", signum)
        shutdown.set()

    signal.signal(signal.SIGINT, request_shutdown)
    signal.signal(signal.SIGTERM, request_shutdown)

    try:
        proxy.start()
        if not proxy.connect(args.connect_timeout):
            logger.error("CTP login did not complete before timeout")
            return 1
        logger.info("CTP proxy is ready")
        shutdown.wait()
        return 0
    except Exception:
        logger.exception("CTP proxy terminated unexpectedly")
        return 1
    finally:
        proxy.stop()


if __name__ == "__main__":
    raise SystemExit(main())

