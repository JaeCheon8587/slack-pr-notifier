"""Tests for app/logging_setup.py — the logging master switch.

Two things matter. Off has to mean off for the loggers this package actually
writes through, not merely a lower level, because every module here shares
"uvicorn.error". And on must not overrule `uvicorn --log-level`: the switch
restores logging without pinning a level the operator chose elsewhere.
"""

from __future__ import annotations

import logging

import pytest

from app.config import Settings
from app.logging_setup import LOGGER_NAMES, configure_logging


@pytest.fixture(autouse=True)
def _restore_loggers():
    """Keep the process-wide logger state this module flips out of the suite."""

    before = {
        name: (logging.getLogger(name).disabled, logging.getLogger(name).level)
        for name in LOGGER_NAMES
    }
    yield
    for name, (disabled, level) in before.items():
        logging.getLogger(name).disabled = disabled
        logging.getLogger(name).setLevel(level)


def _settings(enabled: bool) -> Settings:
    return Settings(log_enabled=enabled)


def test_default_is_off(monkeypatch) -> None:
    """The setting ships off — silence unless someone asks for logs.

    Read with no .env and no ambient variable: this pins the shipped default,
    not whatever the machine running the suite happens to have configured.
    """

    monkeypatch.delenv("LOG_ENABLED", raising=False)
    assert Settings(_env_file=None).log_enabled is False


def test_off_silences_every_logger_the_app_writes_through() -> None:
    configure_logging(_settings(False))
    for name in LOGGER_NAMES:
        assert logging.getLogger(name).disabled is True


def test_off_swallows_even_a_critical_record(caplog) -> None:
    """`disabled` is a hard stop — not a level that CRITICAL slips past."""

    configure_logging(_settings(False))
    logger = logging.getLogger("uvicorn.error")
    with caplog.at_level(logging.DEBUG, logger="uvicorn.error"):
        logger.critical("이 줄은 나가면 안 된다")
    assert caplog.records == []


def test_on_re_enables_and_a_record_gets_through(caplog) -> None:
    configure_logging(_settings(False))
    configure_logging(_settings(True))
    logger = logging.getLogger("uvicorn.error")
    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        logger.info("이 줄은 나가야 한다")
    assert [record.message for record in caplog.records] == ["이 줄은 나가야 한다"]


def test_on_does_not_pin_the_level_uvicorn_chose() -> None:
    """--log-level is uvicorn's call; the switch only toggles disabled."""

    logger = logging.getLogger("uvicorn.error")
    logger.setLevel(logging.WARNING)
    configure_logging(_settings(True))
    assert logger.level == logging.WARNING
    assert logger.disabled is False


def test_switch_is_idempotent_and_reversible() -> None:
    logger = logging.getLogger("uvicorn.access")
    for expected in (False, False, True, True, False):
        configure_logging(_settings(expected))
        assert logger.disabled is not expected


def test_the_names_are_the_ones_the_package_actually_uses() -> None:
    """A module logging to some other name would slip past the switch."""

    from pathlib import Path

    sources = Path("app").rglob("*.py")
    used = {
        line.split('getLogger("')[1].split('"')[0]
        for path in sources
        for line in path.read_text(encoding="utf-8").splitlines()
        if 'getLogger("' in line
    }
    assert used <= set(LOGGER_NAMES)
