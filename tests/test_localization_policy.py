import ast
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from settings_registry import FEATURE_DEFINITIONS, SETTING_DEFINITIONS  # noqa: E402
PYTHON_SOURCES = [
    *ROOT.glob("*.py"),
    *(ROOT / "cogs").glob("*.py"),
    *(ROOT / "scripts").glob("*.py"),
]
NON_LOCALE_SOURCES = [
    *PYTHON_SOURCES,
    *(ROOT / "dashboard").glob("*.js"),
    *(ROOT / "dashboard").glob("*.html"),
    *(ROOT / "dashboard").glob("*.css"),
]
HUNGARIAN_CHARACTERS = re.compile(r"[ÁÉÍÓÖŐÚÜŰáéíóöőúüű]")


def is_translation_call(node):
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "t"
    )


class LocalizationPolicyTests(unittest.TestCase):
    def test_hungarian_prose_is_confined_to_locale_and_data_files(self):
        offenders = []
        for path in NON_LOCALE_SOURCES:
            for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if HUNGARIAN_CHARACTERS.search(line):
                    offenders.append(f"{path.relative_to(ROOT)}:{line_number}")
        self.assertEqual([], offenders)

    def test_command_descriptions_use_locale_lookup(self):
        offenders = []
        for path in PYTHON_SOURCES:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for decorator in node.decorator_list:
                    if not isinstance(decorator, ast.Call):
                        continue
                    name = getattr(decorator.func, "attr", "")
                    if name not in {"hybrid_command", "command"}:
                        continue
                    description = next(
                        (item.value for item in decorator.keywords if item.arg == "description"),
                        None,
                    )
                    if description is not None and not is_translation_call(description):
                        offenders.append(f"{path.relative_to(ROOT)}:{decorator.lineno}")
        self.assertEqual([], offenders)

    def test_all_literal_translation_keys_exist_in_hungarian_catalog(self):
        catalog = json.loads((ROOT / "locales" / "hu.json").read_text(encoding="utf-8"))
        missing = []
        for path in PYTHON_SOURCES:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not is_translation_call(node) or not node.args:
                    continue
                key_node = node.args[0]
                if not isinstance(key_node, ast.Constant) or not isinstance(key_node.value, str):
                    continue
                current = catalog
                for part in key_node.value.split("."):
                    if not isinstance(current, dict) or part not in current:
                        missing.append(f"{path.relative_to(ROOT)}:{node.lineno} {key_node.value}")
                        break
                    current = current[part]
        self.assertEqual([], missing)

    def test_dashboard_literal_keys_exist_in_hungarian_catalog(self):
        """A mistyped key in the front-end renders as `[dashboard.foo]` to the
        operator, which no other test would notice."""
        catalog = json.loads((ROOT / "locales" / "hu.json").read_text(encoding="utf-8"))

        def resolves(dotted):
            current = catalog
            for part in dotted.split("."):
                if not isinstance(current, dict) or part not in current:
                    return False
                current = current[part]
            return True

        # Keys the code composes at runtime are covered by their prefix instead.
        dynamic_prefixes = (
            "dashboard.title_", "dashboard.category_", "dashboard.gacha_",
            "dashboard.apply_", "dashboard.template_", "dashboard.asset_",
            "dashboard.builder_", "dashboard.relative_", "dashboard.theme_",
            "dashboard.status_", "dashboard.pages.",
        )
        missing = []
        for path in [*(ROOT / "dashboard").glob("*.js"), *(ROOT / "dashboard").glob("*.html")]:
            text = path.read_text(encoding="utf-8")
            for key in set(re.findall(r"""["']dashboard\.[A-Za-z0-9_.]+["']""", text)):
                dotted = key.strip("\"'")
                if dotted.startswith(dynamic_prefixes) or resolves(dotted):
                    continue
                missing.append(f"{path.relative_to(ROOT)} {dotted}")
        self.assertEqual([], sorted(missing))

    def test_dashboard_front_end_has_no_markup_injection_sinks(self):
        """Discord-supplied names reach the DOM, so text nodes only."""
        offenders = []
        for path in (ROOT / "dashboard").glob("*.js"):
            for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if line.lstrip().startswith(("*", "//", "/*")):
                    continue
                if re.search(r"\b(innerHTML|outerHTML|insertAdjacentHTML|document\.write)\b", line):
                    offenders.append(f"{path.relative_to(ROOT)}:{line_number}")
        self.assertEqual([], offenders)

    def test_main_language_catalogs_have_identical_key_structure(self):
        def shape(value):
            if isinstance(value, dict):
                return {key: shape(child) for key, child in value.items()}
            return None

        hu = json.loads((ROOT / "locales" / "hu.json").read_text(encoding="utf-8"))
        en = json.loads((ROOT / "locales" / "en.json").read_text(encoding="utf-8"))
        self.assertEqual(shape(hu), shape(en))




class NavigationReachabilityTests(unittest.TestCase):
    """Every sidebar entry must be able to render something.

    The Builders page carried `data-category="builders"`, which no setting
    declares, so `updateNavigation` hid it on every load and the builders were
    unreachable for as long as that attribute was there. Nothing failed; the
    entry simply was not in the sidebar. This is the check that would have said
    so.
    """

    def setUp(self):
        self.html = (ROOT / "dashboard" / "index.html").read_text(encoding="utf-8")
        self.items = [
            {"page": re.search(r'data-page="([\w-]+)"', attributes).group(1),
             "category": (match.group(1) if (match := re.search(
                 r'data-category="([\w-]+)"', attributes)) else None),
             "feature": (match.group(1) if (match := re.search(
                 r'data-feature="([\w-]+)"', attributes)) else None)}
            for attributes in re.findall(
                r'<button type="button" class="nav-item[^"]*"([^>]*)>', self.html)
        ]
        self.sections = set(re.findall(r'<section id="([\w-]+)" class="page',
                                       self.html))

    def test_the_items_were_actually_found(self):
        """Guards the premise: a regex that matches nothing proves nothing."""
        self.assertGreater(len(self.items), 10)
        self.assertIn("overview", {item["page"] for item in self.items})

    def test_every_category_entry_owns_settings_or_child_toggles(self):
        """`categoryHasVisibleSettings` counts both, which is why the Casino and
        Music pages are legitimate with no settings of their own: they exist to
        hold the sub-toggles whose `parent` names them."""
        owning = {definition.category
                  for definition in SETTING_DEFINITIONS.values()}
        parents = {definition.parent
                   for definition in FEATURE_DEFINITIONS.values()
                   if definition.parent}
        for item in self.items:
            if not item["category"]:
                continue
            with self.subTest(page=item["page"]):
                self.assertIn(item["category"], owning | parents,
                              "nothing renders on this page, so it stays hidden")

    def test_a_page_without_a_category_has_a_section_of_its_own(self):
        for item in self.items:
            if item["category"]:
                continue
            with self.subTest(page=item["page"]):
                self.assertIn(item["page"], self.sections)

    def test_no_item_carries_both_hiding_rules(self):
        """`updateNavigation` runs the two loops in sequence and each `toggle`
        overwrites the other's decision, so the second one silently wins."""
        for item in self.items:
            with self.subTest(page=item["page"]):
                self.assertFalse(item["category"] and item["feature"])

    def test_every_declared_feature_gate_exists(self):
        for item in self.items:
            if not item["feature"]:
                continue
            with self.subTest(page=item["page"]):
                self.assertIn(item["feature"], FEATURE_DEFINITIONS)

    def test_every_page_has_a_title_key(self):
        catalog = json.loads(
            (ROOT / "locales" / "hu.json").read_text(encoding="utf-8"))["dashboard"]
        for item in self.items:
            key = f"title_{item['page'].replace('-', '_')}"
            with self.subTest(page=item["page"]):
                self.assertIn(key, catalog,
                              "the header would read `[dashboard.title_…]`")


class LocaleLookupTests(unittest.TestCase):
    """`tr` must degrade, never throw.

    A missing key has always rendered as `[key]`. An *absent key name* threw:
    `tr(undefined)` reached `undefined.split('.')`. That is not a hypothetical —
    `resourcePicker` passes `definition.locale_key`, one hand-written definition
    omitted it, and the resulting TypeError killed the whole content-builder
    editor before it reached the page while looking like an unimplemented
    feature. A visible `[undefined]` is a defect somebody can see; a TypeError
    three frames deep is not.

    Driven through Node, because the function under test is JavaScript and a
    Python re-implementation of it could stay green while the real one broke.
    """

    def driver(self, body):
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not installed")
        source = (ROOT / "dashboard" / "script.js").read_text(encoding="utf-8")
        start = source.index("const tr = (path) => {")
        extracted = source[start:source.index("\n};", start) + 3]
        script = "\n".join([
            "const locale = {dashboard: {greeting: 'Hello', blank: ''}};",
            extracted,
            body,
        ])
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                         encoding="utf-8") as handle:
            handle.write(script)
            path = handle.name
        try:
            return subprocess.run([node, path], capture_output=True, text=True)
        finally:
            os.unlink(path)

    def test_an_absent_key_name_does_not_throw(self):
        result = self.driver("""
            const cases = [undefined, null, 0, false, {}];
            for (const value of cases) {
                let out;
                try {
                    out = tr(value);
                } catch (error) {
                    console.error(`tr(${String(value)}) threw ${error}`);
                    process.exit(1);
                }
                if (typeof out !== 'string') {
                    console.error(`tr(${String(value)}) returned ${typeof out}`);
                    process.exit(1);
                }
            }
        """)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_a_real_key_still_resolves_and_a_missing_one_still_brackets(self):
        """Guards the premise: a `tr` that returned `[key]` for everything would
        pass the test above and break every label in the interface."""
        result = self.driver("""
            const checks = [
                ['dashboard.greeting', 'Hello'],
                ['dashboard.missing', '[dashboard.missing]'],
                ['dashboard.blank', '[dashboard.blank]'],
            ];
            for (const [path, expected] of checks) {
                const actual = tr(path);
                if (actual !== expected) {
                    console.error(`tr(${path}) === ${actual}, wanted ${expected}`);
                    process.exit(1);
                }
            }
        """)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)


class JavaScriptIsActuallyRunTests(unittest.TestCase):
    """A test that skips itself is a test that does not exist.

    Several tests drive `dashboard/script.js` through Node and call `skipTest`
    when it is missing. CI installed no Node, so all of them skipped and reported
    green — and they are precisely the checks guarding the defects that reached
    the deployment. This asserts the workflow installs it, because the failure
    mode of the guard is silence. Any new Node-driven test belongs in the list
    below, or it inherits exactly the invisibility this exists to prevent.
    """

    def test_ci_installs_node(self):
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertIn("actions/setup-node", workflow)
        # And the one test-only package, or the boot test skips in CI —
        # which is the silence this whole class exists to prevent.
        self.assertIn("npm install", workflow)

    def test_the_skip_is_the_only_reason_they_would_not_run(self):
        """If Node is here, none of them may skip — a skip then means the
        harness broke rather than that the environment lacks a runtime."""
        if shutil.which("node") is None:
            self.skipTest("node is not installed")
        suite = unittest.defaultTestLoader.loadTestsFromNames([
            "tests.test_settings_cache.RowEditorReportsCleanTests",
            "tests.test_localization_policy.LocaleLookupTests",
            "tests.test_managed_messages.CreatorRoundTripTests",
            "tests.test_gacha.FeaturedChanceFormulaTests",
            "tests.test_item_creator.TemplateRoundTripTests",
            "tests.test_dashboard_boot.DashboardBootTests",
        ])
        result = unittest.TestResult()
        suite.run(result)
        self.assertEqual([], [str(case) for case, _ in result.skipped])
        self.assertEqual([], [str(case) for case, _ in result.errors + result.failures])


class PickerDefinitionTests(unittest.TestCase):
    """A picker built from a hand-written definition needs the same fields.

    Every picker on the settings form is built from a registry definition, and
    `jsonRowEditor` spreads one, so those carry `locale_key` for free. A builder
    field has no registry definition and must supply one by hand — and the one
    that did not took its whole page down. This walks the literals instead of
    trusting that the next author remembers.
    """

    REQUIRED = ("key", "value_type", "locale_key")

    def literals(self):
        """Every `resourcePicker({...})` argument written inline in the source."""
        source = (ROOT / "dashboard" / "script.js").read_text(encoding="utf-8")
        found = []
        for match in re.finditer(r"resourcePicker\(\s*\{", source):
            start = match.end() - 1
            depth, index = 0, start
            while index < len(source):
                if source[index] == "{":
                    depth += 1
                elif source[index] == "}":
                    depth -= 1
                    if depth == 0:
                        break
                index += 1
            found.append((source[:start].count("\n") + 1, source[start:index + 1]))
        return found

    def test_the_literals_were_found(self):
        """Guards the premise: a regex that matches nothing proves nothing."""
        self.assertGreaterEqual(len(self.literals()), 2)

    def test_every_hand_written_definition_carries_what_the_picker_reads(self):
        for line, literal in self.literals():
            if literal.lstrip("{").lstrip().startswith("..."):
                continue  # A spread of a real definition already has the lot.
            for field in self.REQUIRED:
                with self.subTest(line=line, field=field):
                    self.assertIn(f"{field}:", literal,
                                  f"script.js:{line} omits {field}")


if __name__ == "__main__":
    unittest.main()
