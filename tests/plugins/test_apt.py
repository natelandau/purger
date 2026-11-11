"""Tests for the apt plugin."""

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from pytest_mock import MockerFixture

from purger.config import Config
from purger.output import is_partial
from purger.plugins import apt
from purger.sizing import disk_usage, measure_plugin


def test_apt_footprint_matches_clean(tmp_path: Path, mocker: MockerFixture) -> None:
    """Verify apt is exact: its footprint counts only the package files that clean removes."""
    # Given an apt archive with a package, a lock file, and a partial download directory
    archives = tmp_path / "archives"
    (archives / "partial").mkdir(parents=True)
    (archives / "pkg.deb").write_bytes(b"x" * 10_000)
    (archives / "lock").write_bytes(b"x" * 100)

    def moved(paths: tuple[str, ...] | Callable[[], tuple[str, ...]]) -> tuple[str, ...]:
        assert isinstance(paths, tuple)
        return tuple(path.replace("/var/cache/apt/archives", str(archives)) for path in paths)

    plugin = replace(
        apt.PLUGIN,
        paths=moved(apt.PLUGIN.paths),
        actions=tuple(replace(a, paths=moved(a.paths)) for a in apt.PLUGIN.actions),
    )

    # When
    report = measure_plugin(plugin, include_dangerous=False, settings=Config().settings_for(plugin))

    # Then
    assert report.footprint_bytes == disk_usage([archives / "pkg.deb"])
    assert is_partial(report) is False
