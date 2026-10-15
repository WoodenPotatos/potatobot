"""The project's log handlers, shared by both processes.

This lived in `main.py`, which meant it applied only when the bot was the
entry point. `python dashboard_api.py` — the two-process split this codebase
prefers beyond the private deployment — got no configuration at all, so
`waitress.serve`'s `logging.basicConfig()` installed a root handler and the
dashboard's own records came out in Python's default format with no rotating
file behind them. A cog cannot import `main`, and `dashboard_api` must not, so
the setup belongs in a module both can reach.
"""

import faulthandler
import logging
import logging.handlers
import os
import queue
import signal
import smtplib
import sys
import threading
import time
from email.message import EmailMessage

from core.bounded import BoundedTimestampMap

# Two levels up: this module lives in `core/` and the log directory belongs
# beside the entry points at the repository root -- unless a deployment
# overrides it, the way `POTATOBOT_DB_PATH` already lets the database move.
# A container's root filesystem should hold nothing mutable, so the override
# points this at the same `/data` volume the database already lives on.
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.getenv("POTATOBOT_LOG_DIR", os.path.join(BASE_DIR, "logs"))

LOG_FORMAT = logging.Formatter(
    "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


class AlertEmailHandler(logging.Handler):
    """Email the maintainer on a recurring ERROR/CRITICAL, independent of
    Discord's own availability.

    A Discord-delivered alert (a webhook) cannot reach anyone during the
    exact failure mode most worth alerting on -- Discord itself being
    unreachable, which this project has already hit twice (a 14-hour silent
    gateway loss and two DNS outages; see docs/lessons.md). SMTP email is a
    genuinely separate channel.

    `emit()` never opens the SMTP connection itself: the same "no blocking
    network call in the hot path" rule this codebase already applies to
    database transactions applies here to logging. A record is only
    formatted and queued; one background daemon thread drains the queue and
    sends. An identical `(logger name, message)` within `dedup_window`
    seconds collapses into one email -- aimed squarely at the failure mode
    that motivated this: six identical lines for one command becoming six
    emails.
    """

    def __init__(self, *, host, port, username, password, sender, recipient,
                 dedup_window=600.0, queue_maxsize=200):
        super().__init__(level=logging.ERROR)
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._sender = sender
        self._recipient = recipient
        self._dedup_window = dedup_window
        self._seen = BoundedTimestampMap(max_age=dedup_window, max_entries=512)
        self._queue = queue.Queue(maxsize=queue_maxsize)
        self._thread = threading.Thread(
            target=self._drain, name="alert-email", daemon=True)
        self._thread.start()

    def emit(self, record):
        try:
            key = (record.name, record.getMessage())
            now = time.monotonic()
            last = self._seen.get(key)
            if last is not None and (now - last) < self._dedup_window:
                return
            self._seen.start(key, now)
            subject = f"[PotatoBot] {record.levelname} in {record.name}"
            body = self.format(record)
            self._queue.put_nowait((subject, body))
        except queue.Full:
            pass
        except Exception:
            self.handleError(record)

    def _drain(self):
        while True:
            subject, body = self._queue.get()
            try:
                self._send(subject, body)
            except Exception as exc:
                # Never through a "PotatoBot"-named logger: that would route
                # straight back into this same handler's own emit().
                print(f"[alert-email] send failed: {exc}", file=sys.stderr)
            finally:
                self._queue.task_done()

    def _send(self, subject, body):
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = self._sender
        message["To"] = self._recipient
        message.set_content(body)
        with smtplib.SMTP(self._host, self._port, timeout=10) as smtp:
            smtp.starttls()
            if self._username:
                smtp.login(self._username, self._password)
            smtp.send_message(message)


def _alert_email_handler():
    """None unless every required setting is present -- opt-in through the
    environment, like everything else this deployment configures that way.
    A blank `POTATOBOT_ALERT_EMAIL_TO` or `_SMTP_HOST` disables it entirely.
    """
    recipient = os.getenv("POTATOBOT_ALERT_EMAIL_TO")
    host = os.getenv("POTATOBOT_ALERT_SMTP_HOST")
    if not recipient or not host:
        return None
    username = os.getenv("POTATOBOT_ALERT_SMTP_USER", "")
    handler = AlertEmailHandler(
        host=host,
        port=int(os.getenv("POTATOBOT_ALERT_SMTP_PORT", "587")),
        username=username,
        password=os.getenv("POTATOBOT_ALERT_SMTP_PASSWORD", ""),
        sender=os.getenv("POTATOBOT_ALERT_SMTP_FROM") or username or recipient,
        recipient=recipient,
    )
    handler.setFormatter(LOG_FORMAT)
    return handler


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
    handlers = [file_handler, console_handler]
    alert_handler = _alert_email_handler()
    if alert_handler is not None:
        handlers.append(alert_handler)
    _handlers = tuple(handlers)
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


def enable_stack_dumps():
    """Make the process able to say what it is doing, on demand.

    A bot that stopped talking to Discord for fourteen hours could not be
    diagnosed at all: the process was alive and idle, the event loop was in
    `epoll`, and there was no way to see what the gateway task was waiting on
    without installing a profiler on a production host. `faulthandler` is in the
    standard library and costs nothing until it fires.

    Two halves. `enable()` prints every thread's Python stack to stderr — and so
    to the journal — if the interpreter dies on a fatal signal, which otherwise
    leaves nothing behind at all. `register(SIGUSR1)` does the same on demand:

        kill -USR1 $(systemctl show potatobot -p MainPID --value)

    `chain=False` because nothing else handles these, and `all_threads=True`
    because the interesting one is rarely the main thread.

    Idempotent, so the in-process dashboard calling this after `main` is a no-op
    rather than a second registration.
    """
    if not faulthandler.is_enabled():
        faulthandler.enable()
    # Windows has no SIGUSR1, and neither has a stripped-down container.
    if hasattr(signal, "SIGUSR1"):
        faulthandler.register(signal.SIGUSR1, all_threads=True, chain=False)


def configure_dashboard_logging():
    """Everything a standalone `python dashboard_api.py` needs.

    `waitress` is included deliberately: it logs its own queue-depth warnings,
    and they are worth reading in the same format as everything else rather than
    in whatever `basicConfig` picks.
    """
    configure_logger("PotatoBot")
    configure_logger("waitress")
    enable_stack_dumps()
