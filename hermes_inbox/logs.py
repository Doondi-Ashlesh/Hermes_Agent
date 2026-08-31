"""Logging setup.

The agent is meant to run unattended under systemd, where `print()` is close to
useless: no levels, no timestamps, nothing a log aggregator can parse, and no way
to turn detail up without editing code.

Two formats:

- `text`  — human-readable, the default. What you want in a terminal.
- `json`  — one object per line, for `journalctl -o cat | jq` or shipping
  somewhere. Extra fields attached via `extra={...}` are merged into the object,
  so a decision can be queried by uid or score rather than grepped.

Library code logs; the CLI prints. Anything the user explicitly asked to see
(`stats`, `eval` reports, notifications) goes to stdout as before — those are
output, not telemetry, and should not vanish when someone sets a log level.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

LOGGER_NAME = "hermes_inbox"

# Attributes present on every LogRecord; anything else was passed via `extra`.
_STANDARD = frozenset(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__
) | {"message", "asctime", "taskName"}


class TextFormatter(logging.Formatter):
    default_time_format = "%H:%M:%S"

    def format(self, record: logging.LogRecord) -> str:
        base = f"{self.formatTime(record)} {record.levelname:<7} {record.getMessage()}"
        extras = _extras(record)
        if extras:
            base += "  " + " ".join(f"{k}={v}" for k, v in extras.items())
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)
        return base


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            **_extras(record),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def _extras(record: logging.LogRecord) -> dict[str, Any]:
    return {k: v for k, v in record.__dict__.items() if k not in _STANDARD}


def configure(level: str = "INFO", fmt: str = "text", stream=None) -> logging.Logger:
    """Attach a single handler to the package logger. Idempotent."""
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(getattr(logging, str(level).upper(), logging.INFO))

    for handler in list(logger.handlers):
        logger.removeHandler(handler)

    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(JsonFormatter() if str(fmt).lower() == "json" else TextFormatter())
    logger.addHandler(handler)

    # Logs go to our handler only; the root logger is left alone so importing
    # this package never hijacks a host application's logging.
    logger.propagate = False
    return logger


def get_logger(name: str) -> logging.Logger:
    """Child logger for a module: `get_logger(__name__)`."""
    if name.startswith(LOGGER_NAME):
        return logging.getLogger(name)
    return logging.getLogger(f"{LOGGER_NAME}.{name}")
