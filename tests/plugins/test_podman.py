"""Tests for the podman plugin."""

import json

import pytest
from nclutils.sh import CompletedCommand
from pytest_mock import MockerFixture

from purger.plugins import podman
from tests.conftest import make_completed

_PODMAN_DF = (
    '[{"Type":"Images","RawReclaimable":900,"Reclaimable":"900B (7%)"},'
    '{"Type":"Containers","RawReclaimable":100,"Reclaimable":"100B"},'
    '{"Type":"Local Volumes","RawReclaimable":5000,"Reclaimable":"5kB"}]'
)


@pytest.mark.parametrize(
    ("types", "expected"),
    [(frozenset({"Images", "Containers"}), 1000), (frozenset({"Local Volumes"}), 5000)],
)
def test_podman_sums_raw_reclaimable(types: frozenset[str], expected: int) -> None:
    """Verify podman sizes come from the integer RawReclaimable field of the chosen rows."""
    # Given / When / Then
    assert podman._reclaimable_from(_PODMAN_DF, types=types) == expected


def _patch_podman(
    mocker: MockerFixture, *, info: str | None, connections: list[dict[str, object]] | None = None
) -> None:
    """Answer podman info and podman system connection list; None makes that command fail."""

    def answer(argv: list[str], **_: object) -> CompletedCommand:
        out = (
            info if argv[1] == "info" else None if connections is None else json.dumps(connections)
        )
        return make_completed(returncode=125) if out is None else make_completed(stdout=out)

    mocker.patch("purger.plugins.podman.run_command", autospec=True, side_effect=answer)


def _podman_info(*, remote: bool) -> str:
    return json.dumps({"host": {"serviceIsRemote": remote}})


_MACHINE: dict[str, object] = {
    "Name": "podman-machine-default",
    "URI": "ssh://core@127.0.0.1:53298/run/user/501/podman/podman.sock",
    "IsMachine": True,
    "Default": True,
}
_REMOTE: dict[str, object] = {
    "Name": "prod",
    "URI": "ssh://root@example.com/run/podman/podman.sock",
    "IsMachine": False,
    "Default": False,
}


def test_podman_unavailable_when_info_fails(mocker: MockerFixture) -> None:
    """Verify podman is skipped when it does not answer, such as a stopped machine on macOS."""
    # Given
    _patch_podman(mocker, info=None)
    assert podman.PLUGIN.is_available is not None

    # When / Then
    assert podman.PLUGIN.is_available() is False


def test_podman_available_for_a_local_service(mocker: MockerFixture) -> None:
    """Verify native podman on Linux, whose service is not remote, is available."""
    # Given
    _patch_podman(mocker, info=_podman_info(remote=False))

    # When / Then
    assert podman._is_available() is True


def test_podman_available_through_a_podman_machine(
    mocker: MockerFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify the default podman machine connection on macOS counts as local."""
    # Given
    monkeypatch.delenv("CONTAINER_HOST", raising=False)
    monkeypatch.delenv("CONTAINER_CONNECTION", raising=False)
    _patch_podman(mocker, info=_podman_info(remote=True), connections=[_MACHINE, _REMOTE])

    # When / Then
    assert podman._is_available() is True


@pytest.mark.parametrize(
    ("env", "connections"),
    [
        ({"CONTAINER_CONNECTION": "prod"}, [_MACHINE, _REMOTE]),
        ({}, [{**_MACHINE, "Default": False}, {**_REMOTE, "Default": True}]),
        ({"CONTAINER_HOST": "ssh://root@example.com/run/podman/podman.sock"}, [_MACHINE]),
        ({"CONTAINER_HOST": "tcp://127.0.0.1:34451"}, [_MACHINE]),
        ({"CONTAINER_CONNECTION": "missing"}, [_MACHINE]),
        (
            {"CONTAINER_HOST": "unix:///run/podman/podman.sock", "CONTAINER_CONNECTION": "prod"},
            [_MACHINE, _REMOTE],
        ),
        ({}, None),
    ],
)
def test_podman_unavailable_for_a_remote_service(
    mocker: MockerFixture,
    monkeypatch: pytest.MonkeyPatch,
    env: dict[str, str],
    connections: list[dict[str, object]] | None,
) -> None:
    """Verify a connection to another host, or one podman cannot name, is never available."""
    # Given
    monkeypatch.delenv("CONTAINER_HOST", raising=False)
    monkeypatch.delenv("CONTAINER_CONNECTION", raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    _patch_podman(mocker, info=_podman_info(remote=True), connections=connections)

    # When / Then
    assert podman._is_available() is False
