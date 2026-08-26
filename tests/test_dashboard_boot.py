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
