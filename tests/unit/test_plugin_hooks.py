"""Tests for plugin size-parsing and availability hooks."""

import pytest
from nclutils.sh import CompletedCommand, ShellCommandNotFoundError
from pytest_mock import MockerFixture

from purger.plugins import docker
from purger.plugins.brew import _parse_dry_run
from purger.sizing import measure_plugin

_DF_OUTPUT = (
    '{"Reclaimable":"1MB (50%)","Type":"Images"}\n'
    '{"Reclaimable":"0B","Type":"Containers"}\n'
    '{"Reclaimable":"2MB (100%)","Type":"Local Volumes"}\n'
    '{"Reclaimable":"0B","Type":"Build Cache"}\n'
)


def _completed(*, returncode: int = 0, stdout: str = "") -> CompletedCommand:
    return CompletedCommand(
        argv=("docker",), returncode=returncode, stdout=stdout, stderr="", duration=0.0, cwd=None
    )


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        (
            "Would remove: x (64B)\n==> This operation would free approximately 3GB of disk space.",
            3 * 1024**3,
        ),
        ("==> This operation would free approximately 861.6KB of disk space.", 882_278),
        ("==> This operation would free approximately 12MB of disk space.", 12 * 1024**2),
        ("==> This operation would free approximately 64B of disk space.", 64),
        ("Would remove: x (64B)", 0),
        ("", 0),
    ],
)
def test_parse_dry_run_extracts_freed_bytes(output: str, expected: int) -> None:
    """Verify the brew parser reads the freed-space summary line in binary units."""
    # Given / When
    result = _parse_dry_run(output)

    # Then
    assert result == expected


@pytest.mark.parametrize(
    ("types", "expected"),
    [
        (frozenset({"Images", "Containers", "Build Cache"}), 1_000_000),
        (frozenset({"Local Volumes"}), 2_000_000),
        (frozenset({"Build Cache"}), 0),
    ],
)
def test_reclaimable_from_sums_requested_types(types: frozenset[str], expected: int) -> None:
    """Verify the docker parser sums only the requested types."""
    # Given / When
    result = docker._reclaimable_from(_DF_OUTPUT, types)

    # Then
    assert result == expected


def test_docker_measurement_runs_system_df_once(mocker: MockerFixture) -> None:
    """Verify measuring docker reads one docker system df run for every size."""
    # Given docker system df output
    run = mocker.patch(
        "purger.sizing.run_command", autospec=True, return_value=_completed(stdout=_DF_OUTPUT)
    )

    # When
    report = measure_plugin(docker.PLUGIN, include_dangerous=True)

    # Then the command ran once and each action reads its own row types
    run.assert_called_once()
    assert run.call_args.args[0] == ["docker", "system", "df", "--format", "json"]
    assert {a.action.name: a.size_bytes for a in report.actions} == {
        "builder": 0,
        "system": 1_000_000,
        "volumes": 2_000_000,
    }
    assert report.footprint_bytes == 3_000_000


def test_docker_is_available_false_when_daemon_down(mocker: MockerFixture) -> None:
    """Verify docker is unavailable when docker info exits non-zero."""
    # Given
    mocker.patch(
        "purger.runner.run_command",
        autospec=True,
        return_value=_completed(returncode=1),
    )

    # When / Then
    assert docker._is_available() is False


def test_docker_is_available_false_on_shell_error(mocker: MockerFixture) -> None:
    """Verify docker is unavailable when the command cannot run."""
    # Given
    mocker.patch(
        "purger.runner.run_command",
        autospec=True,
        side_effect=ShellCommandNotFoundError(argv=["docker"]),
    )

    # When / Then
    assert docker._is_available() is False


def test_docker_is_available_true_when_daemon_up(mocker: MockerFixture) -> None:
    """Verify docker is available when docker info succeeds."""
    # Given
    mocker.patch("purger.runner.run_command", autospec=True, return_value=_completed())

    # When / Then
    assert docker._is_available() is True
