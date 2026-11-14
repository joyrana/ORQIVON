"""Log level and format come from settings; the text format keeps the JSON format's redaction."""

from __future__ import annotations

import json
import logging

import pytest

from agentic_sdlc.core.logging import JsonFormatter, TextFormatter, configure_logging, log
from agentic_sdlc.core.settings import get_settings


def _record(msg: str, **fields: object) -> logging.LogRecord:
    rec = logging.LogRecord("agentic_sdlc.test", logging.WARNING, __file__, 1, msg, None, None)
    rec.fields = fields  # type: ignore[attr-defined]
    return rec


def test_text_format_is_one_readable_line_and_redacts() -> None:
    line = TextFormatter().format(
        _record("calling api_key=sk-abcdefghijklmnopqrstuvwx", run="r1", token="t0p")
    )
    assert "WARNING" in line and "agentic_sdlc.test:" in line and "run=r1" in line
    assert "sk-abcdefghijklmnopqrstuvwx" not in line and "t0p" not in line and "[REDACTED]" in line
    assert "\n" not in line and "\033" not in line  # no colour unless writing to a terminal
    assert TextFormatter(color=True).format(_record("x")).startswith("\033[33m")


def test_json_stays_the_default(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    s = get_settings()
    root = logging.getLogger()
    saved = root.handlers[:], root.level
    try:
        configure_logging()
        assert isinstance(root.handlers[0].formatter, JsonFormatter) and root.level == logging.INFO
        monkeypatch.setattr(s, "log_level", "DEBUG")
        monkeypatch.setattr(s, "log_format", "text")
        configure_logging()
        assert isinstance(root.handlers[0].formatter, TextFormatter) and root.level == logging.DEBUG
        # Chatty libraries stay at INFO even in debug mode.
        assert logging.getLogger("httpx").level == logging.INFO
        log(logging.getLogger("agentic_sdlc.test"), logging.DEBUG, "visible in debug", step="qa")
        out = capsys.readouterr().out
        assert "visible in debug step=qa" in out
        with pytest.raises(json.JSONDecodeError):
            json.loads(out.splitlines()[-1])
    finally:
        root.handlers[:], level = saved
        root.setLevel(level)
