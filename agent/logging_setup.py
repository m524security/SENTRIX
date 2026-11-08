"""
SENTRIX — Structured Logging
============================
Replaces scattered print() calls with the stdlib logging framework:

  • development (default): human-readable single-line format
  • production, or SENTRIX_LOG_JSON=true: one JSON object per line, ready for
    Loki / CloudWatch / ELK / any log shipper — no parsing regexes needed.

Usage:
    from agent.logging_setup import get_logger
    log = get_logger(__name__)
    log.info("cycle complete", extra={"cycle": 12, "assets": 40})

Level comes from SENTRIX_LOG_LEVEL (existing config knob). Idempotent — safe
to call setup() from both the agent and the API entry points.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone

_CONFIGURED = False

# Fields of LogRecord we don't copy into JSON "extra"
_RESERVED = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "taskName", "message", "asctime",
}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for k, v in record.__dict__.items():
            if k not in _RESERVED and not k.startswith("_"):
                entry[k] = v
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


class HumanFormatter(logging.Formatter):
    ICONS = {"DEBUG": "..", "INFO": "->", "WARNING": "[warn]",
             "ERROR": "[err]", "CRITICAL": "[FATAL]"}

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.now(timezone.utc).strftime("%H:%M:%SZ")
        icon = self.ICONS.get(record.levelname, "->")
        base = f"[{ts}] {icon} {record.getMessage()}"
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)
        return base


def setup() -> None:
    """Configure the root 'sentrix' logger once."""
    global _CONFIGURED
    if _CONFIGURED:
        return
    _CONFIGURED = True

    env = os.getenv("SENTRIX_ENV", "development").strip().lower()
    use_json = (os.getenv("SENTRIX_LOG_JSON", "").strip().lower()
                in ("1", "true", "yes")) or env == "production"
    level_name = os.getenv("SENTRIX_LOG_LEVEL", "info").strip().upper()
    level = getattr(logging, level_name, logging.INFO)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if use_json else HumanFormatter())

    root = logging.getLogger("sentrix")
    root.setLevel(level)
    root.handlers.clear()
    root.addHandler(handler)
    root.propagate = False


def get_logger(name: str = "sentrix") -> logging.Logger:
    setup()
    if not name.startswith("sentrix"):
        name = f"sentrix.{name}"
    return logging.getLogger(name)
