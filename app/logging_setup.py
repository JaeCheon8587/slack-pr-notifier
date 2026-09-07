"""Application logging master switch — settings.log_enabled, off by default.

Every module in this package logs through ``logging.getLogger("uvicorn.error")``
rather than a per-module logger, and the package configures no handlers or
levels of its own; uvicorn owns both. That makes the on/off switch a single
pair of loggers rather than a config tree: "uvicorn.error" carries this
application's own records (and uvicorn's startup/shutdown lines, which share
it), "uvicorn.access" carries the per-request access log.

The switch flips ``Logger.disabled`` instead of forcing a level, for one
reason: turning logging *on* must not overrule ``uvicorn --log-level``. A
level assignment here would silently pin what the operator chose on the
command line; ``disabled`` leaves it exactly as uvicorn set it.
"""

from __future__ import annotations

import logging

from app.config import Settings

#: The two loggers uvicorn owns and this package writes through.
LOGGER_NAMES = ("uvicorn.error", "uvicorn.access")


def configure_logging(settings: Settings) -> None:
    """Apply ``settings.log_enabled`` to the loggers this app writes through.

    Idempotent, and safe to call more than once — the flag is re-applied, not
    accumulated, so a later call with a different setting reverses an earlier
    one. Call it after uvicorn has configured its own logging (importing the
    ASGI app happens after ``Config.configure_logging``, which is why
    app/main.py applies it at module scope).
    """

    disabled = not settings.log_enabled
    for name in LOGGER_NAMES:
        logging.getLogger(name).disabled = disabled
