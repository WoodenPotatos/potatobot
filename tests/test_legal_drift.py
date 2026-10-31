"""The Privacy Policy is held to the code by `scripts/legal_drift.py`.

The policy makes claims about tables, hosts, log lines, loops and lifetimes.
Each is a fact the repository can state; the stamp records them as of the last
review, and this suite fails when they move, naming the section to re-read.
Re-stamping is a deliberate command, never done here: the failure is the point.
"""

import contextlib
import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import legal_drift  # noqa: E402
from core import database  # noqa: E402


class LegalDriftTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.facts = legal_drift.compute_facts()
        cls.stamp = legal_drift.load_stamp()

    def test_the_stamp_exists_and_is_committed_beside_the_policy(self):
        self.assertIsNotNone(self.stamp, "run `scripts/legal_drift.py stamp` after "
                                         "reviewing docs/legal/PRIVACY.md")
        self.assertEqual("docs/legal/drift_stamp.json",
                         legal_drift.STAMP_PATH.relative_to(ROOT).as_posix())

    def test_the_code_still_has_the_shape_the_policy_describes(self):
        report = legal_drift.drift(self.stamp or {}, self.facts)
        self.assertEqual([], report, "\n" + legal_drift.format_report(report))

    def test_every_fact_names_the_policy_section_it_belongs_to(self):
        self.assertEqual(set(legal_drift.SECTION_FOR), set(self.facts))

    def test_the_facts_are_specific_rather_than_empty(self):
        """A collector that silently finds nothing would pass the drift check forever."""
        self.assertIn("discord.com", self.facts["outbound_hosts"])
        self.assertIn("api.twitch.tv", self.facts["outbound_hosts"])
        self.assertIn("users", self.facts["schema"])
        self.assertTrue(any("serverevents" in line
                            for line in self.facts["log_formats_naming_a_user"]))
        self.assertTrue(any(name.endswith(":retention_sweep")
                            for name in self.facts["loops"]))
        self.assertEqual("True", self.facts["intents"]["message_content"])
        self.assertEqual(["identify guilds"], self.facts["dashboard_session"]["oauth_scopes"])
        self.assertIn("data_retention_days", self.facts["privacy_settings"])
        self.assertEqual(
            "5000", self.facts["retention_constants"]["core/database.py:EXPORT_ROW_LIMIT"])
        self.assertEqual(
            "30", self.facts["retention_constants"]["scripts/offsite_backup.py:--retention-days"])

    def test_a_new_subject_table_is_reported_against_the_export_section(self):
        widened = tuple(database.SUBJECT_TABLES) + (("member_notes", "user_id = ?", 1),)
        with patch.object(database, "SUBJECT_TABLES", widened):
            facts = legal_drift.compute_facts()
        report = dict(legal_drift.drift(self.stamp, facts))
        self.assertIn("subject_tables", report)
        self.assertTrue(any("member_notes" in line for line in report["subject_tables"]))
        self.assertIn("§8.1", legal_drift.SECTION_FOR["subject_tables"])

    def test_a_new_outbound_host_is_reported(self):
        altered = dict(self.facts)
        altered["outbound_hosts"] = sorted(altered["outbound_hosts"] + ["api.example.test"])
        report = dict(legal_drift.drift(self.stamp, altered))
        self.assertEqual(["    + \"api.example.test\""], report["outbound_hosts"])

    def test_a_changed_lifetime_is_reported_with_both_values(self):
        altered = json.loads(json.dumps(self.facts))
        altered["dashboard_session"]["SESSION_LIFETIME"] = "timedelta(hours=48)"
        report = dict(legal_drift.drift(self.stamp, altered))
        self.assertEqual(1, len(report["dashboard_session"]))
        self.assertIn("timedelta(hours=12)", report["dashboard_session"][0])
        self.assertIn("timedelta(hours=48)", report["dashboard_session"][0])

    def test_the_check_command_exits_nonzero_on_drift_and_zero_without(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, legal_drift.main(["check"]))
            with patch.object(legal_drift, "load_stamp", return_value={}):
                self.assertEqual(1, legal_drift.main(["check"]))
            with patch.object(legal_drift, "load_stamp", return_value=None):
                self.assertEqual(1, legal_drift.main(["check"]))

    def test_the_schema_fact_never_touches_the_configured_database(self):
        before = database.DB_PATH
        legal_drift.compute_facts()
        self.assertEqual(before, database.DB_PATH)
        self.assertFalse(os.path.exists(os.path.join(ROOT, "drift.db")))

    def test_a_document_in_force_carries_no_placeholder(self):
        """Every placeholder is one `[UPPER_CASE]` token, so one pattern finds
        them all; a file whose effective date has been set may hold none."""
        import re
        placeholder = re.compile(r"\[[A-Z][A-Z_]+\]")
        for path in sorted((ROOT / "docs" / "legal").glob("*.md")):
            if path.name == "README.md":
                continue
            text = path.read_text(encoding="utf-8")
            found = sorted(set(placeholder.findall(text)))
            in_force = "[EFFECTIVE_DATE]" not in text
            if in_force:
                self.assertEqual([], found, f"{path.name} is in force but still "
                                            f"carries placeholders")
            else:
                # Not in force yet: the brackets that remain must be tokens the
                # README's table explains, never prose someone forgot to replace.
                readme = (ROOT / "docs" / "legal" / "README.md").read_text(encoding="utf-8")
                for token in found:
                    self.assertIn(token, readme, f"{path.name}: {token} is not "
                                                 "explained in docs/legal/README.md")

    def test_hosts_in_comments_and_docstrings_are_not_facts(self):
        """A link in a comment or a docstring reaches nobody; only code literals count."""
        hosts = set(self.facts["outbound_hosts"])
        self.assertNotIn("evil", hosts, "cogs/utils.py's docstring example")
        self.assertNotIn("www.w3.org", hosts, "an XML namespace, listed in NOT_OUTBOUND")
        self.assertNotIn("support-dev.discord.com", hosts)

    def test_the_cookie_secure_flag_is_captured_whole(self):
        secure = self.facts["dashboard_session"]["SESSION_COOKIE_SECURE"]
        self.assertIn("startswith", secure)
        self.assertNotEqual("(", secure)


if __name__ == "__main__":
    unittest.main()
