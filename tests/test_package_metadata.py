"""Release metadata remains internally consistent."""

import tomllib
import unittest
from pathlib import Path

from polymarket_bot import __version__


class PackageMetadataTests(unittest.TestCase):
    def test_runtime_version_matches_project_version(self):
        project_file = Path(__file__).resolve().parent.parent / "pyproject.toml"
        with project_file.open("rb") as handle:
            project_version = tomllib.load(handle)["project"]["version"]

        self.assertEqual(__version__, project_version)
