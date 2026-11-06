"""core/logging_setup.py's email-alert handler.

Root cause this exists to close: the only automated safety net before it was
`main.py`'s crash watchdog, which self-heals and notifies nobody -- a
recurring `ERROR` had to be found by manually reading the journal. These
tests pin the three properties that make it safe to enable: it does nothing
until configured, a burst of identical errors collapses to one email rather
than flooding the mailbox, and the network call never blocks the logging
call site itself.
"""

import logging
import logging.handlers
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import logging_setup


class AlertEmailHandlerFactoryTests(unittest.TestCase):
    """`_alert_email_handler` is what `_shared_handlers` calls; it must be
    the single place that decides whether alerting is configured at all."""

    def test_none_when_unconfigured(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(logging_setup._alert_email_handler())

    def test_none_without_a_recipient(self):
        env = {"POTATOBOT_ALERT_SMTP_HOST": "smtp.example.com"}
        with patch.dict(os.environ, env, clear=True):
            self.assertIsNone(logging_setup._alert_email_handler())

    def test_none_without_a_host(self):
        env = {"POTATOBOT_ALERT_EMAIL_TO": "ops@example.com"}
        with patch.dict(os.environ, env, clear=True):
            self.assertIsNone(logging_setup._alert_email_handler())

    def test_builds_a_handler_once_both_are_set(self):
        env = {
            "POTATOBOT_ALERT_EMAIL_TO": "ops@example.com",
            "POTATOBOT_ALERT_SMTP_HOST": "smtp.example.com",
        }
        with patch.dict(os.environ, env, clear=True):
            handler = logging_setup._alert_email_handler()
        try:
            self.assertIsInstance(handler, logging_setup.AlertEmailHandler)
            self.assertEqual(logging.ERROR, handler.level)
        finally:
            if handler is not None:
                handler.close()


class AlertEmailHandlerBehaviorTests(unittest.TestCase):
    def _handler(self, **overrides):
        kwargs = dict(host="smtp.example.com", port=587, username="user",
                      password="pw", sender="bot@example.com",
                      recipient="ops@example.com", dedup_window=600.0)
        kwargs.update(overrides)
        return logging_setup.AlertEmailHandler(**kwargs)

    def _record(self, message, name="PotatoBot.Gacha", level=logging.ERROR):
        return logging.LogRecord(
            name=name, level=level, pathname=__file__, lineno=1,
            msg=message, args=(), exc_info=None)

    def test_emit_never_opens_an_smtp_connection_itself(self):
        """The calling thread must never block on the network -- only the
        background drain thread may touch smtplib."""
        with patch("core.logging_setup.smtplib.SMTP") as mocked_smtp:
            handler = self._handler()
            try:
                handler.emit(self._record("boom"))
                mocked_smtp.assert_not_called()
                handler._queue.join()
            finally:
                handler.close()
        mocked_smtp.assert_called_once()

    def test_starttls_verifies_the_server_certificate(self):
        """With no `context`, `starttls()` checks no certificate at all."""
        import ssl
        with patch("core.logging_setup.smtplib.SMTP") as mocked_smtp:
            smtp_instance = MagicMock()
            mocked_smtp.return_value.__enter__.return_value = smtp_instance
            handler = self._handler()
            try:
                handler.emit(self._record("boom"))
                handler._queue.join()
            finally:
                handler.close()
        context = smtp_instance.starttls.call_args.kwargs.get("context")
        self.assertIsInstance(context, ssl.SSLContext)
        self.assertEqual(ssl.CERT_REQUIRED, context.verify_mode)
        self.assertTrue(context.check_hostname)

    def test_a_burst_of_identical_records_sends_exactly_one_email(self):
        with patch("core.logging_setup.smtplib.SMTP") as mocked_smtp:
            smtp_instance = MagicMock()
            mocked_smtp.return_value.__enter__.return_value = smtp_instance
            handler = self._handler()
            try:
                for _ in range(6):
                    handler.emit(self._record("Missing locale key: gacha.rewards.premium_7d"))  # gitleaks:allow
                handler._queue.join()
            finally:
                handler.close()
        self.assertEqual(1, smtp_instance.send_message.call_count)

    def test_a_different_message_is_not_collapsed_by_the_dedup_window(self):
        with patch("core.logging_setup.smtplib.SMTP") as mocked_smtp:
            smtp_instance = MagicMock()
            mocked_smtp.return_value.__enter__.return_value = smtp_instance
            handler = self._handler()
            try:
                handler.emit(self._record("first problem"))
                handler.emit(self._record("second problem"))
                handler._queue.join()
            finally:
                handler.close()
        self.assertEqual(2, smtp_instance.send_message.call_count)

    def test_a_send_failure_does_not_crash_the_drain_thread(self):
        """One bad send must not take down alerting for every message after
        it -- the thread has no supervisor of its own."""
        with patch("core.logging_setup.smtplib.SMTP", side_effect=OSError("unreachable")):
            handler = self._handler(dedup_window=0.0)
            try:
                handler.emit(self._record("first"))
                handler._queue.join()
                self.assertTrue(handler._thread.is_alive())
            finally:
                handler.close()

    def test_no_username_skips_login(self):
        with patch("core.logging_setup.smtplib.SMTP") as mocked_smtp:
            smtp_instance = MagicMock()
            mocked_smtp.return_value.__enter__.return_value = smtp_instance
            handler = self._handler(username="")
            try:
                handler.emit(self._record("boom"))
                handler._queue.join()
            finally:
                handler.close()
        smtp_instance.login.assert_not_called()
        smtp_instance.send_message.assert_called_once()


class ConsoleHandlerTests(unittest.TestCase):
    """`POTATOBOT_LOG_CONSOLE` decides whether log lines also go to stdout,
    which under systemd is the journal on the host's plain disk. Unset keeps
    them (a self-hoster changes nothing); `false` leaves the file alone; a
    malformed value is refused rather than guessed."""

    def setUp(self):
        import tempfile
        self.saved = logging_setup._handlers
        self.saved_dir = logging_setup.LOG_DIR
        self.tmp = tempfile.TemporaryDirectory()
        logging_setup.LOG_DIR = self.tmp.name

    def tearDown(self):
        for handler in logging_setup._handlers or ():
            if handler not in (self.saved or ()):
                handler.close()
        logging_setup._handlers = self.saved
        logging_setup.LOG_DIR = self.saved_dir
        self.tmp.cleanup()

    def handlers_with(self, value):
        logging_setup._handlers = None
        env = {} if value is None else {"POTATOBOT_LOG_CONSOLE": value}
        with patch.dict(os.environ, env, clear=True):
            return logging_setup._shared_handlers()

    @staticmethod
    def console(handlers):
        return [h for h in handlers if type(h) is logging.StreamHandler
                and h.stream is sys.stdout]

    def test_unset_keeps_the_console(self):
        self.assertEqual(1, len(self.console(self.handlers_with(None))))

    def test_false_leaves_only_the_file(self):
        handlers = self.handlers_with("false")
        self.assertEqual([], self.console(handlers))
        self.assertEqual(1, len([h for h in handlers if isinstance(
            h, logging.handlers.RotatingFileHandler)]))

    def test_a_malformed_value_is_refused(self):
        with self.assertRaises(ValueError):
            self.handlers_with("sometimes")


if __name__ == "__main__":
    unittest.main()
