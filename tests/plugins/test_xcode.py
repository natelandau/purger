"""Tests for the xcode plugin."""

from pathlib import Path

import pytest
from pytest_mock import MockerFixture

from purger.config import Config
from purger.plugin import DeletePaths, OperatingSystem, RiskLevel
from purger.plugins import xcode
from purger.sizing import disk_usage, measure_action


def test_xcode_is_macos_only() -> None:
    """Verify the plugin applies only on macOS."""
    # Given / When / Then
    assert xcode.PLUGIN.operating_systems == (OperatingSystem.MACOS,)


@pytest.mark.parametrize(
    ("name", "folder", "risk"),
    [
        ("derived-data", "DerivedData", RiskLevel.SAFE),
        ("device-support", "iOS DeviceSupport", RiskLevel.CAUTION),
        ("archives", "Archives", RiskLevel.DANGEROUS),
    ],
)
def test_xcode_actions_delete_the_contents_of_one_developer_folder(
    name: str, folder: str, risk: RiskLevel
) -> None:
    """Verify each action deletes what is inside its folder under ~/Library/Developer/Xcode."""
    # Given
    action = next(a for a in xcode.PLUGIN.actions if a.name == name)

    # When / Then
    assert isinstance(action.cleanup, DeletePaths)
    assert action.cleanup.globs == (f"~/Library/Developer/Xcode/{folder}/*",)
    assert action.risk_level is risk


def test_xcode_derived_data_size_comes_from_the_deleted_paths(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    """Verify the derived-data size counts each project folder inside DerivedData."""
    # Given a home with two derived-data projects and an unrelated archive
    derived = tmp_path / "Library/Developer/Xcode/DerivedData"
    for project in ("App-abc", "Lib-def"):
        (derived / project).mkdir(parents=True)
        (derived / project / "build").write_bytes(b"x" * 10_000)
    archives = tmp_path / "Library/Developer/Xcode/Archives"
    archives.mkdir(parents=True)
    (archives / "old").write_bytes(b"x" * 50_000)
    mocker.patch("purger.sizing.invoking_home", return_value=tmp_path)
    action = next(a for a in xcode.PLUGIN.actions if a.name == "derived-data")

    # When
    report = measure_action(action, settings=Config().settings_for(xcode.PLUGIN))

    # Then
    assert report.resolved_paths == sorted(derived.iterdir())
    assert report.size_bytes == disk_usage(list(derived.iterdir()))
