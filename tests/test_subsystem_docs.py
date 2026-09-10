"""The subsystem rule documents, and the pointers that make them reachable.

`CLAUDE.md` is loaded into every session, so three subsystems' detailed rules
live in `docs/subsystems/` instead and are read on demand. That only works if the
pointer is somewhere it cannot be missed, which is **the file being edited** — a
promise to consult a document is exactly the thing that fails six weeks later.

So each document names the files it governs, each governed file names the
document, and `CLAUDE.md` keeps the trip-wires inline and links the rest. These
tests hold the three together: the failure they exist to catch is somebody adding
a file to a subsystem, or splitting one out, and leaving the rules unreachable
from it.
"""

import os
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SUBSYSTEMS = ROOT / "docs" / "subsystems"

#: How far into a file the pointer has to be. A header comment or module
#: docstring, not something buried where nobody opening the file would see it.
POINTER_WINDOW_LINES = 40


def documents():
    return sorted(SUBSYSTEMS.glob("*.md"))


def governed_files(document: Path):
    """The paths named on the document's `Governs:` line."""
    text = document.read_text(encoding="utf-8")
    match = re.search(r"^Governs:\s*(.+?)(?=\n\n)", text, re.M | re.S)
    assert match, f"{document.name} has no `Governs:` line"
    return [Path(p) for p in re.findall(r"`([^`]+)`", match.group(1))]


class SubsystemDocumentTests(unittest.TestCase):

    def test_there_are_documents_to_check(self):
        """Guards the premise: an empty directory would make every test below
        pass while proving nothing."""
        self.assertTrue(documents(), "docs/subsystems/ is empty")

    def test_every_governed_path_exists(self):
        for document in documents():
            for path in governed_files(document):
                with self.subTest(document=document.name, path=str(path)):
                    self.assertTrue(
                        (ROOT / path).exists(),
                        f"{document.name} governs {path}, which does not exist")

    def test_every_governed_file_points_back(self):
        """The pointer that replaces the promise.

        This is the one that fails when a subsystem gains a file: the rules stay
        written and become unreachable from the place they apply.
        """
        for document in documents():
            relative = f"docs/subsystems/{document.name}"
            for path in governed_files(document):
                with self.subTest(path=str(path)):
                    head = "".join((ROOT / path).read_text(
                        encoding="utf-8").splitlines(keepends=True)[:POINTER_WINDOW_LINES])
                    self.assertIn(
                        relative, head,
                        f"{path} is governed by {relative} but does not name it "
                        f"in its first {POINTER_WINDOW_LINES} lines")

    def test_every_document_is_linked_from_the_instructions(self):
        claude = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
        for document in documents():
            with self.subTest(document=document.name):
                self.assertIn(f"docs/subsystems/{document.name}", claude)

    def test_no_file_is_governed_twice(self):
        """Two documents claiming one file means two places to look and two to
        keep current, which is the duplication the split exists to remove."""
        seen = {}
        for document in documents():
            for path in governed_files(document):
                with self.subTest(path=str(path)):
                    self.assertNotIn(
                        str(path), seen,
                        f"{path} is governed by both {seen.get(str(path))} "
                        f"and {document.name}")
                    seen[str(path)] = document.name

    def test_each_subsystem_keeps_its_trip_wires_inline(self):
        """A block that decayed into a bare link would put every rule behind a
        document I might not open. The trip-wires — the ones that cost money or
        corrupt a record — stay in `CLAUDE.md` whatever else moves.
        """
        claude = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
        for document in documents():
            with self.subTest(document=document.name):
                link = f"docs/subsystems/{document.name}"
                # Every section that mentions it, not the first: another
                # section may cross-reference a document legitimately, and the
                # rule is about the block that *owns* the subsystem.
                sections = [s for s in re.split(r"\n(?=## )", claude) if link in s]
                self.assertTrue(sections, f"nothing links {link}")
                owning = [s for s in sections if "Trip-wires" in s]
                self.assertEqual(
                    1, len(owning),
                    f"exactly one block should own {link} and keep its "
                    f"trip-wires inline; {len(owning)} of {len(sections)} "
                    "sections mentioning it have a trip-wire list")
                section = owning[0]
                bullets = [line for line in section.splitlines()
                           if line.startswith("- **")]
                self.assertGreaterEqual(
                    len(bullets), 3,
                    f"the block linking {link} lists {len(bullets)} trip-wires; "
                    "a link with no rules in front of it is what this prevents")

    def test_the_documents_are_never_published(self):
        """They are instructions to an assistant and name this deployment."""
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "publish_public", ROOT / "scripts" / "publish_public.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for document in documents():
            with self.subTest(document=document.name):
                self.assertTrue(
                    module.is_excluded(f"docs/subsystems/{document.name}"),
                    f"{document.name} would be published")


if __name__ == "__main__":
    unittest.main()
