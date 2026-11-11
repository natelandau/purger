"""Tests for the simulators plugin."""

import json
from pathlib import Path

import pytest
from pytest_mock import MockerFixture

from purger.config import Config
from purger.details import merge
from purger.plugin import reset_run_caches
from purger.plugins import simulators
from purger.sizing import disk_usage, measure_action
from tests.conftest import make_completed


@pytest.fixture(autouse=True)
def _fresh_listing() -> None:
    """Forget the cached simctl listing so each test reads its own mock."""
    reset_run_caches()


def _simctl_listing(tmp_path: Path) -> str:
    return json.dumps(
        {
            "devices": {
                "com.apple.CoreSimulator.SimRuntime.iOS-17-0": [
                    {"isAvailable": False, "dataPath": f"{tmp_path}/OLD/data"},
                ],
                "com.apple.CoreSimulator.SimRuntime.iOS-27-1": [
                    {"isAvailable": True, "dataPath": f"{tmp_path}/NEW/data"},
                    {"isAvailable": False},
                ],
            }
        }
    )


def test_simulators_size_counts_only_unavailable_devices(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    """Verify the size covers each unavailable device's folder and skips available ones."""
    # Given
    for device, size in (("OLD", 10_000), ("NEW", 50_000)):
        (tmp_path / device / "data").mkdir(parents=True)
        (tmp_path / device / "data" / "disk").write_bytes(b"x" * size)
    mocker.patch(
        "purger.plugins.simulators.run_command",
        autospec=True,
        return_value=make_completed(stdout=_simctl_listing(tmp_path)),
    )

    # When
    report = measure_action(
        simulators.PLUGIN.actions[0], settings=Config().settings_for(simulators.PLUGIN)
    )

    # Then
    assert report.size_bytes == disk_usage([tmp_path / "OLD"])


def test_simulators_size_breakdown_label_device_and_runtime(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    """Verify each size breakdown row shows its name and runtime."""
    # Given
    listing = json.dumps(
        {
            "devices": {
                "com.apple.CoreSimulator.SimRuntime.iOS-17-5": [
                    {"name": "iPhone 15", "isAvailable": False, "dataPath": f"{tmp_path}/D/data"},
                ],
            }
        }
    )
    mocker.patch(
        "purger.plugins.simulators.run_command",
        autospec=True,
        return_value=make_completed(stdout=listing),
    )

    # When
    rows = simulators.one_row_per_device(
        {tmp_path / "D": 5}, Config().settings_for(simulators.PLUGIN)
    )

    # Then
    assert [(row.label, row.size_bytes) for row in rows] == [("iPhone 15 (iOS 17.5)", 5)]


def test_simulators_size_matches_folders_with_glob_characters(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    """Verify a device folder whose path holds glob characters is still measured."""
    # Given
    base = tmp_path / "home [x]"
    (base / "OLD" / "data").mkdir(parents=True)
    (base / "OLD" / "data" / "disk").write_bytes(b"x" * 10_000)
    mocker.patch(
        "purger.plugins.simulators.run_command",
        autospec=True,
        return_value=make_completed(stdout=_simctl_listing(base)),
    )

    # When
    report = measure_action(
        simulators.PLUGIN.actions[0], settings=Config().settings_for(simulators.PLUGIN)
    )

    # Then
    assert report.resolved_paths == [base / "OLD"]
    assert report.size_bytes == disk_usage([base / "OLD"])


def test_simulators_size_breakdown_keep_same_named_devices_apart(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    """Verify two devices with the same name and runtime stay separate rows after merging."""
    # Given
    listing = json.dumps(
        {
            "devices": {
                "com.apple.CoreSimulator.SimRuntime.iOS-17-5": [
                    {"name": "iPhone 15", "isAvailable": False, "dataPath": f"{tmp_path}/A/data"},
                    {"name": "iPhone 15", "isAvailable": False, "dataPath": f"{tmp_path}/B/data"},
                ],
            }
        }
    )
    mocker.patch(
        "purger.plugins.simulators.run_command",
        autospec=True,
        return_value=make_completed(stdout=listing),
    )
    sizes = {tmp_path / "A": 5, tmp_path / "B": 7}

    # When
    rows = merge(simulators.one_row_per_device(sizes, Config().settings_for(simulators.PLUGIN)))

    # Then
    assert [row.size_bytes for row in rows] == [7, 5]


def test_simulators_unavailable_until_first_launch_finishes(mocker: MockerFixture) -> None:
    """Verify simctl is never called while Xcode's first-launch setup is unfinished."""
    # Given
    run = mocker.patch(
        "purger.runner.run_command",
        autospec=True,
        side_effect=[make_completed(), make_completed(returncode=1)],
    )

    # When / Then
    assert simulators.xcode_first_launch_is_complete() is False
    assert run.call_args.args[0] == ["xcodebuild", "-checkFirstLaunchStatus"]


def test_simulators_never_run_xcodebuild_without_developer_tools(mocker: MockerFixture) -> None:
    """Verify the xcodebuild shim, which opens an install dialog, is skipped without tools."""
    # Given
    run = mocker.patch(
        "purger.runner.run_command", autospec=True, return_value=make_completed(returncode=2)
    )

    # When / Then
    assert simulators.xcode_first_launch_is_complete() is False
    run.assert_called_once()
    assert run.call_args.args[0] == ["xcode-select", "-p"]
