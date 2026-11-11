"""Tests for the simulators plugin."""

import json
from pathlib import Path

from pytest_mock import MockerFixture

from purger.plugins import simulators
from purger.sizing import disk_usage
from tests.conftest import make_completed


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
    size = simulators._unavailable_size()

    # Then
    assert size == disk_usage([tmp_path / "OLD"])


def test_simulators_unavailable_until_first_launch_finishes(mocker: MockerFixture) -> None:
    """Verify simctl is never called while Xcode's first-launch setup is unfinished."""
    # Given
    run = mocker.patch(
        "purger.runner.run_command",
        autospec=True,
        side_effect=[make_completed(), make_completed(returncode=1)],
    )

    # When / Then
    assert simulators._is_available() is False
    assert run.call_args.args[0] == ["xcodebuild", "-checkFirstLaunchStatus"]


def test_simulators_never_run_xcodebuild_without_developer_tools(mocker: MockerFixture) -> None:
    """Verify the xcodebuild shim, which opens an install dialog, is skipped without tools."""
    # Given
    run = mocker.patch(
        "purger.runner.run_command", autospec=True, return_value=make_completed(returncode=2)
    )

    # When / Then
    assert simulators._is_available() is False
    run.assert_called_once()
    assert run.call_args.args[0] == ["xcode-select", "-p"]
