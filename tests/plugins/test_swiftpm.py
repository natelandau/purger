"""Tests for the swiftpm plugin."""

from pathlib import Path

from pytest_mock import MockerFixture

from purger.config import Config
from purger.plugin import DeletePaths, RiskLevel, RunCommand, SizeOfPaths
from purger.plugins import swiftpm
from purger.sizing import disk_usage, measure_action


def test_swiftpm_cache_lives_in_the_real_swift_cache_folder() -> None:
    """Verify the cache folder is the swiftpm folder itself, not the ~/.swiftpm/cache link."""
    # Given / When / Then
    assert swiftpm.SWIFTPM_CACHE_DIRECTORY.endswith("/org.swift.swiftpm")
    assert "/.swiftpm/" not in swiftpm.SWIFTPM_CACHE_DIRECTORY


def test_swiftpm_purge_cache_runs_swift_without_sudo() -> None:
    """Verify the cache is purged by swift itself, as a caution-level action."""
    # Given
    action = swiftpm.PLUGIN.actions[0]

    # When / Then
    assert isinstance(action.cleanup, RunCommand)
    assert action.cleanup.argv == ("swift", "package", "purge-cache")
    assert action.cleanup.with_sudo is False
    assert action.risk_level is RiskLevel.CAUTION


def test_swiftpm_purge_cache_size_counts_repositories_downloads_and_manifests(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    """Verify the purge-cache size covers the three folders swift purges and not artifacts."""
    # Given the declared globs re-rooted below tmp_path
    action = swiftpm.PLUGIN.actions[0]
    assert isinstance(action.size, SizeOfPaths)
    base = swiftpm.SWIFTPM_CACHE_DIRECTORY
    rooted = tuple(glob.replace(base, str(tmp_path)) for glob in action.size.globs)
    folders = ("repositories", "registry/downloads", "manifests", "artifacts")
    for folder in folders:
        (tmp_path / folder).mkdir(parents=True)
        (tmp_path / folder / "f").write_bytes(b"x" * 10_000)
    mocker.patch("purger.sizing.invoking_home", return_value=tmp_path)
    rooted_action = type(action)(
        name=action.name,
        risk_level=action.risk_level,
        description=action.description,
        cleanup=action.cleanup,
        size=SizeOfPaths(*rooted),
    )

    # When
    report = measure_action(rooted_action, settings=Config().settings_for(swiftpm.PLUGIN))

    # Then
    assert report.size_bytes == disk_usage([tmp_path / f for f in folders[:3]])


def test_swiftpm_artifacts_are_deleted_from_the_artifacts_folder() -> None:
    """Verify the artifacts action deletes what is inside the artifacts folder."""
    # Given
    action = next(a for a in swiftpm.PLUGIN.actions if a.name == "artifacts")

    # When / Then
    assert isinstance(action.cleanup, DeletePaths)
    assert action.cleanup.globs == (f"{swiftpm.SWIFTPM_CACHE_DIRECTORY}/artifacts/*",)
    assert action.risk_level is RiskLevel.CAUTION
