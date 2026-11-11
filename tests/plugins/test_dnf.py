"""Tests for the dnf plugin."""

from pathlib import Path

import pytest
from pytest_mock import MockerFixture

from purger.config import Config
from purger.plugin import OperatingSystem, RunCommand, SizeOfPaths
from purger.plugins import dnf
from purger.sizing import disk_usage, measure_plugin


def test_dnf_is_linux_only_and_cleans_packages_with_sudo() -> None:
    """Verify the plugin is limited to Linux and runs `dnf clean packages` as root."""
    # Given
    action = dnf.PLUGIN.actions[0]

    # When / Then
    assert dnf.PLUGIN.operating_systems == (OperatingSystem.LINUX,)
    assert isinstance(action.cleanup, RunCommand)
    assert action.cleanup.argv == ("dnf", "clean", "packages")
    assert action.cleanup.with_sudo is True


def test_dnf_total_size_counts_only_rpm_files_from_dnf4_and_dnf5(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    """Verify the size globs match rpm files under both cache layouts and nothing else."""
    # Given a cache root where each glob is re-rooted below tmp_path
    for cache, repo in (("dnf", "fedora"), ("libdnf5", "updates")):
        packages = tmp_path / "var" / "cache" / cache / repo / "packages"
        packages.mkdir(parents=True)
        (packages / "a.rpm").write_bytes(b"x" * 10_000)
        (packages / "notes.txt").write_bytes(b"x" * 50_000)
    rooted = tuple(f"{tmp_path}{glob}" for glob in dnf.DNF_PACKAGE_FILES)
    plugin = dnf.PLUGIN.__class__(
        name=dnf.PLUGIN.name,
        description=dnf.PLUGIN.description,
        total_size=SizeOfPaths(*rooted),
        actions=dnf.PLUGIN.actions,
    )
    mocker.patch("purger.sizing.invoking_home", return_value=tmp_path)

    # When
    report = measure_plugin(plugin, include_dangerous=True, settings=Config().settings_for(plugin))

    # Then
    expected = disk_usage(
        [
            tmp_path / "var/cache/dnf/fedora/packages/a.rpm",
            tmp_path / "var/cache/libdnf5/updates/packages/a.rpm",
        ]
    )
    assert report.total_size_bytes == expected


@pytest.mark.parametrize("field", ["total_size", "size"])
def test_dnf_total_and_action_size_use_the_same_globs(field: str) -> None:
    """Verify the plugin total and the package action measure the same files."""
    # Given
    source = dnf.PLUGIN.total_size if field == "total_size" else dnf.PLUGIN.actions[0].size

    # When / Then
    assert isinstance(source, SizeOfPaths)
    assert source.globs == dnf.DNF_PACKAGE_FILES
