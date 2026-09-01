from __future__ import annotations

from pathlib import Path
import tomllib
import unittest

from codex_usage import __version__


ROOT = Path(__file__).resolve().parents[2]


class PackageMetadataTests(unittest.TestCase):
    def test_pyproject_uses_package_version_and_exposes_cli(self) -> None:
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

        self.assertEqual(project["project"]["dynamic"], ["version"])
        self.assertEqual(
            project["tool"]["setuptools"]["dynamic"]["version"]["attr"],
            "codex_usage.__version__",
        )
        self.assertEqual(
            project["project"]["scripts"]["codex-usage"],
            "codex_usage.cli:main",
        )
        self.assertEqual(__version__, "0.1.0")

    def test_schema_is_declared_as_installed_data(self) -> None:
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        data_files = project["tool"]["setuptools"]["data-files"]

        self.assertEqual(
            data_files["share/codex-usage-tracker/schemas"],
            ["schemas/ledger-event-v1.schema.json"],
        )
        self.assertTrue((ROOT / "schemas" / "ledger-event-v1.schema.json").is_file())


if __name__ == "__main__":
    unittest.main()
