"""Boot the dashboard and open every page.

Two outages came from a throw during initialisation — a picker built from a
definition missing its locale key, and a listener bound to markup that had been
replaced. Both put "Cannot read properties of null" on the screen and left no
dashboard at all, and neither was catchable by anything the suite had:
`node --check` parses without executing, `tests/test_cog_loading.py` is about
cogs, and nothing drove the page.

`jsdom` is a **test-only** dependency. The dashboard itself still has no build
step and no runtime dependencies; nothing in `package.json` ships.
"""

import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tests" / "js" / "boot_dashboard.js"
STALE_SCRIPT = ROOT / "tests" / "js" / "stale_client.js"


class DashboardBootTests(unittest.TestCase):
    def test_the_shell_starts_and_every_page_opens(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not installed")
        if not (ROOT / "node_modules" / "jsdom").is_dir():
            self.skipTest("jsdom is not installed; run `npm install`")
        result = subprocess.run(
            [node, str(SCRIPT), str(ROOT)], capture_output=True, text=True,
            cwd=str(ROOT), timeout=120,
        )
        self.assertEqual(
            0, result.returncode,
            f"the dashboard failed to boot:\n{result.stdout}\n{result.stderr}",
        )
        # The premise: a run that opened nothing would also exit zero.
        self.assertGreaterEqual(result.stdout.count("ok "), 15, result.stdout)


class StaleClientNoticeTests(unittest.TestCase):
    """A tab left open across a deploy must say so.

    The sidebar version comes from the server, so a page still executing an old
    bundle displays the new number — indistinguishable from a fix that did not
    work. The bundle carries its own token in the `src` it was loaded from, so
    the comparison is exact rather than a guess.
    """

    CASES = [
        # server token, loaded token
        ("new-token", "old-token"),   # stale: the notice must appear
        ("same-token", "same-token"),  # current: it must not
        ("", "old-token"),             # nothing to compare against
    ]

    def test_the_notice_matches_what_the_tokens_imply(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not installed")
        if not (ROOT / "node_modules" / "jsdom").is_dir():
            self.skipTest("jsdom is not installed; run `npm install`")
        for server, loaded in self.CASES:
            with self.subTest(server=server, loaded=loaded):
                result = subprocess.run(
                    [node, str(STALE_SCRIPT), str(ROOT), server, loaded],
                    capture_output=True, text=True, timeout=120)
                self.assertEqual(0, result.returncode,
                                 f"{result.stdout}\n{result.stderr}")
