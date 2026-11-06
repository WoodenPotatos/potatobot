"""The one place a Node-driven test may skip.

Tests that drive `dashboard/script.js` through Node skip only when Node itself
is absent, and only through `require_node`. With Node present nothing may skip:
a missing jsdom is a failure, because a test that skips itself is a test that
does not exist (`JavaScriptIsActuallyRunTests`, which checks statically that
these classes have no other way to skip, so it need not run them twice).

Import it as `import node_harness` after putting this directory on `sys.path`,
as with `pg_harness`.
"""

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_REASON = "node is not installed"


def require_node(test, jsdom: bool = False) -> str:
    """The `node` executable, or skip `test` when Node is not installed. With
    `jsdom`, a missing `node_modules/jsdom` fails the test rather than skipping
    it: Node being here means the harness is expected to run."""
    node = shutil.which("node")
    if node is None:
        test.skipTest(SKIP_REASON)
    if jsdom and not (ROOT / "node_modules" / "jsdom").is_dir():
        test.fail("Node is installed but jsdom is not; run `npm ci`")
    return node
