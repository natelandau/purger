"""Tests for the bun plugin."""

import pytest
from nclutils.sh import ShellCommandNotFoundError
from pytest_mock import MockerFixture

from purger.plugins import bun
from tests.conftest import make_completed


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("1.4.4\n", True),
        ("1.5.0\n", True),
        ("2.0.0-canary.1+abc\n", True),
        ("1.4.3\n", False),
        ("1.3.10\n", False),
        ("not a version\n", False),
    ],
)
def test_bun_available_only_with_cache_prune(
    mocker: MockerFixture, output: str, expected: bool
) -> None:
    """Verify bun before 1.4.4, whose cache prune silently does nothing, is skipped."""
    # Given
    mocker.patch(
        "purger.plugins.bun.run_command", autospec=True, return_value=make_completed(stdout=output)
    )

    # When / Then
    assert bun.bun_supports_cache_prune() is expected


def test_bun_unavailable_when_version_cannot_run(mocker: MockerFixture) -> None:
    """Verify a bun that cannot run counts as unavailable."""
    # Given
    mocker.patch(
        "purger.plugins.bun.run_command",
        autospec=True,
        side_effect=ShellCommandNotFoundError(argv=["bun"]),
    )

    # When / Then
    assert bun.bun_supports_cache_prune() is False
