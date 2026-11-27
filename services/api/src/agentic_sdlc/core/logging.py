"""Structured JSON logging with correlation IDs and secret redaction."""

from __future__ import annotations

import json
import logging
import re
import sys
from contextvars import ContextVar
from typing import Any

correlation_id_var: ContextVar[str | None] = ContextVar("correlation_id", default=None)

_SECRET_PATTERNS = [
    re.compile(r"(?i)(authorization:\s*bearer\s+)[A-Za-z0-9._\-~+/]+=*"),
    re.compile(r"(?i)\b(api[_-]?key|token|secret|password|passwd)\b(\s*[=:]\s*)(['\"]?)[^\s'\",]+"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
]


def redact(text: str) -> str:
    """Remove credential-like material from free text."""
    out = text
    out = _SECRET_PATTERNS[0].sub(r"\1[REDACTED]", out)
    out = _SECRET_PATTERNS[1].sub(r"\1\2\3[REDACTED]", out)
    for pat in _SECRET_PATTERNS[2:]:
        out = pat.sub("[REDACTED]", out)
    return out


def redact_obj(value: Any) -> Any:
    """Recursively redact strings and drop values under secret-looking keys."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for k, v in value.items():
            if re.search(r"(?i)(secret|password|token|api[_-]?key|credential)", str(k)):
                result[k] = "[REDACTED]"
            else:
                result[k] = redact_obj(v)
        return result
    if isinstance(value, list | tuple):
        return [redact_obj(v) for v in value]
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": redact(record.getMessage()),
            "correlation_id": correlation_id_var.get(),
        }
        extra = getattr(record, "fields", None)
        if isinstance(extra, dict):
            payload.update(redact_obj(extra))
        if record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, default=str)


class TextFormatter(logging.Formatter):
    """One readable line per record for local development; same redaction as the JSON format."""

    COLORS = {
        "DEBUG": "\033[2m",
        "INFO": "",
        "WARNING": "\033[33m",
        "ERROR": "\033[31m",
        "CRITICAL": "\033[1;31m",
    }

    def __init__(self, color: bool = False) -> None:
        super().__init__()
        self.color = color

    def format(self, record: logging.LogRecord) -> str:
        fields = getattr(record, "fields", None)
        tail = ""
        if isinstance(fields, dict) and fields:
            tail = " " + " ".join(f"{k}={v}" for k, v in redact_obj(fields).items())
        cid = correlation_id_var.get()
        line = (
            f"{self.formatTime(record, '%H:%M:%S')} {record.levelname:<7} {record.name}: "
            f"{redact(record.getMessage())}{tail}{f' [{cid}]' if cid else ''}"
        )
        if record.exc_info:
            line += "\n" + redact(self.formatException(record.exc_info))
        if self.color and self.COLORS.get(record.levelname):
            line = f"{self.COLORS[record.levelname]}{line}\033[0m"
        return line


def configure_logging(level: str | None = None, fmt: str | None = None) -> None:
    """Root logging from settings (LOG_LEVEL, LOG_FORMAT) unless given explicitly.

    JSON is the default (what log pipelines parse); `text` is for people reading a terminal.
    """
    if level is None or fmt is None:
        from agentic_sdlc.core.settings import get_settings

        s = get_settings()
        level = level or s.log_level
        fmt = fmt or s.log_format
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(TextFormatter(color=sys.stdout.isatty()) if fmt == "text" else JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # Libraries stay at INFO even when the platform logs at DEBUG; their debug output drowns ours.
    for noisy in ("httpx", "httpcore", "urllib3", "asyncio", "multipart", "watchfiles", "psycopg"):
        logging.getLogger(noisy).setLevel(max(logging.INFO, logging.getLevelName(level)))
    # SQLAlchemy logs every statement once its logger is enabled; that is opt-in (SQL_ECHO).
    logging.getLogger("sqlalchemy").setLevel(logging.WARNING)


def log(logger: logging.Logger, level: int, msg: str, **fields: Any) -> None:
    logger.log(level, msg, extra={"fields": fields})
