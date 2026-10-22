"""What the Release workflow relies on: one version everywhere, and a changelog to take the notes from."""

import json
import re
import tomllib
import unittest
from pathlib import Path

from virtual_key_mapper import __version__

ROOT = Path(__file__).resolve().parents[1]


class ReleaseTests(unittest.TestCase):
    def test_the_package_and_both_extensions_carry_the_same_version(self):
        version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
        self.assertEqual(__version__, version)
        for folder in ("vscode-extension", "vscode-extension/local"):
            with self.subTest(folder=folder):
                self.assertEqual(json.loads((ROOT / folder / "package.json").read_text(encoding="utf-8"))["version"], version)
        lock = json.loads((ROOT / "vscode-extension" / "package-lock.json").read_text(encoding="utf-8"))
        self.assertEqual((lock["version"], lock["packages"][""]["version"]), (version, version))

    def test_the_changelog_has_an_unreleased_section_or_an_entry_for_this_version(self):
        headings = re.findall(r"^## (.+)$", (ROOT / "CHANGELOG.md").read_text(encoding="utf-8"), re.MULTILINE)
        self.assertTrue(headings, "CHANGELOG.md needs '## Unreleased' or '## VERSION - DATE' sections")
        self.assertTrue(
            headings[0] == "Unreleased" or re.fullmatch(re.escape(__version__) + r" - \d{4}-\d{2}-\d{2}", headings[0]),
            f"The newest CHANGELOG.md section must be 'Unreleased' or '{__version__} - YYYY-MM-DD', not {headings[0]!r}",
        )
        for heading in headings[1:]:
            self.assertRegex(heading, r"^\d+\.\d+\.\d+ - \d{4}-\d{2}-\d{2}$")


if __name__ == "__main__":
    unittest.main()
