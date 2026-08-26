"""The project's log handlers, shared by both processes.

This lived in `main.py`, which meant it applied only when the bot was the
entry point. `python dashboard_api.py` — the two-process split this codebase
prefers beyond the private deployment — got no configuration at all, so
`waitress.serve`'s `logging.basicConfig()` installed a root handler and the
dashboard's own records came out in Python's default format with no rotating
file behind them. A cog cannot import `main`, and `dashboard_api` must not, so
the setup belongs in a module both can reach.
"""

import logging
import logging.handlers
import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, "logs")

LOG_FORMAT = logging.Formatter(
    "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

_handlers = None


def _shared_handlers():
    """Build the handlers once per process, on first use."""
    global _handlers
    if _handlers is not None:
        return _handlers
    os.makedirs(LOG_DIR, exist_ok=True)
    # Rotate local logs so an unattended deployment cannot exhaust its disk.
    file_handler = logging.handlers.RotatingFileHandler(
        filename=os.path.join(LOG_DIR, "bot.log"),
        encoding="utf-8",
        maxBytes=5 * 1024 * 1024,
        backupCount=10,
    )
    file_handler.setFormatter(LOG_FORMAT)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(LOG_FORMAT)
    _handlers = (file_handler, console_handler)
    return _handlers


def configure_logger(name: str) -> logging.Logger:
    """Give one logger the project's handlers, and stop it propagating.

    `propagate = False` is the load-bearing half. Without it every record also
    reaches the root logger, and something *will* put a handler there:
    `waitress.serve` calls `logging.basicConfig()` — documented as "idempotent
    if logging has already been set up", which means it adds a root handler when
    nothing else has. The result was every line in the journal twice, once in
    this format and once in Python's default, which halves a 500 MB journal cap
    for nothing and makes a grep count read double.
    """
    configured = logging.getLogger(name)
    configured.setLevel(logging.INFO)
    if configured.handlers:
        # Idempotent: a second call must not double every record, which is the
        # very failure this function exists to prevent.
        configured.propagate = False
        return configured
    for handler in _shared_handlers():
        configured.addHandler(handler)
    configured.propagate = False
    return configured


def configure_dashboard_logging():
    """Everything a standalone `python dashboard_api.py` needs.

    `waitress` is included deliberately: it logs its own queue-depth warnings,
    and they are worth reading in the same format as everything else rather than
    in whatever `basicConfig` picks.
    """
    configure_logger("PotatoBot")
    configure_logger("waitress")
