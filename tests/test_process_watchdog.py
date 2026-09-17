"""Noticing that the bot has stopped being a bot.

On 2026-08-31 the gateway went away at 21:59 and nothing rebuilt it. The process
stayed alive and idle — event loop in `epoll`, 132 seconds of CPU across fourteen
hours, one socket and it was the dashboard's — so `Restart=on-failure` saw a
perfectly healthy service and the bot was dead on Discord until somebody noticed
by hand the next day.

The cause is still unknown; there was no `faulthandler` and no profiler on the
host, so the connection task's stack could not be read. What is fixed here is the
part that made a fourteen-hour outage possible regardless of cause: nothing was
watching, and a process that never exits is invisible to a service manager.
"""

import faulthandler
import importlib
import os
import signal
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core import logging_setup


class FakeClient:
    """Only what `gateway_is_up` reads, which is the point: it must stay two
    attribute reads so a thread outside the loop can do them safely."""

    def __init__(self, closed=False, latency=0.05):
        self._closed = closed
        self.latency = latency

    def is_closed(self):
        return self._closed


def main_module():
    """`main.py` without running it.

    It calls `bot.run()` at import, so the watchdog pieces are read out of the
    source into their own namespace instead. Everything under test is pure or
    takes its collaborators as arguments, which is what makes that possible.
    """
    import ast
    import math
    import time
    import types

    with open(os.path.join(ROOT, "main.py"), encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    wanted = {"GATEWAY_GRACE_SECONDS", "LOOP_GRACE_SECONDS",
              "WATCHDOG_INTERVAL_SECONDS", "gateway_is_up", "watchdog_verdict"}
    kept = [node for node in tree.body
            if (isinstance(node, ast.FunctionDef) and node.name in wanted)
            or (isinstance(node, ast.Assign)
                and any(getattr(t, "id", None) in wanted for t in node.targets))]
    module = types.ModuleType("main_watchdog")
    module.__dict__.update(math=math, time=time)
    exec(compile(ast.Module(body=kept, type_ignores=[]), "main.py", "exec"),
         module.__dict__)
    return module


class GatewayHealthTests(unittest.TestCase):
    def setUp(self):
        self.main = main_module()

    def test_a_connected_client_is_up(self):
        self.assertTrue(self.main.gateway_is_up(FakeClient()))

    def test_no_websocket_reads_as_down(self):
        """`latency` is nan while there is no websocket, which is the one cheap
        signal that separates connected from gone."""
        self.assertFalse(self.main.gateway_is_up(FakeClient(latency=float("nan"))))

    def test_a_closed_client_is_down(self):
        self.assertFalse(self.main.gateway_is_up(FakeClient(closed=True)))

    def test_a_missing_latency_is_down_rather_than_an_error(self):
        """It is read from another thread; it must never raise there."""
        self.assertFalse(self.main.gateway_is_up(FakeClient(latency=None)))


class WatchdogVerdictTests(unittest.TestCase):
    def setUp(self):
        self.main = main_module()
        self.verdict = self.main.watchdog_verdict

    def test_a_healthy_process_is_left_alone(self):
        self.assertIsNone(self.verdict(1000.0, 999.0, None))

    def test_nothing_fires_before_the_first_heartbeat(self):
        """A zero heartbeat is a bot still starting, not a bot that has hung.
        Arming on the first heartbeat is what keeps a slow start from being read
        as a hang and restarted into a loop."""
        self.assertIsNone(self.verdict(10_000.0, 0.0, 1.0))

    def test_a_silent_loop_is_a_reason(self):
        reason = self.verdict(1000.0, 800.0, None)
        self.assertIsNotNone(reason)
        self.assertIn("event loop", reason)

    def test_a_loop_within_its_grace_is_not(self):
        self.assertIsNone(self.verdict(1000.0, 1000.0 - 119.0, None))

    def test_a_gateway_down_past_its_grace_is_a_reason(self):
        reason = self.verdict(1000.0, 999.5, 600.0)
        self.assertIsNotNone(reason)
        self.assertIn("gateway", reason)

    def test_a_brief_reconnection_is_tolerated(self):
        """discord.py backs off and retries on its own; restarting through a
        normal reconnect would be worse than waiting."""
        self.assertIsNone(self.verdict(1000.0, 999.5, 999.0))

    def test_the_boundary_is_not_inclusive(self):
        """Exactly at the grace is still within it, so a clock that lands on the
        boundary does not restart the bot."""
        grace = self.main.GATEWAY_GRACE_SECONDS
        self.assertIsNone(self.verdict(1000.0, 999.5, 1000.0 - grace))
        self.assertIsNotNone(self.verdict(1000.0, 999.5, 1000.0 - grace - 0.1))

    def test_the_reason_says_how_long(self):
        """It goes into the journal as the only explanation of why the process
        died, so it has to carry the number."""
        self.assertIn("600", self.verdict(1000.0, 999.5, 400.0))


class RestartLimitTests(unittest.TestCase):
    """The grace has to keep systemd willing to try again.

    `StartLimitBurst=5` within `StartLimitIntervalSec=5min` makes systemd give up
    on the unit entirely. If the watchdog fired faster than that during a long
    Discord outage it would not heal the bot, it would bury it.
    """

    UNIT_START_LIMIT_INTERVAL = 300.0
    UNIT_START_LIMIT_BURST = 5

    def test_the_gateway_grace_cannot_trip_the_start_limit(self):
        main = main_module()
        shortest_gap = main.GATEWAY_GRACE_SECONDS
        starts_in_window = self.UNIT_START_LIMIT_INTERVAL / shortest_gap
        self.assertLess(
            starts_in_window, self.UNIT_START_LIMIT_BURST,
            "the watchdog would restart often enough for systemd to give up",
        )


class WatchdogActuallyExitsTests(unittest.TestCase):
    """The loop around the verdict, in a real process.

    Everything above is pure and proves the decision; this proves the
    consequence. A watchdog that decides correctly and then fails to exit is the
    same fourteen-hour outage with better logging, so the exit is worth a
    subprocess.
    """

    SCRIPT = r"""
import ast, logging, math, os, sys, threading, time, types

ROOT, MODE = sys.argv[1], sys.argv[2]
with open(os.path.join(ROOT, "main.py"), encoding="utf-8") as handle:
    tree = ast.parse(handle.read())
wanted = {"GATEWAY_GRACE_SECONDS", "LOOP_GRACE_SECONDS",
          "WATCHDOG_INTERVAL_SECONDS", "gateway_is_up", "watchdog_verdict",
          "watch_for_a_wedged_process"}
kept = [n for n in tree.body
        if (isinstance(n, ast.FunctionDef) and n.name in wanted)
        or (isinstance(n, ast.Assign)
            and any(getattr(t, "id", None) in wanted for t in n.targets))]
mod = types.ModuleType("w")
mod.__dict__.update(math=math, os=os, time=time, logging=logging,
                    bot_logger=logging.getLogger("PotatoBot"),
                    _loop_heartbeat=time.monotonic())
exec(compile(ast.Module(body=kept, type_ignores=[]), "main.py", "exec"), mod.__dict__)

# Stand in for `monitor_event_loop_lag`, which is what keeps the heartbeat fresh
# in the real bot. "wedged" is that stopping.
def beat():
    while MODE != "wedged":
        mod._loop_heartbeat = time.monotonic()
        time.sleep(0.02)
threading.Thread(target=beat, daemon=True).start()

class Client:
    def is_closed(self): return False
    @property
    def latency(self): return float("nan") if MODE == "down" else 0.05

mod.watch_for_a_wedged_process(Client(), interval=0.05,
                               loop_grace=0.4, gateway_grace=0.3)
print("the watchdog returned instead of exiting")
sys.exit(0)
"""

    def run_watchdog(self, mode, timeout=30):
        import subprocess
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as handle:
            handle.write(self.SCRIPT)
            path = handle.name
        try:
            return subprocess.run([sys.executable, path, ROOT, mode],
                                  capture_output=True, text=True, timeout=timeout)
        finally:
            os.unlink(path)

    def test_a_lost_gateway_exits_non_zero(self):
        """Non-zero specifically: the unit is `Restart=on-failure`, so a clean
        exit would leave the bot dead exactly as it was."""
        result = self.run_watchdog("down")
        self.assertEqual(1, result.returncode, f"{result.stdout}\n{result.stderr}")
        self.assertIn("gateway", (result.stdout + result.stderr).lower())

    def test_a_wedged_loop_exits_non_zero(self):
        """The other failure in the family, and the one an in-loop watchdog
        could never catch."""
        result = self.run_watchdog("wedged")
        self.assertEqual(1, result.returncode, f"{result.stdout}\n{result.stderr}")
        self.assertIn("event loop", (result.stdout + result.stderr).lower())

    def test_a_healthy_bot_is_left_running(self):
        """Or a watchdog that always exits would pass both tests above while
        making the bot unusable."""
        import subprocess

        with self.assertRaises(subprocess.TimeoutExpired):
            self.run_watchdog("up", timeout=3)


class StackDumpTests(unittest.TestCase):
    """The thing whose absence made the outage undiagnosable."""

    def test_it_enables_the_fatal_handler(self):
        logging_setup.enable_stack_dumps()
        self.assertTrue(faulthandler.is_enabled())

    @unittest.skipUnless(hasattr(signal, "SIGUSR1"), "no SIGUSR1 on this platform")
    def test_it_registers_the_on_demand_signal(self):
        logging_setup.enable_stack_dumps()
        # `unregister` reports whether it was registered — and removes it, so it
        # is put back rather than left off for whatever runs next.
        try:
            self.assertTrue(faulthandler.unregister(signal.SIGUSR1))
        finally:
            logging_setup.enable_stack_dumps()

    def test_calling_it_twice_is_a_no_op(self):
        """The in-process dashboard calls it after `main` already has."""
        logging_setup.enable_stack_dumps()
        logging_setup.enable_stack_dumps()
        self.assertTrue(faulthandler.is_enabled())

    def test_the_dashboard_process_gets_it_too(self):
        """`python dashboard_api.py` is a whole process of its own, and it can
        hang the same way."""
        import ast

        with open(os.path.join(ROOT, "core", "logging_setup.py"), encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        setup = next(node for node in ast.walk(tree)
                     if isinstance(node, ast.FunctionDef)
                     and node.name == "configure_dashboard_logging")
        self.assertIn("enable_stack_dumps", ast.dump(setup))


if __name__ == "__main__":
    unittest.main()
