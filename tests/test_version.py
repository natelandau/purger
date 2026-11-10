"""Tests for package metadata."""

import tomllib
from pathlib import Path

from purger import __version__


def test_version_matches_pyproject() -> None:
    """Verify the package version matches pyproject.toml."""
    pyproject = Path(__file__).parents[1] / "pyproject.toml"
    expected = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"]

    assert __version__ == expected
