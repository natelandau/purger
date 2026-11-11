"""Tests for the docker plugin."""

from unittest.mock import MagicMock

import pytest
from nclutils.sh import ShellCommandNotFoundError
from pytest_mock import MockerFixture

from purger.config import Config
from purger.plugins import docker
from purger.sizing import measure_plugin
from tests.conftest import make_completed

_DF_OUTPUT = (
    '{"Reclaimable":"1MB (50%)","Type":"Images"}\n'
    '{"Reclaimable":"0B","Type":"Containers"}\n'
    '{"Reclaimable":"2MB (100%)","Type":"Local Volumes"}\n'
    '{"Reclaimable":"0B","Type":"Build Cache"}\n'
)


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
        "purger.sizing.run_command", autospec=True, return_value=make_completed(stdout=_DF_OUTPUT)
    )

    # When
    report = measure_plugin(
        docker.PLUGIN, include_dangerous=True, settings=Config().settings_for(docker.PLUGIN)
    )

    # Then the command ran once and each action reads its own row types
    run.assert_called_once()
    assert run.call_args.args[0] == ["docker", "system", "df", "--format", "json"]
    assert {a.action.name: a.size_bytes for a in report.actions} == {
        "builder": 0,
        "system": 1_000_000,
        "volumes": 2_000_000,
    }
    assert report.footprint_bytes == 3_000_000


def _patch_docker(
    mocker: MockerFixture, *, context_host: str = "unix:///var/run/docker.sock", info_rc: int = 0
) -> MagicMock:
    """Answer the docker context query and docker info, and return the docker info mock."""
    mocker.patch(
        "purger.plugins.docker.run_command",
        autospec=True,
        return_value=make_completed(stdout=f"{context_host}\n"),
    )
    return mocker.patch(
        "purger.runner.run_command", autospec=True, return_value=make_completed(returncode=info_rc)
    )


def test_docker_is_available_false_when_daemon_down(mocker: MockerFixture) -> None:
    """Verify docker is unavailable when docker info exits non-zero."""
    # Given
    _patch_docker(mocker, info_rc=1)

    # When / Then
    assert docker._is_available() is False


def test_docker_is_available_false_on_shell_error(mocker: MockerFixture) -> None:
    """Verify docker is unavailable when the command cannot run."""
    # Given
    mocker.patch(
        "purger.plugins.docker.run_command",
        autospec=True,
        side_effect=ShellCommandNotFoundError(argv=["docker"]),
    )

    # When / Then
    assert docker._is_available() is False


@pytest.mark.parametrize(
    "context_host",
    [
        "unix:///var/run/docker.sock",
        "unix:///Users/me/.orbstack/run/docker.sock",
        "unix:///Users/me/.docker/run/docker.sock",
    ],
)
def test_docker_is_available_true_for_a_local_engine(
    mocker: MockerFixture, context_host: str
) -> None:
    """Verify docker is available when the context is a unix socket and docker info succeeds."""
    # Given
    _patch_docker(mocker, context_host=context_host)

    # When / Then
    assert docker._is_available() is True


@pytest.mark.parametrize(
    "context_host",
    ["tcp://10.0.0.5:2375", "ssh://me@build-host", "tcp://127.0.0.1:2375", ""],
)
def test_docker_is_unavailable_for_a_remote_engine(
    mocker: MockerFixture, context_host: str
) -> None:
    """Verify a TCP or SSH context, even on loopback, never reaches docker info or a prune."""
    # Given
    info = _patch_docker(mocker, context_host=context_host)

    # When / Then
    assert docker._is_available() is False
    info.assert_not_called()


def test_docker_volumes_count_outside_footprint() -> None:
    """Verify Docker volumes are added to the footprint, since docker df reclaimable excludes them."""
    # Given / When
    flags = {a.name: a.outside_footprint for a in docker.PLUGIN.actions}

    # Then
    assert flags == {"builder": False, "system": False, "volumes": True}
