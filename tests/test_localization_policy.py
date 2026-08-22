import ast
import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
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


if __name__ == "__main__":
    unittest.main()
