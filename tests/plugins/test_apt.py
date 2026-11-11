"""Tests for the apt plugin."""

from dataclasses import replace
from pathlib import Path

from pytest_mock import MockerFixture

from purger.config import Config
from purger.output import is_partial
from purger.plugin import SizeOfPaths
from purger.plugins import apt
from purger.sizing import disk_usage, measure_plugin


def test_apt_total_size_matches_clean(tmp_path: Path, mocker: MockerFixture) -> None:
    """Verify apt is exact: its total size counts only the package files that clean removes."""
    # Given an apt archive with a package, a lock file, and a partial download directory
    archives = tmp_path / "archives"
    (archives / "partial").mkdir(parents=True)
    (archives / "pkg.deb").write_bytes(b"x" * 10_000)
    (archives / "lock").write_bytes(b"x" * 100)

    assert apt.PLUGIN.total_size == SizeOfPaths(apt.APT_PACKAGE_FILES)
    moved = SizeOfPaths(apt.APT_PACKAGE_FILES.replace("/var/cache/apt/archives", str(archives)))
    clean = replace(apt.PLUGIN.actions[1], size=moved)
    plugin = replace(apt.PLUGIN, total_size=None, actions=(apt.PLUGIN.actions[0], clean))

    # When
    report = measure_plugin(plugin, include_dangerous=False, settings=Config().settings_for(plugin))

    # Then
    assert report.total_size_bytes == disk_usage([archives / "pkg.deb"])
    assert is_partial(report) is False
